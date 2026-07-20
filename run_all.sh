#!/bin/bash
# Weekly cron: scrape all queries across all platforms for all projects,
# then split results into per-project ranking tables.
#
# Usage: ./run_all.sh [prod|beta]
#   default: prod

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Env selection: run_all.sh prod → .env.prod; run_all.sh beta → .env.beta
ENV_SUFFIX="${1:-prod}"
export ENV_FILE=".env.$ENV_SUFFIX"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found in $SCRIPT_DIR" >&2
    exit 1
fi

# Prevent overlapping runs — if another instance is running (e.g. last
# week's scrape is still going when Monday's cron fires), exit cleanly
# instead of creating a concurrent migration race or duplicate rows.
LOCK_FILE="/tmp/ai_scraper_run_all.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "$(date -Is)  ERROR: another run_all.sh is already running, exiting" >&2
    exit 1
fi

echo "======================================================================"
echo "$(date -Is)  AI visibility weekly run  env=$ENV_FILE"
echo "======================================================================"

# 1. Scrape all platforms across all projects.
echo ""
echo "--- SCRAPE ---"
if ! ./run.sh scrape; then
    echo "ERROR: scrape failed, aborting split" >&2
    exit 1
fi

# 2. Split scraped results into per-project ranking tables.
echo ""
echo "--- SPLIT ---"
if ! ./run.sh split; then
    echo "ERROR: split failed" >&2
    exit 1
fi

echo ""
echo "$(date -Is)  DONE"