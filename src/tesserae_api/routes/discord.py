"""Discord interactions endpoint (inbound half of template review).

POST /discord/interactions receives button presses and modal submits from the
review channel. Discord signs every request with the application's Ed25519
key; verification failure MUST return 401 (Discord probes the endpoint with
deliberately bad signatures when it is registered).

Flow: Approve/Reject buttons ride ``custom_id`` ``tpl:<action>:<submission_id>``.
Approve flips the row and answers with type 7 (UPDATE_MESSAGE), so the HTTP
response itself rewrites the review message; no outbound call is needed.
Reject opens a modal (type 9) for an optional reason; the modal submit stores
the reason and rewrites the message the same way. Approved messages carry a
Revoke button for takedowns. Only configured reviewer ids may press anything;
everyone else gets an ephemeral denial.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from tesserae_api import discord_client, templates_store
from tesserae_api.config import Settings, get_settings

router = APIRouter(tags=["discord"])

# Interaction request types.
_PING = 1
_MESSAGE_COMPONENT = 3
_MODAL_SUBMIT = 5

# Interaction response types.
_PONG = 1
_CHANNEL_MESSAGE = 4
_UPDATE_MESSAGE = 7
_MODAL = 9

_EPHEMERAL = 64  # message flag


def _verify(settings: Settings, request_body: bytes, signature: str, timestamp: str) -> bool:
    if not settings.discord_public_key:
        return False
    try:
        key = VerifyKey(bytes.fromhex(settings.discord_public_key))
        key.verify(timestamp.encode() + request_body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


def _ephemeral(text: str) -> JSONResponse:
    return JSONResponse({"type": _CHANNEL_MESSAGE, "data": {"content": text, "flags": _EPHEMERAL}})


def _actor_id(payload: dict[str, Any]) -> str:
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    return str(user.get("id") or "")


def _update_message(row: dict[str, Any], outcome: str, by: str, reason: str = "") -> JSONResponse:
    return JSONResponse(
        {
            "type": _UPDATE_MESSAGE,
            "data": {
                "embeds": [discord_client.outcome_embed(row, outcome, by, reason)],
                "components": discord_client.outcome_components(str(row.get("id")), outcome),
            },
        }
    )


@router.post("/discord/interactions")
async def interactions(request: Request) -> JSONResponse:
    settings: Settings = get_settings()
    body = await request.body()
    signature = request.headers.get("x-signature-ed25519", "")
    timestamp = request.headers.get("x-signature-timestamp", "")
    if not _verify(settings, body, signature, timestamp):
        raise HTTPException(status_code=401, detail="invalid request signature")

    payload = json.loads(body)
    itype = payload.get("type")

    if itype == _PING:
        return JSONResponse({"type": _PONG})

    actor = _actor_id(payload)
    if actor not in settings.discord_reviewer_id_set:
        return _ephemeral("You aren't on the reviewer list for template submissions.")

    data = payload.get("data") or {}
    custom_id = str(data.get("custom_id") or "")
    parts = custom_id.split(":")
    if len(parts) != 3 or parts[0] != "tpl":
        return _ephemeral("Unrecognised interaction.")
    action, target_id = parts[1], parts[2]
    url = settings.resolved_database_url

    # "dismiss" addresses a report row; every other action addresses a
    # submission. Resolve accordingly before the shared handling below.
    if action == "dismiss":
        report = templates_store.get_report(url, target_id)
        if report is None:
            return _ephemeral(f"Unknown report {target_id}.")
        submission = templates_store.get_submission(url, str(report["submission_id"])) or {}
        templates_store.set_report_status(url, target_id, "dismissed", handled_by=actor)
        return JSONResponse(
            {
                "type": _UPDATE_MESSAGE,
                "data": {
                    "embeds": [
                        {
                            "title": f"Report dismissed: {submission.get('title', '?')}",
                            "description": f"Dismissed by <@{actor}>; the template stays up.",
                            "color": 0x777777,
                            "footer": {"text": f"report {target_id}"},
                        }
                    ],
                    "components": [],
                },
            }
        )

    submission_id = target_id
    row = templates_store.get_submission(url, submission_id)
    if row is None:
        return _ephemeral(f"Unknown submission {submission_id}.")

    if itype == _MESSAGE_COMPONENT:
        if action == "approve":
            templates_store.set_status(url, submission_id, "approved", reviewed_by=actor)
            return _update_message(row, "approved", actor)
        if action == "revoke":
            templates_store.set_status(url, submission_id, "revoked", reviewed_by=actor)
            # Clear any open reports on it: acting from either message (the
            # original review post or a takedown request) settles both.
            templates_store.close_reports_for_submission(url, submission_id, handled_by=actor)
            return _update_message(row, "revoked", actor)
        if action == "reject":
            return JSONResponse(
                {
                    "type": _MODAL,
                    "data": {
                        "custom_id": f"tpl:rejectm:{submission_id}",
                        "title": "Reject template",
                        "components": [
                            {
                                "type": 1,
                                "components": [
                                    {
                                        "type": 4,  # text input
                                        "custom_id": "reason",
                                        "style": 2,  # paragraph
                                        "label": "Reason (optional)",
                                        "required": False,
                                        "max_length": 300,
                                    }
                                ],
                            }
                        ],
                    },
                }
            )
        return _ephemeral(f"Unknown action {action}.")

    if itype == _MODAL_SUBMIT and action == "rejectm":
        reason = ""
        for outer_row in data.get("components") or []:
            for component in outer_row.get("components") or []:
                if component.get("custom_id") == "reason":
                    reason = str(component.get("value") or "")
        templates_store.set_status(
            url, submission_id, "rejected", reviewed_by=actor, reject_reason=reason or None
        )
        return _update_message(row, "rejected", actor, reason)

    return _ephemeral("Unrecognised interaction.")
