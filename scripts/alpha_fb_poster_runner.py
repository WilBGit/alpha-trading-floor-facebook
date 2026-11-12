#!/usr/bin/env python3
"""Run one Alpha Facebook cycle with a bounded process-group timeout."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 3 or args[0] != "--timeout" or "--" not in args:
        print("usage: alpha_fb_poster_runner.py --timeout SECONDS -- COMMAND [ARGS...]", flush=True)
        return 2

    try:
        timeout = max(30, min(int(args[1]), 300))
    except ValueError:
        print("invalid timeout", flush=True)
        return 2

    separator = args.index("--")
    command = args[separator + 1 :]
    if not command:
        print("missing child command", flush=True)
        return 2

    print(f"[ALPHA_FB_RUNNER] start timeout={timeout}s child={command[0]}", flush=True)
    # Inherit the wrapper's current directory directly. Calling os.getcwd()
    # here can be interrupted by Hermes/macOS while resolving a protected or
    # provider-backed directory, preventing the child from starting at all.
    child = subprocess.Popen(
        command,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )

    try:
        return_code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(
            f"[ALPHA_FB_RUNNER] timeout after {timeout}s; terminating child process group",
            flush=True,
        )
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("[ALPHA_FB_RUNNER] child ignored SIGTERM; sending SIGKILL", flush=True)
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
        print("[ALPHA_FB_RUNNER] end exit=124", flush=True)
        return 124

    print(f"[ALPHA_FB_RUNNER] end exit={return_code}", flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
