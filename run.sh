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

# Raise the open-file limit before launching anything.
#
# Chrome burns file descriptors fast: sockets per renderer, IPC pipes, and one
# per shared-memory segment. The default soft limit on this host is 1024, which
# is simply too low for a multi-hour run with hundreds of browser contexts —
# exhausting it kills the browser silently, with no OOM kill and nothing in
# dmesg. Not confirmed as the cause of the 2026-09-02 death (query 116/299),
# but low enough to be worth removing as a variable either way.
#
# The soft limit can be raised up to the hard limit without privileges, and
# child processes inherit it.
_nofile="$(ulimit -Hn)"
if [[ "$_nofile" == "unlimited" ]] || (( _nofile > 65535 )); then
    _nofile=65535
fi
if ulimit -n "$_nofile" 2>/dev/null; then
    echo "run.sh: open-file limit raised to $(ulimit -n)"
else
    echo "run.sh: WARNING could not raise open-file limit (still $(ulimit -n))" >&2
fi

# Chromium's own stdout/stderr is swallowed by default, which is why the
# 2026-09-02 death (query 116/299, no OOM kill, 188 GB RAM free) left no
# reason behind in the log. Set BROWSER_DEBUG=1 to pipe Playwright's browser
# channel — including Chromium's stderr and its exit code — into the run log.
# Verbose, so leave it off for normal runs and turn it on when hunting a death.
if [[ "${BROWSER_DEBUG:-0}" == "1" ]]; then
    export DEBUG="${DEBUG:+$DEBUG,}pw:browser*"
fi

# xvfb-run -a picks a free display number automatically.
exec xvfb-run -a uv run ai-scraper "$@"