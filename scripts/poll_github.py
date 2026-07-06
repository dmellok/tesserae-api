#!/usr/bin/env python3
"""Poll GitHub and refresh the version cache.

Run by the systemd timer every 15 minutes (via `docker exec`). On failure the
previous cache file is left in place so the API keeps serving the last known
good value.

Usage:
    python -m scripts.poll_github
    docker exec tesserae-api python -m scripts.poll_github
"""

from __future__ import annotations

import logging
import sys

from tesserae_api.cache import github_releases
from tesserae_api.config import get_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("poll_github")
    settings = get_settings()
    try:
        payload = github_releases.poll_and_cache(settings)
    except Exception as exc:  # noqa: BLE001 - keep serving last known good on any failure
        log.error("poll failed, keeping previous cache: %s", exc)
        return 1
    stable = (payload.get("stable") or {}).get("version")
    main_sha = (payload.get("main") or {}).get("short_sha")
    edge = (payload.get("edge") or {}).get("version")
    log.info("cache refreshed: stable=%s edge=%s main=%s", stable, edge, main_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
