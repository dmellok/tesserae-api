"""Integration tests for GET /firmware/{kind}/latest and its aggregate telemetry."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from tesserae_api.stats import collector

DAY = datetime.now(UTC).date()


def _rows(db_path, table):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


# route: resolution ----------------------------------------------------------


def test_firmware_latest_shape(client):
    resp = client.get("/firmware/seeed_reterminal_e1004/latest")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.json()
    assert set(body) == {"latest"}  # only "latest" at the top level
    latest = body["latest"]
    assert latest["version"] == "1.6.0"
    assert latest["url"].endswith("/v1.6.0")
    assert latest["notes_headline"] == "Safe Wi-Fi OTA for E1004"
    assert latest["descriptor_url"].endswith("descriptor-seeed_reterminal_e1004.json")
    assert latest["assets"][0]["name"] == "descriptor-seeed_reterminal_e1004.json"
    assert latest["assets"][0]["content_type"] == "application/json"


def test_firmware_walks_back_to_older_release(client):
    body = client.get("/firmware/legacy_kind/latest").json()
    assert body["latest"]["version"] == "1.5.0"


def test_firmware_unknown_kind_404_empty(client):
    resp = client.get("/firmware/no_such_kind/latest")
    assert resp.status_code == 404
    assert resp.content == b""  # empty body


def test_firmware_cold_cache_404(client, seeded_settings):
    seeded_settings.firmware_cache_path.unlink()
    assert client.get("/firmware/seeed_reterminal_e1004/latest").status_code == 404


def test_firmware_current_param_accepted_response_unchanged(client):
    with_current = client.get(
        "/firmware/seeed_reterminal_e1004/latest", params={"current": "1.5.0"}
    ).json()
    without = client.get("/firmware/seeed_reterminal_e1004/latest").json()
    assert with_current == without


# route: aggregate telemetry -------------------------------------------------


def test_firmware_records_aggregate_not_hits(client, seeded_settings):
    client.get("/firmware/seeed_reterminal_e1004/latest", params={"current": "1.5.0"})
    fc = _rows(seeded_settings.stats_db_path, "firmware_check_stats")
    assert len(fc) == 1
    assert fc[0]["kind"] == "seeed_reterminal_e1004"
    assert fc[0]["version"] == "1.5.0"
    assert fc[0]["count"] == 1
    # No per-request row lands in hits.
    assert len(_rows(seeded_settings.stats_db_path, "hits")) == 0


def test_firmware_checks_aggregate_by_day(client, seeded_settings):
    for _ in range(3):
        client.get("/firmware/seeed_reterminal_e1004/latest")  # no ?current -> "unknown"
    rows = _rows(seeded_settings.stats_db_path, "firmware_check_stats")
    assert len(rows) == 1  # one aggregate row
    assert rows[0]["version"] == "unknown"
    assert rows[0]["count"] == 3  # three checks collapsed into a count


def test_firmware_404_records_nothing(client, seeded_settings):
    client.get("/firmware/no_such_kind/latest")
    assert len(_rows(seeded_settings.stats_db_path, "firmware_check_stats")) == 0


# collector: aggregate counters ----------------------------------------------


def test_record_firmware_check_upserts(settings):
    collector.init_db(settings.resolved_database_url)
    url = settings.resolved_database_url
    collector.record_firmware_check(url, kind="seeed_ee02", version="1.5.0", country="AU", day=DAY)
    collector.record_firmware_check(url, kind="seeed_ee02", version="1.5.0", country="AU", day=DAY)
    collector.record_firmware_check(url, kind="seeed_ee02", version="1.6.0", country="AU", day=DAY)
    collector.record_firmware_check(url, kind="seeed_ee02", version=None, country=None, day=DAY)

    assert collector.firmware_check_totals(url, days=1) == {"seeed_ee02": 4}
    versions = dict(((k, v), n) for k, v, n in collector.firmware_check_versions(url, days=1))
    assert versions[("seeed_ee02", "1.5.0")] == 2  # deduped and counted
    assert versions[("seeed_ee02", "1.6.0")] == 1
    assert versions[("seeed_ee02", "unknown")] == 1  # NULL version -> sentinel
