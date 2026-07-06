"""Integration tests for GET /version/latest across all three channels."""

from __future__ import annotations

import sqlite3


def test_stable_shape(client):
    resp = client.get("/version/latest", params={"channel": "stable", "current": "0.69.18"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.json()
    assert body["channel"] == "stable"
    assert body["current"] == "0.69.18"
    assert body["latest"] == {
        "version": "0.69.19",
        "released_at": "2026-07-08T09:15:00Z",
        "url": "https://github.com/dmellok/tesserae/releases/tag/v0.69.19",
        "notes_headline": "Some fix",
    }
    assert body["is_current"] is False
    assert body["versions_behind"] == 1


def test_stable_is_current(client):
    body = client.get("/version/latest", params={"current": "0.69.19"}).json()
    assert body["is_current"] is True
    assert body["versions_behind"] == 0


def test_stable_default_channel(client):
    # No channel provided -> defaults to stable.
    body = client.get("/version/latest").json()
    assert body["channel"] == "stable"
    assert body["current"] is None
    assert body["is_current"] is None
    assert body["versions_behind"] is None


def test_main_shape(client):
    resp = client.get("/version/latest", params={"channel": "main", "current": "0.69.18+abc1234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "main"
    assert body["current"] == "0.69.18+abc1234"
    assert body["latest"] == {
        "sha": "def5678",
        "committed_at": "2026-07-08T10:00:00Z",
        "url": "https://github.com/dmellok/tesserae/commit/def5678",
        "message_headline": "Commit subject line",
    }
    assert body["is_current"] is False
    assert body["commits_behind"] == 5


def test_main_bare(client):
    body = client.get("/version/latest", params={"channel": "main", "current": "main"}).json()
    assert body["commits_behind"] is None
    assert body["is_current"] is None


def test_edge_shape(client):
    resp = client.get("/version/latest", params={"channel": "edge", "current": "0.69.19"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "edge"
    assert body["latest"]["version"] == "0.70.0-rc.2"
    assert "notes_headline" in body["latest"]
    assert "versions_behind" in body


def test_cors_header(client):
    resp = client.get("/version/latest", headers={"Origin": "https://widget.local"})
    assert resp.headers["access-control-allow-origin"] == "*"


def test_invalid_channel_rejected(client):
    assert client.get("/version/latest", params={"channel": "nope"}).status_code == 422


def test_missing_install_stores_null_uuid(client, seeded_settings):
    r = client.get("/version/latest", params={"channel": "stable", "current": "0.69.18"})
    assert r.status_code == 200
    rows = _rows(seeded_settings.stats_db_path)
    assert len(rows) == 1
    assert rows[0]["install_uuid"] is None
    assert rows[0]["channel"] == "stable"
    assert rows[0]["current_version"] == "0.69.18"


def test_install_uuid_stored(client, seeded_settings):
    uuid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    client.get("/version/latest", params={"current": "0.69.19", "install": uuid})
    rows = _rows(seeded_settings.stats_db_path)
    assert rows[0]["install_uuid"] == uuid


def test_invalid_install_becomes_null(client, seeded_settings):
    client.get("/version/latest", params={"current": "0.69.19", "install": "not-a-uuid"})
    rows = _rows(seeded_settings.stats_db_path)
    assert rows[0]["install_uuid"] is None


def test_missing_cache_returns_503(client, seeded_settings):
    seeded_settings.version_cache_path.unlink()
    resp = client.get("/version/latest")
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"


def _rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM hits ORDER BY ts").fetchall()
    finally:
        conn.close()
