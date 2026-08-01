#!/usr/bin/env python3
"""Post review messages for pending template submissions that have none.

The submit endpoint posts to Discord synchronously (best-effort); when Discord
was unreachable at submit time the row's ``discord_message_id`` stays NULL and
this repair job announces it on the next timer run. Safe to run any time:
rows with a message id are never re-posted.

Usage:
    python -m scripts.notify_pending
    docker exec tesserae-api python -m scripts.notify_pending
"""

from __future__ import annotations

import logging
import sys

from tesserae_api import discord_client, templates_store
from tesserae_api.config import get_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("notify_pending")
    settings = get_settings()
    if not settings.discord_bot_token or not settings.discord_channel_id:
        log.info("discord not configured; nothing to do")
        return 0
    url = settings.resolved_database_url
    failures = 0
    rows = templates_store.pending_without_discord_message(url)
    for row in rows:
        try:
            message_id = discord_client.post_submission_message(settings, row)
            if message_id:
                templates_store.set_discord_message(url, str(row["id"]), message_id)
                log.info("announced submission %s (%s)", row["id"], row["slug"])
        except Exception as exc:  # noqa: BLE001 - keep going; next timer run retries
            log.error("failed to announce %s: %s", row["id"], exc)
            failures += 1
    # Same repair for takedown requests that missed their post.
    reports = templates_store.open_reports_without_discord_message(url)
    for report in reports:
        submission = templates_store.get_submission(url, str(report["submission_id"]))
        if submission is None:
            continue
        try:
            message_id = discord_client.post_report_message(settings, report, submission)
            if message_id:
                templates_store.set_report_discord_message(url, str(report["id"]), message_id)
                log.info("announced report %s (%s)", report["id"], report["slug"])
        except Exception as exc:  # noqa: BLE001 - keep going; next run retries
            log.error("failed to announce report %s: %s", report["id"], exc)
            failures += 1

    if not rows and not reports:
        log.info("no unannounced pending submissions or reports")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
