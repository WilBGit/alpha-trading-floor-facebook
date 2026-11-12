#!/usr/bin/env python3
"""Generate one Alpha Trading Floor background through Hermes' native xAI backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


# The managed Hermes runtime is launched directly with this file as the script,
# so its source checkout is not automatically on sys.path.  Add the active
# checkout before importing the native provider; otherwise the fallback only
# fails in cron with ``ModuleNotFoundError: plugins``.
for _candidate in (
    os.getenv("HERMES_PYTHON_SRC_ROOT", "").strip(),
    str(Path.home() / ".hermes" / "hermes-agent"),
):
    if _candidate and (_candidate_path := Path(_candidate)).is_dir():
        if (_candidate_path / "plugins").is_dir():
            sys.path.insert(0, str(_candidate_path))
            break

# The Hermes launcher normally imports this before loading provider modules. The
# cron invokes the managed interpreter directly, so bootstrap its dependency
# environment here as well (notably requests and the PM runtime package).
import hermes_bootstrap  # noqa: F401,E402

from plugins.image_gen.xai import XAIImageGenProvider


DEFAULT_MODEL = "grok-imagine-image-quality"


def main() -> int:
    try:
        request = json.load(sys.stdin)
        prompt = str(request.get("prompt", "")).strip()
        model = str(request.get("model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
        aspect_ratio = str(request.get("aspect_ratio", "landscape")).strip() or "landscape"
        if not prompt:
            raise ValueError("prompt is required")
        result = XAIImageGenProvider().generate(
            prompt,
            aspect_ratio=aspect_ratio,
            model=model,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("success") else 1
    except Exception as exc:
        print(json.dumps({
            "success": False,
            "image": None,
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
