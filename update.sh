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
# Progress: every step prints a banner and shows a live progress bar (the
# kaggle download %, the import record counter, and the refresh %-bars). All
# output is also teed to $LOG, so from another terminal you can watch with:
#
#   tail -f /tmp/arxiv_update.log
#
# Idempotent and resumable — safe to re-run or Ctrl-C and restart.
# macOS `date` syntax (the -v flags); adjust if you ever run this on Linux.

set -euo pipefail
cd "$(dirname "$0")"

# Mirror everything (incl. the \r-based progress bars) to a stable log file so
# progress is watchable from any terminal, while still rendering live here.
LOG=/tmp/arxiv_update.log
exec > >(tee "$LOG") 2>&1

DUMP_DIR=/tmp/arxiv_dump
DUMP="$DUMP_DIR/arxiv-metadata-oai-snapshot.json"

# Recent months to keep scored, computed from today. Setting the day to the 1st
# before subtracting months avoids the month-end rollover bug (e.g. Mar 31).
CUR=$(date +%Y-%m)
PREV=$(date -v1d -v-1m +%Y-%m)
PREV2=$(date -v1d -v-2m +%Y-%m)
MONTHS=("$CUR" "$PREV" "$PREV2")   # covers the last-week .. last-3-months views

# step "<n/N>" "<description>" — prints a clear banner with elapsed wall time
# so each phase is easy to spot in the live output and the log.
step() {
  printf '\n\033[1m==================== [%s] %s  (+%ds) ====================\033[0m\n' \
    "$1" "$2" "$SECONDS"
}

SCORES_ONLY=0
[[ "${1:-}" == "--scores-only" ]] && SCORES_ONLY=1

if [[ "$SCORES_ONLY" -eq 0 ]]; then
  step "1/4" "download latest arXiv dump (~1.6 GB)"
  uv run kaggle datasets download -d Cornell-University/arxiv -p "$DUMP_DIR" --force
  unzip -o "$DUMP_DIR/arxiv.zip" -d "$DUMP_DIR"

  step "2/4" "import recent papers ($PREV2 .. $CUR)"
  uv run csai-citations import-dump --file "$DUMP" --from "$PREV2" --to "$CUR"

  # Reclaim the ~6.6 GB dump now that papers are in the DB. Only runs after a
  # successful import (set -e aborts earlier on failure, leaving it to resume).
  # A full run always re-downloads via --force, so nothing is lost by deleting.
  echo "    cleaning up dump ($DUMP_DIR)"
  rm -rf "$DUMP_DIR"
else
  step "1-2/4" "skipping dump download/import (--scores-only)"
fi

step "3/4" "top up citations (re-fetch counts older than 14 days)"
uv run csai-citations refresh --source s2 --stale-days 14

step "4/4" "refresh Hacker News scores for recent months"
i=0
for M in "${MONTHS[@]}"; do
  i=$((i + 1))
  echo "    - month $M (${i}/${#MONTHS[@]})"
  # --stale-days 1: same-day re-runs skip work; across days, re-score (HN
  # points keep moving) and pick up any newly-imported papers.
  uv run csai-citations refresh --source hn --month "$M" --stale-days 1
done

printf '\n\033[1m==> done in %ds. Browse it:  uv run csai-citations serve\033[0m\n' "$SECONDS"
