"""POST /heartbeat - daily install heartbeat (server-to-server).

Privacy posture matches the other endpoints: coarse geo from the caller IP then
the IP is discarded, no IP or User-Agent stored, and the write is best-effort. In
addition the server stores only a DAY (never a timestamp) and the write is
idempotent per (install, day), so heartbeat cadence cannot become a sub-daily
activity trace. Bad field values are coerced rather than rejected: the point is
the count.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Body, Request, Response, status

from tesserae_api import blocklist
from tesserae_api.config import Settings, get_settings
from tesserae_api.stats import collector, geo

router = APIRouter(tags=["heartbeat"])

_CHANNEL = {"stable", "main", "edge"}
_OS = {"linux", "macos", "windows", "other"}
_ARCH = {"x86_64", "arm64", "arm", "other"}
_DEPLOY = {"docker", "ha_addon", "pip", "lxc", "source", "unknown"}
_TRANSPORT = {"mqtt", "rest", "both", "none", "unknown"}
_DEVICES = {"0", "1", "2-3", "4-9", "10+"}
_PY_RE = re.compile(r"^3\.\d{1,2}$")
_KIND_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_MAX_KINDS = 32


def _client_ip(request: Request) -> str | None:
    """Resolve the caller IP from proxy headers. Used only for geo, then discarded."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip() or None
    return request.client.host if request.client else None


def _normalise_install(install: Any) -> str | None:
    """Accept a client-generated UUID as opaque. Invalid or absent -> None (no dedup)."""
    if not isinstance(install, str) or not install:
        return None
    try:
        return str(uuid.UUID(install))
    except (ValueError, AttributeError):
        return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _enum(value: Any, allowed: set[str], default: str) -> str | None:
    """Coerce a present value to the allowed set; absent stays None."""
    text = _str(value)
    if text is None:
        return None
    return text if text in allowed else default


def _py(value: Any) -> str | None:
    text = _str(value)
    if text is None:
        return None
    return text if _PY_RE.match(text) else "other"


def _devices(value: Any) -> str | None:
    text = _str(value)
    if text is None:
        return None
    return text if text in _DEVICES else "unknown"


def _ha(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _clean_kinds(value: Any) -> list[str]:
    """Keep valid slugs, dedupe (order-preserving), cap at 32."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str) and item not in seen and _KIND_RE.match(item):
            seen.add(item)
            out.append(item)
            if len(out) >= _MAX_KINDS:
                break
    return out


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def heartbeat(request: Request, body: dict[str, Any] | None = Body(default=None)) -> Response:
    settings: Settings = get_settings()
    data = body or {}

    version = _str(data.get("version"))
    if blocklist.is_blocked(version, settings):
        return blocklist.blocked_response(version)

    # Geo lookup happens on the IP, then the IP is dropped. It is never stored.
    ip = _client_ip(request)
    country, region = geo.lookup(ip, settings.geoip_db_path)
    del ip

    try:
        collector.record_heartbeat(
            settings.resolved_database_url,
            install_uuid=_normalise_install(data.get("install")),
            version=version,
            channel=_enum(data.get("channel"), _CHANNEL, "unknown"),
            os=_enum(data.get("os"), _OS, "other"),
            arch=_enum(data.get("arch"), _ARCH, "other"),
            py=_py(data.get("py")),
            deploy=_enum(data.get("deploy"), _DEPLOY, "unknown"),
            transport=_enum(data.get("transport"), _TRANSPORT, "unknown"),
            devices=_devices(data.get("devices")),
            ha=_ha(data.get("ha")),
            country=country,
            region=region,
            device_kinds=_clean_kinds(data.get("device_kinds")),
        )
    except Exception:
        # Stats collection must never break the response the app depends on.
        pass

    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
