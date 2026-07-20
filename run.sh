#!/bin/bash
# Wrapper that Celery and cron both call. Forwards args to the Python CLI
# under a virtual X display so headless Chromium doesn't get flagged.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Default to production env file if caller didn't set one.
: "${ENV_FILE:=.env.prod}"
export ENV_FILE

# xvfb-run provides a virtual display so headless Chromium doesn't get
# flagged by Google's antibot. -a picks a free display number.
export PATH="/snap/bin:$HOME/.local/bin:$PATH"

exec xvfb-run -a uv run ai-scraper "$@"