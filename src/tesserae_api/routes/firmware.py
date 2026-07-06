"""GET /firmware/{kind}/latest - per-kind firmware update check plus aggregate stats."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from tesserae_api.cache import firmware
from tesserae_api.config import Settings, get_settings
from tesserae_api.stats import collector, geo

router = APIRouter(tags=["firmware"])


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


@router.get("/firmware/{kind}/latest")
def firmware_latest(
    request: Request,
    kind: str,
    current: str | None = Query(default=None),
    install: str | None = Query(default=None),
) -> JSONResponse:
    settings: Settings = get_settings()

    sources = firmware.load_sources(settings.firmware_sources_path)
    if kind not in sources:
        raise HTTPException(status_code=404, detail=f"unknown device kind: {kind}")

    cache = firmware.load_cache(settings.firmware_cache_path) or {}
    body = firmware.resolve(cache, kind, current)
    if body is None:
        # Kind is configured but has no cached release yet (cold start).
        return JSONResponse(
            {"detail": "firmware data not yet available"},
            status_code=503,
            headers={"Cache-Control": "no-store", "Retry-After": "60"},
        )

    # Geo lookup happens on the IP, then the IP is dropped. It is never stored.
    ip = _client_ip(request)
    country, region = geo.lookup(ip, settings.geoip_db_path)
    del ip

    try:
        collector.record_hit(
            settings.resolved_database_url,
            install_uuid=_normalise_install(install),
            country=country,
            region=region,
            kind=kind,
            current_version=current,
        )
    except Exception:
        # Stats collection must never break the response the device depends on.
        pass

    return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})
