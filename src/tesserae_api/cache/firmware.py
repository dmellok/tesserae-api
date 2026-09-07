"""Poll the firmware repos and cache the latest release per device kind.

Two kinds of source feed the cache:

* The device firmware repo (settings.firmware_repo). A published (non-draft,
  non-prerelease) release "covers" a kind iff it carries a
  descriptor-<kind>.json asset; the response points at that descriptor so a
  Tesserae server can import it for OTA.
* Community firmware repos (settings.community_firmware_repos), one repo per
  kind, whose releases carry plain firmware files and no descriptor. Every
  published release of the repo covers its kind. The response lists the
  release's files and leaves ``descriptor_url`` null, so a Tesserae server can
  show "update available" and link the release without offering an OTA.

Requests are served entirely from the on-disk cache; if GitHub is unreachable at
poll time the previous cache file is kept, so callers keep being served the last
known good value. A community repo that fails to fetch keeps its entries from
the previous cache rather than blocking the main feed. Kept separate from
cache/github_releases.py so the version endpoint stays untouched.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tesserae_api.cache.github_releases import load_cache as load_cache  # re-exported
from tesserae_api.cache.github_releases import write_cache
from tesserae_api.config import Settings, get_settings

log = logging.getLogger("tesserae_api.firmware")

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
    # Release bodies that open with a markdown heading read as a plain headline.
    return line.lstrip("#").strip()[:limit]


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


def _fetch_releases(client: httpx.Client, settings: Settings, repo: str) -> list[dict[str, Any]]:
    """Published (non-draft, non-prerelease) releases of ``repo``, as GitHub returns them."""
    url = f"{settings.github_api_base}/repos/{repo}/releases"
    resp = client.get(
        url,
        params={"per_page": settings.history_limit},
        headers=_headers(settings),
        timeout=settings.github_timeout_seconds,
    )
    resp.raise_for_status()
    return [r for r in resp.json() if not (r.get("draft") or r.get("prerelease"))]


def _release_entry(release: dict[str, Any], repo: str, kinds: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": _strip_v(release.get("tag_name", "") or ""),
        "released_at": release.get("published_at"),
        "url": release.get("html_url"),
        "notes_headline": _first_line(release.get("body") or ""),
        "repo": repo,
        "assets": [_asset_summary(a) for a in release.get("assets") or []],
        "kinds": kinds,
    }


def _descriptor_releases(client: httpx.Client, settings: Settings) -> list[dict[str, Any]]:
    """The firmware repo's releases, each indexing its descriptor assets by kind."""
    releases: list[dict[str, Any]] = []
    for release in _fetch_releases(client, settings, settings.firmware_repo):
        kinds: dict[str, dict[str, Any]] = {}
        for asset in release.get("assets") or []:
            kind = _descriptor_kind(asset.get("name", "") or "")
            if kind is not None:
                kinds[kind] = _asset_summary(asset)
        if not kinds:
            continue  # release covers no OTA kind
        releases.append(_release_entry(release, settings.firmware_repo, kinds))
    return releases


def _community_releases(
    client: httpx.Client, settings: Settings, kind: str, repo: str
) -> list[dict[str, Any]]:
    """A community repo's releases; every published one covers ``kind`` with no descriptor."""
    return [
        _release_entry(release, repo, {kind: None})
        for release in _fetch_releases(client, settings, repo)
    ]


def build_cache(
    client: httpx.Client,
    settings: Settings,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch releases from every source and index each kind's coverage, newest first.

    A failure on the main firmware repo raises (the caller keeps the old cache).
    A failure on a community repo is logged and that repo's entries are carried
    over from ``previous`` (or dropped when there is nothing to carry), so one
    unreachable or deleted community repo never stalls the main feed.
    """
    releases = _descriptor_releases(client, settings)
    for kind, repo in settings.community_firmware_sources.items():
        try:
            releases.extend(_community_releases(client, settings, kind, repo))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("community firmware poll failed for %s (%s): %s", kind, repo, exc)
            carried = [
                r
                for r in (previous or {}).get("releases", [])
                if r.get("repo") == repo and kind in (r.get("kinds") or {})
            ]
            releases.extend(carried)

    # Newest first, so resolve() returns the first release that covers a kind.
    releases.sort(key=lambda r: r["released_at"] or "", reverse=True)
    return {"releases": releases}


def poll_and_cache(settings: Settings | None = None) -> dict[str, Any]:
    """Fetch from GitHub and write the cache. On failure the old cache is kept."""
    settings = settings or get_settings()
    previous = load_cache(settings.firmware_cache_path)
    with httpx.Client() as client:
        payload = build_cache(client, settings, previous if cache_is_current(previous) else None)
    write_cache(payload, settings.firmware_cache_path)
    return payload


def cache_is_current(cache: dict[str, Any] | None) -> bool:
    """True if the cache matches the current schema (a releases list).

    A cache written by an older schema (or a missing file) returns False so the
    app re-polls on startup instead of serving stale or unreadable data.
    """
    return isinstance(cache, dict) and isinstance(cache.get("releases"), list)


def resolve(cache: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Return the {"latest": {...}} body for a kind, or None if no release covers it.

    A descriptor release answers with the descriptor as its only asset and its
    URL as ``descriptor_url``. A community release answers with every file the
    release carries and ``descriptor_url`` null.
    """
    for release in cache.get("releases", []):
        kinds = release.get("kinds") or {}
        if kind not in kinds:
            continue
        descriptor = kinds[kind]
        return {
            "latest": {
                "version": release["version"],
                "released_at": release["released_at"],
                "url": release["url"],
                "notes_headline": release["notes_headline"],
                "assets": [descriptor] if descriptor else list(release.get("assets") or []),
                "descriptor_url": descriptor.get("url") if descriptor else None,
            }
        }
    return None
