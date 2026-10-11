"""Tests for the daily heartbeat: idempotent upsert, enum coercion, and the route."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from tesserae_api.stats import collector

# Use today (UTC) so read helpers with an N-day window always include these rows.
DAY = datetime.now(UTC).date()
UUID_A = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
UUID_B = "11111111-1111-4111-8111-111111111111"


def _rows(db_path, table):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


# collector: idempotent upsert -----------------------------------------------


def test_same_install_same_day_upserts(settings):
    collector.init_db(settings.resolved_database_url)
    url = settings.resolved_database_url
    collector.record_heartbeat(url, install_uuid=UUID_A, version="0.94.0", os="linux", day=DAY)
    # Second ping the same day with newer descriptive values.
    collector.record_heartbeat(url, install_uuid=UUID_A, version="0.94.2", os="macos", day=DAY)

    rows = _rows(settings.stats_db_path, "heartbeats")
    assert len(rows) == 1  # collapsed to one row
    assert rows[0]["version"] == "0.94.2"  # latest values win
    assert rows[0]["os"] == "macos"
    assert rows[0]["day"] == DAY.isoformat()


def test_different_installs_same_day_two_rows(settings):
    collector.init_db(settings.resolved_database_url)
    url = settings.resolved_database_url
    collector.record_heartbeat(url, install_uuid=UUID_A, day=DAY)
    collector.record_heartbeat(url, install_uuid=UUID_B, day=DAY)
    assert len(_rows(settings.stats_db_path, "heartbeats")) == 2
    assert collector.heartbeat_active_installs(url, days=1) == 2


def test_stores_only_a_day_not_a_timestamp(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_heartbeat(settings.resolved_database_url, install_uuid=UUID_A, day=DAY)
    cols = {
        c[1]
        for c in sqlite3.connect(str(settings.stats_db_path)).execute(
            "PRAGMA table_info(heartbeats)"
        )
    }
    assert "day" in cols
    assert "ts" not in cols  # no exact timestamp is ever stored


# collector: read helpers ----------------------------------------------------


def test_distribution_and_kinds(settings):
    collector.init_db(settings.resolved_database_url)
    url = settings.resolved_database_url
    collector.record_heartbeat(
        url,
        install_uuid=UUID_A,
        version="0.94.2",
        deploy="docker",
        device_kinds=[("pimoroni_inky_4", "1.3.1"), ("waveshare_spectra6_13", "2.0.0")],
        day=DAY,
    )
    collector.record_heartbeat(
        url,
        install_uuid=UUID_B,
        version="0.94.2",
        deploy="pip",
        device_kinds=[("pimoroni_inky_4", "1.3.1")],
        day=DAY,
    )
    assert collector.heartbeat_distribution(url, "version", days=1) == {"0.94.2": 2}
    assert collector.heartbeat_distribution(url, "deploy", days=1) == {"docker": 1, "pip": 1}
    kinds = collector.heartbeat_kind_active_installs(url, days=1)
    assert kinds == {"pimoroni_inky_4": 2, "waveshare_spectra6_13": 1}
    fw = collector.heartbeat_kind_firmware(url, days=1)
    assert ("pimoroni_inky_4", "1.3.1", 2) in fw
    assert ("waveshare_spectra6_13", "2.0.0", 1) in fw


def test_kinds_upsert_updates_fw_version(settings):
    collector.init_db(settings.resolved_database_url)
    url = settings.resolved_database_url
    collector.record_heartbeat(
        url, install_uuid=UUID_A, device_kinds=[("esp32_client", "1.3.0")], day=DAY
    )
    # Re-ping the same day with a newer firmware version.
    collector.record_heartbeat(
        url, install_uuid=UUID_A, device_kinds=[("esp32_client", "1.3.1")], day=DAY
    )
    rows = _rows(settings.stats_db_path, "heartbeat_kinds")
    assert len(rows) == 1  # still one row per (day, install, kind)
    assert rows[0]["fw_version"] == "1.3.1"  # latest firmware wins


# POST /heartbeat ------------------------------------------------------------


def test_post_heartbeat_204(client, seeded_settings):
    resp = client.post(
        "/heartbeat",
        json={
            "install": UUID_A,
            "version": "0.94.2",
            "channel": "stable",
            "os": "linux",
            "arch": "arm64",
            "py": "3.12",
            "deploy": "docker",
            "transport": "rest",
            "devices": "2-3",
            "companion": "4-9",
            "device_kinds": ["pimoroni_inky_4"],
            "ha": True,
        },
    )
    assert resp.status_code == 204
    assert resp.headers["cache-control"] == "no-store"
    rows = _rows(seeded_settings.stats_db_path, "heartbeats")
    assert len(rows) == 1
    assert rows[0]["install_uuid"] == UUID_A
    assert rows[0]["deploy"] == "docker"
    assert rows[0]["devices"] == "2-3"
    assert rows[0]["companion"] == "4-9"
    assert bool(rows[0]["ha"]) is True


def test_post_heartbeat_enum_coercion(client, seeded_settings):
    client.post(
        "/heartbeat",
        json={
            "install": UUID_A,
            "os": "freebsd",
            "arch": "risc-v",
            "deploy": "snap",
            "transport": "carrier-pigeon",
            "devices": "lots",
            "companion": "heaps",
            "py": "2.7",
            "channel": "beta",
        },
    )
    row = _rows(seeded_settings.stats_db_path, "heartbeats")[0]
    assert row["os"] == "other"
    assert row["arch"] == "other"
    assert row["deploy"] == "unknown"
    assert row["transport"] == "unknown"
    assert row["devices"] == "unknown"
    assert row["companion"] == "unknown"
    assert row["py"] == "other"
    assert row["channel"] == "unknown"


def test_post_heartbeat_companion_absent_is_null(client, seeded_settings):
    # An older server that omits companion still succeeds; the column is NULL.
    resp = client.post("/heartbeat", json={"install": UUID_A, "devices": "1"})
    assert resp.status_code == 204
    row = _rows(seeded_settings.stats_db_path, "heartbeats")[0]
    assert row["companion"] is None
    assert row["devices"] == "1"


def test_companion_installs_metric(settings):
    collector.init_db(settings.resolved_database_url)
    url = settings.resolved_database_url
    day = DAY
    collector.record_heartbeat(url, install_uuid=UUID_A, companion="2-3", day=day)
    collector.record_heartbeat(url, install_uuid=UUID_B, companion="0", day=day)  # has none
    collector.record_heartbeat(
        url, install_uuid="22222222-2222-4222-8222-222222222222", companion=None, day=day
    )  # did not report
    # Only the install with a present, non-"0" companion bucket counts.
    assert collector.heartbeat_companion_installs(url, days=3650) == 1
    # Distribution groups by the bucket, including the not-reported (NULL) group.
    assert collector.heartbeat_distribution(url, "companion", days=3650) == {
        "2-3": 1,
        "0": 1,
        None: 1,
    }


def test_post_heartbeat_device_kinds_sanitised(client, seeded_settings):
    client.post(
        "/heartbeat",
        json={
            "install": UUID_A,
            "device_kinds": [
                "good_kind",
                "Bad Kind!",
                "good_kind",
                42,
                "a" * 100,
                "another-1",
            ],
        },
    )
    rows = _rows(seeded_settings.stats_db_path, "heartbeat_kinds")
    stored = sorted(r["kind"] for r in rows)
    assert stored == ["another-1", "good_kind"]  # junk dropped, deduped


def test_post_heartbeat_device_kinds_objects_with_fw(client, seeded_settings):
    client.post(
        "/heartbeat",
        json={
            "install": UUID_A,
            "device_kinds": [
                {"kind": "esp32_client", "fw_version": "1.3.1"},
                {"kind": "pi_bin_client", "fw_version": "v2.0.0"},  # leading v tolerated
                {"kind": "picpak_client", "fw_version": "not-a-version"},  # non-semver -> null
                "trmnl_client",  # bare slug still accepted -> null fw
            ],
        },
    )
    rows = {
        r["kind"]: r["fw_version"] for r in _rows(seeded_settings.stats_db_path, "heartbeat_kinds")
    }
    assert rows == {
        "esp32_client": "1.3.1",
        "pi_bin_client": "2.0.0",
        "picpak_client": None,
        "trmnl_client": None,
    }


def test_post_heartbeat_best_effort_no_5xx(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("tesserae_api.routes.heartbeat.collector.record_heartbeat", boom)
    assert client.post("/heartbeat", json={"install": UUID_A}).status_code == 204


def test_post_heartbeat_no_ip_stored(client, seeded_settings):
    client.post(
        "/heartbeat",
        json={"install": UUID_A},
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    row = dict(_rows(seeded_settings.stats_db_path, "heartbeats")[0])
    assert "203.0.113.7" not in row.values()
    assert "ip" not in row  # no IP column exists


def test_post_heartbeat_idempotent_via_route(client, seeded_settings):
    for _ in range(3):
        client.post("/heartbeat", json={"install": UUID_A, "version": "0.94.2"})
    # Three pings on the same UTC day collapse to one install row.
    assert len(_rows(seeded_settings.stats_db_path, "heartbeats")) == 1


# 0.415 payload: kinds objects, legacy fw_by_kind, age, lineups, features, ota --


def test_post_heartbeat_kinds_objects_land_with_panel_facts(client, seeded_settings):
    resp = client.post(
        "/heartbeat",
        json={
            "install": UUID_A,
            "version": "0.415.0",
            "channel": "edge",
            "device_kinds": ["esp32_client"],
            "fw_by_kind": {"esp32_client": ["1.2.0"]},  # legacy map, ignored when kinds present
            "kinds": [
                {
                    "kind": "esp32_client",
                    "fw_version": "v1.4.0",
                    "gamut": "bwry_4",
                    "res": "800x480",
                    "sleep": "5-15m",
                },
                {"kind": "weird kind!", "fw_version": "1.0.0"},  # dropped: bad slug
                {"kind": "pi_bin_client", "gamut": "Nope!", "res": "big", "sleep": "never"},
            ],
            "age": "8-30",
            "lineups": "2-3",
            "features": {"relay": True, "touch": False, "mcp": 0, "quiet_hours": None},
            "ota": {"offered": "2-3", "applied": "1", "failed": "0"},
        },
    )
    assert resp.status_code == 204
    hb = _rows(seeded_settings.stats_db_path, "heartbeats")
    assert len(hb) == 1
    assert hb[0]["channel"] == "edge"
    assert hb[0]["age"] == "8-30"
    assert hb[0]["lineups"] == "2-3"
    assert bool(hb[0]["feat_relay"]) is True
    assert bool(hb[0]["feat_touch"]) is False
    assert bool(hb[0]["feat_mcp"]) is False
    assert hb[0]["feat_quiet_hours"] is None
    assert (hb[0]["ota_offered"], hb[0]["ota_applied"], hb[0]["ota_failed"]) == ("2-3", "1", "0")
    kinds = {r["kind"]: r for r in _rows(seeded_settings.stats_db_path, "heartbeat_kinds")}
    assert set(kinds) == {"esp32_client", "pi_bin_client"}
    esp = kinds["esp32_client"]
    assert (esp["fw_version"], esp["gamut"], esp["res"], esp["sleep"]) == (
        "1.4.0",
        "bwry_4",
        "800x480",
        "5-15m",
    )
    pi = kinds["pi_bin_client"]
    assert (pi["fw_version"], pi["gamut"], pi["res"], pi["sleep"]) == (
        None,
        "unknown",
        "unknown",
        "unknown",
    )


def test_post_heartbeat_legacy_fw_by_kind_fills_firmware(client, seeded_settings):
    """Builds before 0.415 send bare ``device_kinds`` + ``fw_by_kind``; the
    newest listed version per kind lands instead of NULL."""
    resp = client.post(
        "/heartbeat",
        json={
            "install": UUID_A,
            "device_kinds": ["esp32_client", "pi_bin_client"],
            "fw_by_kind": {"esp32_client": ["1.2.0", "1.10.0", "garbage"], "other": ["9.9.9"]},
        },
    )
    assert resp.status_code == 204
    kinds = {r["kind"]: r for r in _rows(seeded_settings.stats_db_path, "heartbeat_kinds")}
    assert kinds["esp32_client"]["fw_version"] == "1.10.0"  # semver max, not lexical
    assert kinds["pi_bin_client"]["fw_version"] is None
    assert kinds["esp32_client"]["gamut"] is None  # legacy builds carry no panel facts


def test_post_heartbeat_new_fields_absent_stay_null(client, seeded_settings):
    resp = client.post("/heartbeat", json={"install": UUID_A, "version": "0.400.0"})
    assert resp.status_code == 204
    row = _rows(seeded_settings.stats_db_path, "heartbeats")[0]
    for col in ("age", "lineups", "feat_relay", "ota_offered"):
        assert row[col] is None


def test_post_heartbeat_age_and_ota_coerced(client, seeded_settings):
    client.post(
        "/heartbeat",
        json={"install": UUID_A, "age": "yesterday", "ota": {"offered": "lots"}},
    )
    row = _rows(seeded_settings.stats_db_path, "heartbeats")[0]
    assert row["age"] == "unknown"
    assert row["ota_offered"] == "unknown"
    assert row["ota_applied"] is None


# ui: which admin design the install runs (Paper is opt-in on self-hosted) ------


def test_post_heartbeat_ui_lands_and_is_coerced(client, seeded_settings):
    client.post("/heartbeat", json={"install": UUID_A, "ui": "paper"})
    assert _rows(seeded_settings.stats_db_path, "heartbeats")[0]["ui"] == "paper"
    # A later ping the same day with an unknown value collapses onto the row as "other".
    client.post("/heartbeat", json={"install": UUID_A, "ui": "<script>"})
    assert _rows(seeded_settings.stats_db_path, "heartbeats")[0]["ui"] == "other"


def test_post_heartbeat_ui_absent_stays_null(client, seeded_settings):
    client.post("/heartbeat", json={"install": UUID_A, "version": "0.436.1"})
    assert _rows(seeded_settings.stats_db_path, "heartbeats")[0]["ui"] is None


def test_post_heartbeat_ui_chosen_is_a_nullable_flag(client, seeded_settings):
    # App 0.448.10 says whether the design was set by hand; older apps never send it.
    client.post("/heartbeat", json={"install": UUID_A, "ui": "paper", "ui_chosen": True})
    row = _rows(seeded_settings.stats_db_path, "heartbeats")[0]
    assert row["ui"] == "paper" and bool(row["ui_chosen"]) is True
    client.post("/heartbeat", json={"install": UUID_A, "ui": "classic", "ui_chosen": False})
    row = _rows(seeded_settings.stats_db_path, "heartbeats")[0]
    assert row["ui"] == "classic" and bool(row["ui_chosen"]) is False
    client.post("/heartbeat", json={"install": UUID_B, "ui": "classic"})
    assert _rows(seeded_settings.stats_db_path, "heartbeats")[1]["ui_chosen"] is None
