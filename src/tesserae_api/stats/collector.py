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

The `heartbeats` / `heartbeat_kinds` tables record one row per (install, DAY) from
POST /heartbeat. Only the day is stored, never a timestamp, and the write is an
upsert keyed on (day, install_uuid) so a client that pings many times a day
counts once and its cadence cannot become a sub-daily activity trace.

No IP addresses and no User-Agent strings are ever written.

The store is addressed by a SQLAlchemy URL so the same code runs against SQLite
(local development and CI) and PostgreSQL (production).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
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

# Descriptive columns of a heartbeat (everything except the (day, install) key).
_HEARTBEAT_FIELDS = (
    "version",
    "channel",
    "os",
    "arch",
    "py",
    "deploy",
    "transport",
    "devices",
    "companion",
    "ha",
    "country",
    "region",
    "age",
    "lineups",
    "feat_relay",
    "feat_touch",
    "feat_mcp",
    "feat_quiet_hours",
    "ota_offered",
    "ota_applied",
    "ota_failed",
    "ui",
    "ui_chosen",
)

# Per-kind descriptive columns beyond the kind itself; all upserted to the
# latest ping of the day.
_KIND_FIELDS = ("fw_version", "gamut", "res", "sleep")

heartbeats = Table(
    "heartbeats",
    metadata,
    Column("day", Date, nullable=False),  # bucketed, never a timestamp
    Column("install_uuid", String),
    Column("version", String),
    Column("channel", String),
    Column("os", String),
    Column("arch", String),
    Column("py", String),
    Column("deploy", String),
    Column("transport", String),
    Column("devices", String),  # bucketed count of paired devices
    Column("companion", String),  # bucketed count of paired companion apps
    Column("ha", Boolean),
    Column("country", String),
    Column("region", String),
    Column("age", String),  # bucketed days since the install id was minted
    Column("lineups", String),  # bucketed count of lineups
    Column("feat_relay", Boolean),  # cloud relay linked
    Column("feat_touch", Boolean),  # a touch-capable device in use
    Column("feat_mcp", Boolean),  # MCP experiment on
    Column("feat_quiet_hours", Boolean),  # quiet hours configured on a device
    Column("ota_offered", String),  # bucketed devices offered a firmware release
    Column("ota_applied", String),  # bucketed devices already on it
    Column("ota_failed", String),  # bucketed devices whose last OTA report failed
    Column("ui", String),  # admin design in use: classic / paper / other
    Column("ui_chosen", Boolean),  # the design was set by hand, not the default (app 0.448.10)
    UniqueConstraint("day", "install_uuid", name="uq_heartbeats_day_install"),
    Index("idx_heartbeats_install", "install_uuid"),
    Index("idx_heartbeats_day", "day"),
)

heartbeat_kinds = Table(
    "heartbeat_kinds",
    metadata,
    Column("day", Date, nullable=False),
    Column("install_uuid", String),
    Column("kind", String, nullable=False),
    Column("fw_version", String),  # reported firmware version for this kind, if any
    Column("gamut", String),  # declared panel gamut (bwry_4, gray_4, ...)
    Column("res", String),  # declared panel resolution, landscape WxH
    Column("sleep", String),  # bucketed wake cadence
    UniqueConstraint("day", "install_uuid", "kind", name="uq_heartbeat_kinds"),
    Index("idx_heartbeat_kinds_kind", "kind"),
)

# Aggregate-only counters for GET /firmware/<kind>/latest checks. No per-request
# row, no IP, no install id: just counts per (day, kind, reported version, coarse
# country). NULL-free key columns (sentinels below) so the upsert always dedupes.
firmware_check_stats = Table(
    "firmware_check_stats",
    metadata,
    Column("day", Date, nullable=False),
    Column("kind", String, nullable=False),
    Column("version", String, nullable=False),  # reported ?current, or "unknown"
    Column("country", String, nullable=False),  # coarse country, or "??"
    Column("count", Integer, nullable=False),
    UniqueConstraint("day", "kind", "version", "country", name="uq_firmware_check_stats"),
    Index("idx_firmware_check_kind", "kind"),
)

# Columns added after a table first shipped, applied in place to pre-existing tables.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "hits": {"kind": "VARCHAR"},
    "cloud_waitlist": {"referrer": "VARCHAR", "invited_at": "TIMESTAMP WITH TIME ZONE"},
    "heartbeat_kinds": {
        "fw_version": "VARCHAR",
        "gamut": "VARCHAR",
        "res": "VARCHAR",
        "sleep": "VARCHAR",
    },
    "heartbeats": {
        "companion": "VARCHAR",
        "age": "VARCHAR",
        "lineups": "VARCHAR",
        "feat_relay": "BOOLEAN",
        "feat_touch": "BOOLEAN",
        "feat_mcp": "BOOLEAN",
        "feat_quiet_hours": "BOOLEAN",
        "ota_offered": "VARCHAR",
        "ota_applied": "VARCHAR",
        "ota_failed": "VARCHAR",
        "ui": "VARCHAR",
        "ui_chosen": "BOOLEAN",
    },
}

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
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:  # defined in a module this process never imported
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


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


def _upsert_insert(engine: Engine):
    """Return the dialect-specific insert construct with ON CONFLICT support."""
    name = engine.dialect.name
    if name == "postgresql":
        return _pg_insert
    if name == "sqlite":
        return _sqlite_insert
    raise RuntimeError(f"heartbeat upsert unsupported on dialect: {name}")


def record_heartbeat(
    url: str,
    *,
    install_uuid: str | None,
    version: str | None = None,
    channel: str | None = None,
    os: str | None = None,
    arch: str | None = None,
    py: str | None = None,
    deploy: str | None = None,
    transport: str | None = None,
    devices: str | None = None,
    companion: str | None = None,
    ha: bool | None = None,
    country: str | None = None,
    region: str | None = None,
    device_kinds: list[tuple[str, str | None]] | list[dict[str, Any]] | None = None,
    age: str | None = None,
    lineups: str | None = None,
    features: dict[str, bool | None] | None = None,
    ota: dict[str, str | None] | None = None,
    ui: str | None = None,
    ui_chosen: bool | None = None,
    day: date | None = None,
) -> None:
    """Upsert one heartbeat for (day, install). Repeated pings the same day collapse
    to a single row whose descriptive columns reflect the latest ping.

    The day is server-derived (UTC) and no exact time is stored. A NULL install_uuid
    cannot dedupe (unique NULLs are distinct), so anonymous heartbeats accumulate.

    ``device_kinds`` items are either ``(kind, fw_version)`` tuples or dicts with
    ``kind`` plus any of ``fw_version`` / ``gamut`` / ``res`` / ``sleep``.
    """
    day = day or datetime.now(UTC).date()
    engine = get_engine(url)
    ins = _upsert_insert(engine)
    features = features or {}
    ota = ota or {}
    descriptive = {
        "version": version,
        "channel": channel,
        "os": os,
        "arch": arch,
        "py": py,
        "deploy": deploy,
        "transport": transport,
        "devices": devices,
        "companion": companion,
        "ha": ha,
        "country": country,
        "region": region,
        "age": age,
        "lineups": lineups,
        "feat_relay": features.get("relay"),
        "feat_touch": features.get("touch"),
        "feat_mcp": features.get("mcp"),
        "feat_quiet_hours": features.get("quiet_hours"),
        "ota_offered": ota.get("offered"),
        "ota_applied": ota.get("applied"),
        "ota_failed": ota.get("failed"),
        "ui": ui,
        "ui_chosen": ui_chosen,
    }
    with engine.begin() as conn:
        stmt = ins(heartbeats).values(day=day, install_uuid=install_uuid, **descriptive)
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "install_uuid"],
            set_={name: stmt.excluded[name] for name in _HEARTBEAT_FIELDS},
        )
        conn.execute(stmt)

        for item in device_kinds or []:
            if isinstance(item, dict):
                kind = item.get("kind")
                fields = {name: item.get(name) for name in _KIND_FIELDS}
            else:
                kind, fw_version = item
                fields = {"fw_version": fw_version, "gamut": None, "res": None, "sleep": None}
            if not kind:
                continue
            kind_stmt = ins(heartbeat_kinds).values(
                day=day, install_uuid=install_uuid, kind=kind, **fields
            )
            kind_stmt = kind_stmt.on_conflict_do_update(
                index_elements=["day", "install_uuid", "kind"],
                set_={name: kind_stmt.excluded[name] for name in _KIND_FIELDS},
            )
            conn.execute(kind_stmt)


def _since(days: int) -> date:
    return datetime.now(UTC).date() - timedelta(days=days)


def heartbeat_active_installs(url: str, days: int = 30) -> int:
    """Distinct installs seen in the last `days` days."""
    engine = get_engine(url)
    stmt = (
        select(func.count(func.distinct(heartbeats.c.install_uuid)))
        .where(heartbeats.c.install_uuid.is_not(None))
        .where(heartbeats.c.day >= _since(days))
    )
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


def heartbeat_companion_installs(url: str, days: int = 30) -> int:
    """Distinct installs reporting a paired companion app (companion present, != '0')."""
    engine = get_engine(url)
    stmt = (
        select(func.count(func.distinct(heartbeats.c.install_uuid)))
        .where(heartbeats.c.install_uuid.is_not(None))
        .where(heartbeats.c.day >= _since(days))
        .where(heartbeats.c.companion.is_not(None))
        .where(heartbeats.c.companion != "0")
    )
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


def heartbeat_distribution(url: str, column: str, days: int = 30) -> dict[str, int]:
    """Distinct-install counts grouped by a descriptive column, over the last `days`."""
    engine = get_engine(url)
    key = heartbeats.c[column]
    count = func.count(func.distinct(heartbeats.c.install_uuid)).label("n")
    stmt = (
        select(key, count)
        .where(heartbeats.c.install_uuid.is_not(None))
        .where(heartbeats.c.day >= _since(days))
        .group_by(key)
        .order_by(count.desc())
    )
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(stmt)}


def heartbeat_kind_active_installs(url: str, days: int = 30) -> dict[str, int]:
    """Distinct installs reporting each device kind, over the last `days`."""
    engine = get_engine(url)
    count = func.count(func.distinct(heartbeat_kinds.c.install_uuid)).label("n")
    stmt = (
        select(heartbeat_kinds.c.kind, count)
        .where(heartbeat_kinds.c.install_uuid.is_not(None))
        .where(heartbeat_kinds.c.day >= _since(days))
        .group_by(heartbeat_kinds.c.kind)
        .order_by(count.desc())
    )
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(stmt)}


def heartbeat_kind_firmware(url: str, days: int = 30) -> list[tuple[str, str | None, int]]:
    """Distinct installs per (device kind, firmware version), over the last `days`.

    Returns rows of (kind, fw_version, installs), ordered by kind then count.
    """
    engine = get_engine(url)
    count = func.count(func.distinct(heartbeat_kinds.c.install_uuid)).label("n")
    stmt = (
        select(heartbeat_kinds.c.kind, heartbeat_kinds.c.fw_version, count)
        .where(heartbeat_kinds.c.install_uuid.is_not(None))
        .where(heartbeat_kinds.c.day >= _since(days))
        .group_by(heartbeat_kinds.c.kind, heartbeat_kinds.c.fw_version)
        .order_by(heartbeat_kinds.c.kind, count.desc())
    )
    with engine.connect() as conn:
        return [(row[0], row[1], row[2]) for row in conn.execute(stmt)]


def record_firmware_check(
    url: str,
    *,
    kind: str,
    version: str | None,
    country: str | None,
    day: date | None = None,
) -> None:
    """Increment the aggregate counter for a firmware check. Aggregate only: no
    per-request row, no IP, no install id. Missing version/country use sentinels
    so the (day, kind, version, country) key never contains NULLs (which would
    defeat the ON CONFLICT dedupe)."""
    day = day or datetime.now(UTC).date()
    key = {"day": day, "kind": kind, "version": version or "unknown", "country": country or "??"}
    engine = get_engine(url)
    ins = _upsert_insert(engine)
    with engine.begin() as conn:
        stmt = ins(firmware_check_stats).values(count=1, **key)
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "kind", "version", "country"],
            set_={"count": firmware_check_stats.c.count + 1},
        )
        conn.execute(stmt)


def firmware_check_totals(url: str, days: int = 30) -> dict[str, int]:
    """Total firmware checks per kind over the last `days`."""
    engine = get_engine(url)
    total = func.sum(firmware_check_stats.c.count).label("n")
    stmt = (
        select(firmware_check_stats.c.kind, total)
        .where(firmware_check_stats.c.day >= _since(days))
        .group_by(firmware_check_stats.c.kind)
        .order_by(total.desc())
    )
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(stmt)}


def firmware_check_versions(url: str, days: int = 30) -> list[tuple[str, str, int]]:
    """Checks per (kind, reported version) over the last `days`."""
    engine = get_engine(url)
    total = func.sum(firmware_check_stats.c.count).label("n")
    stmt = (
        select(firmware_check_stats.c.kind, firmware_check_stats.c.version, total)
        .where(firmware_check_stats.c.day >= _since(days))
        .group_by(firmware_check_stats.c.kind, firmware_check_stats.c.version)
        .order_by(firmware_check_stats.c.kind, total.desc())
    )
    with engine.connect() as conn:
        return [(row[0], row[1], row[2]) for row in conn.execute(stmt)]


def dispose() -> None:
    """Dispose all engines (used at application shutdown and in tests)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
