"""Citation refresh: fetch counts from Semantic Scholar / OpenAlex.

Selects target papers from the DB (all, by month, by staleness, or only
missing), fetches counts in batches, and upserts into `citations` while
appending to `citation_history`. Commits per batch so an interrupted run keeps
progress and can be resumed via `--stale-days` / `--only-missing`.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field

from . import db
from .http import HTTPError, make_session, request

S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_BATCH_SIZE = 500
S2_FIELDS = "citationCount,title,externalIds"
# Polite delay between S2 batches (lower limits apply without a key).
S2_DELAY_SECONDS = 1.0

# OpenAlex keys arXiv papers by their DataCite DOI (minted ~2022 onward, which
# covers this tool's recent-papers scope). A 404 means not found -> stored NULL.
OPENALEX_URL = "https://api.openalex.org/works/doi:10.48550/arxiv.{arxiv_id}"
OPENALEX_DELAY_SECONDS = 0.2

# Hacker News via Algolia's public search API (free, no key). For an arXiv id
# we sum the points of every HN story linking that id — a fast-moving social
# popularity signal, useful for recently-published papers before citations
# accrue. No matching story means zero attention (stored as 0, not NULL).
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_DELAY_SECONDS = 0.1

SOURCE_S2 = "s2"
SOURCE_OPENALEX = "openalex"
SOURCE_HN = "hn"

# Sources that produce a citation count vs. the social "attention" score; they
# write independent tables and so are selected/refreshed independently.
CITATION_SOURCES = (SOURCE_S2, SOURCE_OPENALEX)
SOCIAL_SOURCES = (SOURCE_HN,)


@dataclass
class RefreshSummary:
    selected: int = 0
    fetched: int = 0
    not_found: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def select_target_ids(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    stale_days: int | None = None,
    only_missing: bool = False,
    source: str = SOURCE_S2,
) -> list[str]:
    """Return arxiv_ids to refresh, applying the selection filters.

    Filters combine: month narrows the paper set; only_missing keeps papers
    with no/NULL value; stale_days keeps papers never fetched or fetched longer
    than N days ago. The staleness/missing checks are evaluated against the
    table the `source` writes to (`citations` for citation sources, `social`
    for social ones), so each metric is refreshed on its own schedule.
    """
    # Pick the metric table + value column this source populates.
    if source in SOCIAL_SOURCES:
        join_table, value_col = "social", "score"
    else:
        join_table, value_col = "citations", "citation_count"

    clauses = []
    params: list[object] = []
    sql = (
        "SELECT p.arxiv_id FROM papers p "
        f"LEFT JOIN {join_table} c ON c.arxiv_id = p.arxiv_id"
    )

    if month:
        clauses.append("p.month = ?")
        params.append(month)
    if only_missing:
        clauses.append(f"(c.arxiv_id IS NULL OR c.{value_col} IS NULL)")
    if stale_days is not None:
        # Never-fetched (NULL fetched_at) always qualifies; otherwise compare age.
        clauses.append(
            "(c.fetched_at IS NULL "
            "OR julianday('now') - julianday(c.fetched_at) > ?)"
        )
        params.append(stale_days)

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY p.arxiv_id"

    return [row[0] for row in conn.execute(sql, params).fetchall()]


def _s2_headers() -> dict[str, str]:
    key = os.environ.get("S2_API_KEY")
    return {"x-api-key": key} if key else {}


def _refresh_s2(
    conn: sqlite3.Connection,
    arxiv_ids: list[str],
    summary: RefreshSummary,
    progress: bool,
) -> None:
    session = make_session(_s2_headers())
    bar = None
    if progress:
        from tqdm import tqdm

        bar = tqdm(total=len(arxiv_ids), desc="refresh s2", unit="paper")

    try:
        for batch in _chunks(arxiv_ids, S2_BATCH_SIZE):
            body = {"ids": [f"ARXIV:{aid}" for aid in batch]}
            try:
                resp = request(
                    session,
                    "POST",
                    S2_BATCH_URL,
                    params={"fields": S2_FIELDS},
                    json=body,
                )
                results = resp.json()
            except (HTTPError, ValueError) as exc:
                summary.errors += len(batch)
                summary.error_messages.append(str(exc))
                if bar:
                    bar.update(len(batch))
                time.sleep(S2_DELAY_SECONDS)
                continue

            # Response is positionally aligned with the input ids.
            for arxiv_id, entry in zip(batch, results):
                if entry is None:
                    db.upsert_citation(
                        conn,
                        arxiv_id=arxiv_id,
                        citation_count=None,
                        source=SOURCE_S2,
                    )
                    summary.not_found += 1
                else:
                    db.upsert_citation(
                        conn,
                        arxiv_id=arxiv_id,
                        citation_count=entry.get("citationCount"),
                        source=SOURCE_S2,
                    )
                    summary.fetched += 1
            conn.commit()
            if bar:
                bar.update(len(batch))
            time.sleep(S2_DELAY_SECONDS)
    finally:
        if bar:
            bar.close()


def _refresh_openalex(
    conn: sqlite3.Connection,
    arxiv_ids: list[str],
    summary: RefreshSummary,
    progress: bool,
) -> None:
    session = make_session()
    email = os.environ.get("OPENALEX_EMAIL")
    params = {"mailto": email} if email else None

    bar = None
    if progress:
        from tqdm import tqdm

        bar = tqdm(total=len(arxiv_ids), desc="refresh openalex", unit="paper")

    try:
        for i, arxiv_id in enumerate(arxiv_ids, start=1):
            try:
                resp = request(
                    session,
                    "GET",
                    OPENALEX_URL.format(arxiv_id=arxiv_id),
                    params=params,
                )
                data = resp.json()
                count = data.get("cited_by_count")
                db.upsert_citation(
                    conn,
                    arxiv_id=arxiv_id,
                    citation_count=count,
                    source=SOURCE_OPENALEX,
                )
                summary.fetched += 1
            except HTTPError as exc:
                # OpenAlex returns 404 for unknown arxiv ids -> not found.
                if "404" in str(exc):
                    db.upsert_citation(
                        conn,
                        arxiv_id=arxiv_id,
                        citation_count=None,
                        source=SOURCE_OPENALEX,
                    )
                    summary.not_found += 1
                else:
                    summary.errors += 1
                    summary.error_messages.append(str(exc))
            except ValueError as exc:
                summary.errors += 1
                summary.error_messages.append(str(exc))

            if bar:
                bar.update(1)
            # Commit periodically so the run is resumable.
            if i % 50 == 0:
                conn.commit()
            time.sleep(OPENALEX_DELAY_SECONDS)
    finally:
        if bar:
            bar.close()
    conn.commit()


def _hn_points(results: dict, arxiv_id: str) -> int:
    """Sum HN story points across hits that actually link this arXiv id.

    Algolia ranks by relevance and matches title text too, so we keep only hits
    whose URL contains the id — the strong signal that the story is about this
    paper — and sum their points.
    """
    total = 0
    for hit in results.get("hits", []):
        url = hit.get("url") or ""
        if arxiv_id in url:
            total += hit.get("points") or 0
    return total


def _refresh_hackernews(
    conn: sqlite3.Connection,
    arxiv_ids: list[str],
    summary: RefreshSummary,
    progress: bool,
) -> None:
    session = make_session()

    bar = None
    if progress:
        from tqdm import tqdm

        bar = tqdm(total=len(arxiv_ids), desc="refresh hn", unit="paper")

    try:
        for i, arxiv_id in enumerate(arxiv_ids, start=1):
            try:
                resp = request(
                    session,
                    "GET",
                    HN_SEARCH_URL,
                    params={
                        "query": arxiv_id,
                        "restrictSearchableAttributes": "url,title",
                        "tags": "story",
                    },
                )
                points = _hn_points(resp.json(), arxiv_id)
                db.upsert_social(
                    conn,
                    arxiv_id=arxiv_id,
                    score=points,
                    source=SOURCE_HN,
                )
                # A real zero (no HN stories) is the paper's actual attention
                # level, not an error — stored as 0. Report it as "not_found"
                # only for the summary's sake (no attention found).
                if points > 0:
                    summary.fetched += 1
                else:
                    summary.not_found += 1
            except (HTTPError, ValueError) as exc:
                summary.errors += 1
                summary.error_messages.append(str(exc))

            if bar:
                bar.update(1)
            # Commit periodically so the run is resumable.
            if i % 50 == 0:
                conn.commit()
            time.sleep(HN_DELAY_SECONDS)
    finally:
        if bar:
            bar.close()
    conn.commit()


def refresh(
    conn: sqlite3.Connection,
    *,
    source: str = SOURCE_S2,
    month: str | None = None,
    stale_days: int | None = None,
    only_missing: bool = False,
    progress: bool = True,
) -> RefreshSummary:
    """Fetch/update citation counts (or social scores) for selected papers."""
    arxiv_ids = select_target_ids(
        conn,
        month=month,
        stale_days=stale_days,
        only_missing=only_missing,
        source=source,
    )
    summary = RefreshSummary(selected=len(arxiv_ids))
    if not arxiv_ids:
        return summary

    if source == SOURCE_S2:
        _refresh_s2(conn, arxiv_ids, summary, progress)
    elif source == SOURCE_OPENALEX:
        _refresh_openalex(conn, arxiv_ids, summary, progress)
    elif source == SOURCE_HN:
        _refresh_hackernews(conn, arxiv_ids, summary, progress)
    else:
        raise ValueError(f"unknown source: {source!r}")

    return summary
