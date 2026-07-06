"""Poll per-kind firmware release sources and cache them to disk.

Each device kind in firmware_sources.yaml maps to a GitHub repository. The poller
fetches that repository's releases, keeps the latest stable one (with its binary
asset links), and caches the result keyed by kind. Requests are served entirely
from the cache; if a source is unreachable at poll time its previous cached value
is kept, so callers keep being served the last known good firmware.

This mirrors cache/github_releases.py (the /version/latest backend) but is kept
separate so the version endpoint stays untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml
from packaging.version import InvalidVersion, Version

from tesserae_api.cache.github_releases import load_cache, write_cache
from tesserae_api.config import Settings, get_settings


def load_sources(path: Path) -> dict[str, dict[str, Any]]:
    """Parse firmware_sources.yaml into {kind: source}. Missing file -> {}."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tesserae-api",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    tag = release.get("tag_name", "") or ""
    assets = [
        {"name": a.get("name"), "download_url": a.get("browser_download_url")}
        for a in (release.get("assets") or [])
    ]
    return {
        "version": tag.lstrip("v"),
        "prerelease": bool(release.get("prerelease")),
        "released_at": release.get("published_at") or release.get("created_at"),
        "url": release.get("html_url"),
        "notes_headline": _first_line(release.get("name") or release.get("body") or ""),
        "assets": assets,
    }


def build_kind_cache(
    client: httpx.Client, settings: Settings, source: dict[str, Any]
) -> dict[str, Any] | None:
    """Fetch releases for one source and return its cache entry, or None if empty."""
    owner = source["owner"]
    repo = source["repo"]
    channel = source.get("channel", "stable")
    url = f"{settings.github_api_base}/repos/{owner}/{repo}/releases"
    resp = client.get(
        url,
        params={"per_page": settings.history_limit},
        headers=_headers(settings),
        timeout=settings.github_timeout_seconds,
    )
    resp.raise_for_status()
    releases = [_release_summary(r) for r in resp.json()]
    if channel == "stable":
        releases = [r for r in releases if not r["prerelease"]]
    if not releases:
        return None
    return {"latest": releases[0], "versions": [r["version"] for r in releases]}


def poll_and_cache(settings: Settings | None = None) -> dict[str, Any]:
    """Refresh the firmware cache for every configured source.

    A source that fails to fetch keeps its previous cached entry. Kinds removed
    from the config are dropped from the cache.
    """
    settings = settings or get_settings()
    sources = load_sources(settings.firmware_sources_path)
    previous = load_cache(settings.firmware_cache_path) or {}

    cache: dict[str, Any] = {}
    with httpx.Client() as client:
        for kind, source in sources.items():
            if source.get("type") != "github_releases":
                continue
            try:
                entry = build_kind_cache(client, settings, source)
            except Exception:
                entry = None
            if entry is not None:
                cache[kind] = entry
            elif kind in previous:
                cache[kind] = previous[kind]

    write_cache(cache, settings.firmware_cache_path)
    return cache


def _safe_version(value: str | None) -> Version | None:
    try:
        return Version(value) if value else None
    except (InvalidVersion, TypeError):
        return None


def _versions_behind(versions: list[str], current: str | None) -> int | None:
    """Count cached versions strictly newer than current. None if unparseable."""
    current_v = _safe_version(current)
    if current_v is None:
        return None
    count = 0
    for v in versions:
        parsed = _safe_version(v)
        if parsed is not None and parsed > current_v:
            count += 1
    return count


def resolve(cache: dict[str, Any], kind: str, current: str | None) -> dict[str, Any] | None:
    """Build the response body for a kind, or None if it has no cached data yet."""
    entry = cache.get(kind)
    if entry is None:
        return None
    latest = entry["latest"]
    is_current = None
    versions_behind = None
    if current is not None:
        is_current = current == latest["version"]
        versions_behind = _versions_behind(entry.get("versions", []), current)
    return {
        "kind": kind,
        "current": current,
        "latest": {
            "version": latest["version"],
            "released_at": latest["released_at"],
            "url": latest["url"],
            "notes_headline": latest["notes_headline"],
            "assets": latest.get("assets", []),
        },
        "is_current": is_current,
        "versions_behind": versions_behind,
    }
