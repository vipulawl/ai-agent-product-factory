"""
Hermes builtin_hook — routes incoming Telegram approval/rejection messages to our backlog DB.

Install: copy this file to ~/.hermes/hermes-agent/gateway/builtin_hooks/approval_hook.py
Then restart `hermes gateway start`.

Hermes calls every file in builtin_hooks/ on each incoming gateway message.
"""

import os
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

# Allow importing storage module from our project
_FACTORY_DIR = Path(os.environ.get("FACTORY_DIR", Path.home() / "Projects/ai-agent-product-factory"))
if str(_FACTORY_DIR) not in sys.path:
    sys.path.insert(0, str(_FACTORY_DIR))

_DB_PATH = _FACTORY_DIR / "data" / "backlog.db"


def _resolve_approval(token: str, status: str):
    if not _DB_PATH.exists():
        return
    conn = sqlite3.connect(_DB_PATH)
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE approvals SET status=?, resolved_at=? WHERE token=? AND status='pending'",
        (status, now, token)
    )
    conn.commit()
    conn.close()


async def handle(message: str, platform: str, session, **kwargs) -> str | None:
    """
    Called by Hermes gateway for every incoming message.
    Return a string to send a reply, or None to let Hermes handle it normally.
    """
    text = (message or "").strip().upper()

    # Match: /approve ABC12345 | YES ABC12345 | APPROVE ABC12345
    for word in text.split():
        if len(word) == 8 and word.isalnum():
            token = word
            if "REJECT" in text or "NO" in text:
                _resolve_approval(token, "rejected")
                return f"❌ Build rejected (token: {token})"
            elif "APPROVE" in text or "YES" in text:
                _resolve_approval(token, "approved")
                return f"✅ Build approved! Starting... (token: {token})"

    return None  # let Hermes handle normally
