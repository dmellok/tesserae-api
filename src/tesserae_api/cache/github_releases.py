"""Poll GitHub for version information and cache it to disk.

Three upstream sources are tracked:

  stable  GET /repos/{repo}/releases/latest      (latest full release)
  edge    GET /repos/{repo}/releases             (latest release with prerelease: true)
  main    GET /repos/{repo}/commits/main         (latest commit on the default branch)

The list endpoints (/releases and /commits) are also fetched so that
"versions_behind" and "commits_behind" can be computed against the caller's
current version. Results are written atomically to a single JSON file. If GitHub
is unreachable the previous cache file is left untouched, so callers keep being
served the last known good value.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from tesserae_api.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tesserae-api",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _get(client: httpx.Client, settings: Settings, path: str, **params: Any) -> Any:
    url = f"{settings.github_api_base}/repos/{settings.repo_slug}{path}"
    resp = client.get(
        url,
        params=params or None,
        headers=_headers(settings),
        timeout=settings.github_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    tag = release.get("tag_name", "") or ""
    return {
        "version": tag.lstrip("v"),
        "tag": tag,
        "prerelease": bool(release.get("prerelease")),
        "released_at": release.get("published_at") or release.get("created_at"),
        "url": release.get("html_url"),
        "notes_headline": _first_line(release.get("name") or release.get("body") or ""),
    }


def _commit_summary(commit: dict[str, Any]) -> dict[str, Any]:
    sha = commit.get("sha", "") or ""
    data = commit.get("commit", {}) or {}
    author = data.get("author", {}) or {}
    return {
        "sha": sha,
        "short_sha": sha[:7],
        "committed_at": author.get("date"),
        "url": commit.get("html_url"),
        "message_headline": _first_line(data.get("message") or ""),
    }


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def build_cache(client: httpx.Client, settings: Settings) -> dict[str, Any]:
    """Fetch every upstream source and assemble a cache payload."""
    releases_raw = _get(client, settings, "/releases", per_page=settings.history_limit)
    commits_raw = _get(client, settings, "/commits", sha="main", per_page=settings.history_limit)
    # /releases/latest gives the canonical "latest full release" (never a prerelease).
    latest_stable_raw = _get(client, settings, "/releases/latest")

    releases = [_release_summary(r) for r in releases_raw]
    commits = [_commit_summary(c) for c in commits_raw]
    latest_stable = _release_summary(latest_stable_raw)
    latest_edge = next((r for r in releases if r["prerelease"]), None)

    return {
        "stable": latest_stable,
        "edge": latest_edge,
        "main": commits[0] if commits else None,
        "releases": releases,
        "commits": commits,
    }


def poll_and_cache(settings: Settings | None = None) -> dict[str, Any]:
    """Fetch from GitHub and write the cache. On failure the old cache is kept."""
    settings = settings or get_settings()
    with httpx.Client() as client:
        payload = build_cache(client, settings)
    write_cache(payload, settings.version_cache_path)
    return payload


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------


def write_cache(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_cache(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------


def _safe_version(value: str) -> Version | None:
    try:
        return Version(value)
    except (InvalidVersion, TypeError):
        return None


def resolve_stable(cache: dict[str, Any], current: str | None) -> dict[str, Any]:
    latest = cache.get("stable")
    releases = [r for r in cache.get("releases", []) if not r["prerelease"]]
    return _resolve_release_channel("stable", latest, releases, current)


def resolve_edge(cache: dict[str, Any], current: str | None) -> dict[str, Any]:
    latest = cache.get("edge")
    releases = [r for r in cache.get("releases", []) if r["prerelease"]]
    return _resolve_release_channel("edge", latest, releases, current)


def _resolve_release_channel(
    channel: str, latest: dict[str, Any] | None, releases: list[dict[str, Any]], current: str | None
) -> dict[str, Any]:
    if latest is None:
        return {
            "channel": channel,
            "current": current,
            "latest": None,
            "is_current": None,
            "versions_behind": None,
        }

    latest_out = {
        "version": latest["version"],
        "released_at": latest["released_at"],
        "url": latest["url"],
        "notes_headline": latest["notes_headline"],
    }
    is_current = None
    versions_behind = None
    if current is not None:
        is_current = current == latest["version"]
        versions_behind = _versions_behind(releases, current)

    return {
        "channel": channel,
        "current": current,
        "latest": latest_out,
        "is_current": is_current,
        "versions_behind": versions_behind,
    }


def _versions_behind(releases: list[dict[str, Any]], current: str | None) -> int | None:
    """Count releases strictly newer than `current`. None if current is unparseable."""
    current_v = _safe_version(current) if current else None
    if current_v is None:
        return None
    count = 0
    for r in releases:
        rv = _safe_version(r["version"])
        if rv is not None and rv > current_v:
            count += 1
    return count


def resolve_main(cache: dict[str, Any], current: str | None) -> dict[str, Any]:
    latest = cache.get("main")
    commits = cache.get("commits", [])
    if latest is None:
        return {
            "channel": "main",
            "current": current,
            "latest": None,
            "is_current": None,
            "commits_behind": None,
        }

    latest_out = {
        "sha": latest["short_sha"],
        "committed_at": latest["committed_at"],
        "url": latest["url"],
        "message_headline": latest["message_headline"],
    }
    is_current = None
    commits_behind = None
    current_sha = _extract_sha(current)
    if current_sha is not None:
        commits_behind = _commits_behind(commits, current_sha)
        is_current = commits_behind == 0
    elif current is not None:
        # A bare "main" (or anything without a sha) can't be positioned in history.
        is_current = None

    return {
        "channel": "main",
        "current": current,
        "latest": latest_out,
        "is_current": is_current,
        "commits_behind": commits_behind,
    }


def _extract_sha(current: str | None) -> str | None:
    """Pull the sha out of a main-channel version like "0.69.18+abc1234" or "abc1234".

    A literal "+" in a URL query string decodes to a space, so a caller that fails
    to percent-encode it sends "0.69.18 abc1234". Accept whitespace as an equivalent
    separator so the sha is still recovered.
    """
    if not current:
        return None
    parts = current.replace("+", " ").split()
    candidate = (parts[-1] if parts else "").lower()
    if candidate in {"", "main"}:
        return None
    if all(c in "0123456789abcdef" for c in candidate) and len(candidate) >= 4:
        return candidate
    return None


def _commits_behind(commits: list[dict[str, Any]], current_sha: str) -> int | None:
    for index, commit in enumerate(commits):
        full = (commit.get("sha") or "").lower()
        if full.startswith(current_sha) or current_sha.startswith(commit.get("short_sha", "x")):
            return index
    return None
