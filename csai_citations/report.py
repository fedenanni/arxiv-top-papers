"""Ranking queries and output formatters (read-only).

Joins papers + citations, ranks the top-k most-cited papers per month, and
renders the result as a table, CSV, or JSON. Recent months are flagged as
provisional because citations have not yet accrued.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass, asdict
from datetime import date, timedelta

# Matches a modern arXiv id (e.g. 2506.00056, optional version) anywhere in a
# string, so a pasted /abs/ or /pdf/ URL or a bare id both resolve to the id.
_ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")

# A month is "provisional" if it is within this many months of today: too
# recent for citations to have meaningfully accrued.
PROVISIONAL_MONTHS = 3

# Ranking metrics: the citation count (citations table) or the social
# attention score (social table). Each maps to its own table + value column.
METRIC_CITATIONS = "citations"
METRIC_SOCIAL = "social"

# Rolling "recent" windows, anchored to today, expressed in days. Used by the
# web "Recent" granularity; papers are filtered by submitted_at >= today - N.
ROLLING_WINDOWS = {"w": 7, "1m": 30, "2m": 60, "3m": 90}

COLUMNS = ["month", "rank", "citation_count", "score", "title", "arxiv_id", "url", "fetched_at"]


def _metric_value_sql(metric: str) -> str:
    """SQL expression for the ranked value, given the selected metric."""
    return "s.score" if metric == METRIC_SOCIAL else "c.citation_count"


@dataclass
class Row:
    month: str
    rank: int
    citation_count: int | None
    score: float | None
    title: str
    arxiv_id: str
    url: str
    fetched_at: str | None
    provisional: bool


def _month_index(ym: str) -> int:
    """Convert 'YYYY-MM' to an absolute month count for arithmetic."""
    y, m = (int(x) for x in ym.split("-"))
    return y * 12 + (m - 1)


def _provisional_cutoff(today: date) -> int:
    return today.year * 12 + (today.month - 1) - PROVISIONAL_MONTHS


def _data_frontier(conn: sqlite3.Connection) -> date | None:
    """Submission date of the newest paper in the DB (the 'data frontier').

    Rolling windows anchor to this rather than wall-clock today: the Kaggle
    dump lags a few days, so 'last week' counted from today would fall into an
    empty gap. Anchoring to the freshest paper we actually have keeps the
    window populated and makes 'recent' mean 'newest papers on hand'.
    """
    row = conn.execute("SELECT MAX(submitted_at) FROM papers").fetchone()
    val = row[0] if row else None
    return date.fromisoformat(val[:10]) if val else None


def rank_rows(
    conn: sqlite3.Connection,
    *,
    month: str | None,
    top: int,
    metric: str = METRIC_CITATIONS,
    today: date | None = None,
) -> list[Row]:
    """Compute top-k rows per month, ranked by `metric`.

    If `month` is given, only that month is ranked; otherwise every month is
    ranked and concatenated. NULL values sort last. `metric` selects the
    ranking signal: citation count (default) or the social attention score.
    Source isolation is the caller's concern — values are whatever is currently
    stored in `citations` / `social`.
    """
    today = today or date.today()
    cutoff = _provisional_cutoff(today)
    value_sql = _metric_value_sql(metric)
    # The displayed freshness timestamp tracks the metric actually ranked on.
    fetched_sql = "s.fetched_at" if metric == METRIC_SOCIAL else "c.fetched_at"

    params: list[object] = []
    where = ""
    if month:
        where = "WHERE p.month = ?"
        params.append(month)

    # ROW_NUMBER per month, NULL values last; cap to top-k in the outer query.
    sql = f"""
        WITH ranked AS (
            SELECT
                p.month AS month,
                p.title AS title,
                p.arxiv_id AS arxiv_id,
                c.citation_count AS citation_count,
                s.score AS score,
                {fetched_sql} AS fetched_at,
                ROW_NUMBER() OVER (
                    PARTITION BY p.month
                    ORDER BY ({value_sql} IS NULL), {value_sql} DESC,
                             p.arxiv_id ASC
                ) AS rnk
            FROM papers p
            LEFT JOIN citations c ON c.arxiv_id = p.arxiv_id
            LEFT JOIN social s ON s.arxiv_id = p.arxiv_id
            {where}
        )
        SELECT month, rnk, citation_count, score, title, arxiv_id, fetched_at
        FROM ranked
        WHERE rnk <= ?
        ORDER BY month ASC, rnk ASC
    """
    params.append(top)

    rows: list[Row] = []
    for r in conn.execute(sql, params).fetchall():
        rows.append(
            Row(
                month=r["month"],
                rank=r["rnk"],
                citation_count=r["citation_count"],
                score=r["score"],
                title=r["title"],
                arxiv_id=r["arxiv_id"],
                url=f"https://arxiv.org/abs/{r['arxiv_id']}",
                fetched_at=r["fetched_at"],
                provisional=_month_index(r["month"]) > cutoff,
            )
        )
    return rows


def available_periods(conn: sqlite3.Connection) -> dict:
    """Distinct months and years present in `papers`, newest first.

    Years are derived from the 'YYYY-MM' month buckets so a year shows up as
    soon as any of its months has been ingested. `latest` is the data frontier
    (newest submission date, YYYY-MM-DD) that rolling windows anchor to.
    """
    months = [
        r["month"]
        for r in conn.execute(
            "SELECT DISTINCT month FROM papers ORDER BY month DESC"
        ).fetchall()
    ]
    years = sorted({m[:4] for m in months}, reverse=True)
    frontier = _data_frontier(conn)
    return {
        "months": months,
        "years": years,
        "latest": frontier.isoformat() if frontier else None,
    }


def available_categories(conn: sqlite3.Connection) -> list[dict]:
    """Distinct primary categories present in `papers`, most-common first."""
    return [
        {"category": r["category"], "count": r["n"]}
        for r in conn.execute(
            "SELECT primary_category AS category, COUNT(*) AS n "
            "FROM papers GROUP BY primary_category ORDER BY n DESC"
        ).fetchall()
    ]


def _is_year(period: str) -> bool:
    return len(period) == 4 and period.isdigit()


def _is_month(period: str) -> bool:
    parts = period.split("-")
    return (
        len(parts) == 2
        and len(parts[0]) == 4
        and parts[0].isdigit()
        and len(parts[1]) == 2
        and parts[1].isdigit()
    )


def rank_period(
    conn: sqlite3.Connection,
    *,
    period: str,
    top: int,
    categories: list[str] | None = None,
    metric: str = METRIC_CITATIONS,
    today: date | None = None,
) -> list[dict]:
    """Rank papers for a single period, as one leaderboard, by `metric`.

    `period` is one of: 'all' (every year combined — the overall ranking),
    'YYYY' (a whole year), 'YYYY-MM' (a single month), or a rolling window
    token ('w', '1m', '2m', '3m') that selects papers submitted within the
    last N days relative to today. `categories`, if given, restricts to those
    primary categories; None or empty means no category filter. `metric`
    selects the ranking signal: citation count (default) or social score.

    Returns a richer column set than `rank_rows` — including authors, category,
    and *both* metric values — as plain dicts for the web view. NULL values for
    the ranked metric sort last; the `provisional` flag reuses the same
    definition as the CLI report (relevant only when ranking by citations).

    Raises ValueError on a malformed period.
    """
    today = today or date.today()
    cutoff = _provisional_cutoff(today)
    value_sql = _metric_value_sql(metric)
    fetched_sql = "s.fetched_at" if metric == METRIC_SOCIAL else "c.fetched_at"

    clauses: list[str] = []
    params: list[object] = []
    if period == "all":
        pass
    elif period in ROLLING_WINDOWS:
        # Anchor to the data frontier, not today, so dump lag can't empty the
        # window (see _data_frontier).
        anchor = _data_frontier(conn) or today
        start = (anchor - timedelta(days=ROLLING_WINDOWS[period])).isoformat()
        clauses.append("p.submitted_at >= ?")
        params.append(start)
    elif _is_year(period):
        clauses.append("p.month LIKE ?")
        params.append(f"{period}-%")
    elif _is_month(period):
        clauses.append("p.month = ?")
        params.append(period)
    else:
        raise ValueError(
            f"invalid period: {period!r} "
            "(expected 'all', YYYY, YYYY-MM, or w/1m/2m/3m)"
        )

    if categories:
        placeholders = ",".join("?" * len(categories))
        clauses.append(f"p.primary_category IN ({placeholders})")
        params.extend(categories)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    # Single ORDER BY ranks the whole filtered set as one leaderboard.
    sql = f"""
        WITH ranked AS (
            SELECT
                p.month AS month,
                p.title AS title,
                p.authors AS authors,
                p.primary_category AS primary_category,
                p.arxiv_id AS arxiv_id,
                c.citation_count AS citation_count,
                s.score AS score,
                s.top_story_id AS top_story_id,
                {fetched_sql} AS fetched_at,
                ROW_NUMBER() OVER (
                    ORDER BY ({value_sql} IS NULL), {value_sql} DESC,
                             p.arxiv_id ASC
                ) AS rnk
            FROM papers p
            LEFT JOIN citations c ON c.arxiv_id = p.arxiv_id
            LEFT JOIN social s ON s.arxiv_id = p.arxiv_id
            {where}
        )
        SELECT month, rnk, citation_count, score, title, authors,
               primary_category, arxiv_id, top_story_id, fetched_at
        FROM ranked
        WHERE rnk <= ?
        ORDER BY rnk ASC
    """
    params.append(top)

    out: list[dict] = []
    for r in conn.execute(sql, params).fetchall():
        out.append(
            {
                "month": r["month"],
                "rank": r["rnk"],
                "citation_count": r["citation_count"],
                "score": r["score"],
                "title": r["title"],
                "authors": r["authors"],
                "primary_category": r["primary_category"],
                "arxiv_id": r["arxiv_id"],
                "url": f"https://arxiv.org/abs/{r['arxiv_id']}",
                "top_story_id": r["top_story_id"],
                "fetched_at": r["fetched_at"],
                "provisional": _month_index(r["month"]) > cutoff,
            }
        )
    return out


def search_papers(
    conn: sqlite3.Connection,
    *,
    query: str,
    top: int,
    metric: str = METRIC_CITATIONS,
    today: date | None = None,
) -> list[dict]:
    """Find papers by title substring or arXiv id, across all periods.

    If `query` contains an arXiv id (bare, or inside a pasted /abs//pdf/ URL),
    the id takes precedence and is matched exactly. Otherwise the query is a
    case-insensitive substring match against the title. Results use the same
    rich dict shape as `rank_period` (so the web view renders them identically)
    and are ordered by `metric` (NULLs last). Returns [] for a blank query.
    """
    query = query.strip()
    if not query:
        return []
    today = today or date.today()
    cutoff = _provisional_cutoff(today)
    value_sql = _metric_value_sql(metric)
    fetched_sql = "s.fetched_at" if metric == METRIC_SOCIAL else "c.fetched_at"

    id_match = _ARXIV_ID_RE.search(query)
    if id_match:
        where = "WHERE p.arxiv_id = ?"
        params: list[object] = [id_match.group(1)]
    else:
        where = "WHERE p.title LIKE ? ESCAPE '\\'"
        # Escape LIKE wildcards so a literal % or _ in the query matches itself.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = [f"%{escaped}%"]

    sql = f"""
        WITH ranked AS (
            SELECT
                p.month AS month,
                p.title AS title,
                p.authors AS authors,
                p.primary_category AS primary_category,
                p.arxiv_id AS arxiv_id,
                c.citation_count AS citation_count,
                s.score AS score,
                s.top_story_id AS top_story_id,
                {fetched_sql} AS fetched_at,
                ROW_NUMBER() OVER (
                    ORDER BY ({value_sql} IS NULL), {value_sql} DESC,
                             p.arxiv_id ASC
                ) AS rnk
            FROM papers p
            LEFT JOIN citations c ON c.arxiv_id = p.arxiv_id
            LEFT JOIN social s ON s.arxiv_id = p.arxiv_id
            {where}
        )
        SELECT month, rnk, citation_count, score, title, authors,
               primary_category, arxiv_id, top_story_id, fetched_at
        FROM ranked
        WHERE rnk <= ?
        ORDER BY rnk ASC
    """
    params.append(top)

    out: list[dict] = []
    for r in conn.execute(sql, params).fetchall():
        out.append(
            {
                "month": r["month"],
                "rank": r["rnk"],
                "citation_count": r["citation_count"],
                "score": r["score"],
                "title": r["title"],
                "authors": r["authors"],
                "primary_category": r["primary_category"],
                "arxiv_id": r["arxiv_id"],
                "url": f"https://arxiv.org/abs/{r['arxiv_id']}",
                "top_story_id": r["top_story_id"],
                "fetched_at": r["fetched_at"],
                "provisional": _month_index(r["month"]) > cutoff,
            }
        )
    return out


def _month_label(month: str, provisional: bool) -> str:
    if provisional:
        return f"{month} (provisional — low citation accrual)"
    return month


def _metric_cell(row: Row, metric: str) -> str:
    """The ranked value as a display string ('—' when absent)."""
    if metric == METRIC_SOCIAL:
        return "—" if row.score is None else f"{row.score:g}"
    return "—" if row.citation_count is None else str(row.citation_count)


def format_table(rows: list[Row], metric: str = METRIC_CITATIONS) -> str:
    """Human-readable grouped table."""
    if not rows:
        return "(no results)"
    head = "score" if metric == METRIC_SOCIAL else "cites"
    out: list[str] = []
    current_month: str | None = None
    for row in rows:
        if row.month != current_month:
            current_month = row.month
            out.append("")
            out.append(f"## {_month_label(row.month, row.provisional)}")
            out.append(
                f"{'rank':>4}  {head:>6}  {'arxiv_id':<12}  title"
            )
        value = _metric_cell(row, metric)
        title = row.title if len(row.title) <= 70 else row.title[:67] + "..."
        out.append(f"{row.rank:>4}  {value:>6}  {row.arxiv_id:<12}  {title}")
    return "\n".join(out).lstrip("\n")


def format_csv(rows: list[Row]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([*COLUMNS, "provisional"])
    for row in rows:
        writer.writerow(
            [
                row.month,
                row.rank,
                "" if row.citation_count is None else row.citation_count,
                "" if row.score is None else row.score,
                row.title,
                row.arxiv_id,
                row.url,
                row.fetched_at or "",
                int(row.provisional),
            ]
        )
    return buf.getvalue()


def format_json(rows: list[Row]) -> str:
    return json.dumps([asdict(row) for row in rows], indent=2, ensure_ascii=False)


def render(rows: list[Row], fmt: str, metric: str = METRIC_CITATIONS) -> str:
    if fmt == "table":
        return format_table(rows, metric)
    if fmt == "csv":
        return format_csv(rows)
    if fmt == "json":
        return format_json(rows)
    raise ValueError(f"unknown format: {fmt!r}")
