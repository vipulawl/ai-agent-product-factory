# factory_status

Check the AI Agent Product Factory backlog and pipeline status.

## When to use
When the user asks about the factory, backlog, what's being built, or pipeline status.

## Instructions

Import and use the storage module from the factory project:

```python
import sys
sys.path.insert(0, "/Users/vipulagarwal/Projects/ai-agent-product-factory")
from storage.backlog import get_pending_backlog, get_builds_for_refinement, get_pending_approvals
import json

backlog = get_pending_backlog()
approvals = get_pending_approvals()
builds = get_builds_for_refinement(after_days=0)

print("## Backlog (top 10 by priority)")
for item in backlog[:10]:
    print(f"  [{item['status']}] {item['priority_score']:.1f} — {item['title']}")

print("\n## Pending Approvals")
for a in approvals:
    print(f"  Token: {a['token']} | {a['title']} | Requested: {a['requested_at']}")

print("\n## Built Products")
for b in builds:
    print(f"  {b['repo_name']} — {b['github_url']}")
```

Run this code and summarize the output for the user.
