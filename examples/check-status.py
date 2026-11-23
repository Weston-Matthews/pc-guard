#!/usr/bin/env python3
"""Check pc-guard /status before calling a local model.

Usage:
  STATUS_URL=http://127.0.0.1:8743/status python3 examples/check-status.py
  python3 examples/check-status.py http://100.x.x.x:8743/status
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT = os.environ.get("STATUS_URL", "http://127.0.0.1:8743/status")


def fetch_status(url: str, timeout: float = 3) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    try:
        data = fetch_status(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"pc-guard: status unreachable at {url}: {type(exc).__name__}", file=sys.stderr)
        return 3
    if data.get("busy"):
        reasons = ", ".join(data.get("reasons") or []) or "unknown"
        print(f"pc-guard: busy ({reasons})", file=sys.stderr)
        return 2
    print(
        "pc-guard: not busy (idle_seconds=%s, loaded_models=%s)"
        % (data.get("idle_seconds"), data.get("loaded_models") or data.get("ollama_loaded_models"))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
