#!/usr/bin/env python3
"""Human-readable summary of the aggregate stats database.

Works against whichever backend the app uses (SQLite locally, PostgreSQL in
production) via a SQLAlchemy URL.

Usage:
    python -m scripts.dump_stats
    docker exec tesserae-api python -m scripts.dump_stats
    python -m scripts.dump_stats --database-url postgresql+psycopg://user:pw@host/db
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlalchemy import distinct, func, select
from sqlalchemy.engine import Engine

from tesserae_api.config import get_settings
from tesserae_api.stats.collector import get_engine, hits, init_db

_HAS_UUID = hits.c.install_uuid.is_not(None)


def _print_header(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def _scalar(engine: Engine, stmt) -> int:
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


def _seen_in_last(engine: Engine, days: int, now: datetime) -> int:
    since = now - timedelta(days=days)
    stmt = (
        select(func.count(distinct(hits.c.install_uuid))).where(_HAS_UUID).where(hits.c.ts >= since)
    )
    return _scalar(engine, stmt)


def _new_installs_last(engine: Engine, days: int, now: datetime) -> int:
    """Installs whose first-ever sighting falls within the window."""
    since = now - timedelta(days=days)
    first_seen = (
        select(func.min(hits.c.ts).label("first_seen"))
        .where(_HAS_UUID)
        .group_by(hits.c.install_uuid)
        .subquery()
    )
    stmt = select(func.count()).select_from(first_seen).where(first_seen.c.first_seen >= since)
    return _scalar(engine, stmt)


def _grouped(engine: Engine, column, label_default: str) -> list[tuple[str, int]]:
    key = func.coalesce(column, label_default)
    cnt = func.count(distinct(hits.c.install_uuid)).label("n")
    stmt = select(key, cnt).where(_HAS_UUID).group_by(key).order_by(cnt.desc())
    with engine.connect() as conn:
        return [(row[0], row[1]) for row in conn.execute(stmt)]


def summarise(url: str, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    engine = get_engine(url)

    total = _scalar(engine, select(func.count()).select_from(hits))
    unique = _scalar(engine, select(func.count(distinct(hits.c.install_uuid))).where(_HAS_UUID))
    anon = _scalar(engine, select(func.count()).where(hits.c.install_uuid.is_(None)))

    print(f"Tesserae stats  ({engine.url.render_as_string(hide_password=True)})")
    print(f"Generated {now.isoformat()}")
    _print_header("Totals")
    print(f"Total requests           {total}")
    print(f"Unique installs          {unique}")
    print(f"Requests without UUID    {anon}")

    for title, column in (
        ("Unique installs by country", hits.c.country),
        ("Version distribution (unique installs)", hits.c.current_version),
        ("Channel distribution (unique installs)", hits.c.channel),
    ):
        _print_header(title)
        default = "??" if column is hits.c.country else "(unknown)"
        rows = _grouped(engine, column, default)
        for name, n in rows:
            print(f"  {str(name):<20} {n}")
        if not rows:
            print("  (none)")

    _print_header("Firmware checks by kind (unique installs)")
    fw_key = hits.c.kind
    fw_cnt = func.count(distinct(hits.c.install_uuid)).label("n")
    fw_stmt = (
        select(fw_key, fw_cnt)
        .where(_HAS_UUID)
        .where(hits.c.kind.is_not(None))
        .group_by(fw_key)
        .order_by(fw_cnt.desc())
    )
    with engine.connect() as conn:
        fw_rows = [(r[0], r[1]) for r in conn.execute(fw_stmt)]
    for name, n in fw_rows:
        print(f"  {str(name):<24} {n}")
    if not fw_rows:
        print("  (none)")

    _print_header("Retention (unique installs seen within window)")
    for days in (7, 30, 90):
        print(f"  last {days:>3} days   {_seen_in_last(engine, days, now)}")

    _print_header("New installs (first seen within window)")
    print(f"  last   7 days   {_new_installs_last(engine, 7, now)}")

    _summarise_heartbeats(url)


def _summarise_heartbeats(url: str) -> None:
    from tesserae_api.stats import collector

    _print_header("Heartbeat active installs (distinct)")
    for days in (1, 7, 30):
        print(f"  last {days:>3} days   {collector.heartbeat_active_installs(url, days)}")

    for title, column in (
        ("Heartbeat version distribution (30d, distinct installs)", "version"),
        ("Heartbeat deploy mix (30d)", "deploy"),
        ("Heartbeat OS mix (30d)", "os"),
        ("Heartbeat arch mix (30d)", "arch"),
        ("Heartbeat transport mix (30d)", "transport"),
    ):
        _print_header(title)
        rows = collector.heartbeat_distribution(url, column, 30)
        for name, n in rows.items():
            print(f"  {str(name):<20} {n}")
        if not rows:
            print("  (none)")

    _print_header("Heartbeat per-kind active installs (30d)")
    kinds = collector.heartbeat_kind_active_installs(url, 30)
    for name, n in kinds.items():
        print(f"  {str(name):<28} {n}")
    if not kinds:
        print("  (none)")

    _print_header("Heartbeat firmware by kind (30d, distinct installs)")
    fw_rows = collector.heartbeat_kind_firmware(url, 30)
    for kind, fw_version, n in fw_rows:
        print(f"  {str(kind):<24} {str(fw_version or '(unknown)'):<14} {n}")
    if not fw_rows:
        print("  (none)")

    _print_header("Firmware checks by kind (30d, aggregate counts)")
    fc_totals = collector.firmware_check_totals(url, 30)
    for kind, n in fc_totals.items():
        print(f"  {str(kind):<28} {n}")
    if not fc_totals:
        print("  (none)")

    _print_header("Firmware checks by kind + reported version (30d)")
    fc_versions = collector.firmware_check_versions(url, 30)
    for kind, version, n in fc_versions:
        print(f"  {str(kind):<24} {str(version):<14} {n}")
    if not fc_versions:
        print("  (none)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a summary of Tesserae aggregate stats.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL (default: from app config / TESSERAE_DATABASE_URL)",
    )
    args = parser.parse_args()
    url = args.database_url or get_settings().resolved_database_url
    # Ensure the table exists so the reader works even before the app has started.
    init_db(url)
    summarise(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
