#!/usr/bin/env python3
"""Maintainer CLI for the Cloud waitlist: who is waiting, and who has been invited.

The invitation itself is a mail the maintainer sends by hand; this only reads the
list and records the send, so the next round skips people already asked. Runs
inside the container so it reaches the same database.

Usage:
    docker exec tesserae-api python -m scripts.invite_waitlist list [--all]
    docker exec tesserae-api python -m scripts.invite_waitlist emails   # waiting, one per line
    docker exec tesserae-api python -m scripts.invite_waitlist mark --all-pending
    docker exec tesserae-api python -m scripts.invite_waitlist mark a@b.c d@e.f
    docker exec tesserae-api python -m scripts.invite_waitlist unmark a@b.c   # undo a mark
"""

from __future__ import annotations

import argparse

from tesserae_api import waitlist
from tesserae_api.config import get_settings
from tesserae_api.stats.collector import init_db


def _day(value) -> str:
    return value.strftime("%Y-%m-%d") if value else "-"


def _list(url: str, everyone: bool) -> int:
    rows = waitlist.rows(url, pending_only=not everyone)
    print(f"{'email':<40} {'source':<6} {'country':<7} {'panel':<28} {'joined':<10} invited")
    for r in rows:
        print(
            f"{r['email']:<40} {r['source'] or '-':<6} {r['country'] or '-':<7}"
            f" {(r['env'] or '-')[:28]:<28} {_day(r['created_at']):<10} {_day(r['invited_at'])}"
        )
    print(f"({len(rows)} {'listed' if everyone else 'waiting'})")
    return 0


def _emails(url: str) -> int:
    for r in waitlist.rows(url, pending_only=True):
        print(r["email"])
    return 0


def _mark(url: str, emails: list[str], all_pending: bool, clear: bool) -> int:
    if not emails and not all_pending:
        print("give addresses, or --all-pending")
        return 2
    n = waitlist.mark_invited(url, emails=None if all_pending else emails, clear=clear)
    print(f"{n} {'cleared' if clear else 'marked invited'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--all", action="store_true", help="include people already invited")
    sub.add_parser("emails")
    p_mark = sub.add_parser("mark")
    p_mark.add_argument("emails", nargs="*")
    p_mark.add_argument("--all-pending", action="store_true")
    p_unmark = sub.add_parser("unmark")
    p_unmark.add_argument("emails", nargs="+")
    args = parser.parse_args()

    url = get_settings().resolved_database_url
    init_db(url)
    if args.command == "list":
        return _list(url, args.all)
    if args.command == "emails":
        return _emails(url)
    if args.command == "mark":
        return _mark(url, args.emails, args.all_pending, clear=False)
    return _mark(url, args.emails, False, clear=True)


if __name__ == "__main__":
    raise SystemExit(main())
