"""
Scrapes Reddit, HN, and GitHub for developer pain points around AI/coding agents.
Clusters findings into themes and writes them to the backlog.
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

EXTRACT_PROMPT = """You are a developer pain-point analyst. Below are posts from developer communities about AI agents and coding agents.

Extract specific, concrete pain points developers are experiencing. Focus on:
- Recurring complaints or frustrations
- Missing tools or capabilities
- Things that break or behave unexpectedly
- Workflows that are unnecessarily difficult

Return JSON with this structure:
{
  "pain_points": [
    {
      "title": "short descriptive title",
      "description": "what the problem is and why it hurts",
      "evidence_quotes": ["direct quote 1", "direct quote 2"],
      "source_urls": ["url1", "url2"],
      "severity": "high|medium|low"
    }
  ]
}

Posts to analyze:
"""

CLUSTER_PROMPT = """You are a product strategist. Below are raw developer pain points extracted from community posts.

Group similar pain points into themes. Each theme represents a distinct problem area worth building a solution for.

Return JSON:
{
  "themes": [
    {
      "name": "short theme name (5 words max)",
      "description": "what this problem is and why it matters for AI agent developers",
      "pain_point_indices": [0, 2, 5],
      "frequency": 3,
      "source_urls": ["url1", "url2", "url3"],
      "example_solution": "what a tool solving this would look like"
    }
  ]
}

Pain points:
"""


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
    subreddits = config["reddit"]["subreddits"]
    search_terms = config["reddit"]["search_terms"]
    limit = config["reddit"].get("posts_per_search", 25)
    time_filter = config["reddit"].get("time_filter", "week")

    for sub_name in subreddits:
        for term in search_terms[:3]:  # limit API calls per subreddit
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.search(term, time_filter=time_filter, limit=limit):
                    url = f"https://reddit.com{post.permalink}"
                    posts.append({
                        "source_type": "reddit",
                        "url": url,
                        "title": post.title,
                        "content": (post.selftext or "")[:2000],
                        "score": post.score
                    })
            except Exception as e:
                log.warning(f"Reddit {sub_name}/{term}: {e}")

    log.info(f"Reddit: {len(posts)} posts scraped")
    return posts


def scrape_hn(config: dict) -> list[dict]:
    posts = []
    cfg = config["hackernews"]
    min_score = cfg.get("min_score", 3)
    max_results = cfg.get("max_results", 30)

    for query in cfg["search_queries"]:
        try:
            resp = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "story", "hitsPerPage": max_results},
                timeout=15
            )
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                if hit.get("points", 0) < min_score:
                    continue
                posts.append({
                    "source_type": "hackernews",
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    "title": hit.get("title", ""),
                    "content": hit.get("story_text") or "",
                    "score": hit.get("points", 0)
                })
        except Exception as e:
            log.warning(f"HN query '{query}': {e}")

    log.info(f"HN: {len(posts)} posts scraped")
    return posts


def scrape_github(config: dict) -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    posts = []
    cfg = config["github"]
    max_issues = cfg.get("max_issues_per_repo", 15)

    for repo in cfg["repos_to_watch"]:
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/issues",
                headers=headers,
                params={"state": "open", "per_page": max_issues, "sort": "created"},
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
                    "score": issue.get("reactions", {}).get("+1", 0)
                })
        except Exception as e:
            log.warning(f"GitHub {repo}: {e}")

    log.info(f"GitHub: {len(posts)} issues scraped")
    return posts


def extract_pain_points(posts: list[dict]) -> list[dict]:
    from agents.base import chat_json
    from storage.backlog import is_seen, mark_seen, insert_raw_problem

    new_posts = []
    for p in posts:
        if is_seen(p["url"]):
            continue
        mark_seen(p["url"])
        insert_raw_problem(p["source_type"], p["url"], p["title"], p["content"])
        new_posts.append(p)

    if not new_posts:
        log.info("No new posts to process")
        return []

    # Process in batches of 20 to stay within context limits
    all_pain_points = []
    batch_size = 20
    for i in range(0, len(new_posts), batch_size):
        batch = new_posts[i:i + batch_size]
        posts_text = "\n\n---\n\n".join(
            f"[{p['source_type'].upper()}] {p['title']}\nURL: {p['url']}\n{p['content']}"
            for p in batch
        )
        try:
            result = chat_json([{"role": "user", "content": EXTRACT_PROMPT + posts_text}])
            pain_points = result.get("pain_points", [])
            all_pain_points.extend(pain_points)
            log.info(f"Extracted {len(pain_points)} pain points from batch {i//batch_size + 1}")
        except Exception as e:
            log.error(f"Pain point extraction batch failed: {e}")

    return all_pain_points


def cluster_themes(pain_points: list[dict]) -> list[dict]:
    from agents.base import chat_json
    from storage.backlog import upsert_theme, upsert_backlog_item

    if not pain_points:
        return []

    pain_points_text = json.dumps(pain_points, indent=2)
    try:
        result = chat_json([{"role": "user", "content": CLUSTER_PROMPT + pain_points_text}])
        themes = result.get("themes", [])
    except Exception as e:
        log.error(f"Theme clustering failed: {e}")
        return []

    saved_themes = []
    for theme in themes:
        indices = theme.get("pain_point_indices", [])
        source_urls = theme.get("source_urls", [])
        # Collect all source URLs from referenced pain points
        for idx in indices:
            if idx < len(pain_points):
                source_urls.extend(pain_points[idx].get("source_urls", []))
        source_urls = list(set(source_urls))

        theme_id = upsert_theme(
            name=theme["name"],
            description=theme["description"],
            problem_ids=indices,
            source_urls=source_urls
        )
        upsert_backlog_item(
            theme_id=theme_id,
            title=theme["name"],
            description=theme["description"] + "\n\nExample solution: " + theme.get("example_solution", "")
        )
        saved_themes.append(theme)

    log.info(f"Saved {len(saved_themes)} themes to backlog")
    return saved_themes


def run() -> dict:
    from storage.backlog import init_db
    init_db()

    config = load_config()
    log.info("Starting discovery agent")

    all_posts = []
    all_posts.extend(scrape_reddit(config))
    all_posts.extend(scrape_hn(config))
    all_posts.extend(scrape_github(config))

    log.info(f"Total posts scraped: {len(all_posts)}")

    pain_points = extract_pain_points(all_posts)
    themes = cluster_themes(pain_points)

    return {
        "posts_scraped": len(all_posts),
        "pain_points_extracted": len(pain_points),
        "themes_updated": len(themes)
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run()
    print(result)
