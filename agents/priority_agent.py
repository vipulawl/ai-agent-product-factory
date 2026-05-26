"""
Scores and ranks backlog items using GPT-4o and weighted rules.
"""

import json
import logging
import yaml
from pathlib import Path

log = logging.getLogger(__name__)

RULES_PATH = Path(__file__).parent.parent / "config" / "priority_rules.yaml"

SCORE_PROMPT = """You are a product prioritization expert for developer tools. Score these backlog items for AI agent developer tooling.

Scoring criteria (each 0–10):
- frequency_score: How many developers face this? (10 = universal pain)
- novelty_score: How underserved is this? (10 = no good solution exists)
- feasibility_score: Buildable standalone by 1 dev in ~1 week? (10 = very feasible)
- recency_score: How recently is this mentioned? (10 = trending right now)
- market_score: Size of affected developer segment? (10 = all AI/agent devs)

Return JSON:
{
  "scores": [
    {
      "item_id": <id>,
      "frequency_score": 7,
      "novelty_score": 8,
      "feasibility_score": 6,
      "recency_score": 9,
      "market_score": 7,
      "reasoning": "brief justification"
    }
  ]
}

Backlog items to score:
"""


def load_weights() -> dict:
    with open(RULES_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg["weights"]


def compute_priority(scores: dict, weights: dict) -> float:
    return round(
        scores["frequency_score"] * weights["frequency"]
        + scores["novelty_score"] * weights["novelty"]
        + scores["feasibility_score"] * weights["feasibility"]
        + scores["recency_score"] * weights["recency"]
        + scores["market_score"] * weights["market_size"],
        2
    )


def run() -> dict:
    from agents.base import chat_json
    from storage.backlog import init_db, get_pending_backlog, update_scores

    init_db()
    weights = load_weights()

    items = get_pending_backlog()
    if not items:
        log.info("No pending backlog items to score")
        return {"scored": 0}

    # Score in batches of 10
    batch_size = 10
    total_scored = 0

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        items_text = json.dumps([
            {"item_id": it["id"], "title": it["title"], "description": it["description"],
             "frequency": it.get("frequency", 0)}
            for it in batch
        ], indent=2)

        try:
            result = chat_json([{"role": "user", "content": SCORE_PROMPT + items_text}])
            for score_data in result.get("scores", []):
                item_id = score_data["item_id"]
                score_data["priority_score"] = compute_priority(score_data, weights)
                update_scores(item_id, score_data)
                total_scored += 1
                log.info(
                    f"Item {item_id} scored: priority={score_data['priority_score']} "
                    f"({score_data.get('reasoning', '')})"
                )
        except Exception as e:
            log.error(f"Scoring batch {i//batch_size + 1} failed: {e}")

    log.info(f"Scored {total_scored} backlog items")
    return {"scored": total_scored}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
