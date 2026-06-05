#!/usr/bin/env bash
#
# One-shot updater. Run this before you use the tool to bring everything
# current, then `uv run csai-citations serve`.
#
#   ./update.sh                full update: papers + citations + Hacker News
#   ./update.sh --scores-only  skip the 1.6 GB dump download; just refresh
#                              citations + HN for the recent months already in
#                              the DB (fast; use when you only want fresher
#                              scores, not newly-published papers)
#
# Idempotent and resumable — safe to re-run or Ctrl-C and restart.
# macOS `date` syntax (the -v flags); adjust if you ever run this on Linux.

set -euo pipefail
cd "$(dirname "$0")"

DUMP_DIR=/tmp/arxiv_dump
DUMP="$DUMP_DIR/arxiv-metadata-oai-snapshot.json"

# Recent months to keep scored, computed from today. Setting the day to the 1st
# before subtracting months avoids the month-end rollover bug (e.g. Mar 31).
CUR=$(date +%Y-%m)
PREV=$(date -v1d -v-1m +%Y-%m)
PREV2=$(date -v1d -v-2m +%Y-%m)
MONTHS=("$CUR" "$PREV" "$PREV2")   # covers the last-week .. last-3-months views

SCORES_ONLY=0
[[ "${1:-}" == "--scores-only" ]] && SCORES_ONLY=1

if [[ "$SCORES_ONLY" -eq 0 ]]; then
  echo "==> 1/4 download latest arXiv dump (~1.6 GB)"
  uv run kaggle datasets download -d Cornell-University/arxiv -p "$DUMP_DIR" --force
  unzip -o "$DUMP_DIR/arxiv.zip" -d "$DUMP_DIR"

  echo "==> 2/4 import recent papers ($PREV2 .. $CUR)"
  uv run csai-citations import-dump --file "$DUMP" --from "$PREV2" --to "$CUR"
else
  echo "==> skipping dump download/import (--scores-only)"
fi

echo "==> 3/4 top up citations (re-fetch counts older than 14 days)"
uv run csai-citations refresh --source s2 --stale-days 14

echo "==> 4/4 refresh Hacker News scores for recent months"
for M in "${MONTHS[@]}"; do
  echo "    - $M"
  # --stale-days 1: same-day re-runs skip work; across days, re-score (HN
  # points keep moving) and pick up any newly-imported papers.
  uv run csai-citations refresh --source hn --month "$M" --stale-days 1
done

echo
echo "==> done. Browse it:  uv run csai-citations serve"
