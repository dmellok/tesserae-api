"""Integration tests for GET /firmware/{kind}/latest."""

from __future__ import annotations

import sqlite3


def _rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM hits ORDER BY ts").fetchall()
    finally:
        conn.close()


def test_firmware_shape(client):
    resp = client.get("/firmware/picpak_client/latest", params={"current": "0.1.0-dev"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.json()
    assert body["kind"] == "picpak_client"
    assert body["current"] == "0.1.0-dev"
    assert body["latest"] == {
        "version": "0.1.1",
        "released_at": "2026-07-01T09:00:00Z",
        "url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.1.1",
        "notes_headline": "Fix vflip regression",
        "assets": [
            {
                "name": "picpak-firmware-v0.1.1.bin",
                "download_url": "https://github.com/varanu5/picpak-tesserae-client/releases/download/v0.1.1/picpak-firmware-v0.1.1.bin",
            }
        ],
    }
    assert body["is_current"] is False
    assert body["versions_behind"] == 1


def test_firmware_is_current(client):
    body = client.get("/firmware/picpak_client/latest", params={"current": "0.1.1"}).json()
    assert body["is_current"] is True
    assert body["versions_behind"] == 0


def test_firmware_no_current(client):
    body = client.get("/firmware/esp32_client/latest").json()
    assert body["current"] is None
    assert body["is_current"] is None
    assert body["versions_behind"] is None


def test_firmware_empty_assets_ok(client):
    body = client.get("/firmware/esp32_client/latest").json()
    assert body["latest"]["version"] == "1.2.0"
    assert body["latest"]["assets"] == []


def test_firmware_unknown_kind_404(client):
    resp = client.get("/firmware/no_such_kind/latest")
    assert resp.status_code == 404


def test_firmware_cold_cache_503(client, seeded_settings):
    # Kind is configured but has no cached release yet.
    seeded_settings.firmware_cache_path.unlink()
    resp = client.get("/firmware/picpak_client/latest")
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"


def test_firmware_cors_header(client):
    resp = client.get("/firmware/picpak_client/latest", headers={"Origin": "https://widget.local"})
    assert resp.headers["access-control-allow-origin"] == "*"


def test_firmware_records_stats_with_kind(client, seeded_settings):
    uuid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    client.get(
        "/firmware/picpak_client/latest",
        params={"current": "0.1.0-dev", "install": uuid},
    )
    rows = _rows(seeded_settings.stats_db_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "picpak_client"
    assert rows[0]["current_version"] == "0.1.0-dev"
    assert rows[0]["install_uuid"] == uuid
    assert rows[0]["channel"] is None  # channel is a version-endpoint field


def test_firmware_missing_install_null_uuid(client, seeded_settings):
    client.get("/firmware/picpak_client/latest", params={"current": "0.1.0-dev"})
    rows = _rows(seeded_settings.stats_db_path)
    assert rows[0]["install_uuid"] is None
    assert rows[0]["kind"] == "picpak_client"
