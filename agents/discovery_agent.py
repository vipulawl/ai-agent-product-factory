"""
Discovery agent — scrapes Reddit, HN, GitHub then runs a 4-step synthesis pipeline:

  1. SCRAPE   → raw_posts (deduped by URL)
  2. EXTRACT  → pain_points (atomic, one row per complaint, persisted immediately)
  3. MATCH    → cluster_evidence (GPT-4o assigns each pain point to an existing
                cluster or creates a new one, building up cross-run memory)
  4. SYNTHESISE → clusters.synthesis_narrative (once a cluster has enough evidence
                  GPT-4o writes a full problem brief from all accumulated evidence)
  5. PROMOTE  → backlog_items created for mature clusters

Steps 2-5 are accumulative across every run. Evidence never disappears.
"""

import os
import json
import logging
import requests
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"

# ── prompts ───────────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """You are extracting developer pain points from community posts about AI agents and coding agents.

For each post, identify concrete complaints, frustrations, or missing capabilities.
Ignore general discussion, positive feedback, and off-topic content.

Return JSON:
{
  "pain_points": [
    {
      "post_index": 0,
      "description": "one clear sentence describing the pain point",
      "severity": "high|medium|low",
      "direct_quote": "exact quote from the post that shows this pain (optional)"
    }
  ]
}

If a post contains no pain point, omit it. One post can yield multiple pain points.

Posts:
"""

MATCH_PROMPT = """You are maintaining a running knowledge base of developer pain points for AI agent tooling.

EXISTING CLUSTERS (problems already identified across previous runs):
{existing_clusters}

NEW PAIN POINTS just extracted from community posts:
{new_pain_points}

For each new pain point, decide:
- If it describes the SAME underlying problem as an existing cluster → assign to that cluster
- If it's a GENUINELY DIFFERENT problem not covered by any cluster → create a new cluster

Assignment rules:
- Be generous with matching: "can't replay failed agent runs" and "no step-through debugger for agents" are the same cluster
- Different symptoms of the same root cause = same cluster
- Only create NEW if clearly distinct
- Each pain point maps to exactly ONE cluster

Return JSON:
{
  "assignments": [
    {
      "pain_point_id": 42,
      "cluster_id": 3,
      "new_cluster_name": null,
      "reasoning": "same debugging/observability gap"
    },
    {
      "pain_point_id": 43,
      "cluster_id": null,
      "new_cluster_name": "No cost tracking for multi-agent pipelines",
      "reasoning": "not covered by any existing cluster"
    }
  ]
}
"""

SYNTHESIS_PROMPT = """You are writing a product brief for a developer tool to be built.

The following {count} developer complaints were collected from {sources} over multiple weeks.
They all point to the same underlying problem: "{cluster_name}"

Evidence (each item is a real complaint from a real developer):
{evidence}

Write a problem brief in 5-7 sentences covering:
1. What exactly the problem is (be specific, not vague)
2. Who faces it and how often (reference the source counts)
3. How it currently manifests in their workflow
4. Why existing tools/workarounds fall short
5. What a good solution would look like

This brief will be used to spec and build a product. Be concrete and opinionated.
"""


# ── scraping ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def scrape_reddit(config: dict) -> list[dict]:
    import praw
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "ai-factory:v1.0"),
        check_for_async=False
    )
    posts = []
    cfg = config["reddit"]
    for sub_name in cfg["subreddits"]:
        for term in cfg["search_terms"][:3]:
            try:
                for post in reddit.subreddit(sub_name).search(
                    term, time_filter=cfg.get("time_filter", "week"),
                    limit=cfg.get("posts_per_search", 25)
                ):
                    posts.append({
                        "source_type": "reddit",
                        "url": f"https://reddit.com{post.permalink}",
                        "title": post.title,
                        "content": (post.selftext or "")[:2000],
                    })
            except Exception as e:
                log.warning(f"Reddit {sub_name}/{term}: {e}")
    log.info(f"Reddit: {len(posts)} posts")
    return posts


def scrape_hn(config: dict) -> list[dict]:
    posts = []
    cfg = config["hackernews"]
    for query in cfg["search_queries"]:
        try:
            resp = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "story", "hitsPerPage": cfg.get("max_results", 30)},
                timeout=15
            )
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                if hit.get("points", 0) < cfg.get("min_score", 3):
                    continue
                posts.append({
                    "source_type": "hackernews",
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    "title": hit.get("title", ""),
                    "content": hit.get("story_text") or "",
                })
        except Exception as e:
            log.warning(f"HN '{query}': {e}")
    log.info(f"HN: {len(posts)} posts")
    return posts


def scrape_github(config: dict) -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    posts = []
    cfg = config["github"]
    for repo in cfg["repos_to_watch"]:
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/issues",
                headers=headers,
                params={"state": "open", "per_page": cfg.get("max_issues_per_repo", 15), "sort": "created"},
                timeout=15
            )
            resp.raise_for_status()
            for issue in resp.json():
                if "pull_request" in issue:
                    continue
                posts.append({
                    "source_type": "github",
                    "url": issue["html_url"],
                    "title": issue["title"],
                    "content": (issue.get("body") or "")[:2000],
                })
        except Exception as e:
            log.warning(f"GitHub {repo}: {e}")
    log.info(f"GitHub: {len(posts)} issues")
    return posts


# ── step 1: store raw posts ───────────────────────────────────────────────────

def store_new_posts(posts: list[dict]) -> list[dict]:
    from storage.backlog import is_seen, mark_seen, insert_raw_post

    new = []
    for p in posts:
        if not p["url"] or is_seen(p["url"]):
            continue
        mark_seen(p["url"])
        row_id = insert_raw_post(p["source_type"], p["url"], p["title"], p["content"])
        if row_id:
            new.append({**p, "raw_post_id": row_id})

    log.info(f"Stored {len(new)} new posts (skipped {len(posts) - len(new)} already seen)")
    return new


# ── step 2: extract pain points and persist immediately ───────────────────────

def extract_and_store_pain_points(new_posts: list[dict]) -> list[dict]:
    from agents.base import chat_json
    from storage.backlog import insert_pain_point

    if not new_posts:
        return []

    stored = []
    batch_size = 20

    for i in range(0, len(new_posts), batch_size):
        batch = new_posts[i:i + batch_size]
        posts_text = "\n\n---\n\n".join(
            f"[{idx}] [{p['source_type'].upper()}] {p['title']}\n{p['content']}"
            for idx, p in enumerate(batch)
        )
        try:
            result = chat_json([{"role": "user", "content": EXTRACT_PROMPT + posts_text}])
            for pp in result.get("pain_points", []):
                idx = pp.get("post_index", 0)
                if idx >= len(batch):
                    continue
                post = batch[idx]
                pain_point_id = insert_pain_point(
                    raw_post_id=post["raw_post_id"],
                    description=pp["description"],
                    severity=pp.get("severity", "medium"),
                    source_type=post["source_type"],
                    source_url=post["url"]
                )
                stored.append({
                    "id": pain_point_id,
                    "description": pp["description"],
                    "severity": pp.get("severity", "medium"),
                    "source_type": post["source_type"],
                    "source_url": post["url"]
                })
        except Exception as e:
            log.error(f"Extraction batch {i // batch_size + 1} failed: {e}")

    log.info(f"Extracted and stored {len(stored)} pain points")
    return stored


# ── step 3: match pain points to clusters (the cross-run memory step) ─────────

def match_and_assign(unassigned: list[dict]) -> dict:
    from agents.base import chat_json
    from storage.backlog import (
        get_all_clusters, create_cluster,
        add_evidence_to_cluster, assign_pain_point_to_cluster
    )

    if not unassigned:
        return {"assigned": 0, "new_clusters": 0}

    existing = get_all_clusters()

    # Build a compact summary of existing clusters for the prompt
    if existing:
        clusters_text = json.dumps([
            {
                "cluster_id": c["id"],
                "name": c["name"],
                "evidence_count": c["evidence_count"],
                "brief": (c.get("synthesis_narrative") or "")[:200] or c["name"]
            }
            for c in existing
        ], indent=2)
    else:
        clusters_text = "[] (no clusters yet — all pain points will create new clusters)"

    pain_points_text = json.dumps([
        {"pain_point_id": pp["id"], "description": pp["description"],
         "source": pp["source_type"], "severity": pp["severity"]}
        for pp in unassigned
    ], indent=2)

    try:
        result = chat_json([{"role": "user", "content": MATCH_PROMPT.format(
            existing_clusters=clusters_text,
            new_pain_points=pain_points_text
        )}])
    except Exception as e:
        log.error(f"Cluster matching failed: {e}")
        return {"assigned": 0, "new_clusters": 0}

    assigned = 0
    new_clusters = 0
    # Cache newly created cluster IDs within this run to avoid duplicates
    new_cluster_cache: dict[str, int] = {}

    for assignment in result.get("assignments", []):
        pain_point_id = assignment.get("pain_point_id")
        cluster_id = assignment.get("cluster_id")
        new_name = assignment.get("new_cluster_name")

        # Find the pain point's source_type
        pp_match = next((p for p in unassigned if p["id"] == pain_point_id), None)
        if not pp_match:
            continue
        source_type = pp_match["source_type"]

        if cluster_id:
            # Assign to existing cluster
            add_evidence_to_cluster(cluster_id, pain_point_id, source_type)
            assign_pain_point_to_cluster(pain_point_id, cluster_id)
            assigned += 1
        elif new_name:
            # Create new cluster (or reuse one created earlier in this same run)
            if new_name in new_cluster_cache:
                cluster_id = new_cluster_cache[new_name]
            else:
                cluster_id = create_cluster(new_name)
                new_cluster_cache[new_name] = cluster_id
                new_clusters += 1
                log.info(f"New cluster: [{cluster_id}] {new_name}")
            add_evidence_to_cluster(cluster_id, pain_point_id, source_type)
            assign_pain_point_to_cluster(pain_point_id, cluster_id)
            assigned += 1

    log.info(f"Assigned {assigned} pain points, created {new_clusters} new clusters")
    return {"assigned": assigned, "new_clusters": new_clusters}


# ── step 4: synthesise narratives for mature clusters ─────────────────────────

def synthesise_mature_clusters(min_evidence: int = 5) -> int:
    from agents.base import chat
    from storage.backlog import (
        get_clusters_needing_synthesis, get_cluster_pain_points,
        update_cluster_synthesis
    )

    clusters = get_clusters_needing_synthesis(min_evidence)
    synthesised = 0

    for cluster in clusters:
        pain_points = get_cluster_pain_points(cluster["id"])
        if not pain_points:
            continue

        breakdown = json.loads(cluster.get("source_breakdown") or "{}")
        sources_str = ", ".join(f"{k} ({v})" for k, v in breakdown.items())

        evidence_text = "\n".join(
            f"- [{pp['source_type']}] {pp['description']} (severity: {pp['severity']})"
            for pp in pain_points
        )

        try:
            narrative = chat([{"role": "user", "content": SYNTHESIS_PROMPT.format(
                count=len(pain_points),
                sources=sources_str or "various sources",
                cluster_name=cluster["name"],
                evidence=evidence_text
            )}], temperature=0.4)

            status = "mature" if cluster["source_diversity"] >= 2 else "growing"
            update_cluster_synthesis(cluster["id"], narrative.strip(), status)
            synthesised += 1
            log.info(f"Synthesised narrative for cluster [{cluster['id']}] {cluster['name']}")
        except Exception as e:
            log.error(f"Synthesis failed for cluster {cluster['id']}: {e}")

    return synthesised


# ── step 5: promote mature clusters to backlog ────────────────────────────────

def promote_to_backlog(min_evidence: int = 5) -> int:
    from storage.backlog import get_mature_clusters_without_backlog, create_backlog_item

    clusters = get_mature_clusters_without_backlog(min_evidence)
    promoted = 0

    for c in clusters:
        create_backlog_item(
            cluster_id=c["id"],
            title=c["name"],
            description=c["synthesis_narrative"]
        )
        promoted += 1
        log.info(f"Promoted cluster [{c['id']}] to backlog: {c['name']}")

    return promoted


# ── main entry point ──────────────────────────────────────────────────────────

def run() -> dict:
    from storage.backlog import init_db, get_unassigned_pain_points

    init_db()
    config = load_config()
    log.info("Discovery agent starting")

    # Scrape
    all_posts = []
    all_posts.extend(scrape_reddit(config))
    all_posts.extend(scrape_hn(config))
    all_posts.extend(scrape_github(config))
    log.info(f"Total scraped: {len(all_posts)} posts")

    # Step 1: store new raw posts
    new_posts = store_new_posts(all_posts)

    # Step 2: extract pain points from new posts and persist
    extract_and_store_pain_points(new_posts)

    # Step 3: match ALL unassigned pain points to clusters (includes any from previous failed runs)
    unassigned = get_unassigned_pain_points()
    match_result = match_and_assign(unassigned)

    # Step 4: synthesise clusters that now have enough evidence
    synthesised = synthesise_mature_clusters(min_evidence=5)

    # Step 5: promote to backlog
    promoted = promote_to_backlog(min_evidence=5)

    result = {
        "posts_scraped": len(all_posts),
        "new_posts_stored": len(new_posts),
        "pain_points_matched": match_result["assigned"],
        "new_clusters": match_result["new_clusters"],
        "clusters_synthesised": synthesised,
        "backlog_items_promoted": promoted
    }
    log.info(f"Discovery complete: {result}")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
