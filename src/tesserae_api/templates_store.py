"""Template marketplace storage.

Three tables, defined on the shared collector metadata so the existing
``init_db()`` ``create_all`` creates them on deploy:

  template_submissions   one row per submitted template: metadata, the full
                         template JSON (TEXT, never queried into), the preview
                         PNG (bytea, capped small; covered by the pg_dump
                         backup timer), moderation state + Discord linkage.
  template_installs      one row per reported install (insert-only clone of
                         widget_installs; distinct-install counts).
  template_authors       per-install author identity: a stable pseudonym
                         seeded from the install UUID, plus the sponsor flag
                         and optional custom display name (sponsor perk).

Status lifecycle: pending -> approved | rejected; approved -> revoked;
(v2: approved -> superseded by a newer version).

No IP addresses are ever stored; geo happens in the route then the IP is
discarded, mirroring the stats tables.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)

from tesserae_api.stats.collector import get_engine, metadata

# Installs whose UUID starts with this prefix are synthetic test traffic (the
# convention the app's test-suite uses); they are excluded from quotas and
# public counts, mirroring how the stats consumers filter.
TEST_INSTALL_PREFIX = "7e57c0de-"

STATUSES = ("pending", "approved", "rejected", "revoked", "superseded")

template_submissions = Table(
    "template_submissions",
    metadata,
    Column("id", String, primary_key=True),  # uuid4 hex; unguessable, used in preview URLs
    Column("slug", String, nullable=False),  # public catalog id, slugified title + id prefix
    Column("status", String, nullable=False),
    Column("title", String, nullable=False),
    Column("description", Text),
    Column("tags", Text),  # JSON array (text)
    Column("install_uuid", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("supersedes", String),  # prior submission id (v2 update flow)
    Column("doc", Text, nullable=False),  # full template JSON
    Column("doc_sha256", String, nullable=False),
    Column("preview_png", LargeBinary, nullable=False),
    Column("w", Integer),
    Column("h", Integer),
    Column("requires", Text),  # JSON array (text), denormalised for the catalog
    Column("inputs_count", Integer),
    Column("inputs_spec", Text),  # JSON array of declared inputs (catalog install forms)
    Column("lint", Text),  # JSON lint report (shown in the review embed)
    Column("tesserae_version", String),
    Column("country", String),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("reviewed_at", DateTime(timezone=True)),
    Column("reviewed_by", String),  # Discord user id
    Column("reject_reason", String),
    Column("discord_message_id", String),
    Index("idx_tpl_status", "status"),
    Index("idx_tpl_slug", "slug"),
    Index("idx_tpl_install", "install_uuid"),
)

template_installs = Table(
    "template_installs",
    metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("template_slug", String, nullable=False),
    Column("install_uuid", String),
    Column("tesserae_version", String),
    Column("country", String),
    Column("region", String),
    Index("idx_tpl_installs_slug", "template_slug"),
    Index("idx_tpl_installs_install", "install_uuid"),
)

template_authors = Table(
    "template_authors",
    metadata,
    Column("install_uuid", String, primary_key=True),
    Column("pseudonym", String, nullable=False),
    Column("sponsor", Boolean, nullable=False, default=False),
    Column("custom_name", String),  # sponsor perk, set via scripts/moderate.py
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _is_test_install(install_uuid: str | None) -> bool:
    return bool(install_uuid) and str(install_uuid).startswith(TEST_INSTALL_PREFIX)


# -- authors -------------------------------------------------------------


def ensure_author(url: str, install_uuid: str, pseudonym: str) -> dict[str, Any]:
    """Return the author row for an install, creating it with the derived
    pseudonym on first contact. The pseudonym is computed by the caller
    (:mod:`tesserae_api.pseudonym`) so this module stays secret-free."""
    engine = get_engine(url)
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(template_authors).where(template_authors.c.install_uuid == install_uuid)
            )
            .mappings()
            .first()
        )
        if row is not None:
            return dict(row)
        conn.execute(
            insert(template_authors).values(
                install_uuid=install_uuid,
                pseudonym=pseudonym,
                sponsor=False,
                custom_name=None,
                created_at=_now(),
            )
        )
    return {
        "install_uuid": install_uuid,
        "pseudonym": pseudonym,
        "sponsor": False,
        "custom_name": None,
    }


def author_display(row: dict[str, Any] | None) -> dict[str, Any]:
    """The public author shape served in the catalog: the sponsor's chosen
    name when set, else the seeded pseudonym."""
    if not row:
        return {"name": "unknown", "sponsor": False}
    name = row.get("custom_name") if row.get("sponsor") else None
    return {"name": name or row["pseudonym"], "sponsor": bool(row.get("sponsor"))}


def set_sponsor(
    url: str, install_uuid: str, *, sponsor: bool, custom_name: str | None = None
) -> bool:
    """Maintainer CLI: flag an author as a sponsor and optionally set their
    display name. Returns False when the author row doesn't exist yet."""
    engine = get_engine(url)
    with engine.begin() as conn:
        result = conn.execute(
            update(template_authors)
            .where(template_authors.c.install_uuid == install_uuid)
            .values(sponsor=sponsor, custom_name=custom_name)
        )
    return result.rowcount > 0


def authors_by_install(url: str, install_uuids: list[str]) -> dict[str, dict[str, Any]]:
    if not install_uuids:
        return {}
    engine = get_engine(url)
    stmt = select(template_authors).where(template_authors.c.install_uuid.in_(install_uuids))
    with engine.connect() as conn:
        return {row["install_uuid"]: dict(row) for row in conn.execute(stmt).mappings()}


# -- submissions ---------------------------------------------------------


def create_submission(url: str, **fields: Any) -> None:
    """Insert one submission row. Caller supplies every column value (the
    route builds ids/slugs/hashes so this stays a plain insert)."""
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(
            insert(template_submissions).values(created_at=_now(), status="pending", **fields)
        )


def get_submission(url: str, submission_id: str) -> dict[str, Any] | None:
    engine = get_engine(url)
    stmt = select(template_submissions).where(template_submissions.c.id == submission_id)
    with engine.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def get_by_slug(url: str, slug: str) -> dict[str, Any] | None:
    """Newest row for a slug (any status), so the catalog route can
    distinguish revoked (410) from unknown (404)."""
    engine = get_engine(url)
    stmt = (
        select(template_submissions)
        .where(template_submissions.c.slug == slug)
        .order_by(template_submissions.c.version.desc())
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def list_approved(url: str) -> list[dict[str, Any]]:
    """Approved submissions for the public catalog, newest first, WITHOUT the
    doc/preview payload columns (cards only need metadata)."""
    engine = get_engine(url)
    cols = [
        template_submissions.c.id,
        template_submissions.c.slug,
        template_submissions.c.title,
        template_submissions.c.description,
        template_submissions.c.tags,
        template_submissions.c.install_uuid,
        template_submissions.c.w,
        template_submissions.c.h,
        template_submissions.c.requires,
        template_submissions.c.inputs_count,
        template_submissions.c.inputs_spec,
        template_submissions.c.created_at,
    ]
    stmt = (
        select(*cols)
        .where(template_submissions.c.status == "approved")
        .order_by(template_submissions.c.created_at.desc())
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings()]


def preview_bytes(url: str, submission_id: str) -> tuple[bytes, str] | None:
    """(png, status) for a submission, selecting only what the route serves
    (keeps the bytea column out of every other query's row)."""
    engine = get_engine(url)
    stmt = select(template_submissions.c.preview_png, template_submissions.c.status).where(
        template_submissions.c.id == submission_id
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        return None
    return bytes(row[0]), str(row[1])


def set_status(
    url: str,
    submission_id: str,
    status: str,
    *,
    reviewed_by: str | None = None,
    reject_reason: str | None = None,
) -> bool:
    """Transition a submission's moderation state. Returns False for an
    unknown id so the Discord handler can answer honestly."""
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    engine = get_engine(url)
    values: dict[str, Any] = {"status": status, "reviewed_at": _now()}
    if reviewed_by is not None:
        values["reviewed_by"] = reviewed_by
    if reject_reason is not None:
        values["reject_reason"] = reject_reason
    with engine.begin() as conn:
        result = conn.execute(
            update(template_submissions)
            .where(template_submissions.c.id == submission_id)
            .values(**values)
        )
    return result.rowcount > 0


def set_discord_message(url: str, submission_id: str, message_id: str) -> None:
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(
            update(template_submissions)
            .where(template_submissions.c.id == submission_id)
            .values(discord_message_id=message_id)
        )


def pending_without_discord_message(url: str) -> list[dict[str, Any]]:
    """Pending rows the submit-time Discord post missed (repair timer input)."""
    engine = get_engine(url)
    stmt = select(template_submissions).where(
        template_submissions.c.status == "pending",
        template_submissions.c.discord_message_id.is_(None),
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings()]


def pending_count(url: str, install_uuid: str) -> int:
    if _is_test_install(install_uuid):
        return 0
    engine = get_engine(url)
    stmt = select(func.count()).where(
        template_submissions.c.install_uuid == install_uuid,
        template_submissions.c.status == "pending",
    )
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())


def daily_count(url: str, install_uuid: str) -> int:
    if _is_test_install(install_uuid):
        return 0
    engine = get_engine(url)
    since = _now() - timedelta(days=1)
    stmt = select(func.count()).where(
        template_submissions.c.install_uuid == install_uuid,
        template_submissions.c.created_at >= since,
    )
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())


# -- install counts (clone of the widget_installs pair) ------------------


def record_template_install(
    url: str,
    *,
    template_slug: str,
    install_uuid: str | None,
    tesserae_version: str | None,
    country: str | None,
    region: str | None,
) -> None:
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(
            insert(template_installs).values(
                ts=_now(),
                template_slug=template_slug,
                install_uuid=install_uuid,
                tesserae_version=tesserae_version,
                country=country,
                region=region,
            )
        )


def template_install_counts(url: str) -> dict[str, int]:
    """Unique install counts per template slug, test installs excluded."""
    engine = get_engine(url)
    count = func.count(func.distinct(template_installs.c.install_uuid))
    stmt = (
        select(template_installs.c.template_slug, count)
        .where(template_installs.c.install_uuid.is_not(None))
        .where(template_installs.c.install_uuid.not_like(f"{TEST_INSTALL_PREFIX}%"))
        .group_by(template_installs.c.template_slug)
    )
    with engine.connect() as conn:
        return {row[0]: row[1] for row in conn.execute(stmt)}
