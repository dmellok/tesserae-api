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

# Discord's per-field value cap. Anything longer is rejected outright, so the
# inputs list is assembled against a budget rather than trimmed afterwards.
_FIELD_LIMIT = 1024
_INPUT_LINE_CAP = 12
_INPUT_LABEL_CAP = 80
# Target slots that mean the input shapes an outbound request. Worth naming in
# the embed: a reviewer approving one of these is approving where a stranger's
# install will talk to, and with what headers.
_REQUEST_SLOTS = {"source_url": "request URL", "source_header": "request header"}


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


def _input_line(spec: Any) -> str:
    """One declared input as a review line. Never raises: an entry that isn't
    the shape we expect still renders as something a human can read."""
    if not isinstance(spec, dict):
        return f"• {str(spec)[:60]} (unreadable entry)"
    name = str(spec.get("name") or "?")[:32]
    facets = [str(spec.get("type") or "?")[:20]]
    if spec.get("secret"):
        facets.append("secret")
    if spec.get("required"):
        facets.append("required")
    targets = spec.get("targets") if isinstance(spec.get("targets"), list) else []
    slots = {t.get("slot") for t in targets if isinstance(t, dict)}
    facets += sorted({_REQUEST_SLOTS[s] for s in slots if s in _REQUEST_SLOTS})
    label = " ".join(str(spec.get("label") or "").split())[:_INPUT_LABEL_CAP]
    mark = "🔒" if spec.get("secret") else "•"
    return f"{mark} {name} ({', '.join(facets)}){f' {label}' if label else ''}"


def _inputs_summary(inputs_json: str | None) -> str:
    """The install questions a template will ask, one per line. A reviewer
    approving a template is approving what it asks a stranger for, so the
    names, types and secret flags belong in the embed rather than a count."""
    try:
        specs = json.loads(inputs_json or "[]")
    except ValueError:
        return "inputs list unreadable"
    if not isinstance(specs, list):
        return "inputs list unreadable"
    if not specs:
        return "none (installs without asking anything)"
    # Leave room for the trailing "…and N more" before spending the budget.
    budget = _FIELD_LIMIT - 32
    lines: list[str] = []
    used = 0
    for spec in specs[:_INPUT_LINE_CAP]:
        line = _input_line(spec)
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    if len(lines) < len(specs):
        lines.append(f"…and {len(specs) - len(lines)} more")
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
            {"name": "Asks the installer for", "value": _inputs_summary(row.get("inputs_spec"))},
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


def report_embed(
    settings: Settings, report: dict[str, Any], submission: dict[str, Any]
) -> dict[str, Any]:
    """The takedown-request embed. Leads with who asked and why; the template's
    own preview rides along so the decision needs no other context."""
    by = (
        "the install that submitted it (author asking for their own takedown)"
        if report.get("by_submitter")
        else "a user of the catalog"
    )
    return {
        "title": f"Takedown requested: {submission.get('title', '?')}",
        "description": (report.get("reason") or "_no reason given_")[:1000],
        "color": 0xB8860B,
        "fields": [
            {"name": "Reported by", "value": by},
            {"name": "Template", "value": str(submission.get("slug") or "?"), "inline": True},
            {"name": "Status", "value": str(submission.get("status") or "?"), "inline": True},
        ],
        "image": {
            "url": f"{settings.public_base_url}/templates/preview/{submission.get('id')}.png"
        },
        "footer": {"text": f"report {report.get('id')} · submission {submission.get('id')}"},
    }


def report_components(report_id: str, submission_id: str) -> list[dict[str, Any]]:
    """Take it down, or dismiss the report and leave the template up."""
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": _STYLE_DANGER,
                    "label": "Take down",
                    "custom_id": f"tpl:revoke:{submission_id}",
                },
                {
                    "type": 2,
                    "style": _STYLE_SECONDARY,
                    "label": "Dismiss report",
                    "custom_id": f"tpl:dismiss:{report_id}",
                },
            ],
        }
    ]


def post_report_message(
    settings: Settings, report: dict[str, Any], submission: dict[str, Any]
) -> str | None:
    """Post a takedown request to the review channel; returns the message id."""
    if not settings.discord_bot_token or not settings.discord_channel_id:
        return None
    response = httpx.post(
        f"{_API}/channels/{settings.discord_channel_id}/messages",
        headers={"Authorization": f"Bot {settings.discord_bot_token}"},
        json={
            "embeds": [report_embed(settings, report, submission)],
            "components": report_components(str(report.get("id")), str(submission.get("id"))),
        },
        timeout=5.0,
    )
    response.raise_for_status()
    return str(response.json().get("id"))


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
