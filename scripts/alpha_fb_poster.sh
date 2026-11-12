#!/bin/bash
# Alpha Trading Floor — Facebook Poster (single-shot via TEST_MODE)
# Runs one bounded FB photo-post cycle, respecting the cooldown timer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FB_PROJECT_DIR="${ALPHA_FACEBOOK_PROJECT_DIR:-$SCRIPT_DIR/../facebook_content}"
FB_SCRIPT="$FB_PROJECT_DIR/alpha_trading_floor.py"
PYTHON="/usr/local/bin/python3"
RUN_TIMEOUT_SECONDS=240

cd "$FB_PROJECT_DIR"

# The Python poster loads this directory's .env itself. Do not source it from
# the Hermes cron shell, which can trigger macOS permission failures.
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

if [ ! -f "$FB_SCRIPT" ]; then
    printf '[ALPHA_FB_WRAPPER] missing script: %s\n' "$FB_SCRIPT" >&2
    exit 1
fi

# Run exactly one TEST_MODE cycle. The runner kills the entire child process
# group if a model, browser, or network call exceeds the hard deadline.
exec env DRY_RUN="${DRY_RUN:-true}" POST_VERIFY_ENABLED="${POST_VERIFY_ENABLED:-true}" \
    TEST_MODE=true WATCHDOG_ENABLED=false PYTHONUNBUFFERED=1 \
    "$PYTHON" "$SCRIPT_DIR/alpha_fb_poster_runner.py" \
    --timeout "$RUN_TIMEOUT_SECONDS" -- "$PYTHON" "$FB_SCRIPT"
