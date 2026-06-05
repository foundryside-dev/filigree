"""Contract tests for TypedDict shapes vs actual runtime return values."""

from __future__ import annotations

import ast
from collections.abc import Generator
from pathlib import Path
from typing import get_type_hints

import pytest

from filigree.core import FileRecord, FiligreeDB
from filigree.issue_payloads import issue_to_public
from filigree.types.api import (
    ArchiveClosedResponse,
    BlockedIssue,
    ClaimNextEmptyResponse,
    ClaimNextResponse,
    CompactEventsResponse,
    CriticalPathResponse,
    DepDetail,
    DependencyActionResponse,
    EnrichedIssueDetail,
    ErrorCode,
    ErrorResponse,
    IssueDetailEvent,
    IssueMutationResponse,
    IssueWithChangedFields,
    IssueWithTransitions,
    IssueWithUnblocked,
    JsonlTransferResponse,
    OutboundTransitionInfo,
    PackListItem,
    PlanResponse,
    PublicIssue,
    SlimIssue,
    StatsWithPrefix,
    StatusExplanation,
    TransitionDetail,
    TransitionError,
    TransitionHint,
    ValidationResult,
    WorkflowGuideResponse,
    WorkflowStatusesResponse,
)
from filigree.types.core import (
    BatchDismissResult,
    FileRecordDict,
    IssueDict,
    ObservationDict,
    ObservationStatsDict,
    PaginatedResult,
    PromoteObservationResult,
    ScanFindingDict,
)
from filigree.types.events import EventRecord, EventRecordWithTitle, UndoFailure, UndoSuccess
from filigree.types.files import (
    CleanStaleResult,
    EnrichedFileItem,
    FileAssociation,
    FileDetail,
    FileHotspot,
    FindingsSummary,
    GlobalFindingsStats,
    IssueFileAssociation,
    ScanIngestResult,
    ScanRunRecord,
    TimelineEntry,
)
from filigree.types.planning import (
    CommentRecord,
    CriticalPathNode,
    FlowMetrics,
    PlanTree,
    ReleaseSummaryItem,
    ReleaseTree,
    StatsResult,
)
from filigree.types.workflow import TemplateInfo, TemplateListItem

# db fixture inherited from tests/conftest.py (make_db with default packs)
# mcp_db fixture for MCP handler shape tests (section 1E)
# Duplicated from tests/mcp/conftest.py to avoid cross-directory fixture deps.


@pytest.fixture
def mcp_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """Set up a FiligreeDB and patch the MCP module globals."""
    from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, write_config

    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "mcp", "version": 1})
    (filigree_dir / SUMMARY_FILENAME).write_text("# test\n")

    d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="mcp")
    d.initialize()

    import filigree.mcp_server as mcp_mod

    original_db = mcp_mod.db
    original_dir = mcp_mod._filigree_dir
    mcp_mod.db = d
    mcp_mod._filigree_dir = filigree_dir

    yield d

    mcp_mod.db = original_db
    mcp_mod._filigree_dir = original_dir
    d.close()


# ---------------------------------------------------------------------------
# 1. Runtime shape tests — key-set + value-type checks
#
# NOTE: get_type_hints() resolves string annotations to actual types,
# but for TypedDict it returns the declared key→type mapping — we only
# compare *key sets* here, not value types, because NewType (e.g.
# ISOTimestamp) resolves to its base type (str) under get_type_hints().
# ---------------------------------------------------------------------------


class TestIssueDictShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = issue.to_dict()
        hints = get_type_hints(IssueDict)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task", priority=1, labels=["a"])
        result = issue.to_dict()
        assert isinstance(result["id"], str)
        assert isinstance(result["title"], str)
        assert isinstance(result["priority"], int)
        assert isinstance(result["is_ready"], bool)
        assert isinstance(result["labels"], list)
        assert isinstance(result["blocks"], list)
        assert isinstance(result["blocked_by"], list)
        assert isinstance(result["children"], list)
        assert isinstance(result["fields"], dict)

    def test_closed_issue_value_types(self, db: FiligreeDB) -> None:
        """Verify closed_at is a string (not None) for a closed issue."""
        issue = db.create_issue("Test", type="task")
        db.close_issue(issue.id)
        closed = db.get_issue(issue.id)
        result = closed.to_dict()
        assert isinstance(result["closed_at"], str)
        assert result["closed_at"] != ""


class TestIssuePriorityContract:
    """Issue.priority is an int 0-4 and explicitly excludes bool.

    bool is an int subclass in Python, so a bare isinstance(_, int) check
    lets True/False through and they would serialize as priority:true/false
    into agent-facing JSON, violating the int-only contract.
    """

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_priority_rejected(self, bad: bool) -> None:
        from filigree.models import Issue

        with pytest.raises(ValueError, match="Invalid priority"):
            Issue(id="x", title="t", priority=bad)

    @pytest.mark.parametrize("good", [0, 1, 2, 3, 4])
    def test_int_priority_accepted(self, good: int) -> None:
        from filigree.models import Issue

        issue = Issue(id="x", title="t", priority=good)
        assert type(issue.priority) is int
        assert type(issue.to_dict()["priority"]) is int


class TestPaginatedResultShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.create_issue("Test", type="task")
        result = db.list_files_paginated(limit=5)
        hints = get_type_hints(PaginatedResult)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        result = db.list_files_paginated(limit=5)
        assert isinstance(result["results"], list)
        assert isinstance(result["total"], int)
        assert isinstance(result["limit"], int)
        assert isinstance(result["offset"], int)
        assert isinstance(result["has_more"], bool)


class TestProjectConfigShape:
    def test_keys_present_in_defaults(self, tmp_path: Path) -> None:
        """ProjectConfig is total=False — verify that read_config() returns at least the core keys."""
        import json

        from filigree.core import read_config

        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "prefix": "test",
                    "version": 1,
                    "enabled_packs": ["core"],
                }
            )
        )
        result = read_config(tmp_path)
        assert "prefix" in result
        assert "version" in result
        assert "enabled_packs" in result
        assert isinstance(result["prefix"], str)
        assert isinstance(result["version"], int)
        assert isinstance(result["enabled_packs"], list)

    def test_malformed_config_fields_fall_back_to_typed_defaults(self, tmp_path: Path) -> None:
        """Malformed config.json values must not cross the project trust boundary."""
        import json

        from filigree.core import read_config

        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "prefix": 123,
                    "version": "two",
                    "enabled_packs": "core",
                }
            )
        )

        result = read_config(tmp_path)

        assert result["prefix"] == "filigree"
        assert result["version"] == 1
        assert result["enabled_packs"] == ["core", "planning", "release"]


class TestFileRecordDictShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        row = db.conn.execute("SELECT * FROM file_records LIMIT 1").fetchone()
        fr = FileRecord(
            id=row["id"],
            path=row["path"],
            language=row["language"] or "",
            file_type=row["file_type"] or "",
        )
        result = fr.to_dict()
        hints = get_type_hints(FileRecordDict)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        row = db.conn.execute("SELECT * FROM file_records LIMIT 1").fetchone()
        fr = FileRecord(
            id=row["id"],
            path=row["path"],
            language=row["language"] or "",
            file_type=row["file_type"] or "",
        )
        result = fr.to_dict()
        assert isinstance(result["id"], str)
        assert isinstance(result["path"], str)
        assert isinstance(result["metadata"], dict)


class TestFileRecordBackendInvariant:
    """FileRecord enforces the correlated (registry_backend, content_hash) invariant.

    ``local`` files carry the empty-hash sentinel (the local backend cannot
    compute a drift hash); ``loomweave`` files must carry a non-empty hash. The
    illegal cross combinations are rejected at construction so a corrupt row
    or a future mis-wired caller cannot hydrate an inconsistent record.
    """

    def test_local_with_empty_hash_is_valid(self) -> None:
        record = FileRecord(id="f1", path="src/a.py", registry_backend="local", content_hash="")
        assert record.registry_backend == "local"
        assert record.content_hash == ""

    def test_loomweave_with_nonempty_hash_is_valid(self) -> None:
        record = FileRecord(id="f1", path="src/a.py", registry_backend="loomweave", content_hash="sha256:abc")
        assert record.registry_backend == "loomweave"
        assert record.content_hash == "sha256:abc"

    def test_local_with_nonempty_hash_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="content_hash"):
            FileRecord(id="f1", path="src/a.py", registry_backend="local", content_hash="sha256:abc")

    def test_loomweave_with_empty_hash_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="content_hash"):
            FileRecord(id="f1", path="src/a.py", registry_backend="loomweave", content_hash="")

    def test_invalid_registry_backend_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="registry_backend"):
            FileRecord(id="f1", path="src/a.py", registry_backend="remote", content_hash="sha256:abc")  # type: ignore[arg-type]


class TestScanFindingDictShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        findings = db.get_findings_paginated(file_id=file_id, limit=1)
        result = findings["results"][0]
        hints = get_type_hints(ScanFindingDict)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high", "line_start": 1}],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        findings = db.get_findings_paginated(file_id=file_id, limit=1)
        result = findings["results"][0]
        assert isinstance(result["id"], str)
        assert isinstance(result["severity"], str)
        assert isinstance(result["seen_count"], int)


class TestFileAssociationShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        issue = db.create_issue("Bug in main", type="bug")
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        db.add_file_association(file_id, issue.id, "bug_in")
        result = db.get_file_associations(file_id)
        assert len(result) >= 1
        hints = get_type_hints(FileAssociation)
        assert set(result[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        issue = db.create_issue("Bug in main", type="bug")
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        db.add_file_association(file_id, issue.id, "bug_in")
        result = db.get_file_associations(file_id)[0]
        assert isinstance(result["file_id"], str)
        assert isinstance(result["issue_id"], str)
        assert isinstance(result["assoc_type"], str)
        assert isinstance(result["created_at"], str)


class TestIssueFileAssociationShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        issue = db.create_issue("Bug in main", type="bug")
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        db.add_file_association(file_id, issue.id, "bug_in")
        result = db.get_issue_files(issue.id)
        assert len(result) >= 1
        hints = get_type_hints(IssueFileAssociation)
        assert set(result[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        issue = db.create_issue("Bug in main", type="bug")
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        db.add_file_association(file_id, issue.id, "bug_in")
        result = db.get_issue_files(issue.id)[0]
        assert isinstance(result["file_id"], str)
        assert isinstance(result["issue_id"], str)
        assert isinstance(result["file_path"], str)
        assert isinstance(result["assoc_type"], str)


class TestFindingsSummaryShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        result = db.get_file_findings_summary(file_id)
        hints = get_type_hints(FindingsSummary)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        result = db.get_file_findings_summary(file_id)
        assert isinstance(result["total_findings"], int)
        assert isinstance(result["open_findings"], int)
        assert isinstance(result["critical"], int)
        assert isinstance(result["high"], int)


class TestGlobalFindingsStatsShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        result = db.get_global_findings_stats()
        hints = get_type_hints(GlobalFindingsStats)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        result = db.get_global_findings_stats()
        assert isinstance(result["total_findings"], int)
        assert isinstance(result["open_findings"], int)
        assert isinstance(result["files_with_findings"], int)
        assert isinstance(result["critical"], int)


class TestFileDetailShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        result = db.get_file_detail(file_id)
        hints = get_type_hints(FileDetail)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        result = db.get_file_detail(file_id)
        assert isinstance(result["file"], dict)
        assert isinstance(result["associations"], list)
        assert isinstance(result["recent_findings"], list)
        assert isinstance(result["summary"], dict)


class TestFileHotspotShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "critical"}],
        )
        result = db.get_file_hotspots(limit=1)
        assert len(result) >= 1
        hints = get_type_hints(FileHotspot)
        assert set(result[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "critical"}],
        )
        result = db.get_file_hotspots(limit=1)[0]
        assert isinstance(result["file"], dict)
        assert isinstance(result["score"], int)
        assert isinstance(result["findings_breakdown"], dict)


class TestScanRunRecordShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            scan_run_id="run-001",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        result = db.get_scan_runs(limit=1)
        assert len(result) >= 1
        hints = get_type_hints(ScanRunRecord)
        assert set(result[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            scan_run_id="run-001",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        result = db.get_scan_runs(limit=1)[0]
        assert isinstance(result["scan_run_id"], str)
        assert isinstance(result["scan_source"], str)
        assert isinstance(result["total_findings"], int)
        assert isinstance(result["files_scanned"], int)


class TestScanIngestResultShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        result = db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        hints = get_type_hints(ScanIngestResult)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        result = db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        assert isinstance(result["files_created"], int)
        assert isinstance(result["files_updated"], int)
        assert isinstance(result["findings_created"], int)
        assert isinstance(result["new_finding_ids"], list)
        assert isinstance(result["warnings"], list)
        assert isinstance(result["observations_created"], int)


class TestCleanStaleResultShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        result = db.clean_stale_findings(days=30)
        hints = get_type_hints(CleanStaleResult)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        result = db.clean_stale_findings(days=30)
        assert isinstance(result["findings_fixed"], int)


# ---------------------------------------------------------------------------
# 1B-2. Observation shape tests
# ---------------------------------------------------------------------------


class TestObservationDictShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        result = db.create_observation("Test obs")
        hints = get_type_hints(ObservationDict)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        result = db.create_observation("Test obs", file_path="foo.py", line=10, priority=2)
        assert isinstance(result["id"], str)
        assert isinstance(result["summary"], str)
        assert isinstance(result["priority"], int)
        assert isinstance(result["created_at"], str)


class TestObservationStatsDictShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        result = db.observation_stats()
        hints = get_type_hints(ObservationStatsDict)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.create_observation("Test obs")
        result = db.observation_stats()
        assert isinstance(result["count"], int)
        assert isinstance(result["stale_count"], int)
        assert isinstance(result["oldest_hours"], (float, type(None)))
        assert isinstance(result["expiring_soon_count"], int)
        assert isinstance(result["sweep_consecutive_failures"], int)
        assert isinstance(result["last_successful_sweep_at"], (str, type(None)))


class TestBatchDismissResultShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        result = db.batch_dismiss_observations([])
        hints = get_type_hints(BatchDismissResult)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Test obs")
        result = db.batch_dismiss_observations([obs["id"]])
        assert isinstance(result["dismissed"], int)
        assert isinstance(result["not_found"], list)


class TestPromoteObservationResultShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Test obs")
        result = db.promote_observation(obs["id"])
        # warnings key is NotRequired — only check required keys
        assert "issue" in result
        from filigree.models import Issue

        hints = get_type_hints(PromoteObservationResult, localns={"Issue": Issue})
        assert set(result.keys()) <= set(hints.keys())


# ---------------------------------------------------------------------------
# 1B-3. Enriched file/timeline shape tests
# ---------------------------------------------------------------------------


class TestEnrichedFileItemShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        paginated = db.list_files_paginated(limit=1)
        assert len(paginated["results"]) >= 1
        item = paginated["results"][0]
        hints = get_type_hints(EnrichedFileItem)
        assert set(item.keys()) == set(hints.keys())


class TestTimelineEntryShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/main.py", "rule_id": "R1", "message": "m", "severity": "high"}],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        timeline = db.get_file_timeline(file_id, limit=1)
        assert len(timeline["results"]) >= 1
        entry = timeline["results"][0]
        hints = get_type_hints(TimelineEntry)
        assert set(entry.keys()) == set(hints.keys())


# ---------------------------------------------------------------------------
# 1B-4. Release summary shape tests
# ---------------------------------------------------------------------------


class TestReleaseSummaryItemShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.create_issue("v1.0.0", type="release", fields={"version": "v1.0.0"})
        results = db.get_releases_summary()
        assert len(results) >= 1
        hints = get_type_hints(ReleaseSummaryItem)
        assert set(results[0].keys()) == set(hints.keys())

    def test_issue_dict_keys_present(self) -> None:
        """Every key in IssueDict must also appear in ReleaseSummaryItem."""
        issue_hints = get_type_hints(IssueDict)
        release_hints = get_type_hints(ReleaseSummaryItem)
        missing = set(issue_hints.keys()) - set(release_hints.keys())
        assert not missing, f"ReleaseSummaryItem is missing IssueDict keys: {missing}"


class TestReleaseTreeShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        release = db.create_issue("v1.0.0", type="release", fields={"version": "v1.0.0"})
        result = db.get_release_tree(release.id)
        hints = get_type_hints(ReleaseTree)
        assert set(result.keys()) == set(hints.keys())


# ---------------------------------------------------------------------------
# 1C. Events, planning, workflow, and analytics shape tests
# ---------------------------------------------------------------------------


class TestEventRecordShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, status="in_progress")
        events = db.get_issue_events(issue.id, limit=1)
        assert len(events) >= 1
        hints = get_type_hints(EventRecord)
        assert set(events[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, status="in_progress")
        event = db.get_issue_events(issue.id, limit=1)[0]
        assert isinstance(event["id"], int)
        assert isinstance(event["issue_id"], str)
        assert isinstance(event["event_type"], str)
        assert isinstance(event["created_at"], str)


class TestEventRecordWithTitleShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, status="in_progress")
        events = db.get_recent_events(limit=1)
        assert len(events) >= 1
        hints = get_type_hints(EventRecordWithTitle)
        keys = set(events[0].keys())
        # ``affected_entities`` is NotRequired — only synthetic ``issue_deleted``
        # tombstone records carry it; a normal event row omits it. (``__required_keys__``
        # is unreliable here: PEP 563 stringized annotations hide ``NotRequired`` from
        # TypedDict at class creation, so assert against the known optional key.)
        assert keys <= set(hints.keys())
        assert set(hints.keys()) - keys == {"affected_entities"}

    def test_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, status="in_progress")
        event = db.get_recent_events(limit=1)[0]
        assert isinstance(event["issue_title"], str)
        assert isinstance(event["event_type"], str)


class TestUndoResultShape:
    def test_failure_keys(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = db.undo_last(issue.id)
        hints = get_type_hints(UndoFailure)
        assert set(result.keys()) == set(hints.keys())

    def test_success_keys(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, status="in_progress")
        result = db.undo_last(issue.id)
        hints = get_type_hints(UndoSuccess)
        assert set(result.keys()) == set(hints.keys())

    def test_success_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, status="in_progress")
        result = db.undo_last(issue.id)
        assert result["undone"] is True
        assert isinstance(result["event_type"], str)
        assert isinstance(result["event_id"], int)
        assert isinstance(result["issue"], dict)


class TestCommentRecordShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.add_comment(issue.id, "hello")
        comments = db.get_comments(issue.id)
        assert len(comments) >= 1
        hints = get_type_hints(CommentRecord)
        assert set(comments[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.add_comment(issue.id, "hello")
        comment = db.get_comments(issue.id)[0]
        assert isinstance(comment["id"], int)
        assert isinstance(comment["text"], str)
        assert isinstance(comment["created_at"], str)


class TestStatsResultShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        db.create_issue("Test", type="task")
        result = db.get_stats()
        hints = get_type_hints(StatsResult)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        db.create_issue("Test", type="task")
        result = db.get_stats()
        assert isinstance(result["by_status"], dict)
        # status_name_counts / status_category_counts are DEPRECATED
        # (filigree-17694d2db8) exact duplicates of by_status / by_category,
        # retained as wire-compatibility aliases per ADR-009 §7. Lock the
        # contract: still present, still identical, until the next major.
        assert isinstance(result["status_name_counts"], dict)
        assert result["status_name_counts"] == result["by_status"]
        assert isinstance(result["status_category_counts"], dict)
        assert result["status_category_counts"] == result["by_category"]
        assert isinstance(result["by_type"], dict)
        assert isinstance(result["ready_count"], int)
        assert isinstance(result["blocked_count"], int)


class TestDependencyRecordShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        db.add_dependency(a.id, b.id)
        deps = db.get_all_dependencies()
        assert len(deps) >= 1
        # DependencyRecord uses functional-form TypedDict with "from" key
        assert set(deps[0].keys()) == {"from", "to", "type"}

    def test_value_types(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        db.add_dependency(a.id, b.id)
        dep = db.get_all_dependencies()[0]
        assert isinstance(dep["from"], str)
        assert isinstance(dep["to"], str)
        assert isinstance(dep["type"], str)


class TestCriticalPathNodeShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        db.add_dependency(b.id, a.id)
        path = db.get_critical_path()
        assert len(path) >= 1
        hints = get_type_hints(CriticalPathNode)
        assert set(path[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        db.add_dependency(b.id, a.id)
        node = db.get_critical_path()[0]
        assert isinstance(node["id"], str)
        assert isinstance(node["title"], str)
        assert isinstance(node["priority"], int)


class TestPlanTreeShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        milestone = db.create_issue("Milestone", type="milestone")
        phase = db.create_issue("Phase", type="phase", parent_id=milestone.id)
        db.create_issue("Step", type="step", parent_id=phase.id)
        result = db.get_plan(milestone.id)
        hints = get_type_hints(PlanTree)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        milestone = db.create_issue("Milestone", type="milestone")
        phase = db.create_issue("Phase", type="phase", parent_id=milestone.id)
        db.create_issue("Step", type="step", parent_id=phase.id)
        result = db.get_plan(milestone.id)
        assert isinstance(result["milestone"], dict)
        assert isinstance(result["phases"], list)
        assert isinstance(result["total_steps"], int)
        assert isinstance(result["completed_steps"], int)


class TestFlowMetricsShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        from filigree.analytics import get_flow_metrics

        result = get_flow_metrics(db, days=30)
        hints = get_type_hints(FlowMetrics)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        from filigree.analytics import get_flow_metrics

        result = get_flow_metrics(db, days=30)
        assert isinstance(result["period_days"], int)
        assert isinstance(result["throughput"], int)
        assert isinstance(result["by_type"], dict)


class TestTemplateInfoShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        result = db.get_template("task")
        assert result is not None
        hints = get_type_hints(TemplateInfo)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        result = db.get_template("task")
        assert result is not None
        assert isinstance(result["type"], str)
        assert isinstance(result["pack"], str)
        assert isinstance(result["states"], list)
        assert isinstance(result["transitions"], list)
        assert isinstance(result["fields_schema"], list)


class TestTemplateListItemShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        result = db.list_templates()
        assert len(result) >= 1
        hints = get_type_hints(TemplateListItem)
        assert set(result[0].keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        result = db.list_templates()
        item = result[0]
        assert isinstance(item["type"], str)
        assert isinstance(item["display_name"], str)
        assert isinstance(item["fields_schema"], list)


# ---------------------------------------------------------------------------
# 1D. API response shape tests (types/api.py)
# ---------------------------------------------------------------------------


class TestSlimIssueShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        from filigree.mcp_tools.common import _slim_issue

        issue = db.create_issue("Test", type="task")
        result = _slim_issue(issue)
        hints = get_type_hints(SlimIssue)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        from filigree.mcp_tools.common import _slim_issue

        issue = db.create_issue("Test", type="task")
        result = _slim_issue(issue)
        assert isinstance(result["issue_id"], str)
        assert isinstance(result["priority"], int)


class TestReadyIssueShape:
    def test_default_ready_issue_extends_slim_issue(self) -> None:
        from filigree.types.api import ReadyIssue

        slim_keys = set(get_type_hints(SlimIssue).keys())
        ready_keys = set(get_type_hints(ReadyIssue).keys())
        assert slim_keys < ready_keys
        # startable always rides along (filigree-406e6b7ee0); next_action and the
        # parent-context fields are NotRequired extras.
        assert {"startable", "next_action", "parent_issue_id", "parent_title"} <= ready_keys - slim_keys


class TestPublicIssueShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Public issue", type="task")
        result = issue_to_public(issue)
        hints = get_type_hints(PublicIssue)
        assert set(result.keys()) == set(hints.keys())
        assert "issue_id" in result
        assert "id" not in result

    def test_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Public issue", type="task", priority=1, labels=["a"])
        result = issue_to_public(issue)
        assert isinstance(result["issue_id"], str)
        assert isinstance(result["title"], str)
        assert isinstance(result["priority"], int)
        assert isinstance(result["is_ready"], bool)
        assert isinstance(result["labels"], list)


class TestBlockedIssueShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        from filigree.mcp_tools.common import _slim_issue

        issue = db.create_issue("Test", type="task")
        result = BlockedIssue(**_slim_issue(issue), blocked_by=["other-id"])
        hints = get_type_hints(BlockedIssue)
        assert set(result.keys()) == set(hints.keys())

    def test_extends_slim_issue(self) -> None:
        """BlockedIssue must include all SlimIssue keys plus blocked_by."""
        slim_keys = set(get_type_hints(SlimIssue).keys())
        blocked_keys = set(get_type_hints(BlockedIssue).keys())
        assert slim_keys < blocked_keys
        assert "blocked_by" in blocked_keys - slim_keys


class TestIssueWithTransitionsShape:
    def test_keys_without_transitions(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = IssueWithTransitions(**issue_to_public(issue))  # type: ignore[typeddict-item]
        # NotRequired keys may be absent
        assert {"issue_id", "title", "status"} <= set(result.keys())

    def test_keys_with_transitions(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = IssueWithTransitions(**issue_to_public(issue), valid_transitions=[])
        hints = get_type_hints(IssueWithTransitions)
        assert set(result.keys()) == set(hints.keys())


class TestIssueWithChangedFieldsShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, title="Updated")
        updated = db.get_issue(issue.id)
        result = IssueWithChangedFields(**issue_to_public(updated), changed_fields=["title"])
        hints = get_type_hints(IssueWithChangedFields)
        assert set(result.keys()) == set(hints.keys())


class TestIssueWithUnblockedShape:
    def test_keys_without_unblocked(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = IssueWithUnblocked(**issue_to_public(issue))  # type: ignore[typeddict-item]
        assert {"issue_id", "title", "status"} <= set(result.keys())

    def test_keys_with_unblocked(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = IssueWithUnblocked(
            **issue_to_public(issue),
            newly_unblocked=[SlimIssue(issue_id="x", title="t", status="open", priority=2, type="task")],
        )
        hints = get_type_hints(IssueWithUnblocked)
        assert set(result.keys()) == set(hints.keys())


class TestClaimNextResponseShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = ClaimNextResponse(**issue_to_public(issue), selection_reason="P2 ready issue")
        hints = get_type_hints(ClaimNextResponse)
        assert set(result.keys()) == set(hints.keys())

    def test_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = ClaimNextResponse(**issue_to_public(issue), selection_reason="P2 ready issue")
        assert isinstance(result["selection_reason"], str)


class TestErrorResponseShape:
    def test_required_keys(self) -> None:
        """Required keys (error, code) always present; details is NotRequired."""
        from filigree.types.api import ErrorCode

        result = ErrorResponse(error="not found", code=ErrorCode.NOT_FOUND)
        assert {"error", "code"} <= set(result.keys())

    def test_with_details(self) -> None:
        """All 3 keys present when details is populated."""
        from filigree.types.api import ErrorCode

        result = ErrorResponse(
            error="conflict",
            code=ErrorCode.CONFLICT,
            details={"issue_id": "abc"},
        )
        hints = get_type_hints(ErrorResponse)
        assert set(result.keys()) == set(hints.keys())


class TestTransitionErrorShape:
    def test_keys_match(self) -> None:
        result = TransitionError(error="bad", code=ErrorCode.INVALID_TRANSITION)
        # NotRequired keys may be absent — check required subset
        assert {"error", "code"} <= set(result.keys())
        assert result["code"] == ErrorCode.INVALID_TRANSITION


class TestDepDetailShape:
    def test_keys_match(self) -> None:
        result = DepDetail(title="t", status="open", status_category="open", priority=2)
        hints = get_type_hints(DepDetail)
        assert set(result.keys()) == set(hints.keys())


class TestIssueDetailEventFromSQL:
    def test_construction_from_sql_row(self, db: FiligreeDB) -> None:
        """Exercise IssueDetailEvent construction from a real SQL row."""
        issue = db.create_issue("Test", type="task")
        db.update_issue(issue.id, status="in_progress")
        rows = db.conn.execute(
            "SELECT event_type, actor, old_value, new_value, created_at FROM events WHERE issue_id = ? LIMIT 1",
            (issue.id,),
        ).fetchall()
        assert len(rows) >= 1
        event = IssueDetailEvent(
            event_type=rows[0]["event_type"],
            actor=rows[0]["actor"],
            old_value=rows[0]["old_value"],
            new_value=rows[0]["new_value"],
            created_at=rows[0]["created_at"],
        )
        hints = get_type_hints(IssueDetailEvent)
        assert set(event.keys()) == set(hints.keys())


class TestStatsWithPrefixShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        stats = db.get_stats()
        result = StatsWithPrefix(**stats, prefix="TEST")
        hints = get_type_hints(StatsWithPrefix)
        assert set(result.keys()) == set(hints.keys())


class TestPlanResponseShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        milestone = db.create_issue("M", type="milestone")
        phase = db.create_issue("P", type="phase", parent_id=milestone.id)
        db.create_issue("S", type="step", parent_id=phase.id)
        plan = db.get_plan(milestone.id)
        result = PlanResponse(
            milestone=plan["milestone"],
            phases=plan["phases"],
            total_steps=plan["total_steps"],
            completed_steps=plan["completed_steps"],
            progress_pct=0.0,
        )
        hints = get_type_hints(PlanResponse)
        assert set(result.keys()) == set(hints.keys())


class TestDependencyActionResponseShape:
    def test_keys_match(self) -> None:
        dependency_keys = set(get_type_hints(DependencyActionResponse).keys())
        assert set(get_type_hints(PublicIssue).keys()) <= dependency_keys
        assert {"dependency", "dependency_result"} <= dependency_keys


class TestCriticalPathResponseShape:
    def test_keys_match(self) -> None:
        from filigree.types.planning import CriticalPathNode

        result = CriticalPathResponse(
            path=[CriticalPathNode(id="a", title="A", priority=2, type="task")],
            length=1,
        )
        hints = get_type_hints(CriticalPathResponse)
        assert set(result.keys()) <= set(hints.keys())
        assert {"path", "length"} <= set(result.keys())

    def test_empty_note_is_optional(self) -> None:
        result = CriticalPathResponse(path=[], length=0, note="no open dependency chains")
        hints = get_type_hints(CriticalPathResponse)
        assert set(result.keys()) == set(hints.keys())


class TestEnrichedIssueDetailShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", type="task")
        result = EnrichedIssueDetail(
            **issue.to_dict(),
            dep_details={},
            events=[],
            comments=[],
        )
        hints = get_type_hints(EnrichedIssueDetail)
        assert set(result.keys()) == set(hints.keys())


class TestDanglingDepDetail:
    def test_fallback_for_missing_dep(self, db: FiligreeDB) -> None:
        """Verify the fallback DepDetail for a deleted dependency."""
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        db.add_dependency(a.id, b.id)
        # Disable FK constraints to simulate orphaned dep row
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("DELETE FROM issues WHERE id = ?", (b.id,))
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = ON")
        # Simulate what api_issue_detail does for dangling deps
        with pytest.raises(KeyError):
            db.get_issue(b.id)
        fallback = DepDetail(title=b.id, status="unknown", status_category="open", priority=2)
        assert set(fallback.keys()) == {"title", "status", "status_category", "priority"}


# ---------------------------------------------------------------------------
# 1E. MCP handler response shape tests (H5 — 14 untested TypedDicts)
#
# These TypedDicts are constructed by MCP tool handlers and returned directly
# to LLM callers.  Tests call the actual MCP handlers via call_tool() and
# verify key-set equality against the TypedDict declarations.
# ---------------------------------------------------------------------------


class TestAddCommentResultShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        await call_tool("create_issue", {"title": "Comment target"})
        issues = _parse(await call_tool("list_issues", {}))
        issue_id = issues["items"][0]["issue_id"]
        result = _parse(await call_tool("add_comment", {"issue_id": issue_id, "text": "hello"}))
        public_issue_keys = set(get_type_hints(PublicIssue).keys())
        assert set(result.keys()) == public_issue_keys | {"comment_id", "comment"}
        # ADR-012 (schema v24): the MCP comment result surfaces the
        # transport-verified identity alongside the claimed author. ``comment_to_mcp``
        # spreads the full CommentRecord, so ``verified_author`` rides along.
        assert set(result["comment"].keys()) == {"comment_id", "author", "verified_author", "text", "created_at"}

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        await call_tool("create_issue", {"title": "Comment target"})
        issues = _parse(await call_tool("list_issues", {}))
        issue_id = issues["items"][0]["issue_id"]
        result = _parse(await call_tool("add_comment", {"issue_id": issue_id, "text": "hello", "actor": "commenter"}))
        assert isinstance(result["issue_id"], str)
        assert isinstance(result["comment_id"], int)
        assert result["comment"]["comment_id"] == result["comment_id"]
        assert result["comment"]["author"] == "commenter"
        assert result["comment"]["text"] == "hello"
        assert isinstance(result["comment"]["created_at"], str)


class TestIssueMutationResponseShape:
    def test_type_includes_public_issue_keys(self) -> None:
        mutation_keys = set(get_type_hints(IssueMutationResponse).keys())
        assert set(get_type_hints(PublicIssue).keys()) <= mutation_keys


class TestLabelActionResponseShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        await call_tool("create_issue", {"title": "Label target"})
        issues = _parse(await call_tool("list_issues", {}))
        issue_id = issues["items"][0]["issue_id"]
        result = _parse(await call_tool("add_label", {"issue_id": issue_id, "label": "test-label"}))
        public_issue_keys = set(get_type_hints(PublicIssue).keys())
        assert set(result.keys()) == public_issue_keys | {"label", "label_result"}

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        await call_tool("create_issue", {"title": "Label target"})
        issues = _parse(await call_tool("list_issues", {}))
        issue_id = issues["items"][0]["issue_id"]
        result = _parse(await call_tool("add_label", {"issue_id": issue_id, "label": "test-label"}))
        assert isinstance(result["issue_id"], str)
        assert isinstance(result["label"], str)
        assert isinstance(result["label_result"], str)


class TestJsonlTransferResponseShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        mcp_db.create_issue("Export me", type="task")
        # _safe_path requires relative paths within project root
        result = _parse(await call_tool("export_jsonl", {"output_path": "export.jsonl"}))
        hints = get_type_hints(JsonlTransferResponse)
        # skipped_types is NotRequired — check required keys are present
        assert set(result.keys()) <= set(hints.keys())
        assert {"status", "records", "path"} <= set(result.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        mcp_db.create_issue("Export me", type="task")
        result = _parse(await call_tool("export_jsonl", {"output_path": "export.jsonl"}))
        assert isinstance(result["status"], str)
        assert isinstance(result["records"], int)
        assert isinstance(result["path"], str)


class TestArchiveClosedResponseShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        # days_old<7 now requires a label filter (filigree-cb980eee0d, P3.17).
        result = _parse(await call_tool("archive_closed", {"days_old": 0, "label": "test-scope"}))
        hints = get_type_hints(ArchiveClosedResponse)
        assert set(result.keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        # days_old<7 now requires a label filter (filigree-cb980eee0d, P3.17).
        result = _parse(await call_tool("archive_closed", {"days_old": 0, "label": "test-scope"}))
        assert isinstance(result["status"], str)
        assert isinstance(result["archived_count"], int)
        assert isinstance(result["archived_ids"], list)


class TestCompactEventsResponseShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("compact_events", {}))
        hints = get_type_hints(CompactEventsResponse)
        assert set(result.keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("compact_events", {}))
        assert isinstance(result["status"], str)
        assert isinstance(result["events_deleted"], int)


class TestClaimNextEmptyResponseShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        # Use type filter that matches nothing to get the empty response
        result = _parse(await call_tool("claim_next", {"assignee": "bot", "type": "nonexistent"}))
        hints = get_type_hints(ClaimNextEmptyResponse)
        assert set(result.keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("claim_next", {"assignee": "bot", "type": "nonexistent"}))
        assert isinstance(result["status"], str)
        assert isinstance(result["reason"], str)


class TestWorkflowStatusesResponseShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("get_workflow_statuses", {}))
        hints = get_type_hints(WorkflowStatusesResponse)
        assert set(result.keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("get_workflow_statuses", {}))
        assert isinstance(result["statuses"], dict)
        for category in ("open", "wip", "done"):
            assert isinstance(result["statuses"][category], list)


class TestPackListItemShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("list_packs", {}))
        items = result["items"]
        assert isinstance(items, list)
        assert len(items) >= 1
        hints = get_type_hints(PackListItem)
        assert set(items[0].keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("list_packs", {}))
        pack = result["items"][0]
        assert isinstance(pack["pack"], str)
        assert isinstance(pack["version"], str)
        assert isinstance(pack["display_name"], str)
        assert isinstance(pack["description"], str)
        assert isinstance(pack["types"], list)
        assert isinstance(pack["requires_packs"], list)


class TestValidationResultShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        issue = mcp_db.create_issue("Validate me", type="task")
        result = _parse(await call_tool("validate_issue", {"issue_id": issue.id}))
        hints = get_type_hints(ValidationResult)
        assert set(result.keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        issue = mcp_db.create_issue("Validate me", type="task")
        result = _parse(await call_tool("validate_issue", {"issue_id": issue.id}))
        assert isinstance(result["valid"], bool)
        assert isinstance(result["warnings"], list)
        assert isinstance(result["errors"], list)


class TestWorkflowGuideResponseShape:
    async def test_keys_with_guide(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("get_workflow_guide", {"pack": "core"}))
        hints = get_type_hints(WorkflowGuideResponse)
        # message and note are NotRequired
        assert set(result.keys()) <= set(hints.keys())
        assert {"pack", "guide"} <= set(result.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("get_workflow_guide", {"pack": "core"}))
        assert isinstance(result["pack"], str)
        # guide is dict or null
        assert result["guide"] is None or isinstance(result["guide"], dict)


class TestStatusExplanationShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("explain_status", {"type": "task", "status": "open"}))
        hints = get_type_hints(StatusExplanation)
        assert set(result.keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("explain_status", {"type": "task", "status": "open"}))
        assert isinstance(result["status"], str)
        assert isinstance(result["category"], str)
        assert isinstance(result["type"], str)
        assert isinstance(result["inbound_transitions"], list)
        assert isinstance(result["outbound_transitions"], list)
        assert isinstance(result["required_fields"], list)


class TestTransitionDetailShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        issue = mcp_db.create_issue("Transition test", type="task")
        result = _parse(await call_tool("get_valid_transitions", {"issue_id": issue.id}))
        assert set(result.keys()) == {"items", "has_more"}
        assert result["has_more"] is False
        assert len(result["items"]) >= 1
        hints = get_type_hints(TransitionDetail)
        assert set(result["items"][0].keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        issue = mcp_db.create_issue("Transition test", type="task")
        result = _parse(await call_tool("get_valid_transitions", {"issue_id": issue.id}))
        t = result["items"][0]
        assert isinstance(t["to"], str)
        assert isinstance(t["category"], str)
        assert isinstance(t["enforcement"], str)
        assert isinstance(t["requires_fields"], list)
        assert isinstance(t["missing_fields"], list)
        assert isinstance(t["ready"], bool)


class TestTransitionHintShape:
    def test_keys_match(self) -> None:
        """TransitionHint is constructed inline in _build_transition_error.
        Verify the TypedDict shape matches the dict literal pattern used there."""
        # Pattern from mcp_tools/common.py: {"to": ..., "category": ..., "ready": ...}
        result = TransitionHint(to="closed", category="done", ready=True)
        hints = get_type_hints(TransitionHint)
        assert set(result.keys()) == set(hints.keys())

    def test_without_ready(self) -> None:
        """ready is NotRequired — verify subset when omitted."""
        result = TransitionHint(to="closed", category="done")
        hints = get_type_hints(TransitionHint)
        assert set(result.keys()) <= set(hints.keys())
        assert {"to", "category"} <= set(result.keys())


class TestOutboundTransitionInfoShape:
    async def test_keys_match(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("explain_status", {"type": "task", "status": "open"}))
        outbound = result["outbound_transitions"]
        assert isinstance(outbound, list)
        assert len(outbound) >= 1
        hints = get_type_hints(OutboundTransitionInfo)
        assert set(outbound[0].keys()) == set(hints.keys())

    async def test_value_types(self, mcp_db: FiligreeDB) -> None:
        from filigree.mcp_server import call_tool
        from tests.mcp._helpers import _parse

        result = _parse(await call_tool("explain_status", {"type": "task", "status": "open"}))
        t = result["outbound_transitions"][0]
        assert isinstance(t["to"], str)
        assert isinstance(t["enforcement"], str)
        assert isinstance(t["requires_fields"], list)


# ---------------------------------------------------------------------------
# 2. Import constraint test — AST-based
# ---------------------------------------------------------------------------

TYPES_DIR = Path(__file__).resolve().parents[2] / "src" / "filigree" / "types"
FORBIDDEN_MODULES = {"filigree.core", "filigree.db_base"}
FORBIDDEN_PREFIXES = ("filigree.db_",)


def _get_imports_from_file(filepath: Path) -> list[str]:
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


@pytest.mark.parametrize(
    "py_file",
    sorted(TYPES_DIR.glob("*.py")),
    ids=lambda p: p.name,
)
def test_types_module_import_constraint(py_file: Path) -> None:
    imports = _get_imports_from_file(py_file)
    for mod in imports:
        assert mod not in FORBIDDEN_MODULES, f"{py_file.name} imports {mod}"
        for prefix in FORBIDDEN_PREFIXES:
            assert not mod.startswith(prefix), f"{py_file.name} imports {mod}"


# ---------------------------------------------------------------------------
# 3. Dashboard JSON key contract test
# ---------------------------------------------------------------------------

DASHBOARD_ISSUE_KEYS = {
    "id",
    "title",
    "type",
    "status",
    "status_category",
    "priority",
    "assignee",
    "parent_id",
    "blocked_by",
    "blocks",
    "updated_at",
    "created_at",
    "closed_at",
    "is_ready",
    "children",
    "labels",
    "description",
    "notes",
    "fields",
}


def test_issue_dict_keys_cover_dashboard_contract() -> None:
    hints = get_type_hints(IssueDict)
    missing = DASHBOARD_ISSUE_KEYS - set(hints.keys())
    assert not missing, f"IssueDict missing dashboard keys: {missing}"


# ---------------------------------------------------------------------------
# 4. Guard: ensure TYPES_DIR exists to prevent vacuous parametrize pass (W8)
# ---------------------------------------------------------------------------


def test_types_dir_exists() -> None:
    """Sanity check: TYPES_DIR must exist, otherwise the import constraint
    test would produce zero parametrize cases and pass vacuously."""
    assert TYPES_DIR.exists(), f"types dir not found at {TYPES_DIR}"


# ---------------------------------------------------------------------------
# 5. Dashboard JS contract: enriched issue detail keys (W9)
# ---------------------------------------------------------------------------

# Keys the JS frontend reads from the enriched issue detail endpoint
DASHBOARD_ENRICHED_KEYS = DASHBOARD_ISSUE_KEYS | {
    "dep_details",
    "events",
    "comments",
}


def test_enriched_issue_detail_keys_cover_dashboard_contract() -> None:
    """EnrichedIssueDetail must contain all keys the dashboard JS reads
    from the issue detail endpoint."""
    hints = get_type_hints(EnrichedIssueDetail)
    missing = DASHBOARD_ENRICHED_KEYS - set(hints.keys())
    assert not missing, f"EnrichedIssueDetail missing keys consumed by dashboard JS: {missing}"
