#!/usr/bin/env python3
"""Human-readable summary of the aggregate stats database.

Usage:
    python -m scripts.dump_stats
    docker exec tesserae-api python -m scripts.dump_stats
    python -m scripts.dump_stats --db /var/lib/tesserae-api/stats.db
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tesserae_api.config import get_settings


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _print_header(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def _seen_in_last(conn: sqlite3.Connection, days: int, now: datetime) -> int:
    since = (now - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT COUNT(DISTINCT install_uuid) AS n FROM hits "
        "WHERE install_uuid IS NOT NULL AND ts >= ?",
        (since,),
    ).fetchone()
    return row["n"] or 0


def _new_installs_last(conn: sqlite3.Connection, days: int, now: datetime) -> int:
    """Installs whose first-ever sighting falls within the window."""
    since = (now - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT install_uuid, MIN(ts) AS first_seen FROM hits "
        "  WHERE install_uuid IS NOT NULL GROUP BY install_uuid"
        ") WHERE first_seen >= ?",
        (since,),
    ).fetchone()
    return row["n"] or 0


def summarise(db_path: Path, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    conn = _connect(db_path)
    try:
        total_hits = conn.execute("SELECT COUNT(*) AS n FROM hits").fetchone()["n"]
        unique = conn.execute(
            "SELECT COUNT(DISTINCT install_uuid) AS n FROM hits WHERE install_uuid IS NOT NULL"
        ).fetchone()["n"]
        anon = conn.execute("SELECT COUNT(*) AS n FROM hits WHERE install_uuid IS NULL").fetchone()[
            "n"
        ]

        print(f"Tesserae stats  ({db_path})")
        print(f"Generated {now.isoformat()}")
        _print_header("Totals")
        print(f"Total requests           {total_hits}")
        print(f"Unique installs          {unique}")
        print(f"Requests without UUID    {anon}")

        _print_header("Unique installs by country")
        rows = conn.execute(
            "SELECT COALESCE(country, '??') AS country, COUNT(DISTINCT install_uuid) AS n "
            "FROM hits WHERE install_uuid IS NOT NULL GROUP BY country ORDER BY n DESC"
        ).fetchall()
        for r in rows:
            print(f"  {r['country']:<6} {r['n']}")
        if not rows:
            print("  (none)")

        _print_header("Version distribution (unique installs)")
        rows = conn.execute(
            "SELECT COALESCE(current_version, '(unknown)') AS v, COUNT(DISTINCT install_uuid) AS n "
            "FROM hits WHERE install_uuid IS NOT NULL GROUP BY v ORDER BY n DESC"
        ).fetchall()
        for r in rows:
            print(f"  {r['v']:<20} {r['n']}")
        if not rows:
            print("  (none)")

        _print_header("Channel distribution (unique installs)")
        rows = conn.execute(
            "SELECT COALESCE(channel, '(unknown)') AS c, COUNT(DISTINCT install_uuid) AS n "
            "FROM hits WHERE install_uuid IS NOT NULL GROUP BY c ORDER BY n DESC"
        ).fetchall()
        for r in rows:
            print(f"  {r['c']:<10} {r['n']}")
        if not rows:
            print("  (none)")

        _print_header("Retention (unique installs seen within window)")
        for days in (7, 30, 90):
            print(f"  last {days:>3} days   {_seen_in_last(conn, days, now)}")

        _print_header("New installs (first seen within window)")
        print(f"  last   7 days   {_new_installs_last(conn, 7, now)}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a summary of Tesserae aggregate stats.")
    parser.add_argument(
        "--db", type=Path, default=None, help="Path to stats.db (default: from config)"
    )
    args = parser.parse_args()
    db_path = args.db or get_settings().stats_db_path
    if not db_path.exists():
        print(f"No stats database at {db_path}")
        return 1
    summarise(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
