"""Shared fixtures: an isolated data dir and a seeded version cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesserae_api.config import Settings, get_settings

# A cache payload shaped exactly as cache.build_cache() produces it.
SEED_CACHE = {
    "stable": {
        "version": "0.69.19",
        "tag": "v0.69.19",
        "prerelease": False,
        "released_at": "2026-07-08T09:15:00Z",
        "url": "https://github.com/dmellok/tesserae/releases/tag/v0.69.19",
        "notes_headline": "Some fix",
    },
    "edge": {
        "version": "0.70.0-rc.2",
        "tag": "v0.70.0-rc.2",
        "prerelease": True,
        "released_at": "2026-07-09T12:00:00Z",
        "url": "https://github.com/dmellok/tesserae/releases/tag/v0.70.0-rc.2",
        "notes_headline": "Release candidate",
    },
    "main": {
        "sha": "def5678000000000000000000000000000000000",
        "short_sha": "def5678",
        "committed_at": "2026-07-08T10:00:00Z",
        "url": "https://github.com/dmellok/tesserae/commit/def5678",
        "message_headline": "Commit subject line",
    },
    "releases": [
        {
            "version": "0.70.0-rc.2",
            "tag": "v0.70.0-rc.2",
            "prerelease": True,
            "released_at": "2026-07-09T12:00:00Z",
            "url": "https://github.com/dmellok/tesserae/releases/tag/v0.70.0-rc.2",
            "notes_headline": "Release candidate",
        },
        {
            "version": "0.69.19",
            "tag": "v0.69.19",
            "prerelease": False,
            "released_at": "2026-07-08T09:15:00Z",
            "url": "https://github.com/dmellok/tesserae/releases/tag/v0.69.19",
            "notes_headline": "Some fix",
        },
        {
            "version": "0.69.18",
            "tag": "v0.69.18",
            "prerelease": False,
            "released_at": "2026-07-01T09:15:00Z",
            "url": "https://github.com/dmellok/tesserae/releases/tag/v0.69.18",
            "notes_headline": "Earlier release",
        },
    ],
    "commits": [
        {
            "sha": "def5678000000000000000000000000000000000",
            "short_sha": "def5678",
            "committed_at": "2026-07-08T10:00:00Z",
            "url": "u",
            "message_headline": "latest",
        },
        {
            "sha": "c4c4c4c0000000000000000000000000000000000",
            "short_sha": "c4c4c4c",
            "committed_at": "2026-07-08T09:00:00Z",
            "url": "u",
            "message_headline": "c4",
        },
        {
            "sha": "b3b3b3b0000000000000000000000000000000000",
            "short_sha": "b3b3b3b",
            "committed_at": "2026-07-08T08:00:00Z",
            "url": "u",
            "message_headline": "b3",
        },
        {
            "sha": "a2a2a2a0000000000000000000000000000000000",
            "short_sha": "a2a2a2a",
            "committed_at": "2026-07-08T07:00:00Z",
            "url": "u",
            "message_headline": "a2",
        },
        {
            "sha": "9191919000000000000000000000000000000000",
            "short_sha": "9191919",
            "committed_at": "2026-07-08T06:00:00Z",
            "url": "u",
            "message_headline": "91",
        },
        {
            "sha": "abc1234000000000000000000000000000000000",
            "short_sha": "abc1234",
            "committed_at": "2026-07-08T05:00:00Z",
            "url": "u",
            "message_headline": "abc",
        },
    ],
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A Settings instance pointing every path at an isolated tmp dir."""
    from tesserae_api.stats import collector

    get_settings.cache_clear()
    s = Settings(data_dir=tmp_path)
    yield s
    collector.dispose()
    get_settings.cache_clear()


@pytest.fixture
def seeded_settings(settings: Settings) -> Settings:
    settings.version_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.version_cache_path.write_text(json.dumps(SEED_CACHE), encoding="utf-8")
    return settings


@pytest.fixture
def client(seeded_settings: Settings, monkeypatch):
    """A FastAPI TestClient wired to the seeded settings; no network, no geo db."""
    from fastapi.testclient import TestClient

    import tesserae_api.config as config_mod
    from tesserae_api.main import create_app

    monkeypatch.setattr(config_mod, "get_settings", lambda: seeded_settings)
    # Route and app modules import get_settings by name, patch those references too.
    monkeypatch.setattr("tesserae_api.routes.version.get_settings", lambda: seeded_settings)
    monkeypatch.setattr("tesserae_api.main.get_settings", lambda: seeded_settings)

    app = create_app()
    with TestClient(app) as c:
        yield c
