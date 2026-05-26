"""Print current pipeline status: backlog, pending approvals, builds."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.backlog import init_db, get_pending_backlog, get_pending_approvals, get_builds_for_refinement

init_db()

backlog = get_pending_backlog()
approvals = get_pending_approvals()
builds = get_builds_for_refinement(after_days=0)

print("── Backlog (top 10 by priority) ─────────────────────")
for item in backlog[:10]:
    print(f"  {item['priority_score']:4.1f}  [{item['status']}]  {item['title']}")
if not backlog:
    print("  (empty)")

print("\n── Pending Approvals ────────────────────────────────")
for a in approvals:
    print(f"  Token: {a['token']}  |  {a['title']}  |  requested: {a['requested_at']}")
if not approvals:
    print("  (none)")

print("\n── Built Products ───────────────────────────────────")
for b in builds:
    print(f"  {b['repo_name']}  →  {b['github_url']}")
if not builds:
    print("  (none yet)")
