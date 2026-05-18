"""MCP tool tests for finding triage handlers (get, list, update, batch, promote, dismiss).

Tests the MCP handler layer via call_tool() — handler wiring, argument parsing,
validation, and error mapping. Core DB methods are covered in test_finding_triage.py;
these tests verify the MCP integration layer on top.
"""

from __future__ import annotations

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.registry import RegistryFileNotFoundError, RegistryUnavailableError, ResolvedFile
from filigree.types.api import ErrorCode
from filigree.types.core import make_entity_id, make_file_id
from tests.mcp._helpers import _parse


def _seed_findings(db: FiligreeDB) -> dict[str, str]:
    """Create a file with 3 findings and return {name: finding_id}."""
    db.register_file("src/main.py", language="python")
    result = db.process_scan_results(
        scan_source="test-scanner",
        findings=[
            {"path": "src/main.py", "rule_id": "logic-error", "severity": "high", "message": "Off by one"},
            {"path": "src/main.py", "rule_id": "type-error", "severity": "medium", "message": "Wrong return type"},
            {"path": "src/main.py", "rule_id": "injection", "severity": "critical", "message": "SQL injection"},
        ],
    )
    ids = result["new_finding_ids"]
    return {"obo": ids[0], "type": ids[1], "sqli": ids[2]}


class TestGetFindingTool:
    async def test_get_finding(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("get_finding", {"finding_id": ids["obo"]}))
        assert data["finding_id"] == ids["obo"]
        assert "id" not in data
        assert data["rule_id"] == "logic-error"
        assert data["severity"] == "high"

    async def test_get_finding_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("get_finding", {"finding_id": "nonexistent"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_finding_empty_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("get_finding", {"finding_id": ""}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_get_finding_missing_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("get_finding", {}))
        assert data["code"] == ErrorCode.VALIDATION


class TestListFindingsTool:
    async def test_list_all(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        data = _parse(await call_tool("list_findings", {}))
        assert len(data["items"]) == 3
        assert all("finding_id" in item for item in data["items"])
        assert all("id" not in item for item in data["items"])

    async def test_filter_by_severity(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        data = _parse(await call_tool("list_findings", {"severity": "critical"}))
        assert len(data["items"]) == 1
        assert data["items"][0]["rule_id"] == "injection"

    async def test_filter_by_status(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        data = _parse(await call_tool("list_findings", {"status": "open"}))
        assert len(data["items"]) == 3

    async def test_pagination(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        page1 = _parse(await call_tool("list_findings", {"limit": 2, "offset": 0}))
        assert len(page1["items"]) == 2
        page2 = _parse(await call_tool("list_findings", {"limit": 2, "offset": 2}))
        assert len(page2["items"]) == 1


class TestReportFindingTool:
    async def test_report_finding_uses_registry_resolved_file_id(self, mcp_db: FiligreeDB) -> None:
        class FixedRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                return {
                    "file_id": make_file_id("core:file:report-target@src/report_target.py"),
                    "content_hash": "",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "local",
                }

            def is_displaced(self) -> bool:
                return False

        mcp_db.registry = FixedRegistry()

        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/report_target.py",
                    "rule_id": "agent-noted-risk",
                    "message": "Agent spotted a follow-up risk",
                    "severity": "medium",
                    "line_start": 7,
                },
            )
        )

        assert data["file_id"] == "core:file:report-target@src/report_target.py"

    async def test_report_finding_registry_unavailable_returns_error_response(self, mcp_db: FiligreeDB) -> None:
        class UnavailableRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError(
                    "Clarion registry unavailable for test",
                    url="http://clarion.test/api/v1/files?path=src%2Freport_target.py",
                    path=path,
                    cause_kind="network",
                )

            def is_displaced(self) -> bool:
                return False

        mcp_db.registry = UnavailableRegistry()

        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/report_target.py",
                    "rule_id": "agent-noted-risk",
                    "message": "Agent spotted a follow-up risk",
                    "severity": "medium",
                },
            )
        )

        assert data["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert data["details"]["cause"] == "registry_unavailable"
        assert data["details"]["cause_kind"] == "network"
        assert data["details"]["path"] == "src/report_target.py"
        assert data["details"]["url"] == "http://clarion.test/api/v1/files?path=src%2Freport_target.py"
        assert "Registry unavailable" in data["error"]
        assert data["details"]["cause"] == "registry_unavailable"

    async def test_report_finding_registry_file_not_found_returns_not_found(self, mcp_db: FiligreeDB) -> None:
        class MissingFileRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryFileNotFoundError(
                    "Clarion registry could not resolve file at http://clarion.test/api/v1/files?path=missing.py: "
                    "HTTP 404 not indexed",
                    status_code=404,
                    url="http://clarion.test/api/v1/files?path=missing.py",
                )

            def is_displaced(self) -> bool:
                return False

        mcp_db.registry = MissingFileRegistry()

        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "missing.py",
                    "rule_id": "agent-noted-risk",
                    "message": "Agent spotted a follow-up risk",
                    "severity": "medium",
                },
            )
        )

        assert data["code"] == ErrorCode.NOT_FOUND
        assert data["details"]["cause"] == "registry_file_not_found"

    async def test_report_finding_does_not_register_file_after_ingest(self, mcp_db: FiligreeDB) -> None:
        class CountingCanonicalRegistry:
            resolve_calls = 0

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                self.resolve_calls += 1
                canonical_path = path.casefold()
                return {
                    "file_id": make_entity_id(f"core:file:{canonical_path.replace('/', ':')}"),
                    "content_hash": f"hash:{canonical_path}",
                    "canonical_path": canonical_path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return False

        registry = CountingCanonicalRegistry()
        mcp_db.registry = registry

        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "SRC/Report_Target.py",
                    "rule_id": "agent-noted-risk",
                    "message": "Agent spotted a follow-up risk",
                    "severity": "medium",
                },
            )
        )

        assert data["file_id"] == "core:file:src:report_target.py"
        assert registry.resolve_calls == 1

    async def test_report_finding_default_does_not_create_observation(self, mcp_db: FiligreeDB) -> None:
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/report_target.py",
                    "rule_id": "agent-noted-risk",
                    "message": "Agent spotted a follow-up risk",
                    "severity": "medium",
                    "line_start": 7,
                    "response_detail": "full",
                },
            )
        )

        observations = mcp_db.list_observations(file_path="src/report_target.py")
        assert observations == []
        assert data["observations_created"] == 0
        assert "observation_id" not in data
        assert data["observation_ids"] == []

    async def test_report_finding_can_create_observation_when_requested(self, mcp_db: FiligreeDB) -> None:
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/report_target.py",
                    "rule_id": "agent-noted-risk",
                    "message": "Agent spotted a follow-up risk",
                    "severity": "medium",
                    "line_start": 7,
                    "response_detail": "full",
                    "create_observation": True,
                },
            )
        )

        observations = mcp_db.list_observations(file_path="src/report_target.py")
        assert len(observations) == 1
        assert data["observations_created"] == 1
        assert data["observation_id"] == observations[0]["id"]
        assert data["observation_ids"] == [observations[0]["id"]]


class TestUpdateFindingTool:
    async def test_update_status(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("update_finding", {"finding_id": ids["obo"], "status": "acknowledged"}))
        assert data["finding_id"] == ids["obo"]
        assert "id" not in data
        assert data["status"] == "acknowledged"

    async def test_update_issue_id(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        issue = mcp_db.create_issue("Bug ticket")
        data = _parse(await call_tool("update_finding", {"finding_id": ids["sqli"], "issue_id": issue.id}))
        assert data["finding_id"] == ids["sqli"]
        assert data["issue_id"] == issue.id

    async def test_update_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("update_finding", {"finding_id": "nonexistent", "status": "acknowledged"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_update_no_fields_rejected(self, mcp_db: FiligreeDB) -> None:
        """At least one of status or issue_id must be provided."""
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("update_finding", {"finding_id": ids["obo"]}))
        assert data["code"] == ErrorCode.VALIDATION
        assert "at least one" in data["error"].lower()

    async def test_update_invalid_status_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("update_finding", {"finding_id": ids["obo"], "status": "banana"}))
        assert data["code"] == ErrorCode.VALIDATION


class TestBatchUpdateFindingsTool:
    async def test_batch_update(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(
            await call_tool(
                "batch_update_findings",
                {"finding_ids": [ids["obo"], ids["type"]], "status": "acknowledged"},
            )
        )
        assert len(data["succeeded"]) == 2
        assert data["failed"] == []

    async def test_batch_update_partial_failure(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(
            await call_tool(
                "batch_update_findings",
                {"finding_ids": [ids["obo"], "nonexistent"], "status": "acknowledged"},
            )
        )
        assert len(data["succeeded"]) == 1
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "nonexistent"
        assert data["failed"][0]["code"] == ErrorCode.NOT_FOUND

    async def test_batch_update_empty_ids_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("batch_update_findings", {"finding_ids": [], "status": "acknowledged"}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_batch_update_missing_status_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("batch_update_findings", {"finding_ids": [ids["obo"]], "status": ""}))
        assert data["code"] == ErrorCode.VALIDATION


class TestPromoteFindingTool:
    async def test_promote_creates_issue(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("promote_finding", {"finding_id": ids["sqli"]}))
        assert "issue_id" in data
        assert "id" not in data
        assert "observation_id" not in data
        assert data["type"] == "bug"
        assert "SQL injection" in data["title"]
        assert "from-finding" in data["labels"]
        assert mcp_db.get_finding(ids["sqli"])["issue_id"] == data["issue_id"]
        assert mcp_db.list_observations() == []

    async def test_promote_with_priority_override(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("promote_finding", {"finding_id": ids["obo"], "priority": 0}))
        assert data["priority"] == 0

    async def test_promote_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("promote_finding", {"finding_id": "nonexistent"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_promote_empty_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("promote_finding", {"finding_id": ""}))
        assert data["code"] == ErrorCode.VALIDATION


class TestDismissFindingTool:
    async def test_dismiss_marks_false_positive(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("dismiss_finding", {"finding_id": ids["type"]}))
        assert data["finding_id"] == ids["type"]
        assert "id" not in data
        assert data["status"] == "false_positive"

    async def test_dismiss_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("dismiss_finding", {"finding_id": "nonexistent"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_dismiss_empty_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("dismiss_finding", {"finding_id": ""}))
        assert data["code"] == ErrorCode.VALIDATION
