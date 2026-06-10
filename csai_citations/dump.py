"""Import paper metadata from the Kaggle arXiv metadata snapshot.

The Kaggle dataset `Cornell-University/arxiv` ships a single JSON-Lines file
(`arxiv-metadata-oai-snapshot.json`, ~4 GB, the whole of arXiv). This module
streams that file, filters to cs.AI within an optional date range, and upserts
into the same `papers` table the arXiv harvester uses — so it bypasses the
rate-limited arXiv query API entirely. Citations are still fetched via
`refresh`; this only fills in metadata.

Each record looks like:
    {"id": "2501.12599", "authors": "...", "title": "...",
     "categories": "cs.AI cs.LG",            # space-separated, primary first
     "versions": [{"version": "v1", "created": "Mon, 20 Jan 2025 ..."}],
     "authors_parsed": [["Last", "First", ""], ...], ...}
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from . import db

DEFAULT_DUMP_FILENAME = "arxiv-metadata-oai-snapshot.json"

# Tier-1 categories most relevant to LLM research. cs.CL (Computation and
# Language) is where most LLM papers live; cs.LG is core machine learning.
DEFAULT_CATEGORIES = ["cs.AI", "cs.CL", "cs.LG"]

# Matches a trailing version suffix like "v2" on an arXiv id.
_VERSION_RE = re.compile(r"v\d+$")


def bare_arxiv_id(raw_id: str) -> str:
    """Reduce an arXiv id/URL to its bare id without version suffix.

    Handles both the new scheme ('2506.00056v2' -> '2506.00056') and the old
    slash scheme ('cs/0501001v1' -> 'cs/0501001'). The dump's `id` field is
    already bare, but this keeps ids canonical regardless of source.
    """
    _, _, tail = raw_id.partition("/abs/")
    tail = tail or raw_id
    return _VERSION_RE.sub("", tail)


@dataclass
class ImportSummary:
    scanned: int
    matched: int
    new: int
    updated: int
    per_month: dict[str, int]


def _submission_datetime(record: dict):
    """Original submission datetime, from the v1 version's `created` field.

    Falls back to the first version, then to `update_date`. Returns None if no
    parseable date is found.
    """
    versions = record.get("versions") or []
    created = None
    for v in versions:
        if v.get("version") == "v1":
            created = v.get("created")
            break
    if created is None and versions:
        created = versions[0].get("created")
    if created:
        try:
            return parsedate_to_datetime(created)
        except (TypeError, ValueError):
            pass
    # Fallback: update_date is 'YYYY-MM-DD'.
    upd = record.get("update_date")
    if upd:
        try:
            return parsedate_to_datetime(upd) if "," in upd else _date_only(upd)
        except (TypeError, ValueError):
            return None
    return None


def _date_only(value: str):
    from datetime import datetime

    return datetime.strptime(value, "%Y-%m-%d")


def _authors(record: dict) -> str:
    """Semicolon-joined author names, matching the arXiv harvester's format."""
    parsed = record.get("authors_parsed")
    if parsed:
        names = []
        for entry in parsed:
            last = entry[0] if len(entry) > 0 else ""
            first = entry[1] if len(entry) > 1 else ""
            name = " ".join(p for p in (first, last) if p).strip()
            if name:
                names.append(name)
        if names:
            return "; ".join(names)
    # Fallback: the raw `authors` string (comma/and separated).
    return (record.get("authors") or "").strip()


def _matches(categories: list[str], targets: set[str], match: str) -> bool:
    """Whether a paper's categories qualify under the chosen match mode.

    `primary`: the paper's primary (first) category is one of the targets.
    `any`: any target category appears anywhere in the paper's category list.
    """
    if not categories:
        return False
    if match == "primary":
        return categories[0] in targets
    return any(c in targets for c in categories)


def import_dump(
    conn: sqlite3.Connection,
    *,
    path: str,
    categories: list[str] | None = None,
    match: str = "primary",
    date_from: str | None = None,
    date_to: str | None = None,
    progress: bool = True,
) -> ImportSummary:
    """Stream the snapshot file and upsert papers in the target categories.

    `categories` defaults to DEFAULT_CATEGORIES (the tier-1 LLM-research set).
    `date_from`/`date_to` are inclusive 'YYYY-MM' bounds on the submission
    month. Commits periodically so a huge file does not hold one transaction.
    """
    targets = set(categories or DEFAULT_CATEGORIES)
    scanned = matched = new = updated = 0
    per_month: Counter[str] = Counter()

    bar = None
    if progress:
        from tqdm import tqdm

        bar = tqdm(desc="import", unit="rec")

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            scanned += 1
            if bar is not None:
                bar.update(1)
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            categories = (record.get("categories") or "").split()
            if not _matches(categories, targets, match):
                continue

            dt = _submission_datetime(record)
            if dt is None:
                continue
            month = dt.strftime("%Y-%m")
            if date_from and month < date_from:
                continue
            if date_to and month > date_to:
                continue

            arxiv_id = bare_arxiv_id(record.get("id", ""))
            if not arxiv_id:
                continue
            title = " ".join((record.get("title") or "").split())

            is_new = db.upsert_paper(
                conn,
                arxiv_id=arxiv_id,
                title=title,
                authors=_authors(record),
                primary_category=categories[0],
                all_categories=",".join(categories),
                submitted_at=dt.isoformat(),
                month=month,
            )
            matched += 1
            per_month[month] += 1
            if is_new:
                new += 1
            else:
                updated += 1

            if matched % 2000 == 0:
                conn.commit()

    conn.commit()
    if bar is not None:
        bar.close()
    return ImportSummary(
        scanned=scanned,
        matched=matched,
        new=new,
        updated=updated,
        per_month=dict(per_month),
    )
