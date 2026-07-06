"""Aggregate stats collection.

One row is written per served request. The only fields stored are:
  ts               request timestamp (UTC)
  install_uuid     client-generated UUID (NULL if the client omitted it)
  country, region  coarse geo, derived from the caller IP then the IP is discarded
  channel          requested channel
  current_version  the caller's reported current version

No IP addresses and no User-Agent strings are ever written.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
  ts DATETIME NOT NULL,
  install_uuid TEXT,
  country TEXT,
  region TEXT,
  channel TEXT,
  current_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_hits_install ON hits(install_uuid);
CREATE INDEX IF NOT EXISTS idx_hits_ts ON hits(ts);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_hit(
    db_path: Path,
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
    conn = connect(db_path)
    try:
        # Idempotent guard: never lose a hit if init_db has not run yet.
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO hits (ts, install_uuid, country, region, channel, current_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), install_uuid, country, region, channel, current_version),
        )
        conn.commit()
    finally:
        conn.close()
