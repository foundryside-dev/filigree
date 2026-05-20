"""MCP tool tests for finding triage handlers (get, list, update, batch, promote, dismiss).

Tests the MCP handler layer via call_tool() — handler wiring, argument parsing,
validation, and error mapping. Core DB methods are covered in test_finding_triage.py;
these tests verify the MCP integration layer on top.
"""

from __future__ import annotations

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.registry import (
    BatchQuery,
    BatchResolution,
    RegistryBriefingBlockedError,
    RegistryFileNotFoundError,
    RegistryUnavailableError,
    ResolvedFile,
    resolve_files_batch_via_loop,
)
from filigree.types.api import ErrorCode
from filigree.types.core import make_entity_id
from tests._fakes.registry import FixedRegistry
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
        mcp_db.registry = FixedRegistry(file_id="core:file:report-target@src/report_target.py")

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

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

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
                    "Clarion registry could not resolve file at http://clarion.test/api/v1/files?path=missing.py: HTTP 404 not indexed",
                    status_code=404,
                    url="http://clarion.test/api/v1/files?path=missing.py",
                )

            def is_displaced(self) -> bool:
                return False

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

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

    async def test_report_finding_registry_briefing_blocked_returns_briefing_blocked(self, mcp_db: FiligreeDB) -> None:
        class BriefingBlockedRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryBriefingBlockedError(
                    "Clarion registry refuses briefing-blocked file",
                    status_code=403,
                    url="http://clarion.test/api/v1/files?path=secret.py",
                )

            def is_displaced(self) -> bool:
                return False

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

        mcp_db.registry = BriefingBlockedRegistry()

        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "secret.py",
                    "rule_id": "agent-noted-risk",
                    "message": "Agent spotted a follow-up risk",
                    "severity": "medium",
                },
            )
        )

        assert data["code"] == ErrorCode.BRIEFING_BLOCKED
        assert data["details"]["cause"] == "registry_briefing_blocked"

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

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

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

    async def test_report_finding_update_fallback_is_scoped_to_reported_file(self, mcp_db: FiligreeDB) -> None:
        finding_shape = {
            "rule_id": "same-risk",
            "message": "Identical finding text",
            "severity": "medium",
            "line_start": 7,
        }
        mcp_db.process_scan_results(
            scan_source="agent",
            findings=[{"path": "src/alpha.py", **finding_shape}],
            create_observations=True,
        )
        mcp_db.process_scan_results(
            scan_source="agent",
            findings=[{"path": "src/beta.py", **finding_shape}],
            create_observations=True,
        )
        alpha_file = mcp_db.get_file_by_path("src/alpha.py")
        beta_file = mcp_db.get_file_by_path("src/beta.py")
        assert alpha_file is not None
        assert beta_file is not None
        alpha_finding = mcp_db.list_findings_global(file_id=alpha_file.id, scan_source="agent")["findings"][0]
        beta_finding = mcp_db.list_findings_global(file_id=beta_file.id, scan_source="agent")["findings"][0]
        beta_observation = mcp_db.list_observations(file_id=beta_file.id)[0]
        mcp_db.conn.execute(
            "UPDATE scan_findings SET updated_at = ? WHERE id = ?",
            ("2999-01-01T00:00:00+00:00", alpha_finding["id"]),
        )
        mcp_db.conn.commit()

        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/beta.py",
                    "create_observation": True,
                    **finding_shape,
                },
            )
        )

        assert data["finding_result"] == "updated"
        assert data["file_id"] == beta_file.id
        assert data["finding_id"] == beta_finding["id"]
        assert data["observation_id"] == beta_observation["id"]


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

    async def test_promote_rejects_invalid_priority(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("promote_finding", {"finding_id": ids["obo"], "priority": 5}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "priority must be <= 4"
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_rejects_invalid_labels_shape(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("promote_finding", {"finding_id": ids["obo"], "labels": ["ok", 42]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "labels must be a list of strings"
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_returns_validation_error_for_reserved_label(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("promote_finding", {"finding_id": ids["obo"], "labels": ["severity:high"]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert "system-managed auto-tag namespace" in data["error"]
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_carries_labels_to_created_issue(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("promote_finding", {"finding_id": ids["obo"], "labels": ["cluster:mcp"]}))

        assert set(data["labels"]) == {"from-finding", "cluster:mcp"}

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

    async def test_dismiss_non_string_status_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("dismiss_finding", {"finding_id": ids["type"], "status": ["fixed"]}))
        assert data["code"] == ErrorCode.VALIDATION
        assert "status" in data["error"].lower()

    async def test_dismiss_non_string_reason_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("dismiss_finding", {"finding_id": ids["type"], "reason": ["not", "a", "string"]}))
        assert data["code"] == ErrorCode.VALIDATION
        assert "reason" in data["error"].lower()
