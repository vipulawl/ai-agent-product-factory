import sqlite3
import json
import uuid
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "backlog.db"


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS raw_problems (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_url  TEXT UNIQUE,
                title       TEXT,
                content     TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS themes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                description  TEXT,
                problem_ids  TEXT,   -- JSON array of raw_problem ids
                frequency    INTEGER DEFAULT 0,
                source_urls  TEXT,   -- JSON array for outreach tracking
                created_at   TEXT DEFAULT (datetime('now')),
                updated_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS backlog_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                theme_id         INTEGER REFERENCES themes(id),
                title            TEXT NOT NULL,
                description      TEXT,
                priority_score   REAL DEFAULT 0,
                frequency_score  REAL DEFAULT 0,
                novelty_score    REAL DEFAULT 0,
                feasibility_score REAL DEFAULT 0,
                recency_score    REAL DEFAULT 0,
                market_score     REAL DEFAULT 0,
                status           TEXT DEFAULT 'pending',
                notes            TEXT,
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                token           TEXT UNIQUE NOT NULL,
                backlog_item_id INTEGER REFERENCES backlog_items(id),
                status          TEXT DEFAULT 'pending',
                requested_at    TEXT DEFAULT (datetime('now')),
                resolved_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS builds (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                backlog_item_id INTEGER REFERENCES backlog_items(id),
                github_url      TEXT,
                repo_name       TEXT,
                build_summary   TEXT,
                notified        INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS outreach (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                build_id    INTEGER REFERENCES builds(id),
                source_url  TEXT,
                message     TEXT,
                status      TEXT DEFAULT 'pending',
                sent_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS seen_urls (
                url        TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS digest_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at    TEXT DEFAULT (datetime('now')),
                summary    TEXT
            );
        """)


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── raw problems ──────────────────────────────────────────────────────────────

def is_seen(url: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,)).fetchone()
        return row is not None


def mark_seen(url: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO seen_urls (url) VALUES (?)", (url,))


def insert_raw_problem(source_type: str, source_url: str, title: str, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO raw_problems (source_type, source_url, title, content) VALUES (?,?,?,?)",
            (source_type, source_url, title, content)
        )
        return cur.lastrowid


# ── themes ────────────────────────────────────────────────────────────────────

def upsert_theme(name: str, description: str, problem_ids: list, source_urls: list) -> int:
    with get_conn() as conn:
        existing = conn.execute("SELECT id, frequency FROM themes WHERE name = ?", (name,)).fetchone()
        now = datetime.utcnow().isoformat()
        if existing:
            conn.execute(
                "UPDATE themes SET description=?, problem_ids=?, source_urls=?, frequency=?, updated_at=? WHERE id=?",
                (description, json.dumps(problem_ids), json.dumps(source_urls), len(problem_ids), now, existing["id"])
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO themes (name, description, problem_ids, source_urls, frequency) VALUES (?,?,?,?,?)",
            (name, description, json.dumps(problem_ids), json.dumps(source_urls), len(problem_ids))
        )
        return cur.lastrowid


def get_all_themes() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM themes ORDER BY frequency DESC").fetchall()
        return [dict(r) for r in rows]


# ── backlog items ─────────────────────────────────────────────────────────────

def upsert_backlog_item(theme_id: int, title: str, description: str) -> int:
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM backlog_items WHERE theme_id = ?", (theme_id,)).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO backlog_items (theme_id, title, description) VALUES (?,?,?)",
            (theme_id, title, description)
        )
        return cur.lastrowid


def update_scores(item_id: int, scores: dict):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("""
            UPDATE backlog_items SET
                priority_score=?, frequency_score=?, novelty_score=?,
                feasibility_score=?, recency_score=?, market_score=?,
                notes=?, updated_at=?
            WHERE id=?
        """, (
            scores["priority_score"], scores["frequency_score"], scores["novelty_score"],
            scores["feasibility_score"], scores["recency_score"], scores["market_score"],
            scores.get("reasoning", ""), now, item_id
        ))


def get_pending_backlog(min_score: float = 0.0) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT b.*, t.name as theme_name, t.source_urls
            FROM backlog_items b
            JOIN themes t ON t.id = b.theme_id
            WHERE b.status = 'pending' AND b.priority_score >= ?
            ORDER BY b.priority_score DESC
        """, (min_score,)).fetchall()
        return [dict(r) for r in rows]


def get_top_item(min_score: float = 6.0) -> dict | None:
    items = get_pending_backlog(min_score)
    return items[0] if items else None


def set_item_status(item_id: int, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE backlog_items SET status=?, updated_at=? WHERE id=?",
            (status, datetime.utcnow().isoformat(), item_id)
        )


# ── approvals ─────────────────────────────────────────────────────────────────

def create_approval(backlog_item_id: int) -> str:
    token = str(uuid.uuid4())[:8].upper()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO approvals (token, backlog_item_id) VALUES (?,?)",
            (token, backlog_item_id)
        )
    return token


def resolve_approval(token: str, status: str = "approved"):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE approvals SET status=?, resolved_at=? WHERE token=?",
            (status, now, token)
        )


def get_approval_status(token: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM approvals WHERE token=?", (token,)).fetchone()
        return row["status"] if row else None


def get_pending_approvals() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT a.*, b.title, b.description
            FROM approvals a
            JOIN backlog_items b ON b.id = a.backlog_item_id
            WHERE a.status = 'pending'
        """).fetchall()
        return [dict(r) for r in rows]


# ── builds ────────────────────────────────────────────────────────────────────

def record_build(backlog_item_id: int, github_url: str, repo_name: str, summary: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO builds (backlog_item_id, github_url, repo_name, build_summary) VALUES (?,?,?,?)",
            (backlog_item_id, github_url, repo_name, summary)
        )
        return cur.lastrowid


def get_unnotified_builds() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT bld.*, bi.title
            FROM builds bld
            JOIN backlog_items bi ON bi.id = bld.backlog_item_id
            WHERE bld.notified = 0
        """).fetchall()
        return [dict(r) for r in rows]


def mark_build_notified(build_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE builds SET notified=1 WHERE id=?", (build_id,))


def get_builds_for_refinement(after_days: int = 7) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT bld.*, bi.title, bi.description, t.source_urls
            FROM builds bld
            JOIN backlog_items bi ON bi.id = bld.backlog_item_id
            JOIN themes t ON t.id = bi.theme_id
            WHERE datetime(bld.created_at) <= datetime('now', ? || ' days')
            AND bi.status = 'built'
        """, (f"-{after_days}",)).fetchall()
        return [dict(r) for r in rows]


# ── outreach ──────────────────────────────────────────────────────────────────

def add_outreach(build_id: int, source_url: str, message: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO outreach (build_id, source_url, message) VALUES (?,?,?)",
            (build_id, source_url, message)
        )


def get_pending_outreach() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM outreach WHERE status='pending'").fetchall()
        return [dict(r) for r in rows]


def mark_outreach_sent(outreach_id: int):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE outreach SET status='sent', sent_at=? WHERE id=?", (now, outreach_id))


# ── digest ────────────────────────────────────────────────────────────────────

def log_digest(summary: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO digest_log (summary) VALUES (?)", (summary,))


def get_todays_discoveries() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM raw_problems
            WHERE date(created_at) = date('now')
            ORDER BY created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_todays_themes() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM themes
            WHERE date(updated_at) = date('now')
            ORDER BY frequency DESC
        """).fetchall()
        return [dict(r) for r in rows]
