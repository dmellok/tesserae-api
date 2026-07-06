"""Unit tests for stats collection and the geo lookup wiring."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from tesserae_api.stats import collector, geo


def _rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM hits ORDER BY ts").fetchall()
    finally:
        conn.close()


def test_init_creates_table(settings):
    collector.init_db(settings.resolved_database_url)
    conn = sqlite3.connect(str(settings.stats_db_path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(hits)")}
    finally:
        conn.close()
    assert cols == {"ts", "install_uuid", "country", "region", "channel", "kind", "current_version"}


def test_record_hit_persists(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_hit(
        settings.resolved_database_url,
        install_uuid="3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        country="AU",
        region="New South Wales",
        channel="stable",
        current_version="0.69.18",
    )
    rows = _rows(settings.stats_db_path)
    assert len(rows) == 1
    assert rows[0]["install_uuid"] == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert rows[0]["country"] == "AU"
    assert rows[0]["region"] == "New South Wales"
    assert rows[0]["channel"] == "stable"
    assert rows[0]["current_version"] == "0.69.18"


def test_record_hit_with_kind(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_hit(
        settings.resolved_database_url,
        install_uuid="3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        country="AU",
        region="Victoria",
        kind="picpak_client",
        current_version="0.1.0-dev",
    )
    rows = _rows(settings.stats_db_path)
    assert rows[0]["kind"] == "picpak_client"
    assert rows[0]["channel"] is None
    assert rows[0]["current_version"] == "0.1.0-dev"


def test_record_hit_null_uuid(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_hit(
        settings.resolved_database_url,
        install_uuid=None,
        country=None,
        region=None,
        channel="stable",
        current_version=None,
    )
    rows = _rows(settings.stats_db_path)
    assert rows[0]["install_uuid"] is None
    assert rows[0]["country"] is None


def test_record_hit_autotimestamps(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_hit(
        settings.resolved_database_url,
        install_uuid=None,
        country=None,
        region=None,
        channel="edge",
        current_version=None,
    )
    ts = _rows(settings.stats_db_path)[0]["ts"]
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:  # SQLite stores DateTime without an offset.
        parsed = parsed.replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - parsed) < timedelta(minutes=5)


def test_geo_lookup_missing_db_returns_none(settings):
    # No mmdb baked in during tests -> graceful (None, None), request still served.
    assert geo.lookup("8.8.8.8", settings.geoip_db_path) == (None, None)


def test_geo_lookup_no_ip(settings):
    assert geo.lookup(None, settings.geoip_db_path) == (None, None)


def test_route_calls_geo_and_stores(client, seeded_settings, monkeypatch):
    calls = {}

    def fake_lookup(ip, db_path):
        calls["ip"] = ip
        return ("AU", "New South Wales")

    monkeypatch.setattr("tesserae_api.routes.version.geo.lookup", fake_lookup)
    client.get(
        "/version/latest",
        params={
            "channel": "stable",
            "current": "0.69.18",
            "install": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        },
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    # The left-most forwarded IP is used for geo, then discarded (never stored).
    assert calls["ip"] == "203.0.113.7"
    rows = _rows(seeded_settings.stats_db_path)
    assert rows[0]["country"] == "AU"
    assert rows[0]["region"] == "New South Wales"
    # Confirm no column can hold an IP: the schema has no such field.
    assert "203.0.113.7" not in dict(rows[0]).values()
