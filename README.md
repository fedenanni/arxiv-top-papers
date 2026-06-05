# arXiv Top-Cited Papers Tool

A command-line tool (plus a small web UI) that fetches citation counts for
arXiv papers and reports the top _k_ most-cited papers per month, per year, or
overall. Covers configurable categories — by default the tier-1 LLM-research
set **`cs.AI`, `cs.CL`, `cs.LG`**. Citation counts are stored locally in SQLite
and can be **refreshed on demand**, so you re-run updates as citations
accumulate.

## License & data sources

The **code** is licensed under the [MIT License](LICENSE). The **data** (the
SQLite database and its exports — the `citations.db` release asset, the database
served by the GitHub Pages site, and CSV/JSON outputs) is licensed under
[CC BY 4.0](LICENSE-DATA) and is a compilation derived from:

| Data | Source | Upstream license |
|------|--------|------------------|
| Paper metadata (titles, authors, categories, dates) | arXiv, via the [Kaggle arXiv snapshot](https://www.kaggle.com/datasets/Cornell-University/arxiv) | CC0 1.0 (public domain) |
| Citation counts | [Semantic Scholar](https://www.semanticscholar.org/product/api) | ODC-BY 1.0 (attribution required) |
| Social attention scores | [Hacker News](https://hn.algolia.com/api) (Algolia API) | public/factual; attribute HN |

If you redistribute the data, keep attribution to these sources (see
[LICENSE-DATA](LICENSE-DATA) for details). Citation counts and attention scores
are point-in-time snapshots and may be incomplete or stale.

## Install

```sh
uv sync                      # install deps into the project venv (Python 3.13+)
```

This exposes the `csai-citations` command (via `uv run csai-citations ...`).
Data is stored in a SQLite file `citations.db` in the current directory; pass
`--db PATH` to any command to use a different location.

## Concepts

- **Paper metadata is immutable** and lives in the `papers` table.
- **Citation counts are mutable** and live in `citations`; every observed count
  is also appended to `citation_history` for trend tracking.
- The "recency" problem is handled by re-running `refresh`: counts are a
  snapshot, and recent months are flagged as *provisional* in reports.

## Commands

### `import-dump` — build the paper list from the Kaggle arXiv snapshot

```sh
uv run csai-citations import-dump --file arxiv-metadata-oai-snapshot.json \
    [--category cs.AI cs.CL cs.LG] [--from YYYY-MM] [--to YYYY-MM] [--match primary|any]
```

Loads paper metadata from the canonical Kaggle dataset
[`Cornell-University/arxiv`](https://www.kaggle.com/datasets/Cornell-University/arxiv)
— the whole of arXiv as one JSON-Lines file. This is the only metadata source:
it has no rate limits and imports a full year in minutes (the arXiv query API
throttles sustained harvesting too aggressively to be usable for backfills).

- `--category` lists the arXiv categories to import (default the tier-1
  LLM-research set `cs.AI cs.CL cs.LG`). Re-run with different categories to add
  more — it upserts into the same table, so categories accumulate.
- `--from` / `--to` are inclusive `YYYY-MM` bounds on the submission month;
  omit both to import **all available years**.
- `--match primary` (default): only papers whose *primary* category is one of
  the targets. `--match any`: a target appears anywhere in the category list.
- Idempotent — re-running upserts; re-import after a fresh download to pick up
  newly published papers. Citations are fetched separately via `refresh`.

One-time setup, then download and import:

```sh
# 1. Kaggle API token: kaggle.com/settings -> Create New Token -> kaggle.json
mkdir -p ~/.kaggle && mv ~/Downloads/kaggle.json ~/.kaggle/ && chmod 600 ~/.kaggle/kaggle.json
uv add kaggle

# 2. Download (~1.6 GB zip -> ~4 GB JSON Lines) and import
uv run kaggle datasets download -d Cornell-University/arxiv -p /tmp/arxiv_dump --force
unzip -o /tmp/arxiv_dump/arxiv.zip -d /tmp/arxiv_dump
uv run csai-citations import-dump --file /tmp/arxiv_dump/arxiv-metadata-oai-snapshot.json \
    --from 2025-01 --to 2025-12
```

Submission month is taken from each record's v1 version date; the dump is
refreshed roughly weekly, so very recent papers may lag by a few days — re-run
the download and `import-dump` to pick them up.

### `refresh` — fetch / update citation counts (and social scores)

```sh
uv run csai-citations refresh [--month YYYY-MM] [--stale-days N] \
    [--source s2|openalex|hn] [--only-missing]
```

- Selects target papers (all / by month / stale / missing), batches up to 500
  ids per Semantic Scholar request, and upserts counts.
- `--stale-days N`: only re-fetch counts older than N days (cheap periodic
  updates). `--only-missing`: only papers with no count yet.
- `--source openalex`: use OpenAlex instead of Semantic Scholar. OpenAlex
  resolves one paper per request (no batching) and only covers papers carrying
  the arXiv DataCite DOI (`10.48550/arxiv.<id>`, minted ~2022 onward); papers
  without it are recorded as not-found (NULL).
- `--source hn`: fetch a **social popularity score from Hacker News** instead
  of a citation count — the sum of points across every HN story linking the
  paper's arXiv id (via Algolia's free, keyless search API). It is stored in a
  separate `social` table, so it never overwrites citation counts: the two
  metrics coexist per paper. A paper with no HN stories scores 0 (real "no
  attention", not NULL). Because HN attention accrues in hours/days rather than
  months, this is the meaningful ranking for recently-published papers before
  citations exist — run it often and scoped to recent papers, e.g.
  `refresh --source hn --month 2026-05 --stale-days 1`.
- `--stale-days` / `--only-missing` are evaluated against whichever metric the
  `--source` populates (`citations` vs `social`), so each refreshes on its own
  schedule.
- Handles `null` results, 429s, and network errors with backoff; commits per
  batch so an interrupted run resumes via `--stale-days` / `--only-missing`.

### `report` — rank and output (read-only)

```sh
uv run csai-citations report (--month YYYY-MM | --all-months) [--top 10] \
    [--by citations|social] [--format table|csv|json] [--out FILE]
```

- Exactly one of `--month` or `--all-months` is required.
- Ranks top-k per month (default `--top 10`, NULL values last), with arXiv URLs
  and a `fetched_at` freshness timestamp.
- `--by social` ranks by the Hacker News attention score instead of citations
  (default `citations`). `csv` / `json` always carry both `citation_count` and
  `score` columns; the table and `fetched_at` reflect the metric ranked on.
- Months within the last ~3 months are annotated as
  `(provisional — low citation accrual)` (relevant when ranking by citations).
- `csv` / `json` include a `provisional` field; `--out FILE` writes to a file
  instead of stdout.

### `serve` — browse the database in a web UI (read-only)

```sh
uv run csai-citations serve [--host 127.0.0.1] [--port 8000]
```

- Starts a tiny local web front-end (Python stdlib only, no extra deps) at
  `http://127.0.0.1:8000`; press Ctrl-C to stop.
- Choose a granularity — **All time** (the default: one overall leaderboard
  across every year), **Year** (`YYYY`), **Month** (`YYYY-MM`), or **Recent**
  (a rolling window — last week / 1 / 2 / 3 months) — plus a "top N", and see
  the papers ranked with arXiv links, authors, category, and the `fetched_at`
  timestamp. Recent months show a *provisional* badge.
- **Recent** windows are anchored to the **data frontier** (the newest paper's
  submission date), not wall-clock today — the Kaggle dump lags a few days, so
  counting from today would leave "last week" empty. The status line shows the
  resolved date range and `data through <date>`. Recent defaults to **Last
  month**: Hacker News attention needs a few days to accrue, so a 1-week window
  is sparse while ~a month is the informative sweet spot.
- **Rank by** toggle — **Citations** or **Hacker News** (social attention).
  Both values are always shown side by side; the active one is emphasised.
  Selecting a **Recent** window defaults the toggle to Hacker News (citations
  haven't accrued yet) but it stays overridable.
- **Category checkboxes** (cs.AI / cs.CL / cs.LG, or whatever has been imported)
  let you include/exclude categories; all are selected by default. Year and
  all-time views rank as one combined leaderboard across the selection.
- Read-only: it never writes to the database. JSON endpoints are also available
  for scripting: `/api/periods`, `/api/categories`, and `/api/rank?period=<all|
  YYYY|YYYY-MM|w|1m|2m|3m>&top=<N>&cats=<cs.AI,cs.CL,...>&metric=<citations|social>`.

## Publish to GitHub Pages (serverless)

The same front-end can be hosted as a **static site with no server**: the SQLite
database is fetched directly by the browser, page by page, over HTTP range
requests (via [sql.js-httpvfs](https://github.com/phiresky/sql.js-httpvfs)). All
ranking and search queries run in WebAssembly client-side. Hosting is free and
there is nothing to keep running — you just publish an updated database now and
then.

`build_public_db.py` turns the local `citations.db` into the assets the site
needs, under `site/db/`:

```sh
python build_public_db.py            # reads citations.db, writes site/db/
```

It drops the `*_history` tables (unused by the UI), adds an FTS5 title index and
indexes on the ranked columns so queries read a handful of rows instead of
scanning, then splits the DB into <25 MB chunks (so it stays under GitHub's
100 MB per-file limit as the data grows) plus a `config.json` manifest and a
`meta.json` of periods/categories.

**Preview locally** (Python's `http.server` does *not* support range requests, so
use one that does):

```sh
npx http-server site -p 8000        # then open http://127.0.0.1:8000
```

**Deploy.** The database is not committed to git (it is large and changes often);
it lives as an asset on a GitHub Release tagged `data`. The workflow in
[`.github/workflows/pages.yml`](.github/workflows/pages.yml) downloads it, runs
the build, and deploys `site/` to Pages.

```sh
# one-time: push the repo to GitHub, then enable
#   Settings → Pages → Source: GitHub Actions
gh release create data citations.db          # first publish (auto-triggers deploy)

# each later refresh:
gh release upload data citations.db --clobber
gh workflow run pages.yml                     # rebuild + redeploy
```

The committed parts of the site (`site/index.html`, `site/vendor/`) are the
static front-end and the vendored sql.js-httpvfs runtime; `site/db/` is
generated and git-ignored.

## API key (recommended)

Semantic Scholar's unauthenticated pool is shared and throttled. Get a free key
(https://www.semanticscholar.org/product/api) and export it:

```sh
export S2_API_KEY=...
```

For OpenAlex, optionally set `OPENALEX_EMAIL` to join the polite pool.

## Typical workflow

```sh
export S2_API_KEY=...                                       # optional but recommended

# one-time: download the Kaggle dump (see import-dump setup above), then
uv run csai-citations import-dump --file /tmp/arxiv_dump/arxiv-metadata-oai-snapshot.json \
    --from 2025-01 --to 2025-12                             # build paper list
uv run csai-citations refresh --source s2                   # first full citation fetch
uv run csai-citations refresh --source hn                   # Hacker News social scores
uv run csai-citations report  --all-months --top 10 --format csv --out top10.csv
uv run csai-citations serve                                 # browse it at http://127.0.0.1:8000
```

### Keeping it up to date

Run the bundled updater whenever you want fresh data, then browse:

```sh
./update.sh                  # new papers + citations + Hacker News scores
./update.sh --scores-only    # skip the 1.6 GB dump; just refresh scores
uv run csai-citations serve
```

`update.sh` downloads the latest dump, imports the last three months of papers,
tops up citations older than 14 days, and re-scores Hacker News for the recent
months (the ones feeding the **Recent** views). It is idempotent and resumable,
so re-running or interrupting it is safe. Use `--scores-only` for a quick score
refresh when you don't need newly-published papers.

## Notes

- Citation counts and social scores are kept in separate tables (`citations`
  and `social`) and never overwrite each other — refreshing one metric leaves
  the other intact.
- Within citations, source counts are never mixed: each row records its
  `source` (`s2` / `openalex`); a `refresh --source` overwrites the current
  count with that source's value. The social metric is sourced from Hacker
  News (`hn`).
- Version suffixes (`vN`) are stripped from arXiv ids everywhere.
- `*.db` is gitignored, so your local data is never committed.
- All commands accept `--no-progress` to disable progress bars (useful in logs).

## Troubleshooting

- **Kaggle download fails / auth error:** ensure `~/.kaggle/kaggle.json` exists
  (with `username` + `key`) and is `chmod 600`. Get the token from
  kaggle.com/settings → Create New Token.
- **Semantic Scholar 429s during `refresh`:** the keyless pool is shared and
  slow. Set `S2_API_KEY` for a dedicated, higher limit; an interrupted run
  resumes via `refresh --only-missing` (rows stay NULL until fetched).
