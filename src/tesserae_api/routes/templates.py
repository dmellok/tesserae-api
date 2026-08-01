"""Dashboard template marketplace.

POST /templates/submit           submit a sanitized template for review (server-to-server)
GET  /templates/index.json       public catalog of approved templates
GET  /templates/{slug}.json      full template doc for one approved template
GET  /templates/preview/{id}.png preview image (pending + approved; embeds + cards)
POST /templates/install          record one template-install event
GET  /templates/author           the pseudonym an install would publish under

Submissions require a valid install UUID (quotas + revocation lineage hang off
it), are linted server-side again (clients can't be trusted to have run the
export lint), and go live only after a human approves them in the Discord
review channel (routes/discord.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tesserae_api import blocklist, discord_client, pseudonym, template_lint, templates_store
from tesserae_api.config import Settings, get_settings
from tesserae_api.stats import geo

router = APIRouter(tags=["templates"])

_INPUT_TYPES = {"string", "textarea", "number", "boolean", "select", "location_search"}
_INPUT_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_TAG_RE = re.compile(r"^[a-z0-9-]{1,24}$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_MAX_ELS = 200
_MAX_INPUTS = 20
_MAX_TARGETS = 20
_MAX_REQUIRES = 32
_MAX_LINT_ITEMS = 2000
# Takedown requests per install per day; the review channel is human-paced.
_MAX_REPORTS_PER_DAY = 5
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_TARGET_SLOTS = {"options", "source_options", "source_header", "source_url", "bind_options"}


class SubmitBody(BaseModel):
    template: dict[str, Any]
    preview_png_b64: str
    install: str | None = None
    version: str | None = None


def _client_ip(request: Request) -> str | None:
    """Resolve the caller IP from proxy headers. Used only for geo, then discarded."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip() or None
    return request.client.host if request.client else None


def _normalise_install(install: str | None) -> str | None:
    if not install:
        return None
    try:
        return str(uuid.UUID(install))
    except (ValueError, AttributeError):
        return None


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _validate_envelope(template: dict[str, Any]) -> None:
    """Structural validation of the template contract (schema_version 1).

    The canvas is checked structurally only (dims, element count); deep
    CanvasLayout validation belongs to the app on both export and install.
    Raises 422 with a specific message on the first violation."""
    if template.get("schema_version") != 1:
        raise _bad("schema_version must be 1")
    title = template.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
        raise _bad("title must be 1-80 characters")
    description = template.get("description", "")
    if not isinstance(description, str) or len(description) > 1000:
        raise _bad("description must be a string of at most 1000 characters")
    tags = template.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 8:
        raise _bad("tags must be a list of at most 8 entries")
    for tag in tags:
        if not isinstance(tag, str) or not _TAG_RE.match(tag):
            raise _bad(f"invalid tag: {tag!r} (lowercase slugs only)")
    requires = template.get("requires", [])
    if not isinstance(requires, list) or len(requires) > _MAX_REQUIRES:
        raise _bad(f"requires must be a list of at most {_MAX_REQUIRES} ids")
    if not all(isinstance(r, str) and r for r in requires):
        raise _bad("requires entries must be non-empty strings")
    inputs = template.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > _MAX_INPUTS:
        raise _bad(f"inputs must be a list of at most {_MAX_INPUTS} entries")
    seen_names: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict):
            raise _bad("each input must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not _INPUT_NAME_RE.match(name):
            raise _bad(f"invalid input name: {name!r}")
        if name in seen_names:
            raise _bad(f"duplicate input name: {name}")
        seen_names.add(name)
        if item.get("type") not in _INPUT_TYPES:
            raise _bad(f"input {name}: unknown type {item.get('type')!r}")
        targets = item.get("targets", [])
        if not isinstance(targets, list) or not 1 <= len(targets) <= _MAX_TARGETS:
            raise _bad(f"input {name}: targets must be a list of 1-{_MAX_TARGETS} entries")
        for target in targets:
            if not isinstance(target, dict) or target.get("slot") not in _TARGET_SLOTS:
                raise _bad(f"input {name}: target slot must be one of {sorted(_TARGET_SLOTS)}")
            if not isinstance(target.get("el"), str) or not target["el"]:
                raise _bad(f"input {name}: target el is required")
    canvas = template.get("canvas")
    if not isinstance(canvas, dict):
        raise _bad("canvas is required")
    w, h = canvas.get("w"), canvas.get("h")
    if not (isinstance(w, int) and isinstance(h, int) and 16 <= w <= 4096 and 16 <= h <= 4096):
        raise _bad("canvas w/h must be integers between 16 and 4096")
    els = canvas.get("els")
    if not isinstance(els, list) or not 1 <= len(els) <= _MAX_ELS:
        raise _bad(f"canvas.els must be a list of 1-{_MAX_ELS} elements")
    bg_image = canvas.get("bg_image") or ""
    if bg_image and not (bg_image.startswith("https://") or bg_image.startswith("data:image/")):
        raise _bad("canvas.bg_image must be empty, https://, or an inline data:image/ URI")


def _lint_items(template: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten every string in the template into (where, value) pairs for the
    lint pass. Generic recursion so new element fields are covered by default."""
    items: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if len(items) >= _MAX_LINT_ITEMS:
            return
        if isinstance(node, str):
            items.append((path, node))
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(template, "")
    return items


def _slugify(title: str, submission_id: str) -> str:
    base = _SLUG_STRIP_RE.sub("-", title.strip().lower()).strip("-")[:40].strip("-")
    return f"{base or 'template'}-{submission_id[:6]}"


@router.post("/templates/submit", status_code=status.HTTP_202_ACCEPTED)
def submit_template(request: Request, body: SubmitBody) -> JSONResponse:
    settings: Settings = get_settings()

    if blocklist.is_blocked(body.version, settings):
        return blocklist.blocked_response(body.version)

    install_uuid = _normalise_install(body.install)
    if install_uuid is None:
        raise HTTPException(status_code=400, detail="a valid install id is required")

    # Synthetic test traffic: acknowledge without recording, mirroring how the
    # stats consumers treat the reserved prefix.
    if install_uuid.startswith(templates_store.TEST_INSTALL_PREFIX):
        return JSONResponse(
            {"status": "pending", "id": "test", "slug": "test"},
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Cache-Control": "no-store"},
        )

    # Length checks before any decode so an oversize body is rejected cheaply.
    doc_json = json.dumps(body.template, separators=(",", ":"), ensure_ascii=False)
    if len(doc_json.encode()) > settings.template_max_doc_bytes:
        raise HTTPException(status_code=413, detail="template too large")
    if len(body.preview_png_b64) > settings.template_max_png_bytes * 4 // 3 + 8:
        raise HTTPException(status_code=413, detail="preview image too large")

    _validate_envelope(body.template)

    lint = template_lint.lint_strings(_lint_items(body.template))
    if lint["errors"]:
        raise HTTPException(
            status_code=422,
            detail={"message": "template contains credential material", "lint": lint},
        )

    if (
        templates_store.pending_count(settings.resolved_database_url, install_uuid)
        >= settings.template_max_pending_per_install
    ):
        raise HTTPException(status_code=429, detail="too many pending submissions")
    if (
        templates_store.daily_count(settings.resolved_database_url, install_uuid)
        >= settings.template_daily_submissions_per_install
    ):
        raise HTTPException(status_code=429, detail="daily submission limit reached")

    try:
        png = base64.b64decode(body.preview_png_b64, validate=True)
    except Exception as err:
        raise HTTPException(status_code=422, detail="preview_png_b64 is not base64") from err
    if len(png) > settings.template_max_png_bytes:
        raise HTTPException(status_code=413, detail="preview image too large")
    if not png.startswith(_PNG_MAGIC):
        raise HTTPException(status_code=422, detail="preview must be a PNG")

    ip = _client_ip(request)
    country, _region = geo.lookup(ip, settings.geoip_db_path)
    del ip

    secret = pseudonym.load_or_create_secret(settings.data_dir, settings.author_secret)
    author = templates_store.ensure_author(
        settings.resolved_database_url, install_uuid, pseudonym.derive(secret, install_uuid)
    )

    submission_id = uuid.uuid4().hex
    slug = _slugify(str(body.template["title"]), submission_id)
    canvas = body.template["canvas"]
    templates_store.create_submission(
        settings.resolved_database_url,
        id=submission_id,
        slug=slug,
        title=str(body.template["title"]).strip(),
        description=str(body.template.get("description") or ""),
        tags=json.dumps(body.template.get("tags") or []),
        install_uuid=install_uuid,
        version=1,
        supersedes=None,
        doc=doc_json,
        doc_sha256=hashlib.sha256(doc_json.encode()).hexdigest(),
        preview_png=png,
        w=int(canvas["w"]),
        h=int(canvas["h"]),
        requires=json.dumps(body.template.get("requires") or []),
        inputs_count=len(body.template.get("inputs") or []),
        inputs_spec=json.dumps(body.template.get("inputs") or []),
        lint=json.dumps(lint),
        tesserae_version=body.version,
        country=country,
    )

    # Review notification. Best-effort: a Discord outage must not lose the
    # submission; rows with no message id are re-posted by the repair timer.
    try:
        message_id = discord_client.post_submission_message(
            settings,
            templates_store.get_submission(settings.resolved_database_url, submission_id) or {},
        )
        if message_id:
            templates_store.set_discord_message(
                settings.resolved_database_url, submission_id, message_id
            )
    except Exception:  # noqa: BLE001 - notification must never break the submit
        pass

    return JSONResponse(
        {
            "status": "pending",
            "id": submission_id,
            "slug": slug,
            "author": templates_store.author_display(author),
        },
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/templates/index.json")
def template_index() -> JSONResponse:
    settings: Settings = get_settings()
    url = settings.resolved_database_url
    rows = templates_store.list_approved(url)
    installs = templates_store.template_install_counts(url)
    authors = templates_store.authors_by_install(url, [r["install_uuid"] for r in rows])
    templates = [
        {
            "slug": row["slug"],
            "title": row["title"],
            "description": row["description"] or "",
            "tags": json.loads(row["tags"] or "[]"),
            "author": templates_store.author_display(authors.get(row["install_uuid"])),
            "w": row["w"],
            "h": row["h"],
            "requires": json.loads(row["requires"] or "[]"),
            "inputs": row["inputs_count"] or 0,
            "input_specs": json.loads(row["inputs_spec"] or "[]"),
            "installs": installs.get(row["slug"], 0),
            "preview_url": f"/templates/preview/{row['id']}.png",
            "created_at": row["created_at"].isoformat()
            if hasattr(row["created_at"], "isoformat")
            else str(row["created_at"]),
        }
        for row in rows
    ]
    return JSONResponse(
        {"version": 1, "templates": templates},
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/templates/preview/{submission_id}.png")
def template_preview(submission_id: str) -> Response:
    settings: Settings = get_settings()
    found = templates_store.preview_bytes(settings.resolved_database_url, submission_id)
    if found is None or found[1] not in ("pending", "approved"):
        raise HTTPException(status_code=404, detail="unknown preview")
    return Response(
        content=found[0],
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/templates/author")
def template_author(install: str | None = None) -> JSONResponse:
    """The author identity an install publishes under (share-dialog preview).
    Derivation is deterministic; no row is created until first submission."""
    settings: Settings = get_settings()
    install_uuid = _normalise_install(install)
    if install_uuid is None:
        raise HTTPException(status_code=400, detail="a valid install id is required")
    rows = templates_store.authors_by_install(settings.resolved_database_url, [install_uuid])
    row = rows.get(install_uuid)
    if row is None:
        secret = pseudonym.load_or_create_secret(settings.data_dir, settings.author_secret)
        row = {"pseudonym": pseudonym.derive(secret, install_uuid), "sponsor": False}
    return JSONResponse(
        {"author": templates_store.author_display(row)},
        headers={"Cache-Control": "no-store"},
    )


class TemplateInstallBody(BaseModel):
    slug: str | None = None
    install: str | None = None
    version: str | None = None


@router.post("/templates/install", status_code=status.HTTP_204_NO_CONTENT)
def template_install(request: Request, body: TemplateInstallBody) -> Response:
    settings: Settings = get_settings()

    if blocklist.is_blocked(body.version, settings):
        return blocklist.blocked_response(body.version)

    slug = (body.slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")

    ip = _client_ip(request)
    country, region = geo.lookup(ip, settings.geoip_db_path)
    del ip

    try:
        templates_store.record_template_install(
            settings.resolved_database_url,
            template_slug=slug,
            install_uuid=_normalise_install(body.install),
            tesserae_version=body.version,
            country=country,
            region=region,
        )
    except Exception:
        # Stats collection must never break the response the app depends on.
        pass

    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


class ReportBody(BaseModel):
    reason: str | None = None
    install: str | None = None
    version: str | None = None


@router.post("/templates/{slug}/report", status_code=status.HTTP_202_ACCEPTED)
def report_template(slug: str, request: Request, body: ReportBody) -> JSONResponse:
    """Request a template's takedown. Open to any install (including the one
    that submitted it, which is how an author pulls their own work back), and
    routed to the same review channel as submissions rather than acting
    automatically: install ids are forgeable, so a self-report is a strong
    hint, not an authorisation."""
    settings: Settings = get_settings()

    row = templates_store.get_by_slug(settings.resolved_database_url, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown template")
    if row["status"] not in ("approved", "pending"):
        # Already gone; nothing to take down.
        return JSONResponse(
            {"status": "already_removed"},
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Cache-Control": "no-store"},
        )

    install_uuid = _normalise_install(body.install)
    if install_uuid and install_uuid.startswith(templates_store.TEST_INSTALL_PREFIX):
        return JSONResponse(
            {"status": "received"},
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Cache-Control": "no-store"},
        )
    if (
        templates_store.daily_report_count(settings.resolved_database_url, install_uuid)
        >= _MAX_REPORTS_PER_DAY
    ):
        raise HTTPException(status_code=429, detail="too many reports today")

    reason = (body.reason or "").strip()[:1000]
    report_id = uuid.uuid4().hex
    templates_store.create_report(
        settings.resolved_database_url,
        id=report_id,
        submission_id=row["id"],
        slug=row["slug"],
        reason=reason,
        install_uuid=install_uuid,
        by_submitter=bool(install_uuid and install_uuid == row["install_uuid"]),
    )

    try:
        report = templates_store.get_report(settings.resolved_database_url, report_id) or {}
        message_id = discord_client.post_report_message(settings, report, row)
        if message_id:
            templates_store.set_report_discord_message(
                settings.resolved_database_url, report_id, message_id
            )
    except Exception:  # noqa: BLE001 - notification must never break the report
        pass

    return JSONResponse(
        {"status": "received", "id": report_id},
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/templates/{slug}.json")
def template_doc(slug: str) -> JSONResponse:
    settings: Settings = get_settings()
    row = templates_store.get_by_slug(settings.resolved_database_url, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown template")
    if row["status"] == "revoked":
        raise HTTPException(status_code=410, detail="template removed by moderators")
    if row["status"] != "approved":
        raise HTTPException(status_code=404, detail="unknown template")
    authors = templates_store.authors_by_install(
        settings.resolved_database_url, [row["install_uuid"]]
    )
    return JSONResponse(
        {
            "slug": row["slug"],
            "author": templates_store.author_display(authors.get(row["install_uuid"])),
            "template": json.loads(row["doc"]),
        },
        headers={"Cache-Control": "public, max-age=300"},
    )
