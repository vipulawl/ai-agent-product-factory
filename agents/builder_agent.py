"""
Picks the top-priority backlog item, asks for Telegram approval, builds the full
project with GPT-4o, pushes to github.com/vipulawl/<repo>, and notifies.
"""

import os
import json
import logging
import subprocess
import time
import tempfile
import shutil
import yaml
from pathlib import Path

log = logging.getLogger(__name__)

RULES_PATH = Path(__file__).parent.parent / "config" / "priority_rules.yaml"
BUILDS_DIR = Path(__file__).parent.parent / "builds"

GITHUB_USER = "vipulawl"

DESIGN_PROMPT = """You are a senior software engineer. Design a complete, shippable developer tool based on this pain point:

Title: {title}
Description: {description}

Return JSON:
{{
  "repo_name": "kebab-case-name",
  "tagline": "one-line description for GitHub",
  "tech_stack": "Python / Node.js / etc.",
  "architecture": "brief architecture description",
  "files": ["README.md", "main.py", "requirements.txt", ".env.example", ".gitignore", "tests/test_main.py"],
  "key_features": ["feature 1", "feature 2", "feature 3"]
}}

Rules:
- Python preferred unless another language is clearly better
- Must be a standalone tool, not a plugin
- Must be usable in under 5 minutes
- Include at least: main file, README, requirements/package.json, .env.example, .gitignore, one test
"""

FILE_PROMPT = """You are a senior software engineer. Write the complete content for this file:

Project: {repo_name}
Problem solved: {title} — {description}
Architecture: {architecture}
Tech stack: {tech_stack}
Key features: {key_features}

File to write: {filepath}

Write ONLY the file content. No explanation, no markdown fences, no commentary.
Make it production-ready: working, clean, well-structured.
"""


def load_rules() -> dict:
    with open(RULES_PATH) as f:
        return yaml.safe_load(f)


def request_approval(item: dict) -> str:
    from agents.base import send_telegram
    from storage.backlog import create_approval

    token = create_approval(item["id"])
    msg = (
        f"🔨 *Ready to Build*\n\n"
        f"*{item['title']}*\n"
        f"{item['description'][:400]}...\n\n"
        f"Priority score: `{item['priority_score']}/10`\n\n"
        f"Reply `/approve {token}` or just `YES {token}` to proceed.\n"
        f"Reply `/reject {token}` to skip this item.\n\n"
        f"Token expires in 24h."
    )
    send_telegram(msg)
    log.info(f"Approval requested — token: {token}")
    return token


def wait_for_approval(token: str, timeout_hours: int = 24) -> bool:
    from agents.base import poll_telegram_updates
    from storage.backlog import get_approval_status, resolve_approval

    deadline = time.time() + timeout_hours * 3600
    offset = 0
    log.info(f"Waiting for approval (token={token}, timeout={timeout_hours}h)")

    # First check if Hermes gateway already resolved it
    while time.time() < deadline:
        status = get_approval_status(token)
        if status == "approved":
            return True
        if status == "rejected":
            return False

        # Poll Telegram directly as fallback
        messages, offset = poll_telegram_updates(offset)
        for msg in messages:
            msg_upper = msg.strip().upper()
            if token in msg and ("APPROVE" in msg_upper or "YES" in msg_upper):
                resolve_approval(token, "approved")
                return True
            if token in msg and "REJECT" in msg_upper:
                resolve_approval(token, "rejected")
                return False

        time.sleep(60)  # check every minute

    log.warning(f"Approval timeout for token {token}")
    return False


def design_project(item: dict) -> dict:
    from agents.base import chat_json

    result = chat_json([{"role": "user", "content": DESIGN_PROMPT.format(
        title=item["title"],
        description=item["description"]
    )}])
    return result


def generate_file(filepath: str, design: dict, item: dict) -> str:
    from agents.base import chat

    content = chat([{"role": "user", "content": FILE_PROMPT.format(
        repo_name=design["repo_name"],
        title=item["title"],
        description=item["description"],
        architecture=design.get("architecture", ""),
        tech_stack=design.get("tech_stack", "Python"),
        key_features=", ".join(design.get("key_features", [])),
        filepath=filepath
    )}], temperature=0.2)
    return content


def write_project_files(design: dict, item: dict, project_dir: Path):
    project_dir.mkdir(parents=True, exist_ok=True)
    generated = {}

    for filepath in design.get("files", []):
        try:
            log.info(f"Generating {filepath}...")
            content = generate_file(filepath, design, item)
            full_path = project_dir / filepath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            generated[filepath] = True
        except Exception as e:
            log.error(f"Failed to generate {filepath}: {e}")

    return generated


def push_to_github(project_dir: Path, design: dict) -> str:
    repo_name = design["repo_name"]
    tagline = design.get("tagline", "")
    token = os.environ["GITHUB_TOKEN"]

    log.info(f"Creating GitHub repo {GITHUB_USER}/{repo_name}")

    # Create repo via GitHub API
    import requests
    resp = requests.post(
        "https://api.github.com/user/repos",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"
        },
        json={
            "name": repo_name,
            "description": tagline,
            "private": False,
            "auto_init": False
        },
        timeout=15
    )
    if not resp.ok and resp.status_code != 422:  # 422 = already exists
        raise RuntimeError(f"GitHub repo creation failed: {resp.text}")

    github_url = f"https://github.com/{GITHUB_USER}/{repo_name}"

    # Init git and push
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    remote_url = f"https://{token}@github.com/{GITHUB_USER}/{repo_name}.git"

    cmds = [
        ["git", "init"],
        ["git", "add", "."],
        ["git", "commit", "-m", f"feat: initial build — {design['tagline']}"],
        ["git", "branch", "-M", "main"],
        ["git", "remote", "add", "origin", remote_url],
        ["git", "push", "-u", "origin", "main"]
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            log.error(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
            if "push" in cmd:
                raise RuntimeError(f"Git push failed: {result.stderr}")

    log.info(f"Pushed to {github_url}")
    return github_url


def notify_complete(item: dict, design: dict, github_url: str):
    from agents.base import send_telegram
    features = "\n".join(f"  • {f}" for f in design.get("key_features", []))
    msg = (
        f"✅ *Build Complete!*\n\n"
        f"*{design['repo_name']}*\n"
        f"_{design.get('tagline', '')}_\n\n"
        f"Solving: {item['title']}\n\n"
        f"Features:\n{features}\n\n"
        f"🔗 {github_url}"
    )
    send_telegram(msg)


def run(no_approval_wait: bool = False) -> dict:
    from storage.backlog import init_db, get_top_item, get_pending_backlog, set_item_status, record_build, mark_build_notified

    init_db()
    rules = load_rules()
    min_score = rules.get("min_priority_to_build", 6.0)
    timeout_hours = rules.get("approval_timeout_hours", 24)

    # Allow CI to specify a particular item via env var
    item_id = os.environ.get("BUILD_ITEM_ID", "").strip()
    if item_id:
        items = [i for i in get_pending_backlog(0) if str(i["id"]) == item_id]
        item = items[0] if items else None
    else:
        item = get_top_item(min_score=min_score)

    if not item:
        log.info("No buildable items in backlog (below min priority or none pending)")
        return {"built": False, "reason": "nothing_to_build"}

    log.info(f"Top item: [{item['id']}] {item['title']} (score={item['priority_score']})")

    if no_approval_wait:
        # Running via GitHub Actions — triggering the workflow is the approval
        from agents.base import send_telegram
        send_telegram(f"🏗️ *Building now (CI triggered)*\n\n*{item['title']}*\nScore: `{item['priority_score']}/10`")
    else:
        # Request Telegram approval and wait
        token = request_approval(item)
        approved = wait_for_approval(token, timeout_hours=timeout_hours)
        if not approved:
            log.info(f"Item {item['id']} not approved — skipping")
            set_item_status(item["id"], "skipped")
            return {"built": False, "reason": "not_approved", "item": item["title"]}

    log.info(f"Approved! Building {item['title']}...")
    set_item_status(item["id"], "building")

    # Design project
    design = design_project(item)
    log.info(f"Designed: {design['repo_name']} ({len(design.get('files', []))} files)")

    # Write files to a temp build directory
    project_dir = BUILDS_DIR / design["repo_name"]
    if project_dir.exists():
        shutil.rmtree(project_dir)

    generated = write_project_files(design, item, project_dir)
    log.info(f"Generated {sum(generated.values())} files")

    # Push to GitHub
    github_url = push_to_github(project_dir, design)

    # Record in DB
    summary = json.dumps({"design": design, "files_generated": list(generated.keys())})
    build_id = record_build(item["id"], github_url, design["repo_name"], summary)

    set_item_status(item["id"], "built")

    # Notify
    notify_complete(item, design, github_url)
    mark_build_notified(build_id)

    return {
        "built": True,
        "repo_name": design["repo_name"],
        "github_url": github_url,
        "item": item["title"]
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
