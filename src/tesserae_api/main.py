"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tesserae_api.cache import firmware, github_releases
from tesserae_api.config import get_settings
from tesserae_api.routes import firmware as firmware_routes
from tesserae_api.routes import version
from tesserae_api.stats import collector, geo

log = logging.getLogger("tesserae_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    collector.init_db(settings.resolved_database_url)
    # Cold start: if there is no cache yet, try a best-effort poll so the very
    # first request (and the post-deploy smoke test) has data to serve.
    if github_releases.load_cache(settings.version_cache_path) is None:
        try:
            github_releases.poll_and_cache(settings)
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal at boot
            log.warning("initial GitHub poll failed: %s", exc)
    if firmware.load_cache(settings.firmware_cache_path) is None:
        try:
            firmware.poll_and_cache(settings)
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal at boot
            log.warning("initial firmware poll failed: %s", exc)
    yield
    geo.close()
    collector.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Tesserae API",
        version="0.2.0",
        description="Public JSON API for Tesserae widgets.",
        lifespan=lifespan,
    )

    # Widgets fetch from a Chromium browser context, so any origin must be allowed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(version.router)
    app.include_router(firmware_routes.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
