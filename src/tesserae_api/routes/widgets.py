"""Widget install counting.

POST /widgets/install   record one widget-install event (server-to-server)
GET  /widgets/installs  unique install counts per widget (for the Browse UI)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tesserae_api import blocklist
from tesserae_api.config import Settings, get_settings
from tesserae_api.stats import collector, geo

router = APIRouter(tags=["widgets"])


class WidgetInstallBody(BaseModel):
    widget: str | None = None
    install: str | None = None
    version: str | None = None


def _client_ip(request: Request) -> str | None:
    """Resolve the caller IP from proxy headers. Used only for geo, then discarded."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip() or None
    return request.client.host if request.client else None


def _normalise_install(install: str | None) -> str | None:
    """Accept a client-generated UUID as opaque. Invalid or absent -> None (no dedup)."""
    if not install:
        return None
    try:
        return str(uuid.UUID(install))
    except (ValueError, AttributeError):
        return None


@router.post("/widgets/install", status_code=status.HTTP_204_NO_CONTENT)
def widget_install(request: Request, body: WidgetInstallBody) -> Response:
    settings: Settings = get_settings()

    if blocklist.is_blocked(body.version, settings):
        return blocklist.blocked_response(body.version)

    widget = (body.widget or "").strip()
    if not widget:
        raise HTTPException(status_code=400, detail="widget is required")

    # Geo lookup happens on the IP, then the IP is dropped. It is never stored.
    ip = _client_ip(request)
    country, region = geo.lookup(ip, settings.geoip_db_path)
    del ip

    try:
        collector.record_widget_install(
            settings.resolved_database_url,
            widget_id=widget,
            install_uuid=_normalise_install(body.install),
            tesserae_version=body.version,
            country=country,
            region=region,
        )
    except Exception:
        # Stats collection must never break the response the app depends on.
        pass

    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


@router.get("/widgets/installs")
def widget_installs(widget: str | None = Query(default=None)) -> JSONResponse:
    settings: Settings = get_settings()
    counts = collector.widget_install_counts(settings.resolved_database_url, widget_id=widget)
    headers = {"Cache-Control": "public, max-age=300"}
    if widget is not None:
        return JSONResponse({"widget": widget, "count": counts.get(widget, 0)}, headers=headers)
    return JSONResponse({"counts": counts}, headers=headers)
