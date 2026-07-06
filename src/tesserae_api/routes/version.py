"""GET /version/latest - channel-aware version check plus aggregate stats."""

from __future__ import annotations

import enum
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from tesserae_api.cache import github_releases
from tesserae_api.config import Settings, get_settings
from tesserae_api.stats import collector, geo

router = APIRouter(tags=["version"])


class Channel(enum.StrEnum):
    stable = "stable"
    main = "main"
    edge = "edge"


def _client_ip(request: Request) -> str | None:
    """Resolve the caller IP from proxy headers. Used only for geo, then discarded."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Left-most entry is the original client.
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


def _resolve(cache: dict[str, Any], channel: Channel, current: str | None) -> dict[str, Any]:
    if channel is Channel.main:
        return github_releases.resolve_main(cache, current)
    if channel is Channel.edge:
        return github_releases.resolve_edge(cache, current)
    return github_releases.resolve_stable(cache, current)


@router.get("/version/latest")
def version_latest(
    request: Request,
    channel: Channel = Query(default=Channel.stable),
    current: str | None = Query(default=None),
    install: str | None = Query(default=None),
) -> Response:
    settings: Settings = get_settings()
    cache = github_releases.load_cache(settings.version_cache_path)
    if cache is None:
        return JSONResponse(
            {"detail": "version data not yet available"},
            status_code=503,
            headers={"Cache-Control": "no-store", "Retry-After": "60"},
        )

    body = _resolve(cache, channel, current)

    # Geo lookup happens on the IP, then the IP is dropped. It is never stored.
    ip = _client_ip(request)
    country, region = geo.lookup(ip, settings.geoip_db_path)
    del ip

    try:
        collector.record_hit(
            settings.stats_db_path,
            install_uuid=_normalise_install(install),
            country=country,
            region=region,
            channel=channel.value,
            current_version=current,
        )
    except Exception:
        # Stats collection must never break the response the widget depends on.
        pass

    return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})
