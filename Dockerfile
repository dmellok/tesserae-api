# syntax=docker/dockerfile:1

# Build stage: resolve dependencies into a self-contained virtualenv with uv.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies first (cached unless the lockfile changes).
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install the project itself.
COPY src ./src
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# Runtime stage: slim image with only the venv and app code.
FROM python:3.12-slim-bookworm AS runtime

# The GeoLite2 database is downloaded in CI to ./data/geoip.mmdb and baked in
# below. Copying the whole data/ directory keeps a plain local `docker build`
# (no mmdb present) working; the app then degrades gracefully with no geo data.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TESSERAE_DATA_DIR=/data \
    TESSERAE_GEOIP_PATH=/app/geoip/geoip.mmdb

WORKDIR /app

# Pin the app UID/GID so the host /data bind mount can be chowned to a known
# owner (999). Without a stable id the /data volume permissions break on rebuild.
RUN groupadd --system --gid 999 app \
    && useradd --system --uid 999 --gid 999 --home-dir /app app

COPY --from=build /app/.venv /app/.venv
COPY src ./src
COPY scripts ./scripts

# Bake the GeoLite2 database in at a fixed path OUTSIDE the /data volume so a
# weekly image rebuild refreshes it without disturbing persistent state. The
# data/ dir always carries a .gitkeep, so this COPY never fails when the mmdb
# is absent (local builds); geoip.mmdb lands here when CI has downloaded it.
COPY data/ /app/geoip/
RUN mkdir -p /data /app/geoip && chown -R app:app /data /app

USER app
EXPOSE 8000

# uvicorn binds all interfaces inside the container; docker-compose publishes it
# only on the host loopback (127.0.0.1:8000) for Caddy to reverse-proxy.
CMD ["uvicorn", "tesserae_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
