"""
Scores backlog items using GPT-4o. Uses the full synthesis narrative + source diversity
so cross-platform problems score higher than single-source ones.
"""

import json
import logging
import yaml
from pathlib import Path

log = logging.getLogger(__name__)

RULES_PATH = Path(__file__).parent.parent / "config" / "priority_rules.yaml"

SCORE_PROMPT = """You are a product prioritisation expert for developer tools targeting AI agent builders.

Score these backlog items. Each item includes a synthesis narrative built from real developer complaints.

Scoring (each 0–10):
- frequency_score: How many developers face this? (use evidence_count as signal — 10+ mentions = 9-10)
- novelty_score: How underserved is this? (10 = nothing usable exists; if known_solutions lists tools that are adequate, score ≤ 3; if they exist but are incomplete/inadequate, score 5-7)
- feasibility_score: Buildable standalone by 1 dev in ~1 week as a useful tool? (10 = very doable)
- recency_score: Is this being complained about actively right now? (10 = mentioned in last 7 days)
- market_score: What fraction of AI/agent developers would use this? (10 = everyone building agents)

Priority = weighted average (see weights). Scores are per-item, not relative rankings.
Items appearing on Reddit + HN + GitHub simultaneously should score higher on frequency and market.

Return JSON:
{
  "scores": [
    {
      "item_id": 1,
      "frequency_score": 8,
      "novelty_score": 7,
      "feasibility_score": 9,
      "recency_score": 6,
      "market_score": 8,
      "reasoning": "14 mentions across 3 platforms, no good replay debugger exists, buildable as a CLI"
    }
  ]
}

Items to score:
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

    total_scored = 0
    for i in range(0, len(items), 10):
        batch = items[i:i + 10]
        items_payload = [
            {
                "item_id": it["id"],
                "title": it["title"],
                "synthesis_narrative": (it.get("synthesis_narrative") or it["description"] or "")[:800],
                "evidence_count": it.get("evidence_count", 0),
                "source_diversity": it.get("source_diversity", 1),
                "source_breakdown": it.get("source_breakdown", "{}"),
                # Known solutions affect novelty score — pass them explicitly
                "known_solutions": json.loads(it.get("known_solutions") or "[]"),
            }
            for it in batch
        ]
        try:
            result = chat_json([{"role": "user", "content": SCORE_PROMPT + json.dumps(items_payload, indent=2)}])
            for score_data in result.get("scores", []):
                score_data["priority_score"] = compute_priority(score_data, weights)
                update_scores(score_data["item_id"], score_data)
                total_scored += 1
                log.info(f"Item {score_data['item_id']} → priority {score_data['priority_score']} ({score_data.get('reasoning', '')})")
        except Exception as e:
            log.error(f"Scoring batch {i // 10 + 1} failed: {e}")

    log.info(f"Scored {total_scored} items")
    return {"scored": total_scored}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
