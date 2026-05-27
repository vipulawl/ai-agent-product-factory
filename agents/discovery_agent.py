"""
Discovery agent — 5-step synthesis pipeline:

  1. SCRAPE    posts + comments (Reddit, HN, GitHub)
  2. EXTRACT   pain points — flags promotional posts, notes solutions mentioned in post/comments
  3. MATCH     assigns each pain point to a cluster (cross-run memory)
  4. SYNTHESISE writes a problem brief from all accumulated evidence
  5. PROMOTE   creates backlog items for mature clusters
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

# Fetch comments only for posts above this engagement threshold
COMMENT_FETCH_MIN_SCORE = 5
MAX_COMMENTS_PER_POST = 15

# ── prompts ───────────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """You are extracting developer pain points from community posts and their comments about AI agents and coding agents.

For each post+comments block, extract genuine pain points. Be careful about:

PROMOTIONAL POSTS: Many posts describe a problem and then plug their own solution.
- The pain point may still be REAL even if a solution is mentioned
- Mark is_promotional=true if the post's primary purpose is selling/promoting a tool
- Note the solution name if one is mentioned
- Mark solution_adequate=true ONLY if the solution genuinely, completely solves the problem (not just "we built something")

COMMENT SIGNAL: Comments often contain more honest signal than the post itself.
- Look for "+1", "same here", "I've had this exact issue" → frequency validation
- Look for "tried X but it doesn't work because..." → solution gaps
- Look for direct quotes that sharpen the pain description

Return JSON:
{
  "pain_points": [
    {
      "post_index": 0,
      "description": "one clear sentence describing the genuine underlying pain",
      "severity": "high|medium|low",
      "is_promotional": false,
      "solution_mentioned": "LangSmith",
      "solution_adequate": false,
      "solution_gap": "only works with LangChain, not framework-agnostic",
      "comment_signal": [
        "tried 3 tools none work for non-LangChain setups",
        "this is my biggest blocker right now"
      ],
      "direct_quote": "most useful exact quote from post or comments"
    }
  ]
}

Skip posts with no pain point. One post can yield multiple pain points.
If is_promotional=true and solution_adequate=true, still include — it's evidence the problem space is active.

Posts and comments:
"""

MATCH_PROMPT = """You maintain a running knowledge base of developer pain points for AI agent tooling.

EXISTING CLUSTERS:
{existing_clusters}

NEW PAIN POINTS (with solution and promotional context):
{new_pain_points}

For each pain point, assign to an existing cluster OR create a new one.
When assigning, also return any solution mentioned so the cluster's known_solutions list can be updated.

Rules:
- Same root cause = same cluster, even if symptoms differ
- Promotional posts count as evidence — someone built a product means the problem is real
- Only create NEW cluster if clearly a distinct problem

Return JSON:
{
  "assignments": [
    {
      "pain_point_id": 42,
      "cluster_id": 3,
      "new_cluster_name": null,
      "reasoning": "same debugging gap",
      "solution": {"name": "LangSmith", "adequate": false, "why_not": "LangChain-only"}
    },
    {
      "pain_point_id": 43,
      "cluster_id": null,
      "new_cluster_name": "No cost tracking for multi-agent pipelines",
      "reasoning": "genuinely distinct from existing clusters",
      "solution": null
    }
  ]
}
"""

SYNTHESIS_PROMPT = """You are writing a product brief for a developer tool to be built.

{count} developer complaints were collected from {sources} — cluster: "{cluster_name}"

{promo_note}

Known solutions that exist but are considered inadequate:
{known_solutions}

Evidence (real complaints, including comment quotes):
{evidence}

Write a problem brief (5-7 sentences):
1. What exactly the problem is
2. Who faces it and how widely (reference source counts)
3. Concrete manifestations in their workflow
4. Why the known solutions (if any) fall short — be specific
5. What an adequate solution needs to do

Be opinionated and specific. This brief becomes the spec for a product.
"""

# ── scraping with comments ────────────────────────────────────────────────────

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
                    comments = []
                    if post.score >= COMMENT_FETCH_MIN_SCORE or post.num_comments >= 3:
                        try:
                            post.comments.replace_more(limit=0)
                            # Sort by upvotes to get the most-agreed comments
                            top_comments = sorted(
                                post.comments.list(),
                                key=lambda c: getattr(c, "score", 0),
                                reverse=True
                            )[:MAX_COMMENTS_PER_POST]
                            comments = [
                                c.body[:400] for c in top_comments
                                if hasattr(c, "body") and len(c.body) > 20
                            ]
                        except Exception:
                            pass

                    posts.append({
                        "source_type": "reddit",
                        "url": f"https://reddit.com{post.permalink}",
                        "title": post.title,
                        "content": (post.selftext or "")[:1500],
                        "comments": comments,
                        "score": post.score,
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

                comments = []
                if hit.get("num_comments", 0) > 0:
                    try:
                        item_resp = requests.get(
                            f"https://hn.algolia.com/api/v1/items/{hit['objectID']}",
                            timeout=10
                        )
                        if item_resp.ok:
                            children = item_resp.json().get("children", [])
                            # Top-level comments only, sorted by relevance (order = HN rank)
                            for child in children[:MAX_COMMENTS_PER_POST]:
                                text = child.get("text") or ""
                                if len(text) > 20:
                                    comments.append(text[:400])
                    except Exception:
                        pass

                posts.append({
                    "source_type": "hackernews",
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    "title": hit.get("title", ""),
                    "content": hit.get("story_text") or "",
                    "comments": comments,
                    "score": hit.get("points", 0),
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

                comments = []
                if issue.get("comments", 0) > 0:
                    try:
                        c_resp = requests.get(
                            issue["comments_url"], headers=headers,
                            params={"per_page": MAX_COMMENTS_PER_POST}, timeout=10
                        )
                        if c_resp.ok:
                            comments = [
                                c["body"][:400] for c in c_resp.json()
                                if c.get("body") and len(c["body"]) > 20
                            ]
                    except Exception:
                        pass

                posts.append({
                    "source_type": "github",
                    "url": issue["html_url"],
                    "title": issue["title"],
                    "content": (issue.get("body") or "")[:1500],
                    "comments": comments,
                    "score": issue.get("reactions", {}).get("+1", 0),
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

    log.info(f"Stored {len(new)} new posts ({len(posts) - len(new)} already seen)")
    return new


# ── step 2: extract pain points (with promotional + solution detection) ────────

def extract_and_store_pain_points(new_posts: list[dict]) -> list[dict]:
    from agents.base import chat_json
    from storage.backlog import insert_pain_point

    if not new_posts:
        return []

    stored = []
    batch_size = 15  # smaller batches since each post now includes comments

    for i in range(0, len(new_posts), batch_size):
        batch = new_posts[i:i + batch_size]

        posts_text = "\n\n---\n\n".join(
            _format_post_with_comments(idx, p)
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
                    source_url=post["url"],
                    is_promotional=pp.get("is_promotional", False),
                    solution_mentioned=pp.get("solution_mentioned"),
                    solution_adequate=pp.get("solution_adequate", False),
                    comment_signal=pp.get("comment_signal", []),
                )
                stored.append({
                    "id": pain_point_id,
                    "description": pp["description"],
                    "severity": pp.get("severity", "medium"),
                    "source_type": post["source_type"],
                    "source_url": post["url"],
                    "is_promotional": pp.get("is_promotional", False),
                    "solution_mentioned": pp.get("solution_mentioned"),
                    "solution_adequate": pp.get("solution_adequate", False),
                    "solution_gap": pp.get("solution_gap"),
                    "comment_signal": pp.get("comment_signal", []),
                })
        except Exception as e:
            log.error(f"Extraction batch {i // batch_size + 1} failed: {e}")

    promo_count = sum(1 for p in stored if p.get("is_promotional"))
    log.info(f"Extracted {len(stored)} pain points ({promo_count} promotional, "
             f"{len(stored) - promo_count} organic)")
    return stored


def _format_post_with_comments(idx: int, post: dict) -> str:
    lines = [
        f"[{idx}] [{post['source_type'].upper()}] {post['title']}",
        f"URL: {post['url']}",
        post["content"] or "(no body)",
    ]
    if post.get("comments"):
        lines.append(f"\nTop comments ({len(post['comments'])}):")
        for c in post["comments"][:10]:
            lines.append(f"  > {c}")
    return "\n".join(lines)


# ── step 3: match to clusters ─────────────────────────────────────────────────

def match_and_assign(unassigned: list[dict]) -> dict:
    from agents.base import chat_json
    from storage.backlog import (
        get_all_clusters, create_cluster,
        add_evidence_to_cluster, assign_pain_point_to_cluster, merge_known_solutions
    )

    if not unassigned:
        return {"assigned": 0, "new_clusters": 0}

    existing = get_all_clusters()
    clusters_text = json.dumps([
        {
            "cluster_id": c["id"],
            "name": c["name"],
            "evidence_count": c["evidence_count"],
            "brief": (c.get("synthesis_narrative") or "")[:200] or c["name"],
            "known_solutions": json.loads(c.get("known_solutions") or "[]")
        }
        for c in existing
    ], indent=2) if existing else "[] (no clusters yet)"

    pain_points_text = json.dumps([
        {
            "pain_point_id": pp["id"],
            "description": pp["description"],
            "source": pp["source_type"],
            "severity": pp["severity"],
            "is_promotional": pp.get("is_promotional", False),
            "solution_mentioned": pp.get("solution_mentioned"),
            "solution_adequate": pp.get("solution_adequate", False),
        }
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
    new_cluster_cache: dict[str, int] = {}

    for a in result.get("assignments", []):
        pain_point_id = a.get("pain_point_id")
        cluster_id = a.get("cluster_id")
        new_name = a.get("new_cluster_name")
        solution = a.get("solution")  # {name, adequate, why_not}

        pp = next((p for p in unassigned if p["id"] == pain_point_id), None)
        if not pp:
            continue

        if not cluster_id and new_name:
            if new_name in new_cluster_cache:
                cluster_id = new_cluster_cache[new_name]
            else:
                cluster_id = create_cluster(new_name)
                new_cluster_cache[new_name] = cluster_id
                new_clusters += 1
                log.info(f"New cluster [{cluster_id}]: {new_name}")

        if cluster_id:
            add_evidence_to_cluster(cluster_id, pain_point_id, pp["source_type"])
            assign_pain_point_to_cluster(pain_point_id, cluster_id)
            assigned += 1

            # Record any solutions discovered in this evidence piece
            if solution and solution.get("name"):
                merge_known_solutions(cluster_id, [solution])

    log.info(f"Assigned {assigned} pain points, {new_clusters} new clusters")
    return {"assigned": assigned, "new_clusters": new_clusters}


# ── step 4: synthesise narratives ─────────────────────────────────────────────

def synthesise_mature_clusters(min_evidence: int = 5) -> int:
    from agents.base import chat
    from storage.backlog import (
        get_clusters_needing_synthesis, get_cluster_pain_points, update_cluster_synthesis
    )

    clusters = get_clusters_needing_synthesis(min_evidence)
    synthesised = 0

    for cluster in clusters:
        pain_points = get_cluster_pain_points(cluster["id"])
        if not pain_points:
            continue

        breakdown = json.loads(cluster.get("source_breakdown") or "{}")
        sources_str = ", ".join(f"{k} ({v})" for k, v in breakdown.items())

        known_solutions = json.loads(cluster.get("known_solutions") or "[]")
        if known_solutions:
            solutions_str = "\n".join(
                f"- {s['name']}: {s.get('why_not', 'inadequate')}"
                for s in known_solutions
            )
        else:
            solutions_str = "None identified yet"

        promo_count = cluster.get("promo_count", 0)
        total = cluster["evidence_count"]
        promo_note = (
            f"Note: {promo_count} of {total} evidence pieces are from promotional posts "
            f"(people selling solutions) — this means the space has commercial activity "
            f"but existing solutions are still considered inadequate."
            if promo_count > 0 else ""
        )

        # Build rich evidence text including comment signals
        evidence_lines = []
        for pp in pain_points:
            line = f"- [{pp['source_type']}] {pp['description']} (severity: {pp['severity']})"
            if pp.get("is_promotional"):
                line += " [PROMOTIONAL POST]"
            comments = json.loads(pp.get("comment_signal") or "[]")
            for c in comments[:2]:
                line += f"\n    comment: \"{c}\""
            evidence_lines.append(line)

        try:
            narrative = chat([{"role": "user", "content": SYNTHESIS_PROMPT.format(
                count=len(pain_points),
                sources=sources_str or "various sources",
                cluster_name=cluster["name"],
                promo_note=promo_note,
                known_solutions=solutions_str,
                evidence="\n".join(evidence_lines)
            )}], temperature=0.4)

            status = "mature" if cluster["source_diversity"] >= 2 else "growing"
            update_cluster_synthesis(cluster["id"], narrative.strip(), status)
            synthesised += 1
            log.info(f"Synthesised [{cluster['id']}] {cluster['name']} "
                     f"({len(pain_points)} evidence, {promo_count} promo)")
        except Exception as e:
            log.error(f"Synthesis failed for cluster {cluster['id']}: {e}")

    return synthesised


# ── step 5: promote to backlog ────────────────────────────────────────────────

def promote_to_backlog(min_evidence: int = 5) -> int:
    from storage.backlog import get_mature_clusters_without_backlog, create_backlog_item

    clusters = get_mature_clusters_without_backlog(min_evidence)
    promoted = 0
    for c in clusters:
        create_backlog_item(c["id"], c["name"], c["synthesis_narrative"])
        promoted += 1
        log.info(f"Promoted cluster [{c['id']}] → backlog: {c['name']}")
    return promoted


# ── main ──────────────────────────────────────────────────────────────────────

def run() -> dict:
    from storage.backlog import init_db, get_unassigned_pain_points

    init_db()
    config = load_config()
    log.info("Discovery agent starting")

    all_posts = []
    all_posts.extend(scrape_reddit(config))
    all_posts.extend(scrape_hn(config))
    all_posts.extend(scrape_github(config))
    log.info(f"Total scraped: {len(all_posts)}")

    new_posts = store_new_posts(all_posts)
    extract_and_store_pain_points(new_posts)

    unassigned = get_unassigned_pain_points()
    match_result = match_and_assign(unassigned)

    synthesised = synthesise_mature_clusters(min_evidence=5)
    promoted = promote_to_backlog(min_evidence=5)

    return {
        "posts_scraped": len(all_posts),
        "new_posts_stored": len(new_posts),
        "pain_points_matched": match_result["assigned"],
        "new_clusters": match_result["new_clusters"],
        "clusters_synthesised": synthesised,
        "backlog_items_promoted": promoted,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
