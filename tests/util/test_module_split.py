"""Verify mixin-based FiligreeDB composition and MCP tool module split."""

import re
from pathlib import Path

import pytest
from mcp.types import Tool

from filigree.core import FiligreeDB
from filigree.db_annotations import AnnotationsMixin
from filigree.db_events import EventsMixin
from filigree.db_files import FilesMixin
from filigree.db_issues import IssuesMixin
from filigree.db_meta import MetaMixin
from filigree.db_planning import PlanningMixin
from filigree.db_workflow import WorkflowMixin

# ---------------------------------------------------------------------------
# MCP tool module split tests
# ---------------------------------------------------------------------------


def test_mcp_tools_package_exists() -> None:
    """All domain modules import and expose register()."""
    from filigree.mcp_tools import annotations, entities, files, issues, meta, observations, planning, workflow

    for mod in (issues, planning, files, workflow, meta, observations, annotations, entities):
        assert callable(getattr(mod, "register", None)), f"{mod.__name__} missing register()"


def test_mcp_tools_register_shape() -> None:
    """register() returns (list[Tool], dict[str, Callable])."""
    from filigree.mcp_tools import annotations, entities, files, issues, meta, observations, planning, workflow

    for mod in (issues, planning, files, workflow, meta, observations, annotations, entities):
        tools, handlers = mod.register()
        assert isinstance(tools, list), f"{mod.__name__}.register() tools is not a list"
        assert all(isinstance(t, Tool) for t in tools), f"{mod.__name__} has non-Tool items"
        assert isinstance(handlers, dict), f"{mod.__name__}.register() handlers is not a dict"
        for name, fn in handlers.items():
            assert isinstance(name, str), f"{mod.__name__} handler key is not str"
            assert callable(fn), f"{mod.__name__} handler {name} is not callable"
        # Every tool should have a matching handler
        tool_names = {t.name for t in tools}
        handler_names = set(handlers.keys())
        assert tool_names == handler_names, (
            f"{mod.__name__}: tool/handler mismatch — tools={tool_names - handler_names}, handlers={handler_names - tool_names}"
        )


def test_mcp_tools_total_count() -> None:
    """All 118 tools are registered across domain modules.

    Count includes the structured observation triage surfaces, the
    four entity-association tools (ADR-029), the reconciliation-debt
    list (B2), the explicit DB checkpoint verb, and the heddle
    reverify-worklist consumer (federation, Seam 2A) so the split-module
    registry test notices dropped tool registrations.
    """
    from filigree.mcp_tools import annotations, entities, federation, files, issues, meta, observations, planning, scanners, workflow

    total = 0
    for mod in (issues, planning, files, workflow, meta, observations, annotations, entities, federation):
        tools, _ = mod.register()
        total += len(tools)
    # Scanner module needs include_legacy=True to include all legacy aliases.
    tools, _ = scanners.register(include_legacy=True)
    total += len(tools)
    # +3 for structured observation triage: link, batch-link, promote-many-to-one.
    # +4 for entity_associations (ADR-029): add/remove/list-by-issue/list-by-entity.
    # +1 for the heddle reverify-worklist consumer (federation).
    assert total == 118, f"Expected 118 tools total, got {total}"


def test_issue_and_file_mcp_tools_do_not_import_mcp_server_private_globals() -> None:
    """Domain MCP modules should use the common context facade, not mcp_server internals."""
    root = Path(__file__).resolve().parents[2]
    for rel in ("src/filigree/mcp_tools/issues.py", "src/filigree/mcp_tools/files.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "from filigree.mcp_server import" not in text, rel


def test_mcp_tools_do_not_import_transport_runtime_globals() -> None:
    """MCP tool modules should get runtime state from the injected MCP context."""
    root = Path(__file__).resolve().parents[2]
    tools_dir = root / "src/filigree/mcp_tools"
    for path in sorted(tools_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "from filigree.mcp_server import" not in text, str(path.relative_to(root))
        assert "import filigree.mcp_server" not in text, str(path.relative_to(root))


def test_file_mixin_delegates_finding_issue_cascade_policy() -> None:
    """Cross-domain finding→issue policy should live in the cascade service."""
    root = Path(__file__).resolve().parents[2]
    files_text = (root / "src/filigree/db_files.py").read_text(encoding="utf-8")
    service_text = (root / "src/filigree/finding_issue_cascade.py").read_text(encoding="utf-8")

    assert "FindingIssueCascadeService" in files_text
    assert "record_reconciliation_debt_comment" not in files_text
    assert "class FindingIssueCascadeService" in service_text
    assert "def close_fixed_finding" in service_text
    assert "def reopen_regressed_finding" in service_text


def test_mcp_docs_tool_count_matches_registry() -> None:
    """docs/mcp.md headline count must match the live tool registry."""
    from filigree.mcp_server import _all_tools

    docs_text = Path("docs/mcp.md").read_text(encoding="utf-8")
    match = re.search(r"server provides (\d+) tools", docs_text)
    assert match is not None, "docs/mcp.md must include the MCP tool count"
    assert int(match.group(1)) == len(_all_tools)


def test_mcp_backward_compat_imports() -> None:
    """_text, _MAX_LIST_RESULTS, _safe_path importable from mcp_server."""
    from filigree.mcp_server import _MAX_LIST_RESULTS, _safe_path, _text  # type: ignore[attr-defined]

    assert _MAX_LIST_RESULTS == 50
    assert callable(_text)
    assert callable(_safe_path)


def test_events_mixin_is_base_class() -> None:
    """FiligreeDB should inherit from EventsMixin."""
    assert issubclass(FiligreeDB, EventsMixin)


def test_record_event_available(db: FiligreeDB) -> None:
    """_record_event should be callable on FiligreeDB instances."""
    issue = db.create_issue(title="test")
    db._record_event(issue.id, "test_event", actor="test")
    events = db.get_issue_events(issue.id)
    assert any(e["event_type"] == "test_event" for e in events)


def test_undo_last_available(db: FiligreeDB) -> None:
    """undo_last should work through mixin composition."""
    issue = db.create_issue(title="original")
    db.update_issue(issue.id, title="changed")
    result = db.undo_last(issue.id)
    assert result["event_type"] == "title_changed"  # type: ignore[typeddict-item]


def test_archive_compact_available(db: FiligreeDB) -> None:
    """archive_closed and compact_events should work."""
    archived = db.archive_closed(days_old=0)
    assert isinstance(archived, list)
    compacted = db.compact_events(keep_recent=50)
    assert isinstance(compacted, int)


# -- WorkflowMixin ----------------------------------------------------------


def test_annotations_mixin_is_base_class() -> None:
    """FiligreeDB should inherit from AnnotationsMixin."""
    assert issubclass(FiligreeDB, AnnotationsMixin)


def test_workflow_mixin_is_base_class() -> None:
    """FiligreeDB should inherit from WorkflowMixin."""
    assert issubclass(FiligreeDB, WorkflowMixin)


def test_templates_available(db: FiligreeDB) -> None:
    """Template listing should work through mixin composition."""
    templates = db.list_templates()
    assert isinstance(templates, list)
    assert len(templates) > 0  # builtins exist


def test_validate_status(db: FiligreeDB) -> None:
    """_validate_status should work through mixin composition."""
    # Should not raise for valid status; returns None on success
    result = db._validate_status("open", "task")  # type: ignore[func-returns-value]
    assert result is None


# -- MetaMixin --------------------------------------------------------------


def test_meta_mixin_is_base_class() -> None:
    """FiligreeDB should inherit from MetaMixin."""
    assert issubclass(FiligreeDB, MetaMixin)


def test_comments_available(db: FiligreeDB) -> None:
    """Comment operations should work through mixin composition."""
    issue = db.create_issue(title="test")
    comment_id = db.add_comment(issue.id, "hello")
    assert isinstance(comment_id, int)
    comments = db.get_comments(issue.id)
    assert len(comments) == 1


def test_export_import_roundtrip(db: FiligreeDB, tmp_path: Path) -> None:
    """Export/import should work through mixin composition."""
    db.create_issue(title="export-test")
    out = str(tmp_path / "export.jsonl")
    count = db.export_jsonl(out)
    assert count > 0


def test_import_merge_returns_zero_for_duplicates(db: FiligreeDB, tmp_path: Path) -> None:
    """import_jsonl with merge=True should count 0 when all records are duplicates."""
    db.create_issue(title="dup-test")
    out = tmp_path / "export.jsonl"
    first_count = db.export_jsonl(str(out))
    assert first_count > 0
    # Import the same data again — all records are duplicates
    dup_result = db.import_jsonl(str(out), merge=True)
    assert dup_result["count"] == 0, f"Expected 0 for duplicate import, got {dup_result['count']}"


def test_export_import_roundtrip_with_files(db: FiligreeDB, tmp_path: Path) -> None:
    """JSONL round-trip should include file-domain tables through MetaMixin composition."""
    issue = db.create_issue(title="file-roundtrip")
    file_rec = db.register_file("src/example.py", language="python")
    db.conn.execute(
        "INSERT INTO scan_findings "
        "(id, file_id, issue_id, scan_source, rule_id, severity, status, message, suggestion, "
        "scan_run_id, line_start, line_end, seen_count, first_seen, updated_at, last_seen_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "mix-sf-1",
            file_rec.id,
            issue.id,
            "ruff",
            "F401",
            "medium",
            "open",
            "unused import",
            "",
            "run-mix",
            1,
            1,
            1,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "{}",
        ),
    )
    db.conn.execute(
        "INSERT INTO file_associations (file_id, issue_id, assoc_type, created_at) VALUES (?, ?, ?, ?)",
        (file_rec.id, issue.id, "bug_in", "2026-01-01T00:00:00+00:00"),
    )
    db.conn.execute(
        "INSERT INTO file_events (file_id, event_type, field, old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (file_rec.id, "file_metadata_update", "language", "", "python", "2026-01-01T00:00:00+00:00"),
    )
    db.conn.commit()

    out = tmp_path / "files-export.jsonl"
    count = db.export_jsonl(str(out))
    assert count > 0

    fresh = FiligreeDB(tmp_path / "fresh.db", prefix="test")
    try:
        fresh.initialize()
        import_result = fresh.import_jsonl(str(out))
        assert import_result["count"] == count
        assert fresh.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 1
        assert fresh.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 1
    finally:
        fresh.close()


# -- PlanningMixin ----------------------------------------------------------


def test_planning_mixin_is_base_class() -> None:
    """FiligreeDB should inherit from PlanningMixin."""
    assert issubclass(FiligreeDB, PlanningMixin)


def test_dependency_management(db: FiligreeDB) -> None:
    """Dependency operations should work through mixin composition."""
    a = db.create_issue(title="a")
    b = db.create_issue(title="b")
    db.add_dependency(b.id, a.id)
    blocked = db.get_blocked()
    assert any(i.id == b.id for i in blocked)


def test_get_ready(db: FiligreeDB) -> None:
    """get_ready should work through mixin composition."""
    db.create_issue(title="ready-test")
    ready = db.get_ready()
    assert len(ready) >= 1


# -- IssuesMixin ------------------------------------------------------------


def test_issues_mixin_is_base_class() -> None:
    """FiligreeDB should inherit from IssuesMixin."""
    assert issubclass(FiligreeDB, IssuesMixin)


def test_full_issue_lifecycle(db: FiligreeDB) -> None:
    """create/update/close should work through IssuesMixin composition."""
    issue = db.create_issue(title="lifecycle")
    assert issue.title == "lifecycle"
    db.update_issue(issue.id, title="updated")
    updated = db.get_issue(issue.id)
    assert updated.title == "updated"
    db.close_issue(issue.id)
    closed = db.get_issue(issue.id)
    assert closed.status_category == "done"


def test_batch_operations(db: FiligreeDB) -> None:
    """batch_update should work through IssuesMixin composition."""
    a = db.create_issue(title="batch-a")
    b = db.create_issue(title="batch-b")
    db.batch_update([a.id, b.id], priority=0)
    assert db.get_issue(a.id).priority == 0
    assert db.get_issue(b.id).priority == 0


def test_claim_and_release(db: FiligreeDB) -> None:
    """claim_issue and release_claim should work through IssuesMixin."""
    issue = db.create_issue(title="claimable")
    claimed = db.claim_issue(issue.id, assignee="agent-1")
    assert claimed.assignee == "agent-1"
    released = db.release_claim(issue.id)
    assert released.assignee == ""


def test_list_issues(db: FiligreeDB) -> None:
    """list_issues should work through IssuesMixin composition."""
    db.create_issue(title="listable-item")
    found = db.list_issues(status="open")
    assert len(found) >= 1


def test_search_issues(db: FiligreeDB) -> None:
    """search_issues should work through IssuesMixin composition."""
    db.create_issue(title="searchable-xyz")
    # search_issues uses FTS5 or LIKE fallback — both should find it
    results = db.search_issues("searchable")
    assert len(results) >= 1


# -- FilesMixin -------------------------------------------------------------


def test_files_mixin_is_base_class() -> None:
    """FiligreeDB should inherit from FilesMixin."""
    assert issubclass(FiligreeDB, FilesMixin)


def test_register_and_get_file(db: FiligreeDB) -> None:
    """register_file and get_file should work through FilesMixin composition."""
    f = db.register_file(path="src/example.py", language="python")
    retrieved = db.get_file(f.id)
    assert retrieved.path == "src/example.py"
    assert retrieved.language == "python"


def test_file_associations(db: FiligreeDB) -> None:
    """add_file_association and get_file_associations should work."""
    issue = db.create_issue(title="assoc-test")
    f = db.register_file(path="src/assoc.py")
    db.add_file_association(f.id, issue.id, assoc_type="bug_in")
    assocs = db.get_file_associations(f.id)
    assert len(assocs) == 1
    assert assocs[0]["issue_id"] == issue.id


# -- _validate_string_list --------------------------------------------------


class TestValidateStringList:
    """Direct tests for _validate_string_list TypeError path (filigree-bdd0f35a36)."""

    def test_non_list_raises_typeerror(self) -> None:
        from filigree.db_issues import _validate_string_list

        with pytest.raises(TypeError, match="must be a list of strings"):
            _validate_string_list("not-a-list", "labels")

    def test_list_with_non_strings_raises_typeerror(self) -> None:
        from filigree.db_issues import _validate_string_list

        with pytest.raises(TypeError, match="must be a list of strings"):
            _validate_string_list([1, 2, 3], "labels")

    def test_mixed_types_raises_typeerror(self) -> None:
        from filigree.db_issues import _validate_string_list

        with pytest.raises(TypeError, match="must be a list of strings"):
            _validate_string_list(["valid", 42], "deps")

    def test_valid_list_passes(self) -> None:
        from filigree.db_issues import _validate_string_list

        # Should not raise
        _validate_string_list(["a", "b", "c"], "labels")

    def test_empty_list_passes(self) -> None:
        from filigree.db_issues import _validate_string_list

        # Empty list is valid
        _validate_string_list([], "labels")

    def test_none_raises_typeerror(self) -> None:
        from filigree.db_issues import _validate_string_list

        with pytest.raises(TypeError, match="must be a list of strings"):
            _validate_string_list(None, "labels")
