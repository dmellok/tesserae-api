"""Unit tests for the firmware cache module: descriptor-based resolution."""

from __future__ import annotations

import httpx
import pytest

from tesserae_api.cache import firmware as fw
from tests.conftest import SEED_FIRMWARE_CACHE


def _asset(name: str) -> dict:
    return {
        "name": name,
        "browser_download_url": f"https://example/{name}",
        "size": 412,
        "content_type": "application/json",
    }


# newest first: v1.6.0 covers e1004 + ee02, v1.5.0 also covers legacy_kind,
# v1.6.0-rc1 is a prerelease (ignored), v1.4.0 has no descriptors (skipped).
RELEASES_RAW = [
    {
        "tag_name": "v1.6.0",
        "prerelease": False,
        "draft": False,
        "published_at": "2026-07-22T10:00:00Z",
        "html_url": "https://github.com/dmellok/tesserae-device-firmware/releases/tag/v1.6.0",
        "body": "Safe Wi-Fi OTA for E1004\n\nmore detail here",
        "assets": [_asset("descriptor-seeed_reterminal_e1004.json"), _asset("firmware-e1004.bin")],
    },
    {
        "tag_name": "v1.6.0-rc1",
        "prerelease": True,
        "draft": False,
        "published_at": "2026-07-21T10:00:00Z",
        "html_url": "https://github.com/dmellok/tesserae-device-firmware/releases/tag/v1.6.0-rc1",
        "body": "rc",
        "assets": [_asset("descriptor-seeed_reterminal_e1004.json")],
    },
    {
        "tag_name": "v1.5.0",
        "prerelease": False,
        "draft": False,
        "published_at": "2026-07-19T10:00:00Z",
        "html_url": "https://github.com/dmellok/tesserae-device-firmware/releases/tag/v1.5.0",
        "body": "Older",
        "assets": [
            _asset("descriptor-seeed_reterminal_e1004.json"),
            _asset("descriptor-legacy_kind.json"),
        ],
    },
    {
        "tag_name": "v1.4.0",
        "prerelease": False,
        "draft": False,
        "published_at": "2026-07-18T10:00:00Z",
        "html_url": "https://github.com/dmellok/tesserae-device-firmware/releases/tag/v1.4.0",
        "body": "no assets",
        "assets": [],
    },
]


def _bin(name: str) -> dict:
    return {
        "name": name,
        "browser_download_url": f"https://example/picpak/{name}",
        "size": 1_048_576,
        "content_type": "application/octet-stream",
    }


# A community repo: plain firmware files, no descriptor asset. v0.9.4 is the
# newest published release, v0.9.5-rc1 a prerelease (ignored).
COMMUNITY_RAW = [
    {
        "tag_name": "v0.9.5-rc1",
        "prerelease": True,
        "draft": False,
        "published_at": "2026-09-08T10:00:00Z",
        "html_url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.9.5-rc1",
        "body": "rc",
        "assets": [_bin("picpak-tesserae-firmware.bin")],
    },
    {
        "tag_name": "v0.9.4",
        "prerelease": False,
        "draft": False,
        "published_at": "2026-09-06T16:57:20Z",
        "html_url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.9.4",
        "body": "# PicPak Tesserae Client: firmware release\n\nmore",
        "assets": [_bin("picpak-tesserae-firmware.bin"), _bin("SHA256SUMS")],
    },
    {
        "tag_name": "v0.9.3",
        "prerelease": False,
        "draft": False,
        "published_at": "2026-08-30T18:49:58Z",
        "html_url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.9.3",
        "body": "older",
        "assets": [_bin("picpak-tesserae-firmware.bin")],
    },
]


def _mock_client(community_status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/dmellok/tesserae-device-firmware/releases"):
            return httpx.Response(200, json=RELEASES_RAW)
        if path.endswith("/repos/varanu5/picpak-tesserae-client/releases"):
            if community_status != 200:
                return httpx.Response(community_status, text="nope")
            return httpx.Response(200, json=COMMUNITY_RAW)
        return httpx.Response(404, json=[])

    return httpx.Client(transport=httpx.MockTransport(handler))


# build_cache ----------------------------------------------------------------


def _descriptor_releases(cache: dict) -> list[dict]:
    return [r for r in cache["releases"] if r["repo"] == "dmellok/tesserae-device-firmware"]


def _community_releases(cache: dict) -> list[dict]:
    return [r for r in cache["releases"] if r["repo"] == "varanu5/picpak-tesserae-client"]


def test_build_cache_indexes_stable_releases_by_kind(settings):
    with _mock_client() as client:
        cache = fw.build_cache(client, settings)
    releases = _descriptor_releases(cache)
    # rc1 (prerelease) and v1.4.0 (no descriptors) are excluded.
    assert [r["version"] for r in releases] == ["1.6.0", "1.5.0"]
    latest = releases[0]
    assert latest["notes_headline"] == "Safe Wi-Fi OTA for E1004"  # first body line
    assert set(latest["kinds"]) == {"seeed_reterminal_e1004"}  # only descriptor assets
    desc = latest["kinds"]["seeed_reterminal_e1004"]
    assert desc["name"] == "descriptor-seeed_reterminal_e1004.json"
    assert desc["content_type"] == "application/json"


def test_build_cache_community_repo_covers_its_kind(settings):
    assert settings.community_firmware_sources == {
        "picpak_client": "varanu5/picpak-tesserae-client"
    }
    with _mock_client() as client:
        cache = fw.build_cache(client, settings)
    community = _community_releases(cache)
    # The prerelease is excluded; every published release covers the kind.
    assert [r["version"] for r in community] == ["0.9.4", "0.9.3"]
    latest = community[0]
    assert latest["kinds"] == {"picpak_client": None}  # no descriptor asset
    assert latest["notes_headline"] == "PicPak Tesserae Client: firmware release"  # "# " stripped
    assert [a["name"] for a in latest["assets"]] == ["picpak-tesserae-firmware.bin", "SHA256SUMS"]
    # Newest first across both sources.
    assert [r["version"] for r in cache["releases"]] == ["0.9.4", "0.9.3", "1.6.0", "1.5.0"]


def test_build_cache_community_failure_carries_previous_entries(settings):
    with _mock_client() as client:
        good = fw.build_cache(client, settings)
    with _mock_client(community_status=500) as client:
        cache = fw.build_cache(client, settings, previous=good)
    # Main feed still refreshed; the community kind keeps its last good releases.
    assert [r["version"] for r in _descriptor_releases(cache)] == ["1.6.0", "1.5.0"]
    assert [r["version"] for r in _community_releases(cache)] == ["0.9.4", "0.9.3"]


def test_build_cache_community_failure_without_previous_drops_kind(settings):
    with _mock_client(community_status=404) as client:
        cache = fw.build_cache(client, settings)
    assert _community_releases(cache) == []
    assert fw.resolve(cache, "picpak_client") is None


def test_build_cache_no_community_sources(settings):
    settings.community_firmware_repos = ""
    assert settings.community_firmware_sources == {}
    with _mock_client() as client:
        cache = fw.build_cache(client, settings)
    assert _community_releases(cache) == []


def test_community_sources_parse_ignores_malformed_pairs(settings):
    settings.community_firmware_repos = " a=o/r , bad, =o/r, b= , c=x/y "
    assert settings.community_firmware_sources == {"a": "o/r", "c": "x/y"}


def test_build_cache_http_error(settings):
    def handler(request):
        return httpx.Response(500, text="boom")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fw.build_cache(client, settings)


def test_poll_keeps_last_good_on_failure(settings, monkeypatch):
    fw.write_cache(SEED_FIRMWARE_CACHE, settings.firmware_cache_path)

    def boom(*a, **k):
        raise httpx.ConnectError("github unreachable")

    monkeypatch.setattr(fw, "build_cache", boom)
    with pytest.raises(httpx.ConnectError):
        fw.poll_and_cache(settings)
    assert fw.load_cache(settings.firmware_cache_path) == SEED_FIRMWARE_CACHE


# resolve --------------------------------------------------------------------


def test_resolve_newest_covering_release():
    out = fw.resolve(SEED_FIRMWARE_CACHE, "seeed_reterminal_e1004")
    latest = out["latest"]
    assert latest["version"] == "1.6.0"
    assert latest["url"].endswith("/v1.6.0")
    assert latest["descriptor_url"].endswith("descriptor-seeed_reterminal_e1004.json")
    assert latest["assets"] == [latest["assets"][0]]
    assert latest["assets"][0]["name"] == "descriptor-seeed_reterminal_e1004.json"
    assert "/v1.6.0/" in latest["descriptor_url"]  # newest release's descriptor


def test_resolve_community_release_lists_files_without_descriptor():
    out = fw.resolve(SEED_FIRMWARE_CACHE, "picpak_client")
    latest = out["latest"]
    assert latest["version"] == "0.9.4"
    assert latest["url"].endswith("/v0.9.4")
    assert latest["descriptor_url"] is None
    assert [a["name"] for a in latest["assets"]] == ["picpak-tesserae-firmware.bin", "SHA256SUMS"]


def test_resolve_walks_back_to_older_release():
    # legacy_kind only exists in v1.5.0.
    out = fw.resolve(SEED_FIRMWARE_CACHE, "legacy_kind")
    assert out["latest"]["version"] == "1.5.0"


def test_resolve_unknown_kind_returns_none():
    assert fw.resolve(SEED_FIRMWARE_CACHE, "no_such_kind") is None


def test_resolve_empty_cache_returns_none():
    assert fw.resolve({"releases": []}, "seeed_reterminal_e1004") is None


def test_strip_v_and_first_line():
    assert fw._strip_v("v1.6.0") == "1.6.0"
    assert fw._strip_v("1.6.0") == "1.6.0"
    assert fw._first_line("line one\nline two") == "line one"
    assert fw._first_line("## Heading\nbody") == "Heading"
    assert len(fw._first_line("x" * 200)) == 100


# cache schema guard ---------------------------------------------------------


def test_cache_is_current():
    assert fw.cache_is_current(SEED_FIRMWARE_CACHE) is True
    assert fw.cache_is_current({"releases": []}) is True
    assert fw.cache_is_current(None) is False
    # Old per-kind schema (no "releases" list) is treated as not current.
    assert fw.cache_is_current({"esp32_client": {"latest": {}}}) is False


def test_lifespan_repolls_stale_firmware_cache(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from tesserae_api import main as main_mod
    from tesserae_api.cache import firmware, github_releases

    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    # Version cache looks current, so it must not re-poll.
    monkeypatch.setattr(github_releases, "load_cache", lambda p: {"releases": [], "commits": []})
    monkeypatch.setattr(
        github_releases, "poll_and_cache", lambda s=None: pytest.fail("version re-polled")
    )
    # Firmware cache is the old schema, so it must re-poll.
    monkeypatch.setattr(firmware, "load_cache", lambda p: {"esp32_client": {}})
    called = {}
    monkeypatch.setattr(firmware, "poll_and_cache", lambda s=None: called.setdefault("fw", True))

    with TestClient(main_mod.create_app()):
        pass
    assert called.get("fw") is True
