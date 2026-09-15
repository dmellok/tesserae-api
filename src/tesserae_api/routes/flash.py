"""POST /flash/events - a completed flash from the web flasher (browser-to-server).

The web flasher at tesserae.ink/flash calls this once a firmware write finishes,
so the maintainer can react live (the reference deployment rings a desk bell
over MQTT). Nothing is stored: the body is reduced to a small allow-listed set
of device facts and forwarded, best-effort and off the request path, to the
webhook named by ``TESSERAE_FLASH_WEBHOOK_URL``. With no webhook configured the
endpoint is a no-op that still answers 204, so the flasher never has to care
whether anyone is listening. No IP, no User-Agent, no install id: the payload
says what was flashed, never who flashed it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Body, Response, status

from tesserae_api.config import Settings, get_settings

log = logging.getLogger("tesserae_api.flash")

router = APIRouter(tags=["flash"])

# The only body fields that are forwarded, and the longest value kept for each.
# Anything else in the body is dropped before it leaves the process.
_FIELDS: dict[str, int] = {
    "device": 96,
    "vendor": 48,
    "model": 64,
    "env": 64,
    "firmware_version": 32,
    "chip": 24,
    "mode": 16,
    "method": 24,
    "factory_reset": 5,
}
_WEBHOOK_TIMEOUT = 5.0


def _clean(value: Any, limit: int) -> str | None:
    """A present string, whitespace-collapsed and clipped; anything else -> None."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:limit]
    return text or None


def build_event(body: Any) -> dict[str, str]:
    """Reduce an arbitrary request body to the forwarded event."""
    data = body if isinstance(body, dict) else {}
    event: dict[str, str] = {"event": "firmware_flash_completed"}
    for name, limit in _FIELDS.items():
        text = _clean(data.get(name), limit)
        if text is not None:
            event[name] = text
    return event


def forward(settings: Settings, event: dict[str, str]) -> None:
    """Deliver one event to the configured webhook. Best-effort: a failure is
    logged and otherwise ignored; the flasher already got its 204."""
    url = settings.flash_webhook_url.strip()
    if not url:
        return
    try:
        response = httpx.post(url, json=event, timeout=_WEBHOOK_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("flash webhook delivery failed: %s", exc)


@router.post("/flash/events", status_code=status.HTTP_204_NO_CONTENT)
def flash_event(
    background: BackgroundTasks, body: dict[str, Any] | None = Body(default=None)
) -> Response:
    settings: Settings = get_settings()
    event = build_event(body)
    if settings.flash_webhook_url.strip():
        background.add_task(forward, settings, event)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
