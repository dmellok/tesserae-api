"""Template marketplace: submit pipeline, catalog visibility, quotas, previews."""

from __future__ import annotations

import base64
import json
import uuid

from tesserae_api import templates_store

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 24
_PNG_B64 = base64.b64encode(_PNG).decode()


def _install() -> str:
    return str(uuid.uuid4())


def _template(title: str = "Test Board") -> dict:
    return {
        "schema_version": 1,
        "title": title,
        "description": "A little test board",
        "tags": ["weather"],
        "requires": [],
        "inputs": [
            {
                "name": "city",
                "label": "City",
                "type": "string",
                "secret": False,
                "required": True,
                "default": "",
                "targets": [{"el": "e1", "slot": "options", "key": "location"}],
            }
        ],
        "canvas": {
            "w": 800,
            "h": 480,
            "els": [{"id": "e1", "kind": "text", "text": "hi", "x": 0, "y": 0, "w": 100, "h": 40}],
        },
    }


def _submit(client, template=None, install=None, png=_PNG_B64):
    return client.post(
        "/templates/submit",
        json={
            "template": template or _template(),
            "preview_png_b64": png,
            "install": install or _install(),
            "version": "0.217.0",
        },
    )


def test_submit_creates_pending_submission(client, seeded_settings):
    resp = _submit(client)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["slug"].startswith("test-board-")
    # Seeded pseudonym: adjective-animal-NN, no free-text name.
    name = body["author"]["name"]
    assert len(name.split("-")) == 3 and body["author"]["sponsor"] is False
    row = templates_store.get_submission(seeded_settings.resolved_database_url, body["id"])
    assert row is not None and row["status"] == "pending"
    assert bytes(row["preview_png"]) == _PNG


def test_pseudonym_is_stable_per_install(client):
    install = _install()
    first = _submit(client, install=install).json()["author"]["name"]
    second = _submit(client, template=_template("Another"), install=install).json()
    assert second["author"]["name"] == first


def test_submit_requires_valid_install(client):
    assert _submit(client, install="not-a-uuid").status_code == 400
    resp = client.post(
        "/templates/submit",
        json={"template": _template(), "preview_png_b64": _PNG_B64, "version": "0.217.0"},
    )
    assert resp.status_code == 400


def test_test_prefix_install_is_acknowledged_not_recorded(client, seeded_settings):
    resp = _submit(client, install="7e57c0de-1a2b-4c3d-8e4f-0123456789ab")
    assert resp.status_code == 202
    assert resp.json()["id"] == "test"
    assert templates_store.list_approved(seeded_settings.resolved_database_url) == []
    assert (
        templates_store.pending_without_discord_message(seeded_settings.resolved_database_url) == []
    )


def test_submit_rejects_credential_material(client):
    template = _template()
    template["canvas"]["els"][0] = {
        "id": "e1",
        "kind": "code",
        "js": "fetch_headers = { Authorization: 'Bearer sk-abcdef1234567890' }",
        "x": 0,
        "y": 0,
        "w": 100,
        "h": 40,
    }
    resp = _submit(client, template=template)
    assert resp.status_code == 422
    assert "lint" in resp.json()["detail"]


def test_submit_size_caps(client, seeded_settings):
    big = _template()
    big["description"] = "x" * 100
    big["canvas"]["els"][0]["text"] = "y" * seeded_settings.template_max_doc_bytes
    assert _submit(client, template=big).status_code == 413
    assert (
        _submit(client, png="A" * (seeded_settings.template_max_png_bytes * 2)).status_code == 413
    )


def test_submit_validates_envelope(client):
    bad = _template()
    bad["inputs"][0]["targets"] = [{"el": "e1", "slot": "nonsense"}]
    assert _submit(client, template=bad).status_code == 422
    bad2 = _template()
    bad2["canvas"]["bg_image"] = "/page-assets/abc/photo.jpg"
    assert _submit(client, template=bad2).status_code == 422
    bad3 = _template()
    bad3["preview"] = None
    bad3["schema_version"] = 2
    assert _submit(client, template=bad3).status_code == 422


def test_pending_quota(client, seeded_settings):
    install = _install()
    for i in range(seeded_settings.template_max_pending_per_install):
        assert _submit(client, template=_template(f"Board {i}"), install=install).status_code == 202
    assert _submit(client, template=_template("One too many"), install=install).status_code == 429


def test_catalog_shows_only_approved(client, seeded_settings):
    url = seeded_settings.resolved_database_url
    submitted = _submit(client).json()
    assert client.get("/templates/index.json").json()["templates"] == []
    assert client.get(f"/templates/{submitted['slug']}.json").status_code == 404

    templates_store.set_status(url, submitted["id"], "approved", reviewed_by="111")
    index = client.get("/templates/index.json").json()
    assert [t["slug"] for t in index["templates"]] == [submitted["slug"]]
    entry = index["templates"][0]
    assert entry["inputs"] == 1 and entry["preview_url"].endswith(".png")

    doc = client.get(f"/templates/{submitted['slug']}.json")
    assert doc.status_code == 200
    assert doc.json()["template"]["title"] == "Test Board"

    templates_store.set_status(url, submitted["id"], "revoked", reviewed_by="111")
    assert client.get(f"/templates/{submitted['slug']}.json").status_code == 410
    assert client.get("/templates/index.json").json()["templates"] == []


def test_preview_served_for_pending_and_approved_only(client, seeded_settings):
    url = seeded_settings.resolved_database_url
    submitted = _submit(client).json()
    resp = client.get(f"/templates/preview/{submitted['id']}.png")
    assert resp.status_code == 200 and resp.content == _PNG

    templates_store.set_status(url, submitted["id"], "rejected", reviewed_by="111")
    assert client.get(f"/templates/preview/{submitted['id']}.png").status_code == 404
    assert client.get("/templates/preview/nonexistent.png").status_code == 404


def test_install_counts(client, seeded_settings):
    url = seeded_settings.resolved_database_url
    submitted = _submit(client).json()
    templates_store.set_status(url, submitted["id"], "approved", reviewed_by="111")
    install = _install()
    for _ in range(2):  # same install twice = one distinct install
        resp = client.post(
            "/templates/install",
            json={"slug": submitted["slug"], "install": install, "version": "0.217.0"},
        )
        assert resp.status_code == 204
    # Test-prefix installs don't count.
    client.post(
        "/templates/install",
        json={"slug": submitted["slug"], "install": "7e57c0de-1a2b-4c3d-8e4f-0123456789ab"},
    )
    index = client.get("/templates/index.json").json()
    assert index["templates"][0]["installs"] == 1


def test_author_endpoint(client):
    install = _install()
    resp = client.get(f"/templates/author?install={install}")
    assert resp.status_code == 200
    first = resp.json()["author"]
    assert first["sponsor"] is False
    again = client.get(f"/templates/author?install={install}").json()["author"]
    assert again == first
    assert client.get("/templates/author?install=junk").status_code == 400


def test_sponsor_custom_name_and_emblem(client, seeded_settings):
    url = seeded_settings.resolved_database_url
    install = _install()
    submitted = _submit(client, install=install).json()
    templates_store.set_status(url, submitted["id"], "approved", reviewed_by="111")
    assert templates_store.set_sponsor(url, install, sponsor=True, custom_name="Kayden")
    entry = client.get("/templates/index.json").json()["templates"][0]
    assert entry["author"] == {"name": "Kayden", "sponsor": True}
    # Sponsor off: back to the seeded pseudonym.
    templates_store.set_sponsor(url, install, sponsor=False, custom_name="Kayden")
    entry = client.get("/templates/index.json").json()["templates"][0]
    assert entry["author"]["sponsor"] is False and entry["author"]["name"] != "Kayden"


def test_lint_report_stored_for_reviewer(client, seeded_settings):
    template = _template()
    template["canvas"]["els"][0]["text"] = "see https://x.example/?token=abcdef012345"
    body = _submit(client, template=template).json()
    row = templates_store.get_submission(seeded_settings.resolved_database_url, body["id"])
    lint = json.loads(row["lint"])
    assert lint["errors"] == []
    assert any(w["rule"] == "url-query-secret" for w in lint["warnings"])
