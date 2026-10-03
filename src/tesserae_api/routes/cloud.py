"""POST /cloud/waitlist - ask to hear about Tesserae Cloud (browser-to-server).

The tesserae.ink homepage carries a sign-up block for Tesserae Cloud, the
separate hosted Tesserae at cloud.tesserae.ink that opens to the waitlist in
rounds. This is how that gets counted. The body's email is stored (lowercased, once); the
panel picked on the flasher at the time, the site that referred the visitor
(host and path, sent by the page) and a coarse country ride along so the
demand can be read by device, region and channel. Nothing else about the
request is kept: no IP, no User-Agent. ``/cloud/waitlist/leave`` takes the address off
again.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response, status

from tesserae_api import waitlist
from tesserae_api.config import Settings, get_settings
from tesserae_api.routes.heartbeat import _client_ip
from tesserae_api.stats import geo

log = logging.getLogger("tesserae_api.cloud")

router = APIRouter(tags=["cloud"])

_NO_STORE = {"Cache-Control": "no-store"}


def _email_or_422(body: Any) -> str:
    data = body if isinstance(body, dict) else {}
    email = waitlist.normalise_email(data.get("email"))
    if email is None:
        raise HTTPException(
            status_code=422,
            detail="email: a plausible address is required",
        )
    return email


@router.post("/cloud/waitlist")
def join_waitlist(request: Request, body: dict[str, Any] | None = Body(default=None)) -> Response:
    settings: Settings = get_settings()
    email = _email_or_422(body)
    fields = waitlist.describe(body)
    country, _region = geo.lookup(_client_ip(request), settings.geoip_db_path)
    added = waitlist.add(settings.resolved_database_url, email=email, country=country, **fields)
    if added:
        log.info("cloud waitlist: +1 (%s, %s)", fields.get("env") or "no panel", country or "??")
    # The answer is the same whether the address was new or already listed, so
    # the endpoint cannot be used to check whether someone signed up.
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_NO_STORE)


@router.post("/cloud/waitlist/leave")
def leave_waitlist(body: dict[str, Any] | None = Body(default=None)) -> Response:
    settings: Settings = get_settings()
    email = _email_or_422(body)
    waitlist.remove(settings.resolved_database_url, email=email)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_NO_STORE)
