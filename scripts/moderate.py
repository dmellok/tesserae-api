#!/usr/bin/env python3
"""Maintainer CLI for template moderation and sponsor perks.

The escape hatch when Discord is down, plus the v1 home of the sponsor flag
(cosmetic perks: emblem + custom display name). Runs inside the container so
it reaches the same database.

Usage:
    docker exec tesserae-api python -m scripts.moderate list [--status pending]
    docker exec tesserae-api python -m scripts.moderate approve <id>
    docker exec tesserae-api python -m scripts.moderate reject <id> [--reason "..."]
    docker exec tesserae-api python -m scripts.moderate revoke <id>
    docker exec tesserae-api python -m scripts.moderate sponsor <install_uuid> \
        [--name "Display Name"] [--off]
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from tesserae_api import templates_store
from tesserae_api.config import get_settings


def _list(url: str, status: str | None) -> int:
    stmt = select(
        templates_store.template_submissions.c.id,
        templates_store.template_submissions.c.slug,
        templates_store.template_submissions.c.status,
        templates_store.template_submissions.c.title,
        templates_store.template_submissions.c.created_at,
    ).order_by(templates_store.template_submissions.c.created_at.desc())
    if status:
        stmt = stmt.where(templates_store.template_submissions.c.status == status)
    engine = templates_store.get_engine(url)
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            print(f"{row[0]}  {row[2]:<10}  {row[1]:<40}  {row[3]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    for name in ("approve", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("id")
    p_reject = sub.add_parser("reject")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", default=None)
    p_sponsor = sub.add_parser("sponsor")
    p_sponsor.add_argument("install_uuid")
    p_sponsor.add_argument("--name", default=None)
    p_sponsor.add_argument("--off", action="store_true")
    args = parser.parse_args()

    url = get_settings().resolved_database_url
    if args.command == "list":
        return _list(url, args.status)
    if args.command == "sponsor":
        ok = templates_store.set_sponsor(
            url, args.install_uuid, sponsor=not args.off, custom_name=args.name
        )
        print("updated" if ok else "no such author (they must submit once first)")
        return 0 if ok else 1

    status = {"approve": "approved", "reject": "rejected", "revoke": "revoked"}[args.command]
    ok = templates_store.set_status(
        url,
        args.id,
        status,
        reviewed_by="cli",
        reject_reason=getattr(args, "reason", None),
    )
    if ok:
        row = templates_store.get_submission(url, args.id)
        print(json.dumps({"id": args.id, "status": row["status"] if row else status}))
        return 0
    print(f"no such submission: {args.id}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
