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


def _mock_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=RELEASES_RAW)
        return httpx.Response(404, json=[])

    return httpx.Client(transport=httpx.MockTransport(handler))


# build_cache ----------------------------------------------------------------


def test_build_cache_indexes_stable_releases_by_kind(settings):
    with _mock_client() as client:
        cache = fw.build_cache(client, settings)
    releases = cache["releases"]
    # rc1 (prerelease) and v1.4.0 (no descriptors) are excluded.
    assert [r["version"] for r in releases] == ["1.6.0", "1.5.0"]
    latest = releases[0]
    assert latest["notes_headline"] == "Safe Wi-Fi OTA for E1004"  # first body line
    assert set(latest["kinds"]) == {"seeed_reterminal_e1004"}  # only descriptor assets
    desc = latest["kinds"]["seeed_reterminal_e1004"]
    assert desc["name"] == "descriptor-seeed_reterminal_e1004.json"
    assert desc["content_type"] == "application/json"


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
