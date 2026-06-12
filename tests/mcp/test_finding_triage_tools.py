"""MCP tool tests for finding triage handlers (get, list, update, batch, promote, dismiss).

Tests the MCP handler layer via call_tool() — handler wiring, argument parsing,
validation, and error mapping. Core DB methods are covered in test_finding_triage.py;
these tests verify the MCP integration layer on top.
"""

from __future__ import annotations

from typing import cast

import pytest

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
from filigree.types.core import make_entity_id, make_issue_id
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
        data = _parse(await call_tool("finding_get", {"finding_id": ids["obo"]}))
        assert data["finding_id"] == ids["obo"]
        assert "id" not in data
        assert data["rule_id"] == "logic-error"
        assert data["severity"] == "high"

    async def test_get_finding_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_get", {"finding_id": "nonexistent"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_finding_empty_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_get", {"finding_id": ""}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_get_finding_missing_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_get", {}))
        assert data["code"] == ErrorCode.VALIDATION


class TestListFindingsTool:
    async def test_list_all(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_list", {}))
        assert len(data["items"]) == 3
        assert all("finding_id" in item for item in data["items"])
        assert all("id" not in item for item in data["items"])

    async def test_filter_by_severity(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_list", {"severity": "critical"}))
        assert len(data["items"]) == 1
        assert data["items"][0]["rule_id"] == "injection"

    async def test_filter_by_status(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_list", {"status": "open"}))
        assert len(data["items"]) == 3

    async def test_pagination(self, mcp_db: FiligreeDB) -> None:
        _seed_findings(mcp_db)
        page1 = _parse(await call_tool("finding_list", {"limit": 2, "offset": 0}))
        assert len(page1["items"]) == 2
        page2 = _parse(await call_tool("finding_list", {"limit": 2, "offset": 2}))
        assert len(page2["items"]) == 1


def _seed_wardline_mix(db: FiligreeDB) -> None:
    """Metric noise + a real defect + a baselined defect (FIL-2/X-5 shape)."""
    db.register_file("src/app.py", language="python")
    db.process_scan_results(
        scan_source="wardline",
        findings=[
            {
                "path": "src/app.py",
                "rule_id": "WLN-METRIC",
                "severity": "info",
                "message": "telemetry",
                "line_start": 1,
                "metadata": {"wardline": {"kind": "metric"}},
            },
            {
                "path": "src/app.py",
                "rule_id": "PY-WL-101",
                "severity": "high",
                "message": "real defect",
                "line_start": 10,
                "metadata": {"wardline": {"kind": "defect", "qualname": "app.handler"}},
            },
            {
                "path": "src/app.py",
                "rule_id": "PY-WL-102",
                "severity": "high",
                "message": "baselined",
                "line_start": 20,
                "metadata": {"wardline": {"kind": "defect", "suppression_state": "baselined"}},
            },
        ],
    )
    # A non-wardline (agent) finding: carries no wardline metadata, so it is
    # active-by-absence and must stay visible under the active default (mirrors
    # the core seed). Pins that the surface default-hide uses NOT-suppressed, not
    # suppression_state IS NULL matching.
    db.process_scan_results(
        scan_source="agent",
        findings=[{"path": "src/app.py", "rule_id": "agent-misuse", "severity": "medium", "message": "agent finding", "line_start": 30}],
    )


class TestListFindingsToolKindSuppression:
    """FIL-2/X-5: the MCP ``finding_list`` tool forwards the nested wardline
    axes (``kind``, ``suppression``) and ``rule_id`` to the core query."""

    async def test_filter_by_kind(self, mcp_db: FiligreeDB) -> None:
        # The surface defaults to suppression='active', so to see BOTH defects
        # (including the baselined one) the caller must opt in with
        # suppression='all'. This keeps the test's intent — 'kind forwards to
        # the core query and returns both defects' — under the new default.
        _seed_wardline_mix(mcp_db)
        data = _parse(await call_tool("finding_list", {"kind": "defect", "suppression": "all"}))
        assert sorted(i["rule_id"] for i in data["items"]) == ["PY-WL-101", "PY-WL-102"]

    async def test_default_excludes_suppressed(self, mcp_db: FiligreeDB) -> None:
        """filigree-2bdb878bd2: a plain ``finding_list`` work-query defaults to
        active-only at the agent surface — the baselined defect is hidden so it
        does not read as fresh, open work."""
        _seed_wardline_mix(mcp_db)
        data = _parse(await call_tool("finding_list", {}))
        rules = {i["rule_id"] for i in data["items"]}
        # metric (no suppression_state) + the active defect + the non-wardline
        # agent finding (active-by-absence); NOT the baselined one.
        assert rules == {"PY-WL-101", "WLN-METRIC", "agent-misuse"}
        assert "PY-WL-102" not in rules

    async def test_suppression_all_includes_suppressed(self, mcp_db: FiligreeDB) -> None:
        """``suppression='all'`` opts back in to the full set including suppressed."""
        _seed_wardline_mix(mcp_db)
        data = _parse(await call_tool("finding_list", {"suppression": "all"}))
        assert sorted(i["rule_id"] for i in data["items"]) == ["PY-WL-101", "PY-WL-102", "WLN-METRIC", "agent-misuse"]

    async def test_ticket_repro_status_open_severity_high(self, mcp_db: FiligreeDB) -> None:
        """The literal bug repro (filigree-2bdb878bd2): ``finding_list
        status=open severity=high`` previously returned the baselined defect
        mixed with the real one. The active default now excludes it; only
        suppression='all' brings it back."""
        _seed_wardline_mix(mcp_db)  # PY-WL-101 (active) + PY-WL-102 (baselined) are both open+high
        default = _parse(await call_tool("finding_list", {"status": "open", "severity": "high"}))
        assert [i["rule_id"] for i in default["items"]] == ["PY-WL-101"]  # baselined hidden
        with_all = _parse(await call_tool("finding_list", {"status": "open", "severity": "high", "suppression": "all"}))
        assert sorted(i["rule_id"] for i in with_all["items"]) == ["PY-WL-101", "PY-WL-102"]

    async def test_filter_real_unsuppressed_defects(self, mcp_db: FiligreeDB) -> None:
        _seed_wardline_mix(mcp_db)
        data = _parse(await call_tool("finding_list", {"kind": "defect", "suppression": "active"}))
        assert [i["rule_id"] for i in data["items"]] == ["PY-WL-101"]

    async def test_filter_by_suppression_baselined(self, mcp_db: FiligreeDB) -> None:
        _seed_wardline_mix(mcp_db)
        data = _parse(await call_tool("finding_list", {"suppression": "baselined"}))
        assert [i["rule_id"] for i in data["items"]] == ["PY-WL-102"]

    async def test_filter_by_rule_id(self, mcp_db: FiligreeDB) -> None:
        _seed_wardline_mix(mcp_db)
        data = _parse(await call_tool("finding_list", {"rule_id": "PY-WL-101"}))
        assert [i["rule_id"] for i in data["items"]] == ["PY-WL-101"]

    async def test_filter_by_qualname(self, mcp_db: FiligreeDB) -> None:
        # Guards the hand-written "qualname" key string in the handler's forward
        # loop: a typo there silently no-ops the filter (returns everything).
        _seed_wardline_mix(mcp_db)
        data = _parse(await call_tool("finding_list", {"qualname": "app.handler"}))
        assert [i["rule_id"] for i in data["items"]] == ["PY-WL-101"]

    async def test_invalid_kind_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_list", {"kind": "bogus"}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_invalid_suppression_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_list", {"suppression": "bogus"}))
        assert data["code"] == ErrorCode.VALIDATION


class TestReportFindingTool:
    async def test_report_finding_uses_registry_resolved_file_id(self, mcp_db: FiligreeDB) -> None:
        mcp_db.registry = FixedRegistry(file_id="core:file:report-target@src/report_target.py")

        data = _parse(
            await call_tool(
                "finding_report",
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
                    "Loomweave registry unavailable for test",
                    url="http://loomweave.test/api/v1/files?path=src%2Freport_target.py",
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
                "finding_report",
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
        assert data["details"]["url"] == "http://loomweave.test/api/v1/files?path=src%2Freport_target.py"
        assert "Registry unavailable" in data["error"]
        assert data["details"]["cause"] == "registry_unavailable"

    async def test_report_finding_registry_file_not_found_returns_not_found(self, mcp_db: FiligreeDB) -> None:
        class MissingFileRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryFileNotFoundError(
                    "Loomweave registry could not resolve file at http://loomweave.test/api/v1/files?path=missing.py: HTTP 404 not indexed",
                    status_code=404,
                    url="http://loomweave.test/api/v1/files?path=missing.py",
                )

            def is_displaced(self) -> bool:
                return False

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

        mcp_db.registry = MissingFileRegistry()

        data = _parse(
            await call_tool(
                "finding_report",
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
                    "Loomweave registry refuses briefing-blocked file",
                    status_code=403,
                    url="http://loomweave.test/api/v1/files?path=secret.py",
                )

            def is_displaced(self) -> bool:
                return False

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

        mcp_db.registry = BriefingBlockedRegistry()

        data = _parse(
            await call_tool(
                "finding_report",
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
                return cast(
                    ResolvedFile,
                    {
                        "file_id": make_entity_id(f"core:file:{canonical_path.replace('/', ':')}"),
                        "content_hash": f"hash:{canonical_path}",
                        "canonical_path": canonical_path,
                        "language": language,
                        "registry_backend": "loomweave",
                    },
                )

            def is_displaced(self) -> bool:
                return False

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

        registry = CountingCanonicalRegistry()
        mcp_db.registry = registry

        data = _parse(
            await call_tool(
                "finding_report",
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
                "finding_report",
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
                "finding_report",
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

    async def test_report_finding_line_end_before_line_start_returns_validation(self, mcp_db: FiligreeDB) -> None:
        data = _parse(
            await call_tool(
                "finding_report",
                {
                    "file_path": "src/report_target.py",
                    "rule_id": "invalid-range",
                    "message": "Line range is backwards",
                    "severity": "medium",
                    "line_start": 10,
                    "line_end": 2,
                },
            )
        )

        assert data["code"] == ErrorCode.VALIDATION
        assert "line_end" in data["error"]

    async def test_report_finding_update_after_line_start_normalization(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        (project_root / "src").mkdir()
        (project_root / "src/report_target.py").write_text("x = 1\n")
        payload = {
            "file_path": "src/report_target.py",
            "rule_id": "line-too-high",
            "message": "Line attribution should be cleared",
            "severity": "high",
            "line_start": 389,
            "line_end": 391,
            "response_detail": "full",
        }

        first = _parse(await call_tool("finding_report", payload))
        assert first["finding_result"] == "created"
        assert any("line_start 389" in warning for warning in first["warnings"])

        second = _parse(await call_tool("finding_report", payload))
        assert second["finding_result"] == "updated"
        assert second["findings_updated"] == 1

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
                "finding_report",
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
        data = _parse(await call_tool("finding_update", {"finding_id": ids["obo"], "status": "acknowledged"}))
        assert data["finding_id"] == ids["obo"]
        assert "id" not in data
        assert data["status"] == "acknowledged"

    async def test_update_issue_id(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        issue = mcp_db.create_issue("Bug ticket")
        data = _parse(await call_tool("finding_update", {"finding_id": ids["sqli"], "issue_id": issue.id}))
        assert data["finding_id"] == ids["sqli"]
        assert data["issue_id"] == issue.id

    async def test_update_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_update", {"finding_id": "nonexistent", "status": "acknowledged"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_update_no_fields_rejected(self, mcp_db: FiligreeDB) -> None:
        """At least one of status or issue_id must be provided."""
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_update", {"finding_id": ids["obo"]}))
        assert data["code"] == ErrorCode.VALIDATION
        assert "at least one" in data["error"].lower()

    async def test_update_invalid_status_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_update", {"finding_id": ids["obo"], "status": "banana"}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_update_non_string_status_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_update", {"finding_id": ids["obo"], "status": ["fixed"]}))
        assert data["code"] == ErrorCode.VALIDATION
        assert "status must be a string" in data["error"]


class TestBatchUpdateFindingsTool:
    async def test_batch_update(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(
            await call_tool(
                "finding_batch_update",
                {"finding_ids": [ids["obo"], ids["type"]], "status": "acknowledged"},
            )
        )
        assert len(data["succeeded"]) == 2
        assert data["failed"] == []

    async def test_batch_update_partial_failure(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(
            await call_tool(
                "finding_batch_update",
                {"finding_ids": [ids["obo"], "nonexistent"], "status": "acknowledged"},
            )
        )
        assert len(data["succeeded"]) == 1
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "nonexistent"
        assert data["failed"][0]["code"] == ErrorCode.NOT_FOUND

    async def test_batch_update_empty_ids_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_batch_update", {"finding_ids": [], "status": "acknowledged"}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_batch_update_missing_status_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_batch_update", {"finding_ids": [ids["obo"]], "status": ""}))
        assert data["code"] == ErrorCode.VALIDATION


class TestPromoteFindingTool:
    async def test_promote_creates_issue(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["sqli"]}))
        assert "issue_id" in data
        assert "id" not in data
        assert "observation_id" not in data
        assert data["type"] == "bug"
        assert data["fields"]["severity"] == "critical"
        assert "SQL injection" in data["title"]
        assert "from-finding" in data["labels"]
        assert mcp_db.get_finding(ids["sqli"])["issue_id"] == data["issue_id"]
        assert mcp_db.list_observations() == []

    async def test_promote_with_priority_override(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["obo"], "priority": 0}))
        assert data["priority"] == 0

    async def test_promote_rejects_invalid_priority(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["obo"], "priority": 5}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "priority must be <= 4"
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_rejects_invalid_labels_shape(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["obo"], "labels": ["ok", 42]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "labels must be a list of strings"
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_returns_validation_error_for_reserved_label(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["obo"], "labels": ["severity:high"]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert "system-managed auto-tag namespace" in data["error"]
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_carries_labels_to_created_issue(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["obo"], "labels": ["cluster:mcp"]}))

        assert set(data["labels"]) == {"from-finding", "cluster:mcp"}

    async def test_promote_and_attach_entity(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)

        data = _parse(
            await call_tool(
                "finding_promote_and_attach_entity",
                {
                    "finding_id": ids["sqli"],
                    "entity_id": "loomweave:eid:mcp",
                    "content_hash": "hash-v1",
                    "entity_kind": "function",
                    "actor": "mcp-agent",
                },
            )
        )

        assert data["issue_id"]
        assert data["association"]["entity_id"] == "loomweave:eid:mcp"
        assert data["association"]["entity_kind"] == "function"
        assert mcp_db.list_entity_associations(data["issue_id"])[0]["content_hash_at_attach"] == "hash-v1"

    @staticmethod
    def _seed_entity_finding(db: FiligreeDB, entity_id: str, *, content_hash: str = "hash-ent-1") -> str:
        """Seed one loomweave finding carrying its own entity identity, with a
        file-record content hash as the loomweave-registry mode would leave it."""
        result = db.process_scan_results(
            scan_source="loomweave",
            findings=[
                {
                    "path": "src/ent.py",
                    "rule_id": "LMWV-R1",
                    "severity": "high",
                    "message": "entity-bearing finding",
                    "metadata": {"loomweave": {"entity_id": entity_id}},
                }
            ],
        )
        if content_hash:
            db.conn.execute("UPDATE file_records SET content_hash = ? WHERE path = 'src/ent.py'", (content_hash,))
            db.conn.commit()
        return cast(str, result["new_finding_ids"][0])

    async def test_promote_attaches_entity_by_default(self, mcp_db: FiligreeDB) -> None:
        """B9 (weft-4a46553503): a finding carrying metadata.loomweave.entity_id
        gets its entity association created as part of the plain promote."""
        fid = self._seed_entity_finding(mcp_db, "loomweave:eid:mcp-default")

        data = _parse(await call_tool("finding_promote", {"finding_id": fid}))

        assert data["entity_attachment"]["attached"] is True
        assert data["entity_attachment"]["entity_id"] == "loomweave:eid:mcp-default"
        assert data["association"]["entity_id"] == "loomweave:eid:mcp-default"
        rows = mcp_db.list_entity_associations(make_issue_id(data["issue_id"]))
        assert len(rows) == 1
        assert rows[0]["content_hash_at_attach"] == "hash-ent-1"

    async def test_promote_reports_why_nothing_attached(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["sqli"]}))

        assert data["entity_attachment"]["attached"] is False
        assert "no entity identity on finding" in data["entity_attachment"]["reason"]
        assert "association" not in data

    async def test_promote_attach_entity_opt_out(self, mcp_db: FiligreeDB) -> None:
        fid = self._seed_entity_finding(mcp_db, "loomweave:eid:mcp-optout")

        data = _parse(await call_tool("finding_promote", {"finding_id": fid, "attach_entity": False}))

        assert data["entity_attachment"]["attached"] is False
        assert "attach_entity" in data["entity_attachment"]["reason"]
        assert mcp_db.list_entity_associations(make_issue_id(data["issue_id"])) == []

    async def test_promote_rejects_non_bool_attach_entity(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["obo"], "attach_entity": "yes"}))
        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "attach_entity must be a boolean"
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_attach_failure_is_in_band_warning(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed attach never fails the promote: the issue is created and the
        failure is reported in-band (warnings + entity_attachment.reason)."""
        fid = self._seed_entity_finding(mcp_db, "loomweave:eid:mcp-fail")

        def boom(*args: object, **kwargs: object) -> object:
            msg = "simulated attach failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(mcp_db, "add_entity_association", boom)
        data = _parse(await call_tool("finding_promote", {"finding_id": fid}))

        assert data["issue_id"]
        assert mcp_db.get_finding(fid)["issue_id"] == data["issue_id"]
        assert data["entity_attachment"]["attached"] is False
        assert "attach failed: simulated attach failure" in data["entity_attachment"]["reason"]
        assert any("simulated attach failure" in w for w in data["warnings"])
        assert mcp_db.list_entity_associations(make_issue_id(data["issue_id"])) == []

    async def test_promote_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_promote", {"finding_id": "nonexistent"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_promote_empty_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_promote", {"finding_id": ""}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_promote_suppressed_finding_refused_without_force(self, mcp_db: FiligreeDB) -> None:
        """weft-171fc22a50: a wardline-suppressed finding is refused (clean
        VALIDATION coded error, not a 500) — it is an already-accepted defect,
        not active work. End-to-end through the MCP promote_finding tool."""
        result = mcp_db.process_scan_results(
            scan_source="wardline",
            findings=[
                {
                    "path": "src/main.py",
                    "rule_id": "WLN-001",
                    "severity": "high",
                    "message": "tainted sink",
                    "metadata": {"wardline": {"suppression_state": "baselined"}},
                }
            ],
        )
        fid = result["new_finding_ids"][0]

        data = _parse(await call_tool("finding_promote", {"finding_id": fid}))

        assert data["code"] == ErrorCode.VALIDATION
        assert "baselined" in data["error"]
        assert "force=true" in data["error"]
        # Refused → no issue linked.
        assert mcp_db.get_finding(fid)["issue_id"] is None

    async def test_promote_suppressed_finding_succeeds_with_force(self, mcp_db: FiligreeDB) -> None:
        """force=true overrides the suppression guard and records the override
        as a warning on the result. End-to-end through the MCP tool."""
        result = mcp_db.process_scan_results(
            scan_source="wardline",
            findings=[
                {
                    "path": "src/main.py",
                    "rule_id": "WLN-002",
                    "severity": "high",
                    "message": "tainted sink",
                    "metadata": {"wardline": {"suppression_state": "waived"}},
                }
            ],
        )
        fid = result["new_finding_ids"][0]

        data = _parse(await call_tool("finding_promote", {"finding_id": fid, "force": True}))

        assert "issue_id" in data
        assert "code" not in data  # not an error envelope
        assert mcp_db.get_finding(fid)["issue_id"] == data["issue_id"]
        warnings = data.get("warnings") or []
        assert any("force override" in w and "waived" in w for w in warnings)

    async def test_promote_rejects_non_bool_force(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["obo"], "force": "yes"}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "force must be a boolean"
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None

    async def test_promote_active_finding_unaffected_by_guard(self, mcp_db: FiligreeDB) -> None:
        """Regression guard: an active finding (no suppression_state) promotes
        normally without force."""
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_promote", {"finding_id": ids["sqli"]}))

        assert "issue_id" in data
        assert "code" not in data
        assert mcp_db.get_finding(ids["sqli"])["issue_id"] == data["issue_id"]

    async def test_promote_and_attach_suppressed_threads_force(self, mcp_db: FiligreeDB) -> None:
        """weft-171fc22a50: the attach-entity tool also honours the suppression
        guard and its force override (the override is recorded as a warning)."""
        result = mcp_db.process_scan_results(
            scan_source="wardline",
            findings=[
                {
                    "path": "src/main.py",
                    "rule_id": "WLN-003",
                    "severity": "high",
                    "message": "tainted sink",
                    "metadata": {"wardline": {"suppression_state": "judged"}},
                }
            ],
        )
        fid = result["new_finding_ids"][0]
        args = {
            "finding_id": fid,
            "entity_id": "loomweave:eid:attach",
            "content_hash": "hash-v1",
        }
        # Without force: refused as a suppressed defect (clean VALIDATION error).
        refused = _parse(await call_tool("finding_promote_and_attach_entity", args))
        assert refused["code"] == ErrorCode.VALIDATION
        assert "judged" in refused["error"]
        assert mcp_db.get_finding(fid)["issue_id"] is None
        # With force: promotes, attaches, and records the override warning.
        forced = _parse(await call_tool("finding_promote_and_attach_entity", {**args, "force": True}))
        assert "code" not in forced
        assert forced["issue_id"]
        assert forced["association"]["entity_id"] == "loomweave:eid:attach"
        assert any("force override" in w and "judged" in w for w in (forced.get("warnings") or []))

    async def test_promote_and_attach_rejects_non_bool_force(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(
            await call_tool(
                "finding_promote_and_attach_entity",
                {
                    "finding_id": ids["obo"],
                    "entity_id": "loomweave:eid:x",
                    "content_hash": "h",
                    "force": "yes",
                },
            )
        )
        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "force must be a boolean"
        assert mcp_db.get_finding(ids["obo"])["issue_id"] is None


class TestDismissFindingTool:
    async def test_dismiss_marks_false_positive(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_dismiss", {"finding_id": ids["type"]}))
        assert data["finding_id"] == ids["type"]
        assert "id" not in data
        assert data["status"] == "false_positive"

    async def test_dismiss_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_dismiss", {"finding_id": "nonexistent"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_dismiss_empty_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("finding_dismiss", {"finding_id": ""}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_dismiss_non_string_status_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_dismiss", {"finding_id": ids["type"], "status": ["fixed"]}))
        assert data["code"] == ErrorCode.VALIDATION
        assert "status" in data["error"].lower()

    async def test_dismiss_non_string_reason_rejected(self, mcp_db: FiligreeDB) -> None:
        ids = _seed_findings(mcp_db)
        data = _parse(await call_tool("finding_dismiss", {"finding_id": ids["type"], "reason": ["not", "a", "string"]}))
        assert data["code"] == ErrorCode.VALIDATION
        assert "reason" in data["error"].lower()
