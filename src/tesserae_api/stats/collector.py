"""Aggregate stats collection.

The `hits` table records one row per update-check request. The only fields stored are:
  ts               request timestamp (UTC)
  install_uuid     client-generated UUID (NULL if the client omitted it)
  country, region  coarse geo, derived from the caller IP then the IP is discarded
  channel          requested channel (version endpoint)
  kind             device kind (firmware endpoint)
  current_version  the caller's reported current version

The `widget_installs` table records one row per widget-install event reported by
an app backend (POST /widgets/install), for per-widget unique install counts.

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
    func,
    insert,
    inspect,
    select,
    text,
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
    Column("kind", String),
    Column("current_version", String),
    Index("idx_hits_install", "install_uuid"),
    Index("idx_hits_ts", "ts"),
)

widget_installs = Table(
    "widget_installs",
    metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("widget_id", String, nullable=False),
    Column("install_uuid", String),
    Column("tesserae_version", String),
    Column("country", String),
    Column("region", String),
    Index("idx_widget_installs_widget", "widget_id"),
    Index("idx_widget_installs_install", "install_uuid"),
)

# Columns added after the initial schema shipped, applied to pre-existing tables.
_ADDED_COLUMNS = {"kind": "VARCHAR"}

# One engine per URL, reused across requests (SQLAlchemy pools connections).
_engines: dict[str, Engine] = {}


def get_engine(url: str) -> Engine:
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(url, pool_pre_ping=True, future=True)
        _engines[url] = engine
    return engine


def init_db(url: str) -> None:
    """Create the hits table and indexes, and add any newer columns in place."""
    engine = get_engine(url)
    metadata.create_all(engine)
    _apply_added_columns(engine)


def _apply_added_columns(engine: Engine) -> None:
    existing = {col["name"] for col in inspect(engine).get_columns("hits")}
    for name, sql_type in _ADDED_COLUMNS.items():
        if name not in existing:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE hits ADD COLUMN {name} {sql_type}"))


def record_hit(
    url: str,
    *,
    install_uuid: str | None,
    country: str | None,
    region: str | None,
    channel: str | None = None,
    kind: str | None = None,
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
                kind=kind,
                current_version=current_version,
            )
        )


def record_widget_install(
    url: str,
    *,
    widget_id: str,
    install_uuid: str | None,
    tesserae_version: str | None,
    country: str | None,
    region: str | None,
    ts: datetime | None = None,
) -> None:
    """Insert one widget-install event. `install_uuid` may be None (no dedup possible)."""
    ts = ts or datetime.now(UTC)
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(
            insert(widget_installs).values(
                ts=ts,
                widget_id=widget_id,
                install_uuid=install_uuid,
                tesserae_version=tesserae_version,
                country=country,
                region=region,
            )
        )


def widget_install_counts(url: str, widget_id: str | None = None) -> dict[str, int]:
    """Unique install counts (COUNT DISTINCT install_uuid) per widget.

    NULL install_uuid rows are excluded from the distinct count by SQL semantics.
    Optionally filtered to a single widget_id.
    """
    engine = get_engine(url)
    count = func.count(func.distinct(widget_installs.c.install_uuid))
    stmt = select(widget_installs.c.widget_id, count).group_by(widget_installs.c.widget_id)
    if widget_id is not None:
        stmt = stmt.where(widget_installs.c.widget_id == widget_id)
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(stmt)}


def dispose() -> None:
    """Dispose all engines (used at application shutdown and in tests)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
