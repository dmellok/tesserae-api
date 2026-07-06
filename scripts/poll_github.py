#!/usr/bin/env python3
"""Poll GitHub and refresh the version and firmware caches.

Run by the systemd timer every 15 minutes (via `docker exec`). Each cache is
refreshed independently; on failure the previous cache file is left in place so
the API keeps serving the last known good value.

Usage:
    python -m scripts.poll_github
    docker exec tesserae-api python -m scripts.poll_github
"""

from __future__ import annotations

import logging
import sys

from tesserae_api.cache import firmware, github_releases
from tesserae_api.config import get_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("poll_github")
    settings = get_settings()
    failures = 0

    try:
        payload = github_releases.poll_and_cache(settings)
        stable = (payload.get("stable") or {}).get("version")
        main_sha = (payload.get("main") or {}).get("short_sha")
        edge = (payload.get("edge") or {}).get("version")
        log.info("version cache refreshed: stable=%s edge=%s main=%s", stable, edge, main_sha)
    except Exception as exc:  # noqa: BLE001 - keep serving last known good on any failure
        log.error("version poll failed, keeping previous cache: %s", exc)
        failures += 1

    try:
        firmware_cache = firmware.poll_and_cache(settings)
        log.info("firmware cache refreshed: %d kinds", len(firmware_cache))
    except Exception as exc:  # noqa: BLE001 - keep serving last known good on any failure
        log.error("firmware poll failed, keeping previous cache: %s", exc)
        failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
