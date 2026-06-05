#!/usr/bin/env python3
"""Build the static, browser-queryable database for the GitHub Pages site.

Takes the full local `citations.db` and produces, under `site/db/`:

  * `arxiv.sqlite3.NNN`  — the public DB, split into fixed-size chunks so it
                           stays under GitHub's 100 MB per-file limit (and keeps
                           working as the data grows).
  * `config.json`        — the chunk manifest read by sql.js-httpvfs.
  * `meta.json`          — periods, the data frontier, and category counts,
                           precomputed so the front-end never has to full-scan
                           `papers` at startup.

The public DB differs from the source in three ways, all aimed at making
queries cheap when SQLite runs in the browser over HTTP range requests:

  * the `*_history` tables are dropped (the front-end never reads them);
  * an FTS5 index over titles backs substring/keyword search;
  * indexes on the ranked columns (citation_count, score, submitted_at) let the
    common queries read a handful of rows instead of scanning the whole table.

Run it from the repo root:  python build_public_db.py [--src citations.db]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

# Page size of the public DB and the byte-range granularity sql.js-httpvfs
# fetches. Keeping them equal means one logical SQLite page read == one HTTP
# range request. 8 KiB halves the request count vs. the 4 KiB default at a
# small cost in bytes-per-request.
PAGE_SIZE = 8192
REQUEST_CHUNK_SIZE = PAGE_SIZE

# Physical size of each chunk file. Must be a multiple of REQUEST_CHUNK_SIZE and
# stay well under GitHub's 100 MB hard per-file limit. 24 MiB / 8 KiB = 3072.
SERVER_CHUNK_SIZE = 24 * 1024 * 1024

SUFFIX_LENGTH = 3


def _build_public_db(src: str, work: str) -> None:
    """Copy `src` to `work`, then prune, index, and compact it in place."""
    shutil.copyfile(src, work)
    conn = sqlite3.connect(work)
    try:
        # The front-end only reads papers / citations / social.
        conn.executescript(
            """
            DROP TABLE IF EXISTS citation_history;
            DROP TABLE IF EXISTS social_history;
            DROP INDEX IF EXISTS idx_papers_month;
            """
        )

        # FTS5 over titles, external-content (content='papers') so titles are
        # not duplicated — the index stores only postings and maps back to a
        # papers row via rowid. Prefix queries ("atten*") still work at query
        # time; we skip a prefix index to keep the file small. MATCH returns
        # rowid, which the search query joins back to papers.
        conn.executescript(
            """
            DROP TABLE IF EXISTS papers_fts;
            CREATE VIRTUAL TABLE papers_fts USING fts5(
                title, content='papers', content_rowid='rowid'
            );
            INSERT INTO papers_fts (rowid, title)
                SELECT rowid, title FROM papers;
            """
        )

        # Denormalize the filter columns (primary_category, year, month) from
        # papers onto the metric tables. This is the key to staying fast in the
        # browser: every ranking filter then lives on the same row as the metric
        # value, so a composite index can seek straight to the top-k. Without
        # it, a "2024 by citations" query scans the citation index from the top
        # (checking each paper's month) and, because recent papers have few
        # citations, ends up reading almost the entire DB over HTTP — it hangs.
        conn.executescript(
            """
            ALTER TABLE citations ADD COLUMN primary_category TEXT;
            ALTER TABLE citations ADD COLUMN year TEXT;
            ALTER TABLE citations ADD COLUMN month TEXT;
            UPDATE citations SET
                primary_category = p.primary_category,
                year = substr(p.month, 1, 4),
                month = p.month
            FROM papers p WHERE p.arxiv_id = citations.arxiv_id;

            ALTER TABLE social ADD COLUMN primary_category TEXT;
            ALTER TABLE social ADD COLUMN year TEXT;
            ALTER TABLE social ADD COLUMN month TEXT;
            UPDATE social SET
                primary_category = p.primary_category,
                year = substr(p.month, 1, 4),
                month = p.month
            FROM papers p WHERE p.arxiv_id = social.arxiv_id;
            """
        )

        # Access paths for the ranking queries: read the top-k from the metric
        # in sorted order, scoped to all-time / a category / a year / a month,
        # or pull a recent slice by submission date.
        conn.executescript(
            """
            CREATE INDEX idx_citations_count
                ON citations(citation_count DESC, arxiv_id);
            CREATE INDEX idx_citations_cat
                ON citations(primary_category, citation_count DESC, arxiv_id);
            CREATE INDEX idx_citations_year
                ON citations(year, citation_count DESC, arxiv_id);
            CREATE INDEX idx_citations_month
                ON citations(month, citation_count DESC, arxiv_id);
            CREATE INDEX idx_social_score
                ON social(score DESC, arxiv_id);
            CREATE INDEX idx_social_cat
                ON social(primary_category, score DESC, arxiv_id);
            CREATE INDEX idx_social_year
                ON social(year, score DESC, arxiv_id);
            CREATE INDEX idx_social_month
                ON social(month, score DESC, arxiv_id);
            CREATE INDEX idx_papers_submitted
                ON papers(submitted_at DESC, arxiv_id);
            """
        )

        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.commit()
        # VACUUM rewrites the file at the new page size, compacted.
        conn.execute("VACUUM")
        # VACUUM may renumber the implicit rowids of `papers` (it has a TEXT,
        # not INTEGER, primary key). Rebuild the external-content FTS index
        # afterwards so its rowid → papers mapping is guaranteed to match.
        conn.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
    finally:
        conn.close()


def _write_meta(db_path: str, out_dir: Path) -> None:
    """Precompute the small aggregates the UI needs into meta.json."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        months = [
            r["month"]
            for r in conn.execute(
                "SELECT DISTINCT month FROM papers ORDER BY month DESC"
            )
        ]
        years = sorted({m[:4] for m in months}, reverse=True)
        latest = conn.execute("SELECT MAX(submitted_at) FROM papers").fetchone()[0]
        categories = [
            {"category": r["category"], "count": r["n"]}
            for r in conn.execute(
                "SELECT primary_category AS category, COUNT(*) AS n "
                "FROM papers GROUP BY primary_category ORDER BY n DESC"
            )
        ]
    finally:
        conn.close()
    meta = {
        "months": months,
        "years": years,
        "latest": latest[:10] if latest else None,
        "categories": categories,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _split(db_path: str, out_dir: Path) -> dict:
    """Split the DB file into chunk files and return the manifest dict.

    Chunk filenames embed a content hash of the DB (e.g.
    `arxiv.<hash>.sqlite3.000`). Because the chunks' *contents* change on every
    rebuild but their offsets/filenames previously did not, a browser or CDN
    could pair freshly-fetched chunks with cached ones from an older build —
    which SQLite reports as "database disk image is malformed". A per-build
    hash in the name makes every build's files cache-distinct, so mixing is
    impossible.
    """
    h = hashlib.sha1()
    with open(db_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    prefix = f"arxiv.{h.hexdigest()[:10]}.sqlite3."

    # Remove chunks from any previous build (any hash) so they don't pile up.
    for old in out_dir.glob("arxiv.*.sqlite3.*"):
        old.unlink()

    total = os.path.getsize(db_path)
    with open(db_path, "rb") as f:
        index = 0
        while True:
            chunk = f.read(SERVER_CHUNK_SIZE)
            if not chunk:
                break
            (out_dir / f"{prefix}{index:0{SUFFIX_LENGTH}d}").write_bytes(chunk)
            index += 1

    return {
        "serverMode": "chunked",
        "requestChunkSize": REQUEST_CHUNK_SIZE,
        "databaseLengthBytes": total,
        "serverChunkSize": SERVER_CHUNK_SIZE,
        "urlPrefix": prefix,
        "suffixLength": SUFFIX_LENGTH,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default="citations.db", help="source DB (default: citations.db)")
    ap.add_argument("--out", default="site/db", help="output dir (default: site/db)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "public.sqlite3")
        _build_public_db(args.src, work)
        _write_meta(work, out_dir)
        manifest = _split(work, out_dir)

    (out_dir / "config.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    mb = manifest["databaseLengthBytes"] / 1e6
    n_chunks = -(-manifest["databaseLengthBytes"] // SERVER_CHUNK_SIZE)
    print(f"built {out_dir}/  —  {mb:.1f} MB across {n_chunks} chunk(s)")


if __name__ == "__main__":
    main()
