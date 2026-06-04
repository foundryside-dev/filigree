"""Tests for dashboard file/finding API endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """Fresh DB for dashboard API tests with check_same_thread=False."""
    d = FiligreeDB(tmp_path / "filigree.db", prefix="test", check_same_thread=False)
    d.initialize()
    yield d
    d.close()


@pytest.fixture
async def client(api_db: FiligreeDB) -> AsyncGenerator[AsyncClient, None]:
    """Test client wired to the api_db fixture (single-project mode)."""
    dash_module._db = api_db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    dash_module._db = None


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


class TestFileEndpoints:
    """Tests for file API endpoints."""

    async def test_list_files_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == []
        assert data["total"] == 0

    async def test_list_files_with_data(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.register_file("src/main.py", language="python")
        api_db.register_file("src/utils.py", language="python")
        resp = await client.get("/api/files")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2

    async def test_list_files_with_language_filter(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.register_file("a.py", language="python")
        api_db.register_file("b.js", language="javascript")
        resp = await client.get("/api/files?language=python")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["language"] == "python"

    async def test_get_file_detail_structure(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("src/main.py", language="python")
        resp = await client.get(f"/api/files/{f.id}")
        assert resp.status_code == 200
        data = resp.json()
        # Top-level keys are separated data layers
        assert set(data.keys()) == {"file", "associations", "recent_findings", "summary", "observation_count"}
        assert data["file"]["path"] == "src/main.py"
        assert data["file"]["language"] == "python"
        assert data["associations"] == []
        assert data["recent_findings"] == []
        assert data["summary"]["total_findings"] == 0
        assert data["summary"]["open_findings"] == 0

    async def test_get_file_detail_with_findings(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "x.py", "rule_id": "E501", "severity": "high", "message": "Line too long"},
                {"path": "x.py", "rule_id": "E302", "severity": "low", "message": "Spacing"},
            ],
        )
        f = api_db.get_file_by_path("x.py")
        assert f is not None
        resp = await client.get(f"/api/files/{f.id}")
        data = resp.json()
        assert data["summary"]["total_findings"] == 2
        assert data["summary"]["open_findings"] == 2
        assert data["summary"]["high"] == 1
        assert data["summary"]["low"] == 1
        assert data["summary"]["critical"] == 0
        assert len(data["recent_findings"]) == 2

    async def test_get_file_detail_with_associations(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("src/main.py")
        issue = api_db.create_issue("Fix the bug")
        api_db.add_file_association(f.id, issue.id, "bug_in")
        resp = await client.get(f"/api/files/{f.id}")
        data = resp.json()
        assert len(data["associations"]) == 1
        assert data["associations"][0]["issue_id"] == issue.id
        assert data["associations"][0]["assoc_type"] == "bug_in"
        assert data["associations"][0]["issue_title"] == "Fix the bug"

    async def test_get_file_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/test-f-nope")
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"

    async def test_get_file_findings(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Too long"},
            ],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        resp = await client.get(f"/api/files/{f.id}/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["rule_id"] == "E501"

    async def test_post_scan_results(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [
                    {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Too long"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["findings_created"] == 1

    async def test_post_scan_results_infers_file_language(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [
                    {"path": "src/inferred.py", "rule_id": "E501", "severity": "low", "message": "Too long"},
                    {"path": "docs/inferred.md", "rule_id": "MD001", "severity": "low", "message": "Heading"},
                    {"path": "tools/inferred.unknownext", "rule_id": "X1", "severity": "low", "message": "Unknown"},
                ],
            },
        )
        assert resp.status_code == 200

        py = await client.get("/api/files?language=python")
        md = await client.get("/api/files?language=markdown")
        unknown = await client.get("/api/files?language=")

        assert py.status_code == 200
        assert md.status_code == 200
        assert unknown.status_code == 200
        assert [item["path"] for item in py.json()["results"]] == ["src/inferred.py"]
        assert [item["path"] for item in md.json()["results"]] == ["docs/inferred.md"]
        assert [item["path"] for item in unknown.json()["results"]] == ["tools/inferred.unknownext"]

    async def test_post_scan_results_unknown_severity_maps_to_info(self, client: AsyncClient) -> None:
        """Unknown severity strings are accepted and mapped to 'info' with warnings."""
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [
                    {"path": "a.py", "rule_id": "E501", "severity": "extreme", "message": "Bad"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["findings_created"] == 1
        assert any("extreme" in w for w in data["warnings"])

    async def test_post_scan_results_non_string_severity_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [
                    {"path": "a.py", "rule_id": "E501", "severity": 42, "message": "Bad"},
                ],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_post_scan_results_scanned_paths_closes_fixed_issue(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        """End-to-end: a finding's file present solely via ``scanned_paths`` on a
        clean re-scan flips the finding unseen and cascade-closes its issue
        (proves the route threads ``scanned_paths`` into ingest)."""
        first = await client.post(
            "/api/loom/scan-results",
            json={
                "scan_source": "wardline",
                "findings": [{"path": "src/a.py", "rule_id": "WLN-001", "message": "sink", "fingerprint": "fp-e2e"}],
            },
        )
        assert first.status_code == 200
        finding = api_db.find_finding_by_fingerprint("wardline", "fp-e2e")
        assert finding is not None
        issue = api_db.promote_finding_to_issue(finding["id"], actor="t")["issue"]

        clean = await client.post(
            "/api/loom/scan-results",
            json={
                "scan_source": "wardline",
                "findings": [],
                "scanned_paths": ["src/a.py"],
                "mark_unseen": True,
            },
        )
        assert clean.status_code == 200
        assert api_db.get_finding(finding["id"])["status"] == "unseen_in_latest"
        refreshed = api_db.get_issue(issue.id)
        assert api_db._resolve_status_category(refreshed.type, refreshed.status) == "done"

    async def test_post_scan_results_without_scanned_paths_still_works(self, client: AsyncClient) -> None:
        """Back-compat: a body omitting ``scanned_paths`` is accepted unchanged."""
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Too long"}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["findings_created"] == 1

    async def test_post_scan_results_scanned_paths_must_be_array(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/loom/scan-results",
            json={"scan_source": "wardline", "findings": [], "scanned_paths": "src/a.py", "mark_unseen": True},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_post_scan_results_scanned_paths_rejects_non_string_element(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/loom/scan-results",
            json={"scan_source": "wardline", "findings": [], "scanned_paths": ["ok.py", 42], "mark_unseen": True},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_post_file_association(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("src/main.py")
        issue = api_db.create_issue("Fix bug")
        resp = await client.post(
            f"/api/files/{f.id}/associations",
            json={"issue_id": issue.id, "assoc_type": "bug_in"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "created"

    async def test_post_file_association_invalid_type(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("src/main.py")
        issue = api_db.create_issue("Fix bug")
        resp = await client.post(
            f"/api/files/{f.id}/associations",
            json={"issue_id": issue.id, "assoc_type": "invalid"},
        )
        assert resp.status_code == 400

    async def test_post_file_association_nonexistent_issue(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("src/main.py")
        resp = await client.post(
            f"/api/files/{f.id}/associations",
            json={"issue_id": "nonexistent-id", "assoc_type": "bug_in"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "Issue not found" in body["error"]

    async def test_post_invalid_json_body_returns_400(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        """Bug filigree-4d8aa1: invalid JSON must return 400, not swallow unexpected exceptions."""
        f = api_db.register_file("src/main.py")
        resp = await client.post(
            f"/api/files/{f.id}/associations",
            content=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Invalid JSON body" in resp.json()["error"]

    async def test_schema_endpoint_statuses_updated(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        # All endpoints should now be live
        for ep in data["endpoints"]:
            assert ep["status"] == "live", f"{ep['path']} should be live"


class TestBidirectionalEndpoints:
    """Tests for issue→files and issue→findings endpoints."""

    async def test_issue_files_endpoint(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        issue = api_db.create_issue("Fix bug")
        f = api_db.register_file("src/main.py")
        api_db.add_file_association(f.id, issue.id, "bug_in")
        resp = await client.get(f"/api/issue/{issue.id}/files")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["file_path"] == "src/main.py"

    async def test_issue_findings_endpoint(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        issue = api_db.create_issue("Fix bug")
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Too long"}],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        api_db.add_file_association(f.id, issue.id, "scan_finding")
        resp = await client.get(f"/api/issue/{issue.id}/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    async def test_issue_files_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issue/test-nope/files")
        assert resp.status_code == 404


class TestFileFindingUpdateEndpoint:
    async def test_patch_file_finding_closes_and_links_issue(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Too long"}],
        )
        file_record = api_db.get_file_by_path("a.py")
        assert file_record is not None
        finding = api_db.get_findings(file_record.id)[0]
        issue = api_db.create_issue("Fix lint finding", type="bug")

        resp = await client.patch(
            f"/api/files/{file_record.id}/findings/{finding.id}",
            json={"status": "fixed", "issue_id": issue.id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "fixed"
        assert data["issue_id"] == issue.id

        associations = api_db.get_file_associations(file_record.id)
        assert any(a["issue_id"] == issue.id and a["assoc_type"] == "bug_in" for a in associations)

    async def test_patch_file_finding_requires_status_or_issue_id(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Too long"}],
        )
        file_record = api_db.get_file_by_path("a.py")
        assert file_record is not None
        finding = api_db.get_findings(file_record.id)[0]

        resp = await client.patch(f"/api/files/{file_record.id}/findings/{finding.id}", json={})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"


class TestHotspotsEndpoint:
    """Tests for the hotspots API endpoint."""

    async def test_hotspots_endpoint(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S001", "severity": "critical", "message": "Critical"},
                {"path": "b.py", "rule_id": "E501", "severity": "low", "message": "Low"},
            ],
        )
        resp = await client.get("/api/files/hotspots")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["score"] > data[1]["score"]

    async def test_hotspots_with_limit(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        for i in range(5):
            api_db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": f"file{i}.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
            )
        resp = await client.get("/api/files/hotspots?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


class TestPaginatedEndpoints:
    """Tests for paginated API response format."""

    async def test_list_files_paginated_response(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        for i in range(5):
            api_db.register_file(f"file{i}.py")
        resp = await client.get("/api/files?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["limit"] == 3
        assert data["offset"] == 0
        assert data["has_more"] is True
        assert len(data["results"]) == 3

    async def test_file_findings_paginated_response(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": f"E{i}", "severity": "low", "message": f"msg{i}"} for i in range(5)],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        resp = await client.get(f"/api/files/{f.id}/findings?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["has_more"] is True
        assert len(data["results"]) == 3


class TestScanResultsEndpointEnhancements:
    """Tests for enhanced scan results endpoint (mark_unseen, new_finding_ids)."""

    async def test_empty_findings_returns_200(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "findings": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["new_finding_ids"] == []

    async def test_non_empty_findings_returns_200(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
            },
        )
        assert resp.status_code == 200

    async def test_clarion_registry_unavailable_returns_503(self, unavailable_clarion_client: AsyncClient) -> None:
        resp = await unavailable_clarion_client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
            },
        )

        assert resp.status_code == 503
        data = resp.json()
        assert data["code"] == "REGISTRY_UNAVAILABLE"
        # CONTRACT-1: scan-results goes through the batch endpoint now;
        # the unreachable message has the batch-resolve prefix.
        assert "Clarion batch resolve unreachable" in data["error"]

    async def test_new_finding_ids_in_response(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "new_finding_ids" in data
        assert len(data["new_finding_ids"]) == 1

    async def test_mark_unseen_via_api(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        # First scan: 2 findings
        await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [
                    {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"},
                    {"path": "a.py", "rule_id": "E502", "severity": "low", "message": "m2"},
                ],
            },
        )
        # Second scan: only E501, with mark_unseen
        await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "ruff",
                "mark_unseen": True,
                "findings": [{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"}],
            },
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        findings = api_db.get_findings(f.id)
        statuses = {fi.rule_id: fi.status for fi in findings}
        assert statuses["E501"] == "open"
        assert statuses["E502"] == "unseen_in_latest"

    async def test_create_observations_via_api(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "codex",
                "create_observations": True,
                "findings": [
                    {
                        "path": "src/main.py",
                        "rule_id": "logic-error",
                        "severity": "high",
                        "message": "Off-by-one in pagination loop",
                        "line_start": 42,
                    },
                ],
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["observations_created"] == 1


class TestCompleteScanRunEndpoint:
    """Tests for complete_scan_run parameter on scan results endpoint."""

    async def test_complete_scan_run_false_keeps_running(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.create_scan_run(
            scan_run_id="batch-api-test",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py", "b.py"],
            file_ids=["f-1", "f-2"],
        )
        api_db.update_scan_run_status("batch-api-test", "running")

        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "codex",
                "scan_run_id": "batch-api-test",
                "complete_scan_run": False,
                "findings": [{"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "test"}],
            },
        )
        assert resp.status_code == 200
        run = api_db.get_scan_run("batch-api-test")
        assert run["status"] == "running"

    async def test_complete_scan_run_true_completes(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.create_scan_run(
            scan_run_id="batch-api-done",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        api_db.update_scan_run_status("batch-api-done", "running")

        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "codex",
                "scan_run_id": "batch-api-done",
                "complete_scan_run": True,
                "findings": [{"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "test"}],
            },
        )
        assert resp.status_code == 200
        run = api_db.get_scan_run("batch-api-done")
        assert run["status"] == "completed"


class TestSortBySeverityEndpoint:
    """Tests for sort=severity on findings endpoint."""

    async def test_sort_findings_by_severity(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E001", "severity": "low", "message": "Low"},
                {"path": "a.py", "rule_id": "S001", "severity": "critical", "message": "Critical"},
                {"path": "a.py", "rule_id": "H001", "severity": "high", "message": "High"},
            ],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        resp = await client.get(f"/api/files/{f.id}/findings?sort=severity")
        assert resp.status_code == 200
        severities = [r["severity"] for r in resp.json()["results"]]
        assert severities == ["critical", "high", "low"]

    async def test_sort_findings_invalid_returns_400(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("a.py")
        resp = await client.get(f"/api/files/{f.id}/findings?sort=bogus")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "Invalid sort field" in body["error"]
        assert "severity" in body["error"]
        assert "updated_at" in body["error"]


class TestMinFindingsEndpoint:
    """Tests for min_findings filter on files endpoint."""

    async def test_min_findings_filters(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "many.py", "rule_id": f"E{i}", "severity": "low", "message": f"msg{i}"} for i in range(5)],
        )
        api_db.register_file("empty.py")
        resp = await client.get("/api/files?min_findings=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["path"] == "many.py"

    async def test_min_findings_invalid(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files?min_findings=abc")
        assert resp.status_code == 400


class TestHasSeverityEndpoint:
    """Tests for has_severity filter on files endpoint."""

    async def test_has_severity_filters_via_api(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "critical.py", "rule_id": "S1", "severity": "critical", "message": "bad"},
                {"path": "lowonly.py", "rule_id": "E1", "severity": "low", "message": "minor"},
            ],
        )
        resp = await client.get("/api/files?has_severity=critical")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["path"] == "critical.py"

    async def test_has_severity_invalid_returns_400(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.register_file("a.py")
        resp = await client.get("/api/files?has_severity=bogus")
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"


class TestTimelineEndpoint:
    """Tests for GET /api/files/{file_id}/timeline."""

    async def test_timeline_endpoint(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        resp = await client.get(f"/api/files/{f.id}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "total" in data
        assert len(data["results"]) >= 1
        assert "id" in data["results"][0]

    async def test_timeline_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/test-f-nope/timeline")
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"

    async def test_timeline_pagination(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": f"E{i:03d}", "severity": "low", "message": f"msg{i}"} for i in range(10)],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        resp = await client.get(f"/api/files/{f.id}/timeline?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 3
        assert data["has_more"] is True

    async def test_timeline_event_type_filter_finding(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        issue = api_db.create_issue("Fix it")
        api_db.add_file_association(f.id, issue.id, "bug_in")

        resp = await client.get(f"/api/files/{f.id}/timeline?event_type=finding")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] > 0
        for e in data["results"]:
            assert e["type"].startswith("finding_")

    async def test_timeline_event_type_filter_association(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
        )
        f = api_db.get_file_by_path("a.py")
        assert f is not None
        issue = api_db.create_issue("Fix it")
        api_db.add_file_association(f.id, issue.id, "bug_in")

        resp = await client.get(f"/api/files/{f.id}/timeline?event_type=association")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] > 0
        for e in data["results"]:
            assert e["type"] == "association_created"


class TestCacheControlHeaders:
    """Verify Cache-Control headers on file GET endpoints."""

    async def test_list_files_no_cache(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files")
        assert resp.headers.get("cache-control") == "no-cache"

    async def test_file_detail_no_cache(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("src/main.py")
        resp = await client.get(f"/api/files/{f.id}")
        assert resp.headers.get("cache-control") == "no-cache"

    async def test_file_findings_max_age_30(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("src/main.py")
        resp = await client.get(f"/api/files/{f.id}/findings")
        assert resp.headers.get("cache-control") == "max-age=30"

    async def test_schema_max_age_3600(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        assert resp.headers.get("cache-control") == "max-age=3600"


class TestInputValidation400s:
    """Malformed client input must return 400 with structured error, never 500."""

    # -- P2a: scan body must be a JSON object --------------------------------

    async def test_scan_body_is_list(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            content="[]",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_scan_body_is_string(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            content='"hello"',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_association_body_is_list(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("x.py")
        resp = await client.post(
            f"/api/files/{f.id}/associations",
            content="[]",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    # -- P2b: malformed finding entries --------------------------------------

    async def test_scan_finding_missing_path(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "findings": [{"severity": "low"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "path" in resp.json()["error"].lower()

    async def test_scan_finding_missing_rule_id(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "findings": [{"path": "a.py", "severity": "low", "message": "m"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "rule_id" in resp.json()["error"].lower()

    async def test_scan_finding_missing_message(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "findings": [{"path": "a.py", "rule_id": "E1", "severity": "low"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "message" in resp.json()["error"].lower()

    async def test_scan_finding_is_string(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "findings": ["not-a-dict"]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_scan_finding_is_number(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "findings": [42]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_scan_create_observations_must_be_boolean(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "create_observations": "yes", "findings": []},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_scan_mark_unseen_must_be_boolean(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": "ruff", "mark_unseen": "false", "findings": []},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "mark_unseen must be a boolean" in resp.json()["error"]

    # -- P2c: pagination query params ----------------------------------------

    async def test_files_limit_not_int(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files?limit=abc")
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_files_offset_not_int(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files?offset=xyz")
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_findings_limit_not_int(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        f = api_db.register_file("x.py")
        resp = await client.get(f"/api/files/{f.id}/findings?limit=nope")
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_hotspots_limit_not_int(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/hotspots?limit=bad")
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    # -- scan_source type validation -------------------------------------------

    async def test_scan_results_non_string_scan_source_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/scan-results",
            json={"scan_source": 123, "findings": []},
        )
        assert resp.status_code == 400
        assert "scan_source" in resp.json()["error"]

    # -- negative pagination values --------------------------------------------

    async def test_files_negative_limit_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files?limit=-1")
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_files_negative_offset_rejected(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files?offset=-5")
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"


class TestFileStatsEndpoint:
    """Tests for GET /api/files/stats — global findings severity breakdown."""

    async def test_stats_empty_db(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_findings"] == 0
        assert data["files_with_findings"] == 0

    async def test_stats_with_findings(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S001", "severity": "critical", "message": "sec"},
                {"path": "b.py", "rule_id": "E501", "severity": "low", "message": "style"},
            ],
        )
        resp = await client.get("/api/files/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["critical"] >= 1
        assert data["low"] >= 1
        assert data["total_findings"] >= 2
        assert data["files_with_findings"] == 2

    async def test_stats_response_shape(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/stats")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("critical", "high", "medium", "low", "info", "total_findings", "open_findings", "files_with_findings"):
            assert key in data, f"Missing key: {key}"


class TestObservationStatsEndpoint:
    """Tests for GET /api/observations/stats."""

    async def test_stats_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/observations/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["stale_count"] == 0
        assert data["expiring_soon_count"] == 0

    async def test_stats_with_observations(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        api_db.create_observation("test obs 1")
        api_db.create_observation("test obs 2", file_path="src/foo.py")
        resp = await client.get("/api/observations/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        for key in ("count", "stale_count", "oldest_hours", "expiring_soon_count"):
            assert key in data, f"Missing key: {key}"

    async def test_stats_pre_v7_database_returns_503(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        """Pre-v7 databases (missing observations table) return 503 instead of fabricated zeros."""
        import sqlite3
        from unittest.mock import patch

        with patch.object(api_db, "observation_stats", side_effect=sqlite3.OperationalError("no such table: observations")):
            resp = await client.get("/api/observations/stats")
        assert resp.status_code == 503
        data = resp.json()
        assert data["code"] == "IO"
