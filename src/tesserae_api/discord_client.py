"""Discord review-channel client (outbound half).

One capability: post a template submission to the private review channel as an
embed with Approve / Reject buttons. Uses the bot token against the plain REST
API; components require an application-owned message, which is why this is a
bot-token post and not an ordinary channel webhook. No gateway connection, no
long-lived process. The inbound half (button presses) is
``routes/discord.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from tesserae_api.config import Settings

_API = "https://discord.com/api/v10"

# Discord button styles.
_STYLE_SUCCESS = 3
_STYLE_DANGER = 4
_STYLE_SECONDARY = 2


def _lint_summary(lint_json: str | None) -> str:
    try:
        lint = json.loads(lint_json or "{}")
    except ValueError:
        return "lint report unreadable"
    warnings = lint.get("warnings") or []
    if not warnings:
        return "clean"
    lines = [f"{w.get('rule')}: {w.get('where')} ({w.get('match')})" for w in warnings[:8]]
    if len(warnings) > 8:
        lines.append(f"…and {len(warnings) - 8} more")
    return "\n".join(lines)


def submission_embed(settings: Settings, row: dict[str, Any]) -> dict[str, Any]:
    """The review embed for a submission row (pure; unit-testable)."""
    tags = ", ".join(json.loads(row.get("tags") or "[]")) or "none"
    requires = ", ".join(json.loads(row.get("requires") or "[]")) or "none"
    return {
        "title": f"Template submission: {row.get('title', '?')}",
        "description": (row.get("description") or "")[:1000],
        "fields": [
            {"name": "Size", "value": f"{row.get('w')}x{row.get('h')}", "inline": True},
            {"name": "Inputs", "value": str(row.get("inputs_count") or 0), "inline": True},
            {"name": "App version", "value": row.get("tesserae_version") or "?", "inline": True},
            {"name": "Tags", "value": tags, "inline": True},
            {"name": "Requires", "value": requires, "inline": True},
            {"name": "Lint", "value": _lint_summary(row.get("lint"))[:1024]},
        ],
        "image": {"url": f"{settings.public_base_url}/templates/preview/{row.get('id')}.png"},
        "footer": {"text": f"id {row.get('id')} · slug {row.get('slug')}"},
    }


def review_components(submission_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": 1,  # action row
            "components": [
                {
                    "type": 2,  # button
                    "style": _STYLE_SUCCESS,
                    "label": "Approve",
                    "custom_id": f"tpl:approve:{submission_id}",
                },
                {
                    "type": 2,
                    "style": _STYLE_DANGER,
                    "label": "Reject",
                    "custom_id": f"tpl:reject:{submission_id}",
                },
            ],
        }
    ]


def outcome_embed(row: dict[str, Any], outcome: str, by: str, reason: str = "") -> dict[str, Any]:
    """The embed a review decision replaces the pending message with."""
    colors = {"approved": 0x2E8B57, "rejected": 0xB22222, "revoked": 0x777777}
    embed = {
        "title": f"Template {outcome}: {row.get('title', '?')}",
        "description": f"{outcome.capitalize()} by <@{by}>" + (f"\n> {reason}" if reason else ""),
        "color": colors.get(outcome, 0x777777),
        "footer": {"text": f"id {row.get('id')} · slug {row.get('slug')}"},
    }
    return embed


def outcome_components(submission_id: str, outcome: str) -> list[dict[str, Any]]:
    """Approved messages keep a Revoke button; terminal states keep none."""
    if outcome != "approved":
        return []
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": _STYLE_SECONDARY,
                    "label": "Revoke",
                    "custom_id": f"tpl:revoke:{submission_id}",
                }
            ],
        }
    ]


def post_submission_message(settings: Settings, row: dict[str, Any]) -> str | None:
    """Post the review message; returns the Discord message id (stored so the
    repair timer knows the row was announced). Raises on transport errors; the
    caller treats posting as best-effort."""
    if not settings.discord_bot_token or not settings.discord_channel_id:
        return None
    response = httpx.post(
        f"{_API}/channels/{settings.discord_channel_id}/messages",
        headers={"Authorization": f"Bot {settings.discord_bot_token}"},
        json={
            "embeds": [submission_embed(settings, row)],
            "components": review_components(str(row.get("id"))),
        },
        timeout=5.0,
    )
    response.raise_for_status()
    return str(response.json().get("id"))
