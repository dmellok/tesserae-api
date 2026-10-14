"""GET /inat/v1/{path} - a narrow proxy to iNaturalist's public API.

Tesserae Cloud runs on Cloudflare Workers, whose outbound addresses are shared
with every other Worker on the platform, and iNaturalist rate-limits by
address, so from the cloud every request to api.inaturalist.org answers 429.
This machine's address is ours alone, so the cloud asks here instead.

Only a handful of read-only paths are forwarded, only with the shared key in
``X-Tesserae-Proxy-Key`` (``TESSERAE_INAT_PROXY_KEY``; unset disables the
route), the query is passed through as given, every answer is cached for a
while so many panels asking the same question cost iNaturalist one call, and
upstream calls are spaced at least a second apart, which is what iNaturalist
asks of a client. Nothing about the caller is sent upstream or stored.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from tesserae_api.config import Settings, get_settings

router = APIRouter(tags=["inat"])

USER_AGENT = "tesserae-api (+https://tesserae.ink; hello@tesserae.ink)"
KEY_HEADER = "x-tesserae-proxy-key"
# The paths the cloud's widgets and art library read; nothing else is forwarded.
ALLOWED = re.compile(r"^(observations|observations/species_counts|taxa|taxa/\d+)$")
# A query is passed through as given, but kept within reason.
QUERY_MAX_CHARS = 2000
RESPONSE_MAX_BYTES = 5_000_000
CACHE_MAX_ENTRIES = 500
# iNaturalist asks for about one request a second from a client, averaged: a
# few calls may be in flight at once, each started a short gap after the last,
# so a render's two questions are not queued behind the art library's lookups
# for longer than the cloud waits on a source (eight seconds).
MIN_INTERVAL_S = 0.35
MAX_IN_FLIGHT = 3

_cache: OrderedDict[str, tuple[float, int, bytes, str]] = OrderedDict()
_slots = asyncio.Semaphore(MAX_IN_FLIGHT)
_pace = asyncio.Lock()
_last_call = 0.0


async def _upstream_get(url: str, timeout: float) -> httpx.Response:
    """One GET to iNaturalist; a seam for tests."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )


def _cached(key: str, now: float) -> tuple[int, bytes, str] | None:
    hit = _cache.get(key)
    if not hit:
        return None
    expires, status, body, ctype = hit
    if expires < now:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return status, body, ctype


def _remember(key: str, expires: float, status: int, body: bytes, ctype: str) -> None:
    _cache[key] = (expires, status, body, ctype)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def reset_cache() -> None:
    """For tests."""
    global _last_call
    _cache.clear()
    _last_call = 0.0


@router.get("/inat/v1/{path:path}")
async def inat_proxy(
    path: str,
    request: Request,
    x_tesserae_proxy_key: str | None = Header(default=None, alias="X-Tesserae-Proxy-Key"),
) -> Response:
    settings: Settings = get_settings()
    if not settings.inat_proxy_key:
        raise HTTPException(status_code=404, detail="iNaturalist proxy is not enabled")
    if not x_tesserae_proxy_key or x_tesserae_proxy_key != settings.inat_proxy_key:
        raise HTTPException(status_code=403, detail="missing or wrong proxy key")
    if not ALLOWED.fullmatch(path):
        raise HTTPException(status_code=404, detail="that iNaturalist path is not proxied")
    query = request.url.query
    if len(query) > QUERY_MAX_CHARS:
        raise HTTPException(status_code=414, detail="query too long")
    url = f"{settings.inat_upstream.rstrip('/')}/{path}" + (f"?{query}" if query else "")
    now = time.monotonic()
    hit = _cached(url, now)
    if hit:
        status, body, ctype = hit
        return Response(
            content=body,
            status_code=status,
            media_type=ctype,
            headers={
                "X-Inat-Cache": "HIT",
                "Cache-Control": f"public, max-age={settings.inat_cache_seconds}",
            },
        )

    global _last_call
    async with _slots:
        # a few in flight at once, each started a short gap after the last
        async with _pace:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_call = time.monotonic()
        hit = _cached(url, time.monotonic())  # another caller may have filled it while we waited
        if hit:
            status, body, ctype = hit
            return Response(
                content=body, status_code=status, media_type=ctype, headers={"X-Inat-Cache": "HIT"}
            )
        try:
            resp = await _upstream_get(url, settings.inat_timeout_seconds)
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"detail": f"iNaturalist unreachable: {type(exc).__name__}"},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
    body = resp.content
    ctype = (
        resp.headers.get("content-type", "application/json").split(";")[0].strip()
        or "application/json"
    )
    if resp.status_code == 429:
        retry = resp.headers.get("retry-after", "60")
        return JSONResponse(
            {"detail": "iNaturalist is rate limiting"},
            status_code=429,
            headers={"Retry-After": retry, "Cache-Control": "no-store"},
        )
    if resp.status_code >= 500 or resp.status_code in (301, 302, 303, 307, 308):
        return JSONResponse(
            {"detail": f"iNaturalist answered {resp.status_code}"},
            status_code=502,
            headers={"Cache-Control": "no-store"},
        )
    if len(body) > RESPONSE_MAX_BYTES:
        return JSONResponse(
            {"detail": "iNaturalist's answer is too large"},
            status_code=502,
            headers={"Cache-Control": "no-store"},
        )
    # a 200 is kept for the full interval; a 4xx (a bad query) briefly, so a
    # mistaken panel does not hammer upstream
    ttl = settings.inat_cache_seconds if resp.status_code == 200 else 60
    _remember(url, time.monotonic() + ttl, resp.status_code, body, ctype)
    return Response(
        content=body,
        status_code=resp.status_code,
        media_type=ctype,
        headers={"X-Inat-Cache": "MISS", "Cache-Control": f"public, max-age={ttl}"},
    )


def describe() -> dict[str, Any]:
    return {"allowed": ALLOWED.pattern, "cached": len(_cache)}
