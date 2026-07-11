"""Version blocklist for recording endpoints.

A caller reporting a blocked version (a non-release build, for example an errant
test harness that is meant to no-op the API, or a stale dev build) is not
recorded and is handed a notice telling it to stop or update. This keeps that
traffic out of the stats tables at the source, without a heuristic delete.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from tesserae_api.config import Settings

NOTICE = (
    "Build {version} is not a released Tesserae version, so this request was not "
    "recorded. If this is a test harness it is hitting the live API: set it to "
    "no-op. Otherwise pull the latest from https://github.com/dmellok/tesserae"
)


def is_blocked(version: str | None, settings: Settings) -> bool:
    if not version:
        return False
    return any(version.startswith(prefix) for prefix in settings.blocked_version_prefixes)


def blocked_response(version: str | None) -> JSONResponse:
    message = NOTICE.format(version=version)
    return JSONResponse(
        {"notice": message},
        status_code=200,
        headers={"X-Tesserae-Notice": message, "Cache-Control": "no-store"},
    )
