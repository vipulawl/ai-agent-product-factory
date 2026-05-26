"""
Reviews built products for improvements, opens PRs, and notifies original thread posters.
"""

import os
import json
import logging
import requests
import yaml
from pathlib import Path

log = logging.getLogger(__name__)

RULES_PATH = Path(__file__).parent.parent / "config" / "priority_rules.yaml"
GITHUB_USER = "vipulawl"

IMPROVE_PROMPT = """You are a senior code reviewer. Review this GitHub repository and suggest improvements.

Repository: {repo_name}
Problem it solves: {title}
Description: {description}

Current README/summary:
{readme}

Suggest the top 3 concrete improvements that would make this significantly more useful for developers.
Focus on: missing features, better UX, more integrations, clearer docs, tests, error handling.

Return JSON:
{{
  "improvements": [
    {{
      "title": "improvement title",
      "description": "what to add/change and why",
      "priority": "high|medium|low"
    }}
  ],
  "overall_assessment": "brief overall assessment"
}}
"""

OUTREACH_PROMPT = """You are a helpful developer writing a reply to a forum post. The person was asking about or complaining about a problem, and we've built a tool that solves it.

Original post title: {post_title}
Original post URL: {post_url}
Tool we built: {repo_name}
GitHub URL: {github_url}
What it does: {tagline}

Write a genuine, helpful comment (2-3 sentences) that:
- Acknowledges their specific problem
- Introduces our tool naturally
- Gives the GitHub link
- Doesn't sound like spam

Return JSON:
{{
  "message": "the comment text"
}}
"""


def load_rules() -> dict:
    with open(RULES_PATH) as f:
        return yaml.safe_load(f)


def get_github_readme(repo_name: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"}
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo_name}/readme",
            headers=headers, timeout=10
        )
        if resp.ok:
            return resp.text[:3000]
    except Exception as e:
        log.warning(f"Could not fetch README for {repo_name}: {e}")
    return ""


def get_github_issues(repo_name: str) -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo_name}/issues",
            headers=headers, params={"state": "open", "per_page": 10}, timeout=10
        )
        if resp.ok:
            return resp.json()
    except Exception as e:
        log.warning(f"Could not fetch issues for {repo_name}: {e}")
    return []


def create_improvement_issue(repo_name: str, improvement: dict):
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        resp = requests.post(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo_name}/issues",
            headers=headers,
            json={
                "title": f"Enhancement: {improvement['title']}",
                "body": improvement["description"],
                "labels": ["enhancement"]
            },
            timeout=10
        )
        if resp.ok:
            log.info(f"Created issue: {improvement['title']}")
    except Exception as e:
        log.error(f"Failed to create issue: {e}")


def generate_outreach_messages(build: dict) -> list[dict]:
    from agents.base import chat_json

    source_urls = json.loads(build.get("source_urls") or "[]")
    if not source_urls:
        return []

    build_summary = json.loads(build.get("build_summary") or "{}")
    design = build_summary.get("design", {})
    repo_name = build.get("repo_name", "")
    github_url = build.get("github_url", "")
    tagline = design.get("tagline", build.get("title", ""))

    messages = []
    for url in source_urls[:5]:  # max 5 outreach per build
        try:
            result = chat_json([{"role": "user", "content": OUTREACH_PROMPT.format(
                post_title=build.get("title", ""),
                post_url=url,
                repo_name=repo_name,
                github_url=github_url,
                tagline=tagline
            )}])
            messages.append({"source_url": url, "message": result.get("message", "")})
        except Exception as e:
            log.error(f"Outreach generation failed for {url}: {e}")

    return messages


def analyze_and_improve(build: dict):
    from agents.base import chat_json

    repo_name = build.get("repo_name", "")
    readme = get_github_readme(repo_name)
    issues = get_github_issues(repo_name)

    context = f"{readme}\n\nExisting issues: {json.dumps([i.get('title') for i in issues])}"

    try:
        result = chat_json([{"role": "user", "content": IMPROVE_PROMPT.format(
            repo_name=repo_name,
            title=build.get("title", ""),
            description=build.get("description", ""),
            readme=context[:3000]
        )}])

        for improvement in result.get("improvements", [])[:3]:
            if improvement.get("priority") in ("high", "medium"):
                create_improvement_issue(repo_name, improvement)

        log.info(f"Refiner assessment for {repo_name}: {result.get('overall_assessment', '')}")
    except Exception as e:
        log.error(f"Improvement analysis failed for {repo_name}: {e}")


def send_outreach_via_telegram(build: dict, messages: list[dict]):
    from agents.base import send_telegram
    from storage.backlog import add_outreach

    if not messages:
        return

    for m in messages:
        add_outreach(build["id"], m["source_url"], m["message"])

    # Notify via Telegram so you can manually post (Reddit/HN don't allow bot posting easily)
    text = f"📢 *Outreach ready for {build['repo_name']}*\n\n"
    for m in messages:
        text += f"*Post:* {m['source_url']}\n"
        text += f"*Message:* {m['message']}\n\n"
        text += "---\n"

    send_telegram(text[:4000])


def run() -> dict:
    from storage.backlog import init_db, get_builds_for_refinement, mark_outreach_sent

    init_db()
    rules = load_rules()
    refine_after = rules.get("refine_after_days", 7)

    builds = get_builds_for_refinement(after_days=refine_after)
    if not builds:
        log.info(f"No builds ready for refinement (older than {refine_after} days)")
        return {"refined": 0}

    refined = 0
    for build in builds:
        log.info(f"Refining {build['repo_name']}...")
        try:
            analyze_and_improve(build)
            messages = generate_outreach_messages(build)
            send_outreach_via_telegram(build, messages)
            refined += 1
        except Exception as e:
            log.error(f"Refinement failed for {build['repo_name']}: {e}")

    return {"refined": refined, "builds_processed": len(builds)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
