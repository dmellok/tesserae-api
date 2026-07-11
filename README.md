# tesserae-api

Public JSON API server for [Tesserae](https://github.com/dmellok/tesserae) widgets, served at
`https://api.tesserae.ink`.

v1 ships a single endpoint: a channel-aware version check. The structure is laid out so further
endpoints (widget catalog, almanac, space events, and later multi-user features) slot in as new
routers without a rewrite.

License: AGPL-3.0-or-later.

## Architecture

```
[ Internet ]
     |
     v 443/tcp
[ Caddy (systemd) ]  auto-TLS, per-IP rate limit, access logs with no IP / no User-Agent
     |
     | reverse proxy -> 127.0.0.1:8000
     v
[ tesserae-api container (docker compose) ]
     |-- FastAPI + uvicorn (:8000)
     |-- /data volume  <- version_cache.json
     |-- GeoLite2 mmdb  (baked into the image at build time)
     |
     v  postgresql (container network)
[ postgres container ]  aggregate stats, named volume, 127.0.0.1:5432 (SSH tunnel only)

[ systemd timer: tesserae-api-poll.timer ]  every 15 min
     |
     v
[ docker exec tesserae-api python -m scripts.poll_github ]
     -> refreshes /data/version_cache.json from GitHub
```

The application never calls GitHub on the request path. A systemd timer polls GitHub every 15
minutes and writes `version_cache.json` and `firmware_cache.json`; requests are served entirely
from those caches. If GitHub is unreachable at poll time the previous cache is left untouched, so
the API keeps serving the last known good value.

### Layout

```
src/tesserae_api/
  main.py                  FastAPI app factory
  config.py                settings (paths, repo slug, GitHub token)
  routes/version.py        GET /version/latest
  routes/firmware.py       GET /firmware/{kind}/latest
  cache/github_releases.py version polling, cache read/write, channel resolution
  cache/firmware.py        per-kind firmware polling, cache, resolution
  stats/collector.py       aggregate writes (SQLAlchemy: SQLite dev, Postgres prod)
  stats/geo.py             MaxMind GeoLite2 lookup
firmware_sources.yaml      device kind -> GitHub release source map
scripts/
  poll_github.py           run by the systemd timer (version + firmware)
  dump_stats.py            maintainer's stats reader
```

## Endpoint contract

### `GET /version/latest`

Query parameters:

| Param     | Required | Notes                                                                        |
| --------- | -------- | ---------------------------------------------------------------------------- |
| `channel` | no       | `stable` (default), `main`, or `edge`.                                        |
| `current` | no       | Caller's running version. Stable: SemVer (`0.69.18`). Main: `<ver>+<sha>` or bare `main`. Edge: a pre-release tag. |
| `install` | no       | Client-generated UUID v4, stored opaquely against the stats record. Omit for no cross-day dedup. |

Response headers: `Cache-Control: public, max-age=300` and `Access-Control-Allow-Origin: *`.

**stable / edge** (edge resolves to the latest `prerelease: true` release):

```json
{
  "channel": "stable",
  "current": "0.69.18",
  "latest": {
    "version": "0.69.19",
    "released_at": "2026-07-08T09:15:00Z",
    "url": "https://github.com/dmellok/tesserae/releases/tag/v0.69.19",
    "notes_headline": "Some fix"
  },
  "is_current": false,
  "versions_behind": 1
}
```

**main**:

```json
{
  "channel": "main",
  "current": "0.69.18+abc1234",
  "latest": {
    "sha": "def5678",
    "committed_at": "2026-07-08T10:00:00Z",
    "url": "https://github.com/dmellok/tesserae/commit/def5678",
    "message_headline": "Commit subject line"
  },
  "is_current": false,
  "commits_behind": 5
}
```

When `current` is omitted (or unparseable), `is_current` and `versions_behind` / `commits_behind`
are `null`. If the cache has not been populated yet the endpoint returns `503` with
`Cache-Control: no-store` and a `Retry-After` header.

### `GET /firmware/{kind}/latest`

Per-device-kind firmware update check. `kind` is a device slug (`esp32_client`, `pi_bin_client`,
`picpak_client`, etc.) defined in `firmware_sources.yaml`. Each kind maps to a GitHub repository
whose latest stable release is polled and cached, mirroring `/version/latest`.

Query parameters:

| Param     | Required | Notes                                                                 |
| --------- | -------- | --------------------------------------------------------------------- |
| `current` | no       | Caller's running firmware version. Adds `is_current` and `versions_behind`. |
| `install` | no       | Client-generated UUID v4, stored opaquely against the stats record.   |

Response headers: `Cache-Control: public, max-age=300` and `Access-Control-Allow-Origin: *`.

```json
{
  "kind": "picpak_client",
  "current": "0.1.0-dev",
  "latest": {
    "version": "0.1.1",
    "released_at": "2026-07-01T09:00:00Z",
    "url": "https://github.com/varanu5/picpak-tesserae-client/releases/tag/v0.1.1",
    "notes_headline": "Fix vflip regression",
    "assets": [
      { "name": "picpak-firmware-v0.1.1.bin", "download_url": "https://github.com/..." }
    ]
  },
  "is_current": false,
  "versions_behind": 1
}
```

`assets` is empty when the release attached no binaries; the API links to GitHub's asset URLs and
does not proxy the binaries. Unknown `kind` returns `404`. A configured kind with no cached release
yet (for example a source repo that has not cut a release) returns `503`. Adding a new device kind
is a `firmware_sources.yaml` edit plus a redeploy, no code change. Only the `stable` channel is
implemented; `edge`/`main` are placeholders in the config schema.

### `POST /widgets/install`

Records one widget-install event, called server-to-server by an app backend when a user installs a
widget from the marketplace. JSON body:

```json
{ "widget": "spotify", "install": "<uuid>", "version": "0.93.0" }
```

`widget` is required (a catalog id); `install` is the app-level install UUID (opaque, dedupe key);
`version` is the app's Tesserae version (optional). Missing `widget` returns `400`. On success the
endpoint returns `204 No Content` with `Cache-Control: no-store`. The write is best-effort and never
fails the response. Same privacy posture as the other endpoints: coarse geo from the IP, then the IP
is discarded; no IP or User-Agent is stored.

### `GET /widgets/installs`

Unique install counts per widget for the Browse UI (`Cache-Control: public, max-age=300`):

```json
{ "counts": { "spotify": 42, "weather_now": 130 } }
```

Counts are `COUNT(DISTINCT install_uuid)` grouped by widget (rows with a NULL install UUID are
excluded from the distinct count). `?widget=<id>` returns `{ "widget": "<id>", "count": <int> }`.

Interactive OpenAPI docs are served at `/docs`.

## Aggregate stats collected

One row per served request. Storage is a SQLAlchemy URL: a local SQLite file in
development, PostgreSQL in production (the `postgres` service in docker-compose).
Set `TESSERAE_DATABASE_URL` to switch; unset defaults to `sqlite:///data/stats.db`.

```sql
CREATE TABLE hits (
  ts TIMESTAMPTZ NOT NULL,    -- DATETIME under SQLite
  install_uuid TEXT,          -- client-generated UUID, NULL if the client omitted it
  country TEXT,               -- coarse geo from MaxMind, IP discarded after lookup
  region TEXT,
  channel TEXT,               -- version endpoint: requested channel
  kind TEXT,                  -- firmware endpoint: device kind
  current_version TEXT
);
```

Both update-check endpoints record into `hits`. `/version/latest` sets `channel`;
`/firmware/{kind}/latest` sets `kind`. Both honour the same optional `install` UUID contract.

Widget installs use a separate `widget_installs` table (`ts, widget_id, install_uuid,
tesserae_version, country, region`), written by `POST /widgets/install` and counted by
`GET /widgets/installs`. The `hits` table is untouched by the widget path.

What is deliberately **not** collected, ever:

- No IP addresses. The caller IP is used only for the GeoLite2 lookup and is discarded before the
  DB write.
- No User-Agent strings.
- Caddy access logs record only method, path, status, size, and duration. No IP, no User-Agent.

`install_uuid` is generated by the client on first install and stored in its own config, so it is
stable across requests and lets unique installs be deduped across days for retention analysis. If a
user resets their widget config they get a new UUID and count as a new install. That is expected.

## How to read stats

Summary reader (talks to whatever `TESSERAE_DATABASE_URL` points at):

```bash
# On the VPS (uses the container's Postgres config):
docker exec tesserae-api python -m scripts.dump_stats
# Or against an explicit URL:
python -m scripts.dump_stats --database-url postgresql+psycopg://tesserae:PW@localhost:5432/tesserae
```

Prints: total unique installs, unique installs by country, version distribution, channel
distribution, retention (installs seen within the last 7 / 30 / 90 days), and new installs in the
last 7 days (first-seen UUIDs).

### Remote / ODBC access

Postgres listens only on the VPS loopback and is never exposed publicly. Reach it from your machine
over an SSH tunnel, then point psql or any ODBC/BI tool at `localhost:5432`:

```bash
ssh -N -L 5432:127.0.0.1:5432 deploy@api.tesserae.ink   # leave running
psql "host=localhost port=5432 dbname=tesserae user=tesserae"
```

See [BOOTSTRAP.md](BOOTSTRAP.md) for the ODBC DSN details. The password is in
`/opt/tesserae-api/.env` on the VPS.

## Local development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
# Run the server locally (writes to ./data):
uv run uvicorn tesserae_api.main:app --reload
uv run python -m scripts.poll_github   # populate ./data/version_cache.json
```

## Deployment

See [BOOTSTRAP.md](BOOTSTRAP.md) for first-time VPS setup. `docker-compose.yml` is copied to
`/opt/tesserae-api/` and `Caddyfile` to `/etc/caddy/Caddyfile` on the server.

### CI/CD

Three GitHub Actions workflows:

- **ci.yml** - on every push / PR: `ruff format --check`, `ruff check`, `pytest`.
- **release.yml** - on a `v*` tag: build the image, bake in a fresh GeoLite2 mmdb, push to
  `ghcr.io/dmellok/tesserae-api:<tag>` and `:latest`, SSH-deploy to the VPS, then smoke-test
  `https://api.tesserae.ink/version/latest`. The workflow fails if the smoke test does not return
  200.
- **geoip-refresh.yml** - Sundays 06:00 UTC: rebuild `:latest` with a fresh mmdb and redeploy,
  keeping GeoIP data current without a code release.

Deploy runs automatically on tag. To require manual approval instead, add a GitHub Environment named
`production` with a required reviewer and set `environment: production` on the `deploy` job in
`release.yml`.

### Trigger a manual redeploy

```bash
# On the VPS, as the deploy user:
cd /opt/tesserae-api
docker compose pull && docker compose up -d
```

Or re-run the `release.yml` deploy job from the GitHub Actions tab. A weekly image refresh also
runs automatically.

### Rotate secrets

Secrets live in the repo's Settings -> Secrets and variables -> Actions:

- `DEPLOY_SSH_KEY` - deploy user's private SSH key. To rotate: generate a new keypair, add the new
  public key to `~deploy/.ssh/authorized_keys` on the VPS, update the secret, confirm a deploy
  succeeds, then remove the old public key from the VPS.
- `DEPLOY_HOST`, `DEPLOY_USER` - VPS host / deploy user.
- `MAXMIND_LICENSE_KEY` - MaxMind license key for the GeoLite2 download. To rotate: create a new key
  in the MaxMind account, update the secret, then revoke the old key. The next release or weekly
  refresh picks it up.

For the GHCR login the deploy user uses on the VPS, generate a new GitHub PAT with `read:packages`
and re-run `docker login ghcr.io` as shown in BOOTSTRAP.md.

The database password is not a GitHub secret; it lives in `/opt/tesserae-api/.env` on the VPS as
`POSTGRES_PASSWORD`. To rotate it, update the password in Postgres
(`ALTER ROLE tesserae WITH PASSWORD ...`), edit the `.env`, and `docker compose up -d` to restart
the API with the new value.

### Image visibility

The published `ghcr.io/dmellok/tesserae-api` image is public, so any Tesserae install can pull it
without authentication and `docker compose pull` works unattended. To run your own copy of the API,
follow BOOTSTRAP.md against your own VPS and point your widgets at your own host.

If you prefer to keep the image private (repo public, image private), the deploy user needs a GHCR
login on the VPS. See BOOTSTRAP.md step 6.
