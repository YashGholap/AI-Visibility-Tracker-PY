#!/bin/bash
# Wrapper called by both cron (run_all.sh) and Celery (on-demand).
# Provides xvfb virtual display so patchright's headed Chromium works
# on headless servers without getting flagged by Google's antibot.
#
# Usage:
#   ./run.sh scrape
#   ./run.sh ondemand < request.json
#   ./run.sh split
#   ./run.sh run-all
#   ./run.sh ping

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Default to production env file if caller didn't set one.
: "${ENV_FILE:=.env.prod}"
export ENV_FILE

# Ensure uv is on PATH — cron has a minimal PATH that excludes /snap/bin
# and ~/.local/bin where uv may be installed.
export PATH="/snap/bin:$HOME/.local/bin:$PATH"

# Always run headed (headless=False) — patchright's stealth works headed.
# xvfb-run provides a virtual display so Chromium thinks it has a screen
# even on a headless server. Without this, Google flags the IP immediately.
export BROWSER_HEADLESS=false

# xvfb-run -a picks a free display number automatically.
exec xvfb-run -a uv run ai-scraper "$@"