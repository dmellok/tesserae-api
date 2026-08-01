"""Discord interactions: signature verification and the moderation state machine.

Payloads are signed with a real Ed25519 keypair generated per test session, so
the verification path runs exactly as in production; there is no bypass flag.
"""

from __future__ import annotations

import base64
import json
import time
import uuid

import pytest
from nacl.signing import SigningKey

from tesserae_api import templates_store

_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 24).decode()
_REVIEWER = "111222333"


@pytest.fixture(scope="module")
def keypair() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def discord_client_fixture(client, seeded_settings, keypair):
    """The shared TestClient with Discord verification + reviewer configured."""
    seeded_settings.discord_public_key = keypair.verify_key.encode().hex()
    seeded_settings.discord_reviewer_ids = _REVIEWER
    return client


def _signed_post(client, keypair: SigningKey, payload: dict, *, corrupt: bool = False):
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = keypair.sign(timestamp.encode() + body).signature.hex()
    if corrupt:
        signature = ("0" if signature[0] != "0" else "1") + signature[1:]
    return client.post(
        "/discord/interactions",
        content=body,
        headers={
            "content-type": "application/json",
            "x-signature-ed25519": signature,
            "x-signature-timestamp": timestamp,
        },
    )


def _component(custom_id: str, user_id: str = _REVIEWER) -> dict:
    return {
        "type": 3,
        "member": {"user": {"id": user_id}},
        "data": {"custom_id": custom_id},
    }


def _pending_submission(client) -> dict:
    resp = client.post(
        "/templates/submit",
        json={
            "template": {
                "schema_version": 1,
                "title": "Reviewed Board",
                "description": "",
                "tags": [],
                "requires": [],
                "inputs": [],
                "canvas": {
                    "w": 800,
                    "h": 480,
                    "els": [{"id": "e1", "kind": "text", "text": "hi"}],
                },
            },
            "preview_png_b64": _PNG_B64,
            "install": str(uuid.uuid4()),
            "version": "0.217.0",
        },
    )
    assert resp.status_code == 202
    return resp.json()


def test_ping_pong(discord_client_fixture, keypair):
    resp = _signed_post(discord_client_fixture, keypair, {"type": 1})
    assert resp.status_code == 200 and resp.json() == {"type": 1}


def test_bad_signature_is_401(discord_client_fixture, keypair):
    assert (
        _signed_post(discord_client_fixture, keypair, {"type": 1}, corrupt=True).status_code == 401
    )


def test_unconfigured_public_key_is_401(client, keypair, seeded_settings):
    seeded_settings.discord_public_key = ""
    assert _signed_post(client, keypair, {"type": 1}).status_code == 401


def test_non_reviewer_gets_ephemeral_denial(discord_client_fixture, keypair, seeded_settings):
    sub = _pending_submission(discord_client_fixture)
    resp = _signed_post(
        discord_client_fixture,
        keypair,
        _component(f"tpl:approve:{sub['id']}", user_id="999"),
    )
    body = resp.json()
    assert body["type"] == 4 and body["data"]["flags"] == 64
    row = templates_store.get_submission(seeded_settings.resolved_database_url, sub["id"])
    assert row["status"] == "pending"  # unchanged


def test_approve_flips_status_and_edits_message(discord_client_fixture, keypair, seeded_settings):
    sub = _pending_submission(discord_client_fixture)
    resp = _signed_post(discord_client_fixture, keypair, _component(f"tpl:approve:{sub['id']}"))
    body = resp.json()
    assert body["type"] == 7  # UPDATE_MESSAGE: the response IS the edit
    assert "approved" in body["data"]["embeds"][0]["title"]
    # Approved messages keep a Revoke button.
    assert body["data"]["components"][0]["components"][0]["custom_id"] == f"tpl:revoke:{sub['id']}"
    row = templates_store.get_submission(seeded_settings.resolved_database_url, sub["id"])
    assert row["status"] == "approved" and row["reviewed_by"] == _REVIEWER


def test_reject_opens_modal_then_stores_reason(discord_client_fixture, keypair, seeded_settings):
    sub = _pending_submission(discord_client_fixture)
    modal = _signed_post(discord_client_fixture, keypair, _component(f"tpl:reject:{sub['id']}"))
    body = modal.json()
    assert body["type"] == 9 and body["data"]["custom_id"] == f"tpl:rejectm:{sub['id']}"

    submit = _signed_post(
        discord_client_fixture,
        keypair,
        {
            "type": 5,
            "member": {"user": {"id": _REVIEWER}},
            "data": {
                "custom_id": f"tpl:rejectm:{sub['id']}",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {"type": 4, "custom_id": "reason", "value": "low contrast on mono"}
                        ],
                    }
                ],
            },
        },
    )
    body = submit.json()
    assert body["type"] == 7 and body["data"]["components"] == []
    row = templates_store.get_submission(seeded_settings.resolved_database_url, sub["id"])
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "low contrast on mono"


def test_revoke_after_approval(discord_client_fixture, keypair, seeded_settings):
    sub = _pending_submission(discord_client_fixture)
    _signed_post(discord_client_fixture, keypair, _component(f"tpl:approve:{sub['id']}"))
    resp = _signed_post(discord_client_fixture, keypair, _component(f"tpl:revoke:{sub['id']}"))
    assert resp.json()["type"] == 7
    row = templates_store.get_submission(seeded_settings.resolved_database_url, sub["id"])
    assert row["status"] == "revoked"


def test_unknown_submission_and_bad_custom_id(discord_client_fixture, keypair):
    resp = _signed_post(discord_client_fixture, keypair, _component("tpl:approve:nope"))
    assert resp.json()["type"] == 4  # ephemeral notice
    resp = _signed_post(discord_client_fixture, keypair, _component("something:else"))
    assert resp.json()["type"] == 4
