"""Tests for the flash completion report: allow-listed forwarding, no-op when
unconfigured, and the browser preflight the flasher depends on."""

from __future__ import annotations

import httpx
import pytest

from tesserae_api.routes import flash


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.fixture
def sent(monkeypatch) -> _Sent:
    fake = _Sent()
    monkeypatch.setattr(flash.httpx, "post", fake)
    return fake


BODY = {
    "device": "Seeed reTerminal E1001",
    "vendor": "Seeed",
    "model": "reTerminal E1001",
    "env": "seeed_reterminal_e1001",
    "firmware_version": "1.37.0",
    "chip": "ESP32-S3",
    "mode": "catalog",
    "method": "webserial",
    "factory_reset": "false",
}


def test_forwards_completed_flash_to_webhook(client, seeded_settings, sent):
    seeded_settings.flash_webhook_url = "https://hooks.example/flash"
    res = client.post("/flash/events", json=BODY)
    assert res.status_code == 204
    assert res.headers["cache-control"] == "no-store"
    # Background tasks run before TestClient returns.
    assert sent.calls == [
        ("https://hooks.example/flash", {"event": "firmware_flash_completed", **BODY})
    ]


def test_no_webhook_configured_is_a_silent_no_op(client, seeded_settings, sent):
    seeded_settings.flash_webhook_url = ""
    res = client.post("/flash/events", json=BODY)
    assert res.status_code == 204
    assert sent.calls == []


def test_only_allow_listed_fields_leave_the_process(client, seeded_settings, sent):
    seeded_settings.flash_webhook_url = "https://hooks.example/flash"
    body = {
        **BODY,
        "email": "someone@example.com",
        "ip": "203.0.113.9",
        "model": "  spaced   out  " + "x" * 200,
        "chip": 42,
    }
    client.post("/flash/events", json=body)
    ((_, payload),) = sent.calls
    assert "email" not in payload and "ip" not in payload
    assert "chip" not in payload  # non-string dropped, not coerced
    assert payload["model"].startswith("spaced out x")
    assert len(payload["model"]) == 64


def test_empty_body_forwards_a_bare_event_and_a_non_object_is_rejected(
    client, seeded_settings, sent
):
    seeded_settings.flash_webhook_url = "https://hooks.example/flash"
    assert client.post("/flash/events").status_code == 204
    assert client.post("/flash/events", json=["not", "an", "object"]).status_code == 422
    assert [p for _, p in sent.calls] == [{"event": "firmware_flash_completed"}]


def test_webhook_failure_never_reaches_the_flasher(client, seeded_settings, monkeypatch):
    seeded_settings.flash_webhook_url = "https://hooks.example/flash"

    def boom(url, json=None, timeout=None):
        raise httpx.ConnectError("down", request=httpx.Request("POST", url))

    monkeypatch.setattr(flash.httpx, "post", boom)
    assert client.post("/flash/events", json=BODY).status_code == 204


def test_cross_origin_preflight_allows_the_flasher_post(client):
    res = client.options(
        "/flash/events",
        headers={
            "Origin": "https://tesserae.ink",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert res.status_code == 200
    assert "POST" in res.headers["access-control-allow-methods"]
