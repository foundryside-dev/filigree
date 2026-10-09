"""Call-outcome logging on the HTTP surface (Task 0.1)."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import JsonLogCapture, PopulatedDB


async def test_http_route_logged_with_template_and_status(client: AsyncClient, caplog_json: JsonLogCapture) -> None:
    await client.get("/api/issue/nope")
    rec = caplog_json.last(event="call", surface="http")
    assert rec["name"] == "GET /api/issue/{issue_id}"
    assert rec["outcome"] == "error"
    assert rec["code"] == "NOT_FOUND"


async def test_http_success_logged_ok(client: AsyncClient, dashboard_db: PopulatedDB, caplog_json: JsonLogCapture) -> None:
    resp = await client.get(f"/api/issue/{dashboard_db.ids['a']}")
    assert resp.status_code == 200
    rec = caplog_json.last(event="call", surface="http")
    assert rec["name"] == "GET /api/issue/{issue_id}"
    assert rec["outcome"] == "ok"
    assert rec["code"] is None
    assert isinstance(rec["duration_ms"], float)


async def test_http_unmatched_path_falls_back_to_raw_path(client: AsyncClient, caplog_json: JsonLogCapture) -> None:
    await client.get("/api/no-such-route")
    rec = caplog_json.last(event="call", surface="http")
    assert rec["name"] == "GET /api/no-such-route"
    assert rec["outcome"] == "error"


async def test_http_population_comes_from_project_config(
    client: AsyncClient, dashboard_db: PopulatedDB, caplog_json: JsonLogCapture
) -> None:
    from filigree.core import read_config, write_config

    meta = dashboard_db.db.meta_dir
    config = dict(read_config(meta))
    config["population"] = "product-use"
    write_config(meta, config)
    await client.get("/api/health")
    assert caplog_json.last(event="call", surface="http")["population"] == "product-use"
