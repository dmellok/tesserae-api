"""Tests for widget install counting: collector functions and the routes."""

from __future__ import annotations

import sqlite3

from tesserae_api.stats import collector

UUID_A = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
UUID_B = "11111111-1111-4111-8111-111111111111"


def _widget_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM widget_installs ORDER BY ts").fetchall()
    finally:
        conn.close()


# collector ------------------------------------------------------------------


def test_record_widget_install_persists(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_widget_install(
        settings.resolved_database_url,
        widget_id="spotify",
        install_uuid=UUID_A,
        tesserae_version="0.93.0",
        country="AU",
        region="Victoria",
    )
    rows = _widget_rows(settings.stats_db_path)
    assert len(rows) == 1
    assert rows[0]["widget_id"] == "spotify"
    assert rows[0]["install_uuid"] == UUID_A
    assert rows[0]["tesserae_version"] == "0.93.0"
    assert rows[0]["country"] == "AU"
    assert rows[0]["region"] == "Victoria"


def test_counts_same_install_counts_once(settings):
    collector.init_db(settings.resolved_database_url)
    for _ in range(2):
        collector.record_widget_install(
            settings.resolved_database_url,
            widget_id="spotify",
            install_uuid=UUID_A,
            tesserae_version=None,
            country=None,
            region=None,
        )
    assert collector.widget_install_counts(settings.resolved_database_url) == {"spotify": 1}


def test_counts_distinct_installs(settings):
    collector.init_db(settings.resolved_database_url)
    for uid in (UUID_A, UUID_B):
        collector.record_widget_install(
            settings.resolved_database_url,
            widget_id="spotify",
            install_uuid=uid,
            tesserae_version=None,
            country=None,
            region=None,
        )
    collector.record_widget_install(
        settings.resolved_database_url,
        widget_id="weather_now",
        install_uuid=UUID_A,
        tesserae_version=None,
        country=None,
        region=None,
    )
    counts = collector.widget_install_counts(settings.resolved_database_url)
    assert counts == {"spotify": 2, "weather_now": 1}


def test_counts_null_uuid_excluded(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_widget_install(
        settings.resolved_database_url,
        widget_id="spotify",
        install_uuid=UUID_A,
        tesserae_version=None,
        country=None,
        region=None,
    )
    collector.record_widget_install(
        settings.resolved_database_url,
        widget_id="spotify",
        install_uuid=None,  # excluded from distinct count
        tesserae_version=None,
        country=None,
        region=None,
    )
    assert collector.widget_install_counts(settings.resolved_database_url) == {"spotify": 1}


def test_counts_filtered_to_one_widget(settings):
    collector.init_db(settings.resolved_database_url)
    collector.record_widget_install(
        settings.resolved_database_url,
        widget_id="spotify",
        install_uuid=UUID_A,
        tesserae_version=None,
        country=None,
        region=None,
    )
    collector.record_widget_install(
        settings.resolved_database_url,
        widget_id="weather_now",
        install_uuid=UUID_B,
        tesserae_version=None,
        country=None,
        region=None,
    )
    assert collector.widget_install_counts(settings.resolved_database_url, widget_id="spotify") == {
        "spotify": 1
    }


# POST /widgets/install ------------------------------------------------------


def test_post_install_204(client, seeded_settings):
    resp = client.post(
        "/widgets/install",
        json={"widget": "spotify", "install": UUID_A, "version": "0.93.0"},
    )
    assert resp.status_code == 204
    assert resp.headers["cache-control"] == "no-store"
    rows = _widget_rows(seeded_settings.stats_db_path)
    assert len(rows) == 1
    assert rows[0]["widget_id"] == "spotify"
    assert rows[0]["install_uuid"] == UUID_A
    assert rows[0]["tesserae_version"] == "0.93.0"


def test_post_install_missing_widget_400(client):
    assert client.post("/widgets/install", json={"install": UUID_A}).status_code == 400
    assert client.post("/widgets/install", json={"widget": "  "}).status_code == 400


def test_post_install_invalid_uuid_stored_null(client, seeded_settings):
    resp = client.post("/widgets/install", json={"widget": "spotify", "install": "not-a-uuid"})
    assert resp.status_code == 204
    rows = _widget_rows(seeded_settings.stats_db_path)
    assert rows[0]["install_uuid"] is None
    assert rows[0]["widget_id"] == "spotify"


def test_post_install_best_effort_no_5xx(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("tesserae_api.routes.widgets.collector.record_widget_install", boom)
    resp = client.post("/widgets/install", json={"widget": "spotify", "install": UUID_A})
    assert resp.status_code == 204


# GET /widgets/installs ------------------------------------------------------


def test_get_installs_counts_map(client):
    client.post("/widgets/install", json={"widget": "spotify", "install": UUID_A})
    client.post("/widgets/install", json={"widget": "spotify", "install": UUID_B})
    client.post("/widgets/install", json={"widget": "weather_now", "install": UUID_A})
    resp = client.get("/widgets/installs")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert resp.json() == {"counts": {"spotify": 2, "weather_now": 1}}


def test_get_installs_single_widget(client):
    client.post("/widgets/install", json={"widget": "spotify", "install": UUID_A})
    resp = client.get("/widgets/installs", params={"widget": "spotify"})
    assert resp.json() == {"widget": "spotify", "count": 1}
    # Unknown widget returns count 0.
    assert client.get("/widgets/installs", params={"widget": "nope"}).json() == {
        "widget": "nope",
        "count": 0,
    }
