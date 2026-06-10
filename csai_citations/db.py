"""SQLite schema, connection, and upsert helpers.

Paper metadata (immutable) lives in `papers`; mutable citation counts live in
`citations`, with every observed count also appended to `citation_history` so
trends across refreshes are preserved.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = "citations.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id         TEXT PRIMARY KEY,   -- bare id, no version, e.g. 2506.00056
    title            TEXT NOT NULL,
    authors          TEXT,               -- semicolon-joined
    primary_category TEXT,
    all_categories   TEXT,               -- comma-joined
    submitted_at     TEXT NOT NULL,      -- ISO datetime
    month            TEXT NOT NULL,      -- 'YYYY-MM'
    ingested_at      TEXT NOT NULL       -- when we first saw it
);
CREATE INDEX IF NOT EXISTS idx_papers_month ON papers(month);

CREATE TABLE IF NOT EXISTS citations (
    arxiv_id        TEXT PRIMARY KEY REFERENCES papers(arxiv_id),
    citation_count  INTEGER,            -- NULL = not found / not yet fetched
    source          TEXT NOT NULL,      -- 's2' | 'openalex'
    fetched_at      TEXT NOT NULL       -- ISO datetime of this count
);

CREATE TABLE IF NOT EXISTS citation_history (
    arxiv_id        TEXT NOT NULL,
    citation_count  INTEGER,
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (arxiv_id, fetched_at)
);

-- Social "attention" metric, kept separate from citations so the two never
-- overwrite each other. Currently sourced from Hacker News (sum of story
-- points for the paper).
CREATE TABLE IF NOT EXISTS social (
    arxiv_id        TEXT PRIMARY KEY REFERENCES papers(arxiv_id),
    score           REAL,               -- NULL = not yet fetched
    source          TEXT NOT NULL,      -- 'hn'
    fetched_at      TEXT NOT NULL,      -- ISO datetime of this score
    top_story_id    TEXT                -- HN item id of the most-upvoted story
);

CREATE TABLE IF NOT EXISTS social_history (
    arxiv_id        TEXT NOT NULL,
    score           REAL,
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (arxiv_id, fetched_at)
);
"""


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection, enable foreign keys, and ensure the schema exists."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created.

    `CREATE TABLE IF NOT EXISTS` leaves pre-existing tables untouched, so newly
    added columns need an explicit ALTER. Each is guarded to be a no-op once the
    column is present, keeping `connect()` idempotent.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(social)")}
    if "top_story_id" not in cols:
        conn.execute("ALTER TABLE social ADD COLUMN top_story_id TEXT")


def upsert_paper(
    conn: sqlite3.Connection,
    *,
    arxiv_id: str,
    title: str,
    authors: str,
    primary_category: str,
    all_categories: str,
    submitted_at: str,
    month: str,
) -> bool:
    """Insert or update a paper's metadata.

    `ingested_at` is preserved on conflict (it records when we *first* saw the
    paper). Returns True if this was a brand-new paper, False if it already
    existed.
    """
    cur = conn.execute("SELECT 1 FROM papers WHERE arxiv_id = ?", (arxiv_id,))
    is_new = cur.fetchone() is None
    conn.execute(
        """
        INSERT INTO papers (
            arxiv_id, title, authors, primary_category, all_categories,
            submitted_at, month, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            title            = excluded.title,
            authors          = excluded.authors,
            primary_category = excluded.primary_category,
            all_categories   = excluded.all_categories,
            submitted_at     = excluded.submitted_at,
            month            = excluded.month
        """,
        (
            arxiv_id,
            title,
            authors,
            primary_category,
            all_categories,
            submitted_at,
            month,
            now_iso(),
        ),
    )
    return is_new


def upsert_citation(
    conn: sqlite3.Connection,
    *,
    arxiv_id: str,
    citation_count: int | None,
    source: str,
    fetched_at: str | None = None,
) -> None:
    """Replace the current citation count and append a history row.

    A NULL `citation_count` means "not found" — it is stored, not dropped.
    """
    fetched_at = fetched_at or now_iso()
    conn.execute(
        """
        INSERT INTO citations (arxiv_id, citation_count, source, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            citation_count = excluded.citation_count,
            source         = excluded.source,
            fetched_at     = excluded.fetched_at
        """,
        (arxiv_id, citation_count, source, fetched_at),
    )
    conn.execute(
        """
        INSERT INTO citation_history (arxiv_id, citation_count, source, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(arxiv_id, fetched_at) DO UPDATE SET
            citation_count = excluded.citation_count,
            source         = excluded.source
        """,
        (arxiv_id, citation_count, source, fetched_at),
    )


def upsert_social(
    conn: sqlite3.Connection,
    *,
    arxiv_id: str,
    score: float | None,
    source: str,
    fetched_at: str | None = None,
    top_story_id: str | None = None,
) -> None:
    """Replace the current social score and append a history row.

    A NULL `score` means "not found" — it is stored, not dropped. Mirrors
    `upsert_citation` but writes the independent `social` / `social_history`
    tables, so refreshing one metric never disturbs the other.

    `top_story_id` is the HN item id of the most-upvoted story behind the score,
    used by the front-end to link straight to the busiest discussion. It lives
    only on `social` (the current snapshot); `social_history` tracks the score
    over time and doesn't need it.
    """
    fetched_at = fetched_at or now_iso()
    conn.execute(
        """
        INSERT INTO social (arxiv_id, score, source, fetched_at, top_story_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(arxiv_id) DO UPDATE SET
            score        = excluded.score,
            source       = excluded.source,
            fetched_at   = excluded.fetched_at,
            top_story_id = excluded.top_story_id
        """,
        (arxiv_id, score, source, fetched_at, top_story_id),
    )
    conn.execute(
        """
        INSERT INTO social_history (arxiv_id, score, source, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(arxiv_id, fetched_at) DO UPDATE SET
            score  = excluded.score,
            source = excluded.source
        """,
        (arxiv_id, score, source, fetched_at),
    )
