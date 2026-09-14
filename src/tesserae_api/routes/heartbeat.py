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
from packaging.version import InvalidVersion, Version

from tesserae_api import blocklist
from tesserae_api.config import Settings, get_settings
from tesserae_api.stats import collector, geo

router = APIRouter(tags=["heartbeat"])

_CHANNEL = {"stable", "main", "edge"}
_OS = {"linux", "macos", "windows", "other"}
_ARCH = {"x86_64", "arm64", "arm", "other"}
_DEPLOY = {"docker", "ha_addon", "pip", "lxc", "source", "unknown"}
_TRANSPORT = {"mqtt", "rest", "both", "none", "unknown"}
# Shared bucket vocabulary for count fields (devices, companion, lineups, ota_*).
_BUCKETS = {"0", "1", "2-3", "4-9", "10+"}
# Install age in days since the id was minted.
_AGE_BUCKETS = {"0", "1-7", "8-30", "31-90", "90+"}
# Device wake cadence.
_SLEEP_BUCKETS = {"<5m", "5-15m", "15-60m", "1-6h", "6h+", "always_on"}
_GAMUT_RE = re.compile(r"^[a-z0-9_]{1,24}$")
_RES_RE = re.compile(r"^\d{2,5}x\d{2,5}$")
_FEATURES = ("relay", "touch", "mcp", "quiet_hours")
_OTA = ("offered", "applied", "failed")
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


def _bucket(value: Any) -> str | None:
    """Coerce a count field to the bucket vocabulary; absent stays None."""
    text = _str(value)
    if text is None:
        return None
    return text if text in _BUCKETS else "unknown"


def _ha(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _age(value: Any) -> str | None:
    text = _str(value)
    if text is None:
        return None
    return text if text in _AGE_BUCKETS else "unknown"


def _features(value: Any) -> dict[str, bool | None]:
    """Coerce the feature booleans; absent object or key stays None."""
    if not isinstance(value, dict):
        return {}
    return {
        name: (None if value.get(name) is None else bool(value.get(name))) for name in _FEATURES
    }


def _ota(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        return {}
    return {name: _bucket(value.get(name)) for name in _OTA}


def _clean_gamut(value: Any) -> str | None:
    text = _str(value)
    if text is None:
        return None
    return text if _GAMUT_RE.match(text) else "unknown"


def _clean_res(value: Any) -> str | None:
    text = _str(value)
    if text is None:
        return None
    return text if _RES_RE.match(text) else "unknown"


def _clean_sleep(value: Any) -> str | None:
    text = _str(value)
    if text is None:
        return None
    return text if text in _SLEEP_BUCKETS else "unknown"


def _clean_fw(value: Any) -> str | None:
    """Normalise a firmware version: bare semver, one optional leading v. Else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not text or len(text) > 40:
        return None
    try:
        Version(text)
    except (InvalidVersion, TypeError):
        return None
    return text


def _clean_kinds(value: Any, fw_by_kind: Any = None) -> list[dict[str, Any]]:
    """Parse the per-kind list into ``{kind, fw_version, gamut, res, sleep}`` dicts.

    Each item may be a bare slug string or an object with ``kind`` plus any of
    ``fw_version`` / ``gamut`` / ``res`` / ``sleep``. Keeps valid slugs only,
    dedupes on kind (order-preserving), caps at 32, and normalises every
    descriptive field (non-semver firmware becomes None, unknown vocab becomes
    ``unknown``).

    ``fw_by_kind`` is the legacy ``{kind: [versions]}`` map that app builds
    before 0.415 sent next to a bare-string ``device_kinds`` list. It was never
    read, so every install's firmware landed as unknown; the newest listed
    version now fills a bare item's ``fw_version``.
    """
    if not isinstance(value, list):
        return []
    legacy: dict[str, str] = {}
    if isinstance(fw_by_kind, dict):
        for kind, versions in fw_by_kind.items():
            if isinstance(kind, str) and isinstance(versions, list):
                cleaned = [v for v in (_clean_fw(x) for x in versions) if v]
                if cleaned:
                    legacy[kind] = _newest(cleaned)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            kind, fields = item, {"fw_version": legacy.get(item)}
        elif isinstance(item, dict):
            kind = item.get("kind")
            fields = {
                "fw_version": _clean_fw(item.get("fw_version"))
                or (legacy.get(kind) if isinstance(kind, str) else None),
                "gamut": _clean_gamut(item.get("gamut")),
                "res": _clean_res(item.get("res")),
                "sleep": _clean_sleep(item.get("sleep")),
            }
        else:
            continue
        if not isinstance(kind, str) or kind in seen or not _KIND_RE.match(kind):
            continue
        seen.add(kind)
        out.append({"kind": kind, **fields})
        if len(out) >= _MAX_KINDS:
            break
    return out


def _newest(versions: list[str]) -> str:
    """The highest semver in a list of already-cleaned versions."""
    return max(versions, key=Version)


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
            devices=_bucket(data.get("devices")),
            companion=_bucket(data.get("companion")),
            ha=_ha(data.get("ha")),
            country=country,
            region=region,
            # ``kinds`` (0.415+) carries the per-kind objects; older builds send a
            # bare ``device_kinds`` list plus the ``fw_by_kind`` map.
            device_kinds=_clean_kinds(
                data.get("kinds")
                if isinstance(data.get("kinds"), list)
                else data.get("device_kinds"),
                data.get("fw_by_kind"),
            ),
            age=_age(data.get("age")),
            lineups=_bucket(data.get("lineups")),
            features=_features(data.get("features")),
            ota=_ota(data.get("ota")),
        )
    except Exception:
        # Stats collection must never break the response the app depends on.
        pass

    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
