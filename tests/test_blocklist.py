"""Tests for the version blocklist: unit logic and the route behaviour."""

from __future__ import annotations

import sqlite3

from tesserae_api import blocklist
from tesserae_api.config import Settings

UUID_A = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def _count(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# unit -----------------------------------------------------------------------


def test_is_blocked_matches_prefix():
    s = Settings(blocked_versions="0.95.,1.0.0-dev")
    assert blocklist.is_blocked("0.95.3", s)
    assert blocklist.is_blocked("1.0.0-dev", s)
    assert not blocklist.is_blocked("0.71.0", s)
    assert not blocklist.is_blocked(None, s)
    assert not blocklist.is_blocked("", s)


def test_empty_blocklist_blocks_nothing():
    s = Settings(blocked_versions="")
    assert not blocklist.is_blocked("0.95.3", s)
    assert s.blocked_version_prefixes == ()


def test_prefixes_parse_and_trim():
    assert Settings(blocked_versions=" 0.95. , 0.96. ").blocked_version_prefixes == (
        "0.95.",
        "0.96.",
    )


# routes ---------------------------------------------------------------------


def test_heartbeat_blocked_not_recorded(client, seeded_settings):
    seeded_settings.blocked_versions = "0.95."
    resp = client.post("/heartbeat", json={"install": UUID_A, "version": "0.95.3"})
    assert resp.status_code == 200
    assert "github.com/dmellok/tesserae" in resp.json()["notice"]
    assert resp.headers["x-tesserae-notice"]
    assert _count(seeded_settings.stats_db_path, "heartbeats") == 0


def test_heartbeat_allowed_version_recorded(client, seeded_settings):
    seeded_settings.blocked_versions = "0.95."
    resp = client.post("/heartbeat", json={"install": UUID_A, "version": "0.71.0"})
    assert resp.status_code == 204
    assert _count(seeded_settings.stats_db_path, "heartbeats") == 1


def test_version_blocked_not_recorded(client, seeded_settings):
    seeded_settings.blocked_versions = "0.95."
    resp = client.get("/version/latest", params={"current": "0.95.3"})
    assert resp.status_code == 200
    assert "notice" in resp.json()
    assert _count(seeded_settings.stats_db_path, "hits") == 0


def test_version_allowed_recorded(client, seeded_settings):
    seeded_settings.blocked_versions = "0.95."
    resp = client.get("/version/latest", params={"current": "0.69.18"})
    assert resp.status_code == 200
    assert "latest" in resp.json()
    assert _count(seeded_settings.stats_db_path, "hits") == 1


def test_widget_blocked_not_recorded(client, seeded_settings):
    seeded_settings.blocked_versions = "0.95."
    resp = client.post(
        "/widgets/install",
        json={"widget": "spotify", "install": UUID_A, "version": "0.95.3"},
    )
    assert resp.status_code == 200
    assert "notice" in resp.json()
    assert _count(seeded_settings.stats_db_path, "widget_installs") == 0
