"""GET /firmware/{kind}/latest - latest published firmware per device kind.

Returns the newest release of the firmware repo that carries a
descriptor-<kind>.json asset. Unknown kinds (or kinds no release covers) return
404 with an empty body: the client treats any non-2xx as "no data" and hides the
update badge, so 404 is the correct way to say "nothing to report".

Telemetry is aggregate only: counts per (day, kind, reported version, coarse
country). The caller IP is used for the country lookup then discarded; no IP, no
install id, and no per-request row is retained.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response
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


def _normalise_version(current: str | None) -> str | None:
    if not current:
        return None
    text = current.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text[:40] or None


@router.get("/firmware/{kind}/latest")
def firmware_latest(
    request: Request,
    kind: str,
    current: str | None = Query(default=None),
) -> Response:
    settings: Settings = get_settings()

    cache = firmware.load_cache(settings.firmware_cache_path)
    body = firmware.resolve(cache, kind) if cache else None
    if body is None:
        # Unknown kind, uncovered kind, or cold cache: 404, empty body.
        return Response(status_code=404)

    # Aggregate-only telemetry: coarse country from the IP, then drop the IP.
    ip = _client_ip(request)
    country, _region = geo.lookup(ip, settings.geoip_db_path)
    del ip

    try:
        collector.record_firmware_check(
            settings.resolved_database_url,
            kind=kind,
            version=_normalise_version(current),
            country=country,
        )
    except Exception:
        # Telemetry must never break the response the client depends on.
        pass

    return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})
