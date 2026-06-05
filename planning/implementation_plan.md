# Build Spec: cs.AI Top-Cited Papers Tool

## Goal

A command-line tool that, for arXiv papers in category `cs.AI`, fetches citation
counts and reports the top _k_ most-cited papers per calendar month. Citation
counts are stored locally and can be **refreshed on demand** so the user can
re-run updates over time (this is how the "recency / snapshot" problem is
handled — the user re-refreshes as citations accumulate).

Scope for v1: one rolling year (default last 12 months). Design must extend to
"2019-forward" by changing a date range only — no code changes.

## Tech choices

- **Language:** Python 3.11+.
- **Storage:** SQLite (single file `citations.db`). Chosen because the whole
  point is persistence + incremental updates; we are not re-fetching everything
  each run.
- **HTTP:** `requests` (or `httpx`). Add simple retry with backoff.
- **Deps:** `requests`, `feedparser` (parses arXiv Atom responses), `tqdm`
  (progress bars). Keep it minimal.
- **No framework.** A single package with subcommands via `argparse`.

## Data sources (exact details — do not guess these)

### 1. arXiv API — gets the paper list + submission dates

- Endpoint: `http://export.arxiv.org/api/query`
- Query for a category: `search_query=cat:cs.AI` with `sortBy=submittedDate&sortOrder=ascending`.
- Pagination via `start` and `max_results`. Use `max_results=200` per page.
- **Be polite:** sleep ~3 seconds between requests (arXiv requests this). Do not
  parallelize arXiv calls.
- Response is Atom XML → parse with `feedparser`. Each entry gives:
  `id` (contains arXiv id + version), `title`, `authors`, `published`
  (submission datetime), `arxiv_primary_category`, and all categories.
- Derive `arxiv_id` as the bare id without version suffix (strip `vN`).
- Derive `month` bucket as `YYYY-MM` from `published`.
- **Category decision (make it a flag):** `--match primary` (only papers whose
  _primary_ category is cs.AI) vs `--match any` (cs.AI appears anywhere in the
  category list). Default to `primary` — it's the cleaner "published in cs.AI"
  definition. The `cat:cs.AI` query returns cross-listed papers too, so filter
  client-side based on the flag.

### 2. Semantic Scholar — gets citation counts in bulk

- **Batch endpoint:** `POST https://api.semanticscholar.org/graph/v1/paper/batch`
- Body: `{"ids": ["ARXIV:2506.00056", "ARXIV:2506.00073", ...]}` — **up to 500
  ids per request.**
- Query param for fields: `?fields=citationCount,title,externalIds`
- ID format is `ARXIV:<arxiv_id>` (no version suffix).
- Response is a JSON array **positionally aligned** with the input ids. An entry
  may be `null` if the paper isn't found — handle this (store count as NULL /
  "not found", don't crash, don't drop the row).
- **Rate limits:** the unauthenticated pool is shared and throttled — expect to
  sleep ~1s between batches and to hit occasional 429s. Get a free API key
  (https://www.semanticscholar.org/product/api) and send it as header
  `x-api-key`; this gives a dedicated, higher limit. Implement 429 handling:
  exponential backoff, respect `Retry-After` if present.
- **Optional second source (OpenAlex)** for cross-checking / fallback when S2
  returns null: `GET https://api.openalex.org/works/arxiv:<arxiv_id>` returns
  `cited_by_count`. No key needed. Implement as a `--source openalex|s2` flag,
  default `s2`. Keep the source recorded per row (see schema) so counts from
  different providers are never silently mixed.

## Data model (SQLite)

```sql
CREATE TABLE papers (
    arxiv_id        TEXT PRIMARY KEY,   -- bare id, no version, e.g. 2506.00056
    title           TEXT NOT NULL,
    authors         TEXT,               -- semicolon-joined
    primary_category TEXT,
    all_categories  TEXT,               -- comma-joined
    submitted_at    TEXT NOT NULL,      -- ISO datetime
    month           TEXT NOT NULL,      -- 'YYYY-MM', indexed
    ingested_at     TEXT NOT NULL       -- when we first saw it
);
CREATE INDEX idx_papers_month ON papers(month);

CREATE TABLE citations (
    arxiv_id        TEXT PRIMARY KEY REFERENCES papers(arxiv_id),
    citation_count  INTEGER,            -- NULL = not found / not yet fetched
    source          TEXT NOT NULL,      -- 's2' | 'openalex'
    fetched_at      TEXT NOT NULL       -- ISO datetime of this count
);

-- Optional history table for trend tracking across refreshes.
-- Recommended since the user explicitly wants to track updates over time.
CREATE TABLE citation_history (
    arxiv_id        TEXT NOT NULL,
    citation_count  INTEGER,
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (arxiv_id, fetched_at)
);
```

Rationale: paper metadata (immutable) is separated from citation counts
(mutable). Refreshing only touches `citations` + appends to `citation_history`.
This is what makes incremental updates clean.

## Commands (argparse subcommands)

### `ingest`

Fetch the paper list and upsert metadata. Does **not** fetch citations.

```
tool ingest --from 2025-06 --to 2026-05 [--match primary|any]
```

- Walk arXiv API month by month (or as one date-ranged query, paginated).
- `INSERT ... ON CONFLICT(arxiv_id) DO UPDATE` so re-running is idempotent and
  picks up newly submitted papers for partial/recent months.
- Print a summary: papers seen, new, per-month counts.

### `refresh` ← the update functionality

Fetch / update citation counts for stored papers.

```
tool refresh [--month YYYY-MM] [--stale-days N] [--source s2|openalex] [--only-missing]
```

- Select target papers from `papers`:
  - all, or filtered by `--month`;
  - if `--stale-days N`, only papers whose `citations.fetched_at` is older than
    N days (or never fetched) — this is the key knob for cheap periodic updates;
  - if `--only-missing`, only papers with no row / NULL count.
- Chunk selected arxiv_ids into batches of 500, call S2 batch endpoint.
- For each result: upsert into `citations` (replace current count) **and**
  append a row to `citation_history`.
- Handle `null` results, 429s, and network errors with retry/backoff. Make the
  run resumable — commit per batch so an interrupted run keeps progress.
- Print summary: fetched, updated, not-found, errors.

### `report`

Rank and output. Read-only.

```
tool report [--month YYYY-MM | --all-months] --top 10 [--format table|csv|json] [--out FILE]
```

- Join `papers` + `citations`, group by `month`, order by `citation_count DESC`
  (NULLs last), take top _k_ per month.
- Columns: `month, rank, citation_count, title, arxiv_id, url, fetched_at`.
  `url` = `https://arxiv.org/abs/<arxiv_id>`.
- `--all-months` emits every month's top-k in one table/file.
- Always show `fetched_at` so the user knows how fresh the counts are.

## Edge cases / requirements the agent must handle

1. **Version suffixes:** strip `vN` from arXiv ids everywhere; S2/OpenAlex want
   the bare id.
2. **Null citation results:** store as NULL, never crash, surface count in
   report summary.
3. **Idempotency:** `ingest` and `refresh` must be safe to re-run; use upserts.
4. **Resumability:** commit per batch; an interrupted `refresh` resumes via
   `--stale-days`/`--only-missing`.
5. **Rate limiting:** central HTTP helper with backoff + `Retry-After`; arXiv
   gets a fixed 3s delay, S2 ~1s between batches (less with a key).
6. **Recent months are provisional:** `report` should annotate any month within
   the last ~3 months as `(provisional — low citation accrual)` in output, so
   cross-month comparison isn't misread. Within-month ranking is still valid.
7. **Source isolation:** never mix s2 and openalex counts in one ranking; record
   and display `source`.
8. **Config:** read S2 API key from env var `S2_API_KEY`. No secrets in code.

## Suggested file layout

```
csai_citations/
  __init__.py
  db.py          # schema init, connection, upsert helpers
  arxiv.py       # ingest: paginated category harvest -> papers
  citations.py   # refresh: S2 batch + OpenAlex fallback -> citations
  report.py      # ranking queries + output formatters
  http.py        # shared session, retry/backoff, rate limiting
  cli.py         # argparse subcommands: ingest / refresh / report
README.md        # usage, including the periodic-refresh workflow
```

## Typical workflow (document in README)

```
export S2_API_KEY=...                         # optional but recommended
tool ingest  --from 2025-06 --to 2026-05      # build paper list (run occasionally)
tool refresh --source s2                      # first full citation fetch
tool report  --all-months --top 10 --format csv --out top10.csv

# later, to update counts as citations accumulate:
tool ingest  --from 2026-04 --to 2026-06      # pick up new recent papers
tool refresh --stale-days 14                  # only re-fetch counts >2 weeks old
tool report  --month 2025-06 --top 10
```

## Acceptance criteria

- `ingest` over a 12-month range stores all cs.AI papers with correct month
  buckets and is idempotent on re-run.
- `refresh` populates citation counts via ≤500-id batches, handles nulls/429s,
  appends history, and is resumable.
- `report --all-months --top 10` produces correct per-month rankings with arXiv
  URLs and a freshness timestamp, in table/csv/json.
- Re-running `refresh --stale-days N` updates only stale rows and changes the
  report output accordingly.
