"""Unit tests for the GitHub cache module: fetching, persistence, fallback, resolve."""

from __future__ import annotations

import json

import httpx
import pytest

from tesserae_api.cache import github_releases as gh
from tests.conftest import SEED_CACHE

# Raw GitHub-shaped fixtures ------------------------------------------------

RELEASES_RAW = [
    {
        "tag_name": "v0.70.0-rc.2",
        "prerelease": True,
        "published_at": "2026-07-09T12:00:00Z",
        "html_url": "https://github.com/dmellok/tesserae/releases/tag/v0.70.0-rc.2",
        "name": "Release candidate",
        "body": "rc notes",
    },
    {
        "tag_name": "v0.69.19",
        "prerelease": False,
        "published_at": "2026-07-08T09:15:00Z",
        "html_url": "https://github.com/dmellok/tesserae/releases/tag/v0.69.19",
        "name": "Some fix",
        "body": "body",
    },
    {
        "tag_name": "v0.69.18",
        "prerelease": False,
        "published_at": "2026-07-01T09:15:00Z",
        "html_url": "https://github.com/dmellok/tesserae/releases/tag/v0.69.18",
        "name": "Earlier release",
        "body": "body",
    },
]

LATEST_RAW = RELEASES_RAW[1]

COMMITS_RAW = [
    {
        "sha": "def5678000000000000000000000000000000000",
        "html_url": "https://github.com/dmellok/tesserae/commit/def5678",
        "commit": {
            "author": {"date": "2026-07-08T10:00:00Z"},
            "message": "Commit subject line\n\nbody",
        },
    },
    {
        "sha": "c4c4c4c0000000000000000000000000000000000",
        "html_url": "u",
        "commit": {"author": {"date": "2026-07-08T09:00:00Z"}, "message": "c4"},
    },
]


def _mock_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/releases/latest"):
            return httpx.Response(200, json=LATEST_RAW)
        if path.endswith("/releases"):
            return httpx.Response(200, json=RELEASES_RAW)
        if path.endswith("/commits"):
            return httpx.Response(200, json=COMMITS_RAW)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


# Fetching / build_cache ----------------------------------------------------


def test_build_cache(settings):
    with _mock_client() as client:
        cache = gh.build_cache(client, settings)
    assert cache["stable"]["version"] == "0.69.19"
    assert cache["stable"]["prerelease"] is False
    assert cache["edge"]["version"] == "0.70.0-rc.2"
    assert cache["edge"]["prerelease"] is True
    assert cache["main"]["short_sha"] == "def5678"
    assert cache["main"]["message_headline"] == "Commit subject line"
    assert len(cache["releases"]) == 3
    assert len(cache["commits"]) == 2


def test_notes_headline_first_line_only(settings):
    with _mock_client() as client:
        cache = gh.build_cache(client, settings)
    assert "\n" not in cache["main"]["message_headline"]


# Persistence + stale fallback ---------------------------------------------


def test_write_and_load_roundtrip(settings):
    gh.write_cache(SEED_CACHE, settings.version_cache_path)
    loaded = gh.load_cache(settings.version_cache_path)
    assert loaded == SEED_CACHE


def test_load_missing_returns_none(settings):
    assert gh.load_cache(settings.version_cache_path) is None


def test_load_malformed_returns_none(settings):
    settings.version_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.version_cache_path.write_text("{not json", encoding="utf-8")
    assert gh.load_cache(settings.version_cache_path) is None


def test_poll_failure_keeps_previous_cache(settings, monkeypatch):
    gh.write_cache(SEED_CACHE, settings.version_cache_path)

    def boom(*a, **k):
        raise httpx.ConnectError("github unreachable")

    monkeypatch.setattr(gh, "build_cache", boom)
    with pytest.raises(httpx.ConnectError):
        gh.poll_and_cache(settings)
    # The previously written cache must be intact.
    assert gh.load_cache(settings.version_cache_path) == SEED_CACHE


def test_malformed_github_response_raises(settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream error")

    bad = httpx.Client(transport=httpx.MockTransport(handler))
    with bad, pytest.raises(httpx.HTTPStatusError):
        gh.build_cache(bad, settings)


# Resolve -------------------------------------------------------------------


def test_resolve_stable_behind():
    out = gh.resolve_stable(SEED_CACHE, "0.69.18")
    assert out["is_current"] is False
    assert out["versions_behind"] == 1
    assert out["latest"]["version"] == "0.69.19"


def test_resolve_stable_current():
    out = gh.resolve_stable(SEED_CACHE, "0.69.19")
    assert out["is_current"] is True
    assert out["versions_behind"] == 0


def test_resolve_stable_no_current():
    out = gh.resolve_stable(SEED_CACHE, None)
    assert out["is_current"] is None
    assert out["versions_behind"] is None


def test_resolve_stable_unparseable_current():
    out = gh.resolve_stable(SEED_CACHE, "garbage")
    assert out["versions_behind"] is None


def test_resolve_main_behind():
    out = gh.resolve_main(SEED_CACHE, "0.69.18+abc1234")
    assert out["commits_behind"] == 5
    assert out["is_current"] is False
    assert out["latest"]["sha"] == "def5678"


def test_resolve_main_current():
    out = gh.resolve_main(SEED_CACHE, "0.69.18+def5678")
    assert out["commits_behind"] == 0
    assert out["is_current"] is True


def test_resolve_main_bare():
    out = gh.resolve_main(SEED_CACHE, "main")
    assert out["commits_behind"] is None
    assert out["is_current"] is None


def test_resolve_edge():
    out = gh.resolve_edge(SEED_CACHE, "0.69.19")
    assert out["channel"] == "edge"
    assert out["latest"]["version"] == "0.70.0-rc.2"


def test_extract_sha_variants():
    assert gh._extract_sha("0.69.18+abc1234") == "abc1234"
    assert gh._extract_sha("abc1234") == "abc1234"
    assert gh._extract_sha("main") is None
    assert gh._extract_sha(None) is None
    assert gh._extract_sha("") is None


def test_empty_cache_resolves_to_null():
    empty = json.loads("{}")
    assert gh.resolve_stable(empty, "0.1.0")["latest"] is None
    assert gh.resolve_main(empty, "abc1234")["latest"] is None
    assert gh.resolve_edge(empty, "0.1.0")["latest"] is None
