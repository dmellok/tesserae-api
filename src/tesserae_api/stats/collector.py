"""Aggregate stats collection.

One row is written per served request. The only fields stored are:
  ts               request timestamp (UTC)
  install_uuid     client-generated UUID (NULL if the client omitted it)
  country, region  coarse geo, derived from the caller IP then the IP is discarded
  channel          requested channel
  current_version  the caller's reported current version

No IP addresses and no User-Agent strings are ever written.

The store is addressed by a SQLAlchemy URL so the same code runs against SQLite
(local development and CI) and PostgreSQL (production).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

hits = Table(
    "hits",
    metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("install_uuid", String),
    Column("country", String),
    Column("region", String),
    Column("channel", String),
    Column("current_version", String),
    Index("idx_hits_install", "install_uuid"),
    Index("idx_hits_ts", "ts"),
)

# One engine per URL, reused across requests (SQLAlchemy pools connections).
_engines: dict[str, Engine] = {}


def get_engine(url: str) -> Engine:
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(url, pool_pre_ping=True, future=True)
        _engines[url] = engine
    return engine


def init_db(url: str) -> None:
    """Create the hits table and indexes if they do not exist."""
    metadata.create_all(get_engine(url))


def record_hit(
    url: str,
    *,
    install_uuid: str | None,
    country: str | None,
    region: str | None,
    channel: str | None,
    current_version: str | None,
    ts: datetime | None = None,
) -> None:
    """Insert a single aggregate hit. `install_uuid` may be None (no dedup possible)."""
    ts = ts or datetime.now(UTC)
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(
            insert(hits).values(
                ts=ts,
                install_uuid=install_uuid,
                country=country,
                region=region,
                channel=channel,
                current_version=current_version,
            )
        )


def dispose() -> None:
    """Dispose all engines (used at application shutdown and in tests)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
