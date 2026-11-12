#!/bin/bash
# Alpha Trading Floor, one-shot Tuesday Facebook poll.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/usr/local/bin/python3"
RUN_TIMEOUT_SECONDS=240
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

exec env \
    X_TO_FACEBOOK_ENABLED="${X_TO_FACEBOOK_ENABLED:-true}" \
    DRY_RUN="${DRY_RUN:-true}" \
    POST_VERIFY_ENABLED="true" \
    PYTHONUNBUFFERED=1 \
    "$PYTHON" "$SCRIPT_DIR/alpha_fb_poster_runner.py" \
    --timeout "$RUN_TIMEOUT_SECONDS" -- "$PYTHON" "$SCRIPT_DIR/alpha_x_to_facebook.py" --poll "$@"
