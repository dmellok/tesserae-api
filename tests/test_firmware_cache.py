"""Unit tests for the firmware cache module: fetching, persistence, fallback, resolve."""

from __future__ import annotations

import json

import httpx
import pytest

from tesserae_api.cache import firmware as fw
from tests.conftest import SEED_FIRMWARE_CACHE

# Raw GitHub /releases fixture for one source.
RELEASES_RAW = [
    {
        "tag_name": "v0.1.1",
        "prerelease": False,
        "published_at": "2026-07-01T09:00:00Z",
        "html_url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.1.1",
        "name": "Fix vflip regression",
        "body": "notes",
        "assets": [
            {
                "name": "picpak-firmware-v0.1.1.bin",
                "browser_download_url": "https://example/v0.1.1/picpak.bin",
            }
        ],
    },
    {
        "tag_name": "v0.2.0-rc.1",
        "prerelease": True,
        "published_at": "2026-07-05T09:00:00Z",
        "html_url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.2.0-rc.1",
        "name": "Prerelease",
        "body": "notes",
        "assets": [],
    },
    {
        "tag_name": "v0.1.0",
        "prerelease": False,
        "published_at": "2026-06-01T09:00:00Z",
        "html_url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.1.0",
        "name": "First",
        "body": "notes",
        "assets": [],
    },
]

SOURCE = {
    "type": "github_releases",
    "owner": "varanu5",
    "repo": "picpak-tesserae-client",
    "channel": "stable",
}


def _mock_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=RELEASES_RAW)
        return httpx.Response(404, json=[])

    return httpx.Client(transport=httpx.MockTransport(handler))


# build_kind_cache -----------------------------------------------------------


def test_build_kind_cache_stable_filters_prereleases(settings):
    with _mock_client() as client:
        entry = fw.build_kind_cache(client, settings, SOURCE)
    assert entry["latest"]["version"] == "0.1.1"
    assert entry["latest"]["notes_headline"] == "Fix vflip regression"
    assert entry["latest"]["assets"] == [
        {"name": "picpak-firmware-v0.1.1.bin", "download_url": "https://example/v0.1.1/picpak.bin"}
    ]
    # Prerelease excluded for a stable channel.
    assert entry["versions"] == ["0.1.1", "0.1.0"]


def test_build_kind_cache_empty_releases(settings):
    def handler(request):
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fw.build_kind_cache(client, settings, SOURCE) is None


def test_build_kind_cache_http_error(settings):
    def handler(request):
        return httpx.Response(500, text="boom")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fw.build_kind_cache(client, settings, SOURCE)


# load_sources ---------------------------------------------------------------


def test_load_sources(settings):
    sources = fw.load_sources(settings.firmware_sources_path)
    assert "picpak_client" in sources
    assert sources["picpak_client"]["owner"] == "varanu5"


def test_load_sources_missing(tmp_path):
    assert fw.load_sources(tmp_path / "nope.yaml") == {}


# poll_and_cache: orchestration + stale fallback -----------------------------


def test_poll_and_cache_updates_and_keeps_last_good(settings, monkeypatch):
    # Seed a previous cache with old entries plus a kind no longer configured.
    previous = {
        "picpak_client": {"latest": {"version": "0.0.9", "assets": []}, "versions": ["0.0.9"]},
        "esp32_client": {"latest": {"version": "1.0.0", "assets": []}, "versions": ["1.0.0"]},
        "retired_client": {"latest": {"version": "9.9.9", "assets": []}, "versions": ["9.9.9"]},
    }
    settings.firmware_cache_path.write_text(json.dumps(previous), encoding="utf-8")

    def fake_build(client, s, source):
        if source["owner"] == "varanu5":  # picpak: succeeds with a fresh release
            return {"latest": {"version": "0.1.1", "assets": []}, "versions": ["0.1.1"]}
        raise httpx.ConnectError("esp32 source unreachable")  # esp32: fails

    monkeypatch.setattr(fw, "build_kind_cache", fake_build)
    cache = fw.poll_and_cache(settings)

    assert cache["picpak_client"]["latest"]["version"] == "0.1.1"  # updated
    assert cache["esp32_client"]["latest"]["version"] == "1.0.0"  # kept last known good
    assert "retired_client" not in cache  # pruned: no longer in the config


# resolve --------------------------------------------------------------------


def test_resolve_behind():
    out = fw.resolve(SEED_FIRMWARE_CACHE, "picpak_client", "0.1.0-dev")
    assert out["kind"] == "picpak_client"
    assert out["current"] == "0.1.0-dev"
    assert out["latest"]["version"] == "0.1.1"
    assert out["latest"]["assets"][0]["name"] == "picpak-firmware-v0.1.1.bin"
    assert out["is_current"] is False
    assert out["versions_behind"] == 1


def test_resolve_current():
    out = fw.resolve(SEED_FIRMWARE_CACHE, "picpak_client", "0.1.1")
    assert out["is_current"] is True
    assert out["versions_behind"] == 0


def test_resolve_no_current():
    out = fw.resolve(SEED_FIRMWARE_CACHE, "esp32_client", None)
    assert out["is_current"] is None
    assert out["versions_behind"] is None
    assert out["latest"]["assets"] == []


def test_resolve_unknown_kind_returns_none():
    assert fw.resolve(SEED_FIRMWARE_CACHE, "does_not_exist", "1.0.0") is None


def test_resolve_unparseable_current():
    out = fw.resolve(SEED_FIRMWARE_CACHE, "esp32_client", "not-a-version")
    assert out["versions_behind"] is None
