"""Tests for the Cloud waitlist: stored once per address, nothing but the
allow-listed panel facts beside it, removable, and reachable cross-origin
from the flasher."""

from __future__ import annotations

from sqlalchemy import select

from tesserae_api import waitlist
from tesserae_api.stats.collector import get_engine, init_db

BODY = {
    "email": "Someone@Example.com ",
    "source": "flash",
    "vendor": "Seeed",
    "model": "reTerminal E1001",
    "env": "seeed_reterminal_e1001",
}


def _rows(settings):
    url = settings.resolved_database_url
    init_db(url)
    with get_engine(url).connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(waitlist.cloud_waitlist))]


def test_joining_stores_one_lowercased_row(client, seeded_settings):
    res = client.post("/cloud/waitlist", json=BODY)
    assert res.status_code == 204
    assert res.headers["cache-control"] == "no-store"
    (row,) = _rows(seeded_settings)
    assert row["email"] == "someone@example.com"
    assert row["source"] == "flash"
    assert row["env"] == "seeed_reterminal_e1001"
    assert row["country"] is None  # no geo db in tests
    assert row["created_at"] is not None


def test_joining_twice_is_one_row_and_the_same_answer(client, seeded_settings):
    assert client.post("/cloud/waitlist", json=BODY).status_code == 204
    again = {**BODY, "email": "SOMEONE@example.com", "env": "other_panel"}
    assert client.post("/cloud/waitlist", json=again).status_code == 204
    (row,) = _rows(seeded_settings)
    assert row["env"] == "seeed_reterminal_e1001"  # first sign-up wins


def test_bad_or_missing_email_is_rejected(client, seeded_settings):
    for body in (
        {},
        {"email": "nope"},
        {"email": "a@b"},
        {"email": 42},
        {"email": "x" * 300 + "@a.io"},
    ):
        assert client.post("/cloud/waitlist", json=body).status_code == 422, body
    assert client.post("/cloud/waitlist").status_code == 422
    assert _rows(seeded_settings) == []


def test_only_allow_listed_fields_are_kept(client, seeded_settings):
    body = {
        **BODY,
        "ip": "203.0.113.9",
        "note": "please",
        "model": "  spaced   out  " + "x" * 200,
        "vendor": 42,
    }
    client.post("/cloud/waitlist", json=body)
    (row,) = _rows(seeded_settings)
    assert "ip" not in row and "note" not in row
    assert row["vendor"] is None  # non-string dropped, not coerced
    assert row["model"].startswith("spaced out x") and len(row["model"]) == 64


def test_source_defaults_to_flash_and_panel_is_optional(client, seeded_settings):
    client.post("/cloud/waitlist", json={"email": "solo@example.com"})
    (row,) = _rows(seeded_settings)
    assert row["source"] == "flash"
    assert row["vendor"] is None and row["model"] is None and row["env"] is None


def test_leaving_removes_the_row(client, seeded_settings):
    client.post("/cloud/waitlist", json=BODY)
    assert (
        client.post("/cloud/waitlist/leave", json={"email": " someone@EXAMPLE.com"}).status_code
        == 204
    )
    assert _rows(seeded_settings) == []
    # Leaving an address that was never listed is the same answer.
    assert (
        client.post("/cloud/waitlist/leave", json={"email": "ghost@example.com"}).status_code == 204
    )
    assert client.post("/cloud/waitlist/leave", json={"email": "nope"}).status_code == 422


def test_summary_helpers(client, seeded_settings):
    url = seeded_settings.resolved_database_url
    client.post("/cloud/waitlist", json=BODY)
    client.post(
        "/cloud/waitlist", json={"email": "two@example.com", "env": "seeed_reterminal_e1001"}
    )
    client.post("/cloud/waitlist", json={"email": "three@example.com"})
    assert waitlist.count(url) == 3
    assert waitlist.count(url, days=1) == 3
    assert waitlist.distribution(url, "env", "(none)") == {"seeed_reterminal_e1001": 2, "(none)": 1}


def test_cross_origin_preflight_allows_the_flasher_post(client):
    res = client.options(
        "/cloud/waitlist",
        headers={
            "Origin": "https://tesserae.ink",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert res.status_code == 200
    assert "POST" in res.headers["access-control-allow-methods"]
