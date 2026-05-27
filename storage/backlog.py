import sqlite3
import json
import uuid
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "backlog.db"

SCHEMA = """
-- Every scraped post, deduped by URL
CREATE TABLE IF NOT EXISTS raw_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,          -- reddit | hackernews | github
    source_url  TEXT UNIQUE,
    title       TEXT,
    content     TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Atomic extracted pain points, one row per complaint
CREATE TABLE IF NOT EXISTS pain_points (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_post_id       INTEGER REFERENCES raw_posts(id),
    description       TEXT NOT NULL,
    severity          TEXT,                    -- high | medium | low
    source_type       TEXT,
    source_url        TEXT,
    is_promotional    INTEGER DEFAULT 0,       -- 1 if post was plugging a solution
    solution_mentioned TEXT,                   -- name of solution mentioned in post/comments, if any
    solution_adequate  INTEGER DEFAULT 0,      -- 1 if the mentioned solution actually solves it well
    comment_signal    TEXT,                    -- notable quotes from comments (JSON array)
    cluster_id        INTEGER REFERENCES clusters(id),
    created_at        TEXT DEFAULT (datetime('now'))
);

-- Each cluster is a synthesised problem theme built up over many runs
CREATE TABLE IF NOT EXISTS clusters (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    synthesis_narrative  TEXT,          -- GPT-4o written brief, updated as evidence grows
    evidence_count       INTEGER DEFAULT 0,
    promo_count          INTEGER DEFAULT 0,   -- how many evidence pieces were promotional posts
    source_diversity     INTEGER DEFAULT 0,
    source_breakdown     TEXT DEFAULT '{}',  -- JSON: {"reddit":5,"hackernews":2,"github":1}
    known_solutions      TEXT DEFAULT '[]',  -- JSON: [{"name":"LangSmith","adequate":false,"why_not":"no replay"}]
    first_seen           TEXT DEFAULT (datetime('now')),
    last_updated         TEXT DEFAULT (datetime('now')),
    status               TEXT DEFAULT 'growing'  -- growing | mature | ready_to_build | built
);

-- Evidence log: which pain points contributed to which cluster
CREATE TABLE IF NOT EXISTS cluster_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id    INTEGER REFERENCES clusters(id),
    pain_point_id INTEGER REFERENCES pain_points(id),
    added_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(cluster_id, pain_point_id)
);

-- Backlog items derived from mature clusters
CREATE TABLE IF NOT EXISTS backlog_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id        INTEGER REFERENCES clusters(id),
    title             TEXT NOT NULL,
    description       TEXT,
    priority_score    REAL DEFAULT 0,
    frequency_score   REAL DEFAULT 0,
    novelty_score     REAL DEFAULT 0,
    feasibility_score REAL DEFAULT 0,
    recency_score     REAL DEFAULT 0,
    market_score      REAL DEFAULT 0,
    status            TEXT DEFAULT 'pending',  -- pending | building | built | skipped
    notes             TEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

-- Approval tokens for the builder Telegram gate
CREATE TABLE IF NOT EXISTS approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE NOT NULL,
    backlog_item_id INTEGER REFERENCES backlog_items(id),
    status          TEXT DEFAULT 'pending',  -- pending | approved | rejected
    requested_at    TEXT DEFAULT (datetime('now')),
    resolved_at     TEXT
);

-- Built products
CREATE TABLE IF NOT EXISTS builds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backlog_item_id INTEGER REFERENCES backlog_items(id),
    github_url      TEXT,
    repo_name       TEXT,
    build_summary   TEXT,
    notified        INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Outreach messages drafted for original thread authors
CREATE TABLE IF NOT EXISTS outreach (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id   INTEGER REFERENCES builds(id),
    source_url TEXT,
    message    TEXT,
    status     TEXT DEFAULT 'pending',
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS seen_urls (
    url        TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS digest_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at  TEXT DEFAULT (datetime('now')),
    summary  TEXT
);
"""


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Add columns introduced after initial schema without dropping existing data."""
    migrations = [
        ("pain_points", "is_promotional",    "INTEGER DEFAULT 0"),
        ("pain_points", "solution_mentioned", "TEXT"),
        ("pain_points", "solution_adequate",  "INTEGER DEFAULT 0"),
        ("pain_points", "comment_signal",     "TEXT"),
        ("clusters",    "promo_count",        "INTEGER DEFAULT 0"),
        ("clusters",    "known_solutions",    "TEXT DEFAULT '[]'"),
    ]
    for table, col, col_def in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── seen URLs ─────────────────────────────────────────────────────────────────

def is_seen(url: str) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM seen_urls WHERE url=?", (url,)).fetchone() is not None


def mark_seen(url: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO seen_urls (url) VALUES (?)", (url,))


# ── raw posts ─────────────────────────────────────────────────────────────────

def insert_raw_post(source_type: str, source_url: str, title: str, content: str) -> int | None:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO raw_posts (source_type, source_url, title, content) VALUES (?,?,?,?)",
            (source_type, source_url, title, content)
        )
        return cur.lastrowid if cur.lastrowid else None


# ── pain points ───────────────────────────────────────────────────────────────

def insert_pain_point(raw_post_id: int, description: str, severity: str,
                       source_type: str, source_url: str,
                       is_promotional: bool = False,
                       solution_mentioned: str = None,
                       solution_adequate: bool = False,
                       comment_signal: list = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO pain_points
               (raw_post_id, description, severity, source_type, source_url,
                is_promotional, solution_mentioned, solution_adequate, comment_signal)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (raw_post_id, description, severity, source_type, source_url,
             int(is_promotional), solution_mentioned, int(solution_adequate),
             json.dumps(comment_signal or []))
        )
        return cur.lastrowid


def get_unassigned_pain_points() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pain_points WHERE cluster_id IS NULL ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def assign_pain_point_to_cluster(pain_point_id: int, cluster_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE pain_points SET cluster_id=? WHERE id=?", (cluster_id, pain_point_id))


def get_cluster_pain_points(cluster_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pain_points WHERE cluster_id=? ORDER BY created_at DESC",
            (cluster_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── clusters ──────────────────────────────────────────────────────────────────

def create_cluster(name: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO clusters (name) VALUES (?)", (name,))
        return cur.lastrowid


def get_all_clusters() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM clusters ORDER BY evidence_count DESC").fetchall()
        return [dict(r) for r in rows]


def get_cluster_by_id(cluster_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        return dict(row) if row else None


def add_evidence_to_cluster(cluster_id: int, pain_point_id: int, source_type: str):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO cluster_evidence (cluster_id, pain_point_id) VALUES (?,?)",
            (cluster_id, pain_point_id)
        )
        # Recount everything from source tables so counts are always accurate
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM cluster_evidence WHERE cluster_id=?",
            (cluster_id,)
        ).fetchone()
        evidence_count = row["cnt"]

        sources = conn.execute("""
            SELECT pp.source_type, COUNT(*) as cnt
            FROM cluster_evidence ce
            JOIN pain_points pp ON pp.id = ce.pain_point_id
            WHERE ce.cluster_id=?
            GROUP BY pp.source_type
        """, (cluster_id,)).fetchall()
        breakdown = {r["source_type"]: r["cnt"] for r in sources}

        promo_row = conn.execute("""
            SELECT COUNT(*) as cnt
            FROM cluster_evidence ce
            JOIN pain_points pp ON pp.id = ce.pain_point_id
            WHERE ce.cluster_id=? AND pp.is_promotional=1
        """, (cluster_id,)).fetchone()

        conn.execute("""
            UPDATE clusters SET
                evidence_count=?, source_diversity=?, source_breakdown=?,
                promo_count=?, last_updated=?
            WHERE id=?
        """, (evidence_count, len(breakdown), json.dumps(breakdown),
              promo_row["cnt"], now, cluster_id))


def merge_known_solutions(cluster_id: int, new_solutions: list[dict]):
    """Merge newly discovered solutions into the cluster's known_solutions list (no duplicates)."""
    with get_conn() as conn:
        row = conn.execute("SELECT known_solutions FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        existing = json.loads(row["known_solutions"] or "[]") if row else []
        existing_names = {s["name"].lower() for s in existing}
        for sol in new_solutions:
            if sol.get("name", "").lower() not in existing_names:
                existing.append(sol)
                existing_names.add(sol["name"].lower())
        conn.execute("UPDATE clusters SET known_solutions=? WHERE id=?",
                     (json.dumps(existing), cluster_id))


def update_cluster_synthesis(cluster_id: int, narrative: str, status: str = None):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        if status:
            conn.execute(
                "UPDATE clusters SET synthesis_narrative=?, status=?, last_updated=? WHERE id=?",
                (narrative, status, now, cluster_id)
            )
        else:
            conn.execute(
                "UPDATE clusters SET synthesis_narrative=?, last_updated=? WHERE id=?",
                (narrative, now, cluster_id)
            )


def get_clusters_needing_synthesis(min_evidence: int = 5) -> list[dict]:
    """Clusters with enough evidence but no narrative yet, or that have grown since last synthesis."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.*
            FROM clusters c
            WHERE c.evidence_count >= ?
            AND c.status IN ('growing', 'mature')
            ORDER BY c.evidence_count DESC
        """, (min_evidence,)).fetchall()
        return [dict(r) for r in rows]


def get_mature_clusters_without_backlog(min_evidence: int = 5) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.* FROM clusters c
            LEFT JOIN backlog_items b ON b.cluster_id = c.id
            WHERE c.evidence_count >= ?
            AND c.synthesis_narrative IS NOT NULL
            AND b.id IS NULL
            AND c.status NOT IN ('built')
        """, (min_evidence,)).fetchall()
        return [dict(r) for r in rows]


# ── backlog items ─────────────────────────────────────────────────────────────

def create_backlog_item(cluster_id: int, title: str, description: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO backlog_items (cluster_id, title, description) VALUES (?,?,?)",
            (cluster_id, title, description)
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
            SELECT b.*, c.name as cluster_name, c.synthesis_narrative,
                   c.evidence_count, c.source_diversity, c.source_breakdown,
                   c.known_solutions, c.promo_count
            FROM backlog_items b
            JOIN clusters c ON c.id = b.cluster_id
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
            SELECT bld.*, bi.title FROM builds bld
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
            SELECT bld.*, bi.title, bi.description, c.source_breakdown
            FROM builds bld
            JOIN backlog_items bi ON bi.id = bld.backlog_item_id
            JOIN clusters c ON c.id = bi.cluster_id
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


def mark_outreach_sent(outreach_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE outreach SET status='sent', sent_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), outreach_id)
        )


# ── digest helpers ────────────────────────────────────────────────────────────

def log_digest(summary: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO digest_log (summary) VALUES (?)", (summary,))


def get_todays_stats() -> dict:
    with get_conn() as conn:
        new_posts = conn.execute(
            "SELECT COUNT(*) as n FROM raw_posts WHERE date(created_at)=date('now')"
        ).fetchone()["n"]
        new_pain_points = conn.execute(
            "SELECT COUNT(*) as n FROM pain_points WHERE date(created_at)=date('now')"
        ).fetchone()["n"]
        updated_clusters = conn.execute(
            "SELECT COUNT(*) as n FROM clusters WHERE date(last_updated)=date('now')"
        ).fetchone()["n"]
        return {
            "new_posts": new_posts,
            "new_pain_points": new_pain_points,
            "updated_clusters": updated_clusters
        }


def get_top_clusters(limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.*, b.priority_score
            FROM clusters c
            LEFT JOIN backlog_items b ON b.cluster_id = c.id
            WHERE c.evidence_count > 0
            ORDER BY c.evidence_count DESC, c.source_diversity DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
