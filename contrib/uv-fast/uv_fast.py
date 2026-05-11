"""uv-fast: thin wrapper that points uv at a local proxpi when reachable.

If the proxpi health endpoint responds within the timeout, sets
``UV_DEFAULT_INDEX`` to the proxpi index URL before exec'ing uv. Otherwise it
just hands off to uv unchanged, so the wrapper is safe to use everywhere.

Configuration:
    UV_FAST_PROXPI   Base URL of the local proxpi (default ``http://127.0.0.1:5000``)
    UV_FAST_TIMEOUT  Health probe timeout in seconds (default ``0.3``)
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def _proxpi_reachable(base_url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def main() -> None:
    base_url = os.environ.get("UV_FAST_PROXPI", "http://127.0.0.1:5000")
    timeout = float(os.environ.get("UV_FAST_TIMEOUT", "0.3"))

    env = os.environ.copy()
    if _proxpi_reachable(base_url, timeout):
        env["UV_DEFAULT_INDEX"] = f"{base_url.rstrip('/')}/index/"

    try:
        os.execvpe("uv", ["uv", *sys.argv[1:]], env)
    except FileNotFoundError:
        print("uv-fast: `uv` not found in PATH", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()
