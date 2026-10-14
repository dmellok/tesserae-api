"""GET /inat/v1/...: the key, the path allow-list, the cache, and what comes back from upstream."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from tesserae_api.config import get_settings
from tesserae_api.main import create_app
from tesserae_api.routes import inat

KEY = "k" * 64


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TESSERAE_INAT_PROXY_KEY", KEY)
    get_settings.cache_clear()
    inat.reset_cache()
    monkeypatch.setattr(inat, "MIN_INTERVAL_S", 0.0)
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def upstream(monkeypatch):
    calls: list[str] = []
    answers: dict[str, httpx.Response] = {}

    async def fake(url, timeout):  # noqa: ARG001
        calls.append(url)
        return answers.get(url) or httpx.Response(
            200,
            json={"total_results": 1, "results": [{"id": 7}]},
            headers={"content-type": "application/json; charset=utf-8"},
        )

    monkeypatch.setattr(inat, "_upstream_get", fake)
    return calls, answers


def test_needs_the_key_and_an_allowed_path(client, upstream):
    assert client.get("/inat/v1/observations").status_code == 403
    assert (
        client.get("/inat/v1/observations", headers={"X-Tesserae-Proxy-Key": "nope"}).status_code
        == 403
    )
    ok = {"X-Tesserae-Proxy-Key": KEY}
    assert client.get("/inat/v1/users/me", headers=ok).status_code == 404
    assert client.get("/inat/v1/observations/123", headers=ok).status_code == 404
    assert client.get("/inat/v1/taxa/42", headers=ok).status_code == 200
    assert (
        client.get("/inat/v1/observations/species_counts?lat=1&lng=2", headers=ok).status_code
        == 200
    )
    assert not any("users" in u for u in upstream[0])


def test_forwards_the_query_verbatim_and_caches(client, upstream):
    ok = {"X-Tesserae-Proxy-Key": KEY}
    q = "lat=-37.81&lng=144.96&radius=10&taxon_id=3%2C47170&d1=2026-09-14T00%3A00%3A00.000Z"
    r = client.get(f"/inat/v1/observations/species_counts?{q}", headers=ok)
    assert r.status_code == 200 and r.json()["total_results"] == 1
    assert r.headers["x-inat-cache"] == "MISS" and r.headers["content-type"].startswith(
        "application/json"
    )
    assert upstream[0] == [f"https://api.inaturalist.org/v1/observations/species_counts?{q}"]
    r = client.get(f"/inat/v1/observations/species_counts?{q}", headers=ok)
    assert r.headers["x-inat-cache"] == "HIT" and len(upstream[0]) == 1
    # a different query is its own call
    client.get(f"/inat/v1/observations/species_counts?{q}&page=2", headers=ok)
    assert len(upstream[0]) == 2


def test_route_is_off_without_a_key(monkeypatch, upstream):
    monkeypatch.delenv("TESSERAE_INAT_PROXY_KEY", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        assert c.get("/inat/v1/taxa/1", headers={"X-Tesserae-Proxy-Key": KEY}).status_code == 404
    get_settings.cache_clear()
    assert not upstream[0]


def test_upstream_trouble_is_passed_on_and_not_cached(client, upstream):
    ok = {"X-Tesserae-Proxy-Key": KEY}
    calls, answers = upstream
    answers["https://api.inaturalist.org/v1/taxa/1"] = httpx.Response(
        429, headers={"retry-after": "30"}
    )
    r = client.get("/inat/v1/taxa/1", headers=ok)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    answers["https://api.inaturalist.org/v1/taxa/1"] = httpx.Response(503)
    assert client.get("/inat/v1/taxa/1", headers=ok).status_code == 502
    assert len(calls) == 2, "failures are not cached"
    answers["https://api.inaturalist.org/v1/taxa/2"] = httpx.Response(422, json={"error": "bad"})
    assert client.get("/inat/v1/taxa/2", headers=ok).status_code == 422
    assert client.get("/inat/v1/taxa/2", headers=ok).headers["x-inat-cache"] == "HIT", (
        "a bad query is remembered briefly"
    )

    async def boom(url, timeout):  # noqa: ARG001
        raise httpx.ConnectError("down")

    import tesserae_api.routes.inat as mod

    mod._upstream_get = boom
    r = client.get("/inat/v1/taxa/3", headers=ok)
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
