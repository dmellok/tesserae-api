"""Poll the device firmware repo and cache the latest release per device kind.

Source of truth: the newest published (non-draft, non-prerelease) release of a
single GitHub repo (settings.firmware_repo) that carries a descriptor-<kind>.json
asset for the requested kind. A release "covers" a kind iff it has that asset.

Requests are served entirely from the on-disk cache; if GitHub is unreachable at
poll time the previous cache file is kept, so callers keep being served the last
known good value. Kept separate from cache/github_releases.py so the version
endpoint stays untouched.
"""

from __future__ import annotations

from typing import Any

import httpx

from tesserae_api.cache.github_releases import load_cache as load_cache  # re-exported
from tesserae_api.cache.github_releases import write_cache
from tesserae_api.config import Settings, get_settings

_DESCRIPTOR_PREFIX = "descriptor-"
_DESCRIPTOR_SUFFIX = ".json"


def _headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tesserae-api",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _strip_v(tag: str) -> str:
    return tag[1:] if tag[:1] in ("v", "V") else tag


def _first_line(text: str, limit: int = 100) -> str:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line[:limit]


def _descriptor_kind(name: str) -> str | None:
    if name.startswith(_DESCRIPTOR_PREFIX) and name.endswith(_DESCRIPTOR_SUFFIX):
        return name[len(_DESCRIPTOR_PREFIX) : -len(_DESCRIPTOR_SUFFIX)]
    return None


def _asset_summary(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": asset.get("name"),
        "url": asset.get("browser_download_url"),
        "size": asset.get("size"),
        "content_type": asset.get("content_type"),
    }


def build_cache(client: httpx.Client, settings: Settings) -> dict[str, Any]:
    """Fetch releases and index each kind's descriptor asset, newest release first."""
    url = f"{settings.github_api_base}/repos/{settings.firmware_repo}/releases"
    resp = client.get(
        url,
        params={"per_page": settings.history_limit},
        headers=_headers(settings),
        timeout=settings.github_timeout_seconds,
    )
    resp.raise_for_status()

    releases: list[dict[str, Any]] = []
    for release in resp.json():
        if release.get("draft") or release.get("prerelease"):
            continue
        kinds: dict[str, dict[str, Any]] = {}
        for asset in release.get("assets") or []:
            kind = _descriptor_kind(asset.get("name", "") or "")
            if kind is not None:
                kinds[kind] = _asset_summary(asset)
        if not kinds:
            continue  # release covers no OTA kind
        releases.append(
            {
                "version": _strip_v(release.get("tag_name", "") or ""),
                "released_at": release.get("published_at"),
                "url": release.get("html_url"),
                "notes_headline": _first_line(release.get("body") or ""),
                "kinds": kinds,
            }
        )

    # Newest first, so resolve() returns the first release that covers a kind.
    releases.sort(key=lambda r: r["released_at"] or "", reverse=True)
    return {"releases": releases}


def poll_and_cache(settings: Settings | None = None) -> dict[str, Any]:
    """Fetch from GitHub and write the cache. On failure the old cache is kept."""
    settings = settings or get_settings()
    with httpx.Client() as client:
        payload = build_cache(client, settings)
    write_cache(payload, settings.firmware_cache_path)
    return payload


def cache_is_current(cache: dict[str, Any] | None) -> bool:
    """True if the cache matches the current schema (a releases list).

    A cache written by an older schema (or a missing file) returns False so the
    app re-polls on startup instead of serving stale or unreadable data.
    """
    return isinstance(cache, dict) and isinstance(cache.get("releases"), list)


def resolve(cache: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Return the {"latest": {...}} body for a kind, or None if no release covers it."""
    for release in cache.get("releases", []):
        descriptor = release.get("kinds", {}).get(kind)
        if descriptor is None:
            continue
        return {
            "latest": {
                "version": release["version"],
                "released_at": release["released_at"],
                "url": release["url"],
                "notes_headline": release["notes_headline"],
                "assets": [descriptor],
                "descriptor_url": descriptor.get("url"),
            }
        }
    return None
