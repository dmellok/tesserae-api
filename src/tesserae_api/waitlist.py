"""Tesserae Cloud waitlist storage.

One table on the shared collector metadata so ``init_db()`` creates it on
deploy:

  cloud_waitlist   one row per email address that asked to hear about a hosted
                   Tesserae, keyed on the lowercased address so a second sign-up
                   is a no-op. Alongside the address: where the form was (the
                   web flasher or the homepage), the panel picked at the time if
                   any, the site that referred the visitor (host and path only),
                   a coarse country, and the day it was added.

The address is the only personal data the API stores anywhere, it is stored
because the person typed it in for exactly this purpose, and it leaves again
on request (``remove``) or when the list is closed. No IP, no User-Agent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Column, DateTime, String, Table, delete, func, select

from tesserae_api.stats.collector import _upsert_insert, get_engine, metadata

cloud_waitlist = Table(
    "cloud_waitlist",
    metadata,
    Column("email", String, primary_key=True),  # lowercased, trimmed
    Column("source", String, nullable=False),  # where the form lived: "flash"
    Column("vendor", String),  # panel picked at sign-up, if any
    Column("model", String),
    Column("env", String),
    Column("country", String),  # coarse geo from MaxMind, IP discarded after lookup
    Column("referrer", String),  # external site that sent the visitor: host + path, no query
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Deliberately loose: one @, something either side, a dot in the domain. The
# address is used once, for one email, so a bounce costs nothing and a strict
# parser would only reject real addresses.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_EMAIL = 254

# Descriptive body fields kept beside the address, and the longest value kept
# for each. Anything else in the body is dropped.
_FIELDS: dict[str, int] = {"source": 24, "vendor": 48, "model": 64, "env": 64}
_DEFAULT_SOURCE = "flash"
MAX_REFERRER = 200


def clean_referrer(value: Any) -> str | None:
    """An http(s) referrer cut to host and path, or None.

    The query string and fragment are dropped (they can carry session ids or
    tracking tokens), as are any credentials and the port. What is kept says
    which site and which page linked here, e.g. ``news.ycombinator.com/item``.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    path = parts.path.rstrip("/")
    return (host + path)[:MAX_REFERRER]


def normalise_email(value: Any) -> str | None:
    """A plausible address, trimmed and lowercased; anything else -> None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or len(text) > MAX_EMAIL or not _EMAIL_RE.match(text):
        return None
    return text


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:limit]
    return text or None


def describe(body: Any) -> dict[str, str | None]:
    """The allow-listed descriptive fields of a sign-up body."""
    data = body if isinstance(body, dict) else {}
    fields = {name: _clean(data.get(name), limit) for name, limit in _FIELDS.items()}
    fields["source"] = fields["source"] or _DEFAULT_SOURCE
    fields["referrer"] = clean_referrer(data.get("referrer"))
    return fields


def add(
    url: str, *, email: str, country: str | None, now: datetime | None = None, **fields
) -> bool:
    """Add an address. Returns True when it was new, False when already listed."""
    engine = get_engine(url)
    stmt = (
        _upsert_insert(engine)(cloud_waitlist)
        .values(
            email=email,
            source=fields.get("source") or _DEFAULT_SOURCE,
            vendor=fields.get("vendor"),
            model=fields.get("model"),
            env=fields.get("env"),
            referrer=fields.get("referrer"),
            country=country,
            created_at=now or datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=[cloud_waitlist.c.email])
    )
    with engine.begin() as conn:
        return conn.execute(stmt).rowcount == 1


def remove(url: str, *, email: str) -> bool:
    """Delete an address. Returns True when a row was removed."""
    engine = get_engine(url)
    with engine.begin() as conn:
        return (
            conn.execute(delete(cloud_waitlist).where(cloud_waitlist.c.email == email)).rowcount
            == 1
        )


def count(url: str, *, days: int | None = None, now: datetime | None = None) -> int:
    engine = get_engine(url)
    stmt = select(func.count()).select_from(cloud_waitlist)
    if days is not None:
        since = (now or datetime.now(UTC)) - timedelta(days=days)
        stmt = stmt.where(cloud_waitlist.c.created_at >= since)
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


def distribution(url: str, column: str, default: str = "(unknown)") -> dict[str, int]:
    """Sign-ups grouped by one descriptive column, most common first."""
    engine = get_engine(url)
    key = func.coalesce(getattr(cloud_waitlist.c, column), default)
    cnt = func.count().label("n")
    stmt = select(key, cnt).group_by(key).order_by(cnt.desc())
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(stmt)}
