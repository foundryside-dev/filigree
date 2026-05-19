"""Dashboard API tests — file records, scan results, and scan source filtering."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from filigree.registry import RegistryFileNotFoundError, RegistryUnavailableError, ResolvedFile
from tests.conftest import PopulatedDB


class TestFilesSchemaAPI:
    """GET /api/files/_schema — API discovery for file/scan features."""

    async def test_schema_returns_valid_severities(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["valid_severities"]) == {"critical", "high", "medium", "low", "info"}

    async def test_schema_returns_valid_finding_statuses(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert "unseen_in_latest" in data["valid_finding_statuses"]

    async def test_schema_returns_valid_association_types(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert "bug_in" in data["valid_association_types"]
        assert "scan_finding" in data["valid_association_types"]

    async def test_schema_returns_valid_sort_fields(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert set(data["valid_file_sort_fields"]) == {"updated_at", "first_seen", "path", "language"}
        assert set(data["valid_finding_sort_fields"]) == {"updated_at", "severity"}

    async def test_schema_returns_endpoints_catalog(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert isinstance(data["endpoints"], list)
        assert len(data["endpoints"]) >= 1
        ep = data["endpoints"][0]
        assert "method" in ep
        assert "path" in ep
        assert "description" in ep

    async def test_schema_has_cache_control(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        assert resp.headers.get("cache-control") == "max-age=3600"

    async def test_schema_returns_registry_backend_config_flags(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()

        assert data["config_flags"] == {
            "registry_backend": "local",
            "registry_backend_features": ["local", "clarion"],
            "allow_local_fallback": False,
        }

    async def test_schema_config_flags_reflect_project_backend(self, clarion_fallback_client: AsyncClient) -> None:
        resp = await clarion_fallback_client.get("/api/files/_schema")
        data = resp.json()

        assert data["config_flags"]["registry_backend"] == "clarion"
        assert data["config_flags"]["registry_backend_features"] == ["local", "clarion"]
        assert data["config_flags"]["allow_local_fallback"] is True


class TestScanResultsRegistryErrors:
    async def test_scan_results_returns_not_found_for_unknown_clarion_file(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        class MissingFileRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryFileNotFoundError(
                    "Clarion registry could not resolve file at http://clarion.test/api/v1/files?path=missing.py: HTTP 404 not indexed",
                    status_code=404,
                    url="http://clarion.test/api/v1/files?path=missing.py",
                )

            def is_displaced(self) -> bool:
                return True

        dashboard_db.db.registry = MissingFileRegistry()

        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "codex",
                "findings": [{"path": "missing.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            },
        )

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "NOT_FOUND"
        assert "HTTP 404 not indexed" in body["error"]

    @pytest.mark.parametrize("path", ["/api/v1/scan-results", "/api/loom/scan-results", "/api/scan-results"])
    async def test_scan_results_returns_registry_unavailable_code(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        path: str,
    ) -> None:
        class UnavailableRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError("Clarion registry unavailable for test")

            def is_displaced(self) -> bool:
                return True

        dashboard_db.db.registry = UnavailableRegistry()

        resp = await client.post(
            path,
            json={
                "scan_source": "codex",
                "findings": [{"path": "missing.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            },
        )

        assert resp.status_code == 503
        body = resp.json()
        assert body["code"] == "REGISTRY_UNAVAILABLE"
        assert body["code"] != "IO"


class TestScanRunsAPI:
    """GET /api/scan-runs — scan run history."""

    async def test_empty_table(self, client: AsyncClient) -> None:
        resp = await client.get("/api/scan-runs")
        assert resp.status_code == 200
        assert resp.json() == {"scan_runs": []}

    async def test_single_scan_run(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-001",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/scan-runs")
        assert resp.status_code == 200
        runs = resp.json()["scan_runs"]
        assert len(runs) == 1
        assert runs[0]["scan_run_id"] == "run-001"
        assert runs[0]["scan_source"] == "codex"
        assert runs[0]["total_findings"] == 1
        assert runs[0]["files_scanned"] == 1

    async def test_multiple_runs_ordered_by_recent(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-old",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        dashboard_db.db.process_scan_results(
            scan_source="claude",
            scan_run_id="run-new",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "high", "message": "m"}],
        )
        resp = await client.get("/api/scan-runs")
        runs = resp.json()["scan_runs"]
        assert len(runs) == 2
        # Most recent first
        assert runs[0]["scan_run_id"] == "run-new"
        assert runs[1]["scan_run_id"] == "run-old"

    async def test_limit_param(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        for i in range(5):
            dashboard_db.db.process_scan_results(
                scan_source="ruff",
                scan_run_id=f"run-{i:03d}",
                findings=[{"path": f"f{i}.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            )
        resp = await client.get("/api/scan-runs?limit=2")
        runs = resp.json()["scan_runs"]
        assert len(runs) == 2

    async def test_empty_run_id_excluded(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="ruff",
            scan_run_id="",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/scan-runs")
        assert resp.json() == {"scan_runs": []}

    async def test_no_cache_header(self, client: AsyncClient) -> None:
        resp = await client.get("/api/scan-runs")
        assert resp.headers.get("cache-control") == "no-cache"

    async def test_schema_includes_scan_runs_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        paths = [ep["path"] for ep in data["endpoints"]]
        assert "/api/scan-runs" in paths


class TestFilesScanSourceFilterAPI:
    """GET /api/files?scan_source=... — filter files by scan source."""

    async def test_scan_source_filters_files(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        dashboard_db.db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/files?scan_source=codex")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["path"] == "a.py"

    async def test_no_scan_source_returns_all(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        dashboard_db.db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/files")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2


class TestErrorMessagesIncludeValidOptions:
    """Error messages must include valid values to be self-documenting."""

    async def test_unknown_type_lists_valid_types(self, client: AsyncClient) -> None:
        resp = await client.get("/api/type/bogus_type")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert '"bogus_type"' in body["error"]
        # Must include at least some known types
        for expected in ("task", "bug", "feature"):
            assert expected in body["error"], f"Missing valid type '{expected}' in error"

    async def test_create_issue_unknown_type_lists_valid_types(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Bad", "type": "widgets"})
        assert resp.status_code == 400
        body = resp.json()
        assert "widgets" in body["error"]
        assert "task" in body["error"]

    async def test_priority_error_includes_valid_range(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(f"/api/issue/{ids['a']}", json={"priority": "high"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "0" in body["error"]
        assert "4" in body["error"]

    async def test_issue_not_found_includes_id(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issue/nonexistent-id-xyz")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "NOT_FOUND"
        assert "nonexistent-id-xyz" in body["error"]
