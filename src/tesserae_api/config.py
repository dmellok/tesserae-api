"""Runtime configuration.

All paths default to a local ./data directory for development and are overridden
to /data inside the container via the DATA_DIR environment variable.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TESSERAE_", env_file=".env", extra="ignore")

    # Upstream repository that is being tracked for versions.
    repo_owner: str = "dmellok"
    repo_name: str = "tesserae"

    # GitHub API. A token is optional; it only raises the unauthenticated rate limit.
    github_api_base: str = "https://api.github.com"
    github_token: str | None = None
    github_timeout_seconds: float = 10.0

    # On-disk runtime state.
    data_dir: Path = Field(default=Path("data"))

    # Stats database. Defaults to a local SQLite file for development and CI; set
    # to a postgresql+psycopg:// URL in production (see docker-compose.yml).
    database_url: str | None = None

    # Firmware release source: a single GitHub repo whose releases carry
    # descriptor-<kind>.json assets, one per OTA-capable device kind.
    firmware_repo: str = "dmellok/tesserae-device-firmware"

    # Reported app versions to block from recording, as comma-separated prefixes.
    # A caller reporting a blocked version is not recorded and gets a notice to
    # stop or update. Used to keep an errant test harness or stale dev build out
    # of the stats tables. Empty (default) blocks nothing.
    blocked_versions: str = ""

    # GeoLite2 database. Baked into the image outside the /data volume so a weekly
    # image rebuild refreshes it without touching persistent state. Falls back to
    # data_dir/geoip.mmdb for local development.
    geoip_path: Path | None = None

    # Number of recent releases / commits to retain for "behind" calculations.
    history_limit: int = 100

    # -- template marketplace ------------------------------------------
    # Discord review channel: submissions post as embeds with Approve/Reject
    # buttons via the bot token (plain webhooks can't carry components), and
    # button presses come back to POST /discord/interactions, verified against
    # the application public key. Reviewer ids gate who may press the buttons.
    discord_public_key: str = ""  # hex, from the Discord developer portal
    discord_bot_token: str | None = None
    discord_channel_id: str | None = None
    discord_reviewer_ids: str = ""  # comma-separated Discord user ids

    # Public base URL used to build preview links in Discord embeds.
    public_base_url: str = "https://api.tesserae.ink"

    # HMAC secret for seeded author pseudonyms. Empty = auto-generated and
    # persisted at data_dir/author_secret on first use.
    author_secret: str = ""

    # Submission caps (server-side; Caddy adds the transport-level limits).
    template_max_doc_bytes: int = 262_144
    template_max_png_bytes: int = 409_600
    template_max_pending_per_install: int = 3
    template_daily_submissions_per_install: int = 10

    @property
    def version_cache_path(self) -> Path:
        return self.data_dir / "version_cache.json"

    @property
    def firmware_cache_path(self) -> Path:
        return self.data_dir / "firmware_cache.json"

    @property
    def stats_db_path(self) -> Path:
        return self.data_dir / "stats.db"

    @property
    def resolved_database_url(self) -> str:
        """Configured DATABASE_URL, or a SQLite file under data_dir by default."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.stats_db_path}"

    @property
    def geoip_db_path(self) -> Path:
        return self.geoip_path or (self.data_dir / "geoip.mmdb")

    @property
    def repo_slug(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    @property
    def blocked_version_prefixes(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.blocked_versions.split(",") if p.strip())

    @property
    def discord_reviewer_id_set(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.discord_reviewer_ids.split(",") if p.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
