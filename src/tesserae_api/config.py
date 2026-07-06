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

    # GeoLite2 database. Baked into the image outside the /data volume so a weekly
    # image rebuild refreshes it without touching persistent state. Falls back to
    # data_dir/geoip.mmdb for local development.
    geoip_path: Path | None = None

    # Number of recent releases / commits to retain for "behind" calculations.
    history_limit: int = 100

    @property
    def version_cache_path(self) -> Path:
        return self.data_dir / "version_cache.json"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
