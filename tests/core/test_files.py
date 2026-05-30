"""Tests for core file records, scan findings, and associations."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from filigree.core import FiligreeDB, ScanFinding, _normalize_scan_path
from filigree.db_files import _safe_json_loads  # type: ignore[attr-defined]
from filigree.registry import BatchQuery, BatchResolution, ResolvedFile, resolve_files_batch_via_loop
from filigree.types.core import make_entity_id, make_file_id
from tests._fakes.registry import FixedRegistry

# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class _CasefoldingRegistry:
    def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
        canonical_path = path.casefold()
        return {
            "file_id": make_file_id(f"core:file:{canonical_path.replace('/', ':')}"),
            "content_hash": f"hash:{canonical_path}",
            "canonical_path": canonical_path,
            "language": language,
            "registry_backend": "clarion",
        }

    def is_displaced(self) -> bool:
        return False


class TestFileSchema:
    """Verify file/finding tables are created."""

    def test_file_records_table_exists(self, db: FiligreeDB) -> None:
        row = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_records'").fetchone()
        assert row is not None

    def test_scan_findings_table_exists(self, db: FiligreeDB) -> None:
        row = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scan_findings'").fetchone()
        assert row is not None

    def test_file_associations_table_exists(self, db: FiligreeDB) -> None:
        row = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_associations'").fetchone()
        assert row is not None

    def test_scan_runs_table_exists(self, db: FiligreeDB) -> None:
        row = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scan_runs'").fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# FileRecord CRUD tests
# ---------------------------------------------------------------------------


class TestRegisterFile:
    """Tests for registering and retrieving file records."""

    def test_register_file_uses_registry_resolved_file_id(self, tmp_path: Path) -> None:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=FixedRegistry(
                file_id="core:file:def456@src/direct.py",
                content_hash="hash-direct",
                registry_backend="clarion",
            ),
        )
        try:
            db.initialize()

            file_record = db.register_file("src/direct.py", language="python")

            assert file_record.id == "core:file:def456@src/direct.py"
            assert file_record.path == "src/direct.py"
            assert file_record.language == "python"
            assert file_record.content_hash == "hash-direct"
            assert file_record.registry_backend == "clarion"
        finally:
            db.close()

    def test_register_new_file(self, db: FiligreeDB) -> None:
        f = db.register_file("src/main.py", language="python")
        assert f.path == "src/main.py"
        assert f.language == "python"
        assert f.id.startswith("test-f-")

    @pytest.mark.parametrize("path", ["../outside.py", "src/../../outside.py"])
    def test_register_file_rejects_non_project_relative_path(self, db: FiligreeDB, path: str) -> None:
        with pytest.raises(ValueError, match="project-relative"):
            db.register_file(path)

    def test_register_file_rejects_project_root_absolute_path(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="project-relative"):
            db.register_file("/src/main.py", language="python")

    def test_register_file_rejects_escaping_registry_canonical_path(self, tmp_path: Path) -> None:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=FixedRegistry(
                file_id="core:file:escape",
                canonical_path="../outside.py",
                registry_backend="clarion",
            ),
        )
        try:
            db.initialize()

            with pytest.raises(ValueError, match="project-relative"):
                db.register_file("src/input.py", language="python")

            assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0
        finally:
            db.close()

    def test_register_file_rejects_non_string_path(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="File path must be a string"):
            db.register_file(123)  # type: ignore[arg-type]

    def test_register_file_rejects_non_object_metadata(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="metadata must be a JSON object"):
            db.register_file("src/meta.py", metadata=cast(Any, ["bad"]))

        db.register_file("src/meta.py", metadata={"owner": "team-a"})
        with pytest.raises(ValueError, match="metadata must be a JSON object"):
            db.register_file("src/meta.py", metadata=cast(Any, ["bad"]))

    def test_register_file_records_actor_on_record_and_metadata_event(self, db: FiligreeDB) -> None:
        created = db.register_file("src/owned.py", language="python", actor="scanner-a")

        assert created.to_dict()["created_by"] == "scanner-a"
        assert created.to_dict()["updated_by"] == "scanner-a"

        updated = db.register_file("src/owned.py", metadata={"owner": "team-a"}, actor="curator-b")
        timeline = db.get_file_timeline(updated.id, event_type="file_metadata_update")

        assert updated.to_dict()["created_by"] == "scanner-a"
        assert updated.to_dict()["updated_by"] == "curator-b"
        assert timeline["results"][0]["data"]["actor"] == "curator-b"
        assert timeline["results"][0]["data"]["field"] == "metadata"

    def test_register_file_infers_known_language_when_missing(self, db: FiligreeDB) -> None:
        py = db.register_file("src/main.py")
        md = db.register_file("docs/readme.md")
        unknown = db.register_file("scripts/run.unknownext")
        explicit = db.register_file("src/custom.py", language="python3")

        assert py.language == "python"
        assert md.language == "markdown"
        assert unknown.language == ""
        assert explicit.language == "python3"

    def test_register_duplicate_path_returns_existing(self, db: FiligreeDB) -> None:
        f1 = db.register_file("src/main.py")
        f2 = db.register_file("src/main.py")
        assert f1.id == f2.id

    def test_register_duplicate_path_race_returns_existing(self, tmp_path: Path) -> None:
        """If another writer inserts the path after the lookup, retry as an update."""
        db_path = tmp_path / "filigree.db"
        primary = FiligreeDB(db_path, prefix="test")
        racer = FiligreeDB(db_path, prefix="test")
        primary.initialize()
        racer.initialize()
        real_conn = primary.conn

        class RaceOnceConnection:
            def __init__(self) -> None:
                self.armed = True

            def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
                if self.armed and sql.lstrip().startswith("INSERT INTO file_records"):
                    self.armed = False
                    racer.register_file("src/race.py", actor="racer")
                return real_conn.execute(sql, params)

            def __getattr__(self, name: str) -> Any:
                return getattr(real_conn, name)

        primary._conn = RaceOnceConnection()  # type: ignore[assignment]
        try:
            registered = primary.register_file("src/race.py", language="python", actor="scanner")
        finally:
            primary.close()
            racer.close()

        assert registered.path == "src/race.py"
        assert registered.language == "python"

    def test_register_file_recovers_when_registry_canonicalizes_path(self, tmp_path: Path) -> None:
        db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=_CasefoldingRegistry())
        try:
            db.initialize()

            created = db.register_file("src/main.py", language="python")
            recovered = db.register_file("SRC/Main.py", language="python")

            assert recovered.id == created.id
            assert recovered.path == "src/main.py"
            assert db.conn.execute("SELECT count(*) FROM file_records").fetchone()[0] == 1
        finally:
            db.close()

    def test_register_existing_file_refreshes_displaced_registry_metadata(self, tmp_path: Path) -> None:
        class RefreshingRegistry:
            calls = 0

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                self.calls += 1
                return {
                    "file_id": make_entity_id("core:file:stable@src/refresh.py"),
                    "content_hash": f"sha256:refresh-{self.calls}",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        registry = RefreshingRegistry()
        db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=registry, registry_backend="clarion")
        try:
            db.initialize()

            created = db.register_file("src/refresh.py", language="python")
            db.conn.execute(
                "UPDATE file_records SET content_hash = '', registry_backend = 'local' WHERE id = ?",
                (created.id,),
            )
            db.conn.commit()
            updated = db.register_file("src/refresh.py", language="python")

            assert created.id == updated.id
            assert updated.content_hash == "sha256:refresh-2"
            assert updated.registry_backend == "clarion"
            assert registry.calls == 2
            events = db.get_file_timeline(updated.id, event_type="file_metadata_update")
            assert {event["data"]["field"] for event in events["results"]} >= {"content_hash", "registry_backend"}
        finally:
            db.close()

    def test_register_updates_language(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py", language="")
        f2 = db.register_file("src/main.py", language="python")
        assert f2.language == "python"

    def test_register_duplicate_no_changes_preserves_updated_at(self, db: FiligreeDB) -> None:
        """Re-registering with no new data must not bump updated_at.

        Regression: unconditional updated_at write caused phantom updates.
        """
        f1 = db.register_file("src/stable.py", language="python")
        original_updated = f1.updated_at
        f2 = db.register_file("src/stable.py")
        assert f2.updated_at == original_updated

    def test_get_file_by_id(self, db: FiligreeDB) -> None:
        created = db.register_file("src/main.py", language="python")
        fetched = db.get_file(created.id)
        assert fetched.path == "src/main.py"
        assert fetched.language == "python"

    def test_get_file_not_found(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError, match="test-f-nope"):
            db.get_file("test-f-nope")

    def test_get_file_by_path(self, db: FiligreeDB) -> None:
        created = db.register_file("src/main.py")
        fetched = db.get_file_by_path("src/main.py")
        assert fetched is not None
        assert fetched.id == created.id

    def test_get_file_by_path_normalizes_equivalent_paths(self, db: FiligreeDB) -> None:
        """Equivalent path spellings should round-trip through get_file_by_path()."""
        created = db.register_file("src/foo/../bar.py")
        fetched = db.get_file_by_path(r"src\bar.py")
        assert fetched is not None
        assert fetched.id == created.id

    def test_get_file_by_path_not_found(self, db: FiligreeDB) -> None:
        result = db.get_file_by_path("nonexistent.py")
        assert result is None

    def test_register_normalizes_path(self, db: FiligreeDB) -> None:
        """Bug filigree-b78901: register_file must normalize paths for consistent identity."""
        f1 = db.register_file("src/foo/../bar.py")
        f2 = db.register_file("src/bar.py")
        assert f1.id == f2.id  # Same file, same record
        assert f1.path == "src/bar.py"  # Stored normalized

    def test_register_normalizes_backslashes(self, db: FiligreeDB) -> None:
        """Windows-style paths should be normalized to forward slashes."""
        f1 = db.register_file("src\\utils\\helper.py")
        f2 = db.register_file("src/utils/helper.py")
        assert f1.id == f2.id

    def test_register_empty_path_after_normalization_raises(self, db: FiligreeDB) -> None:
        """Path that normalizes to empty (e.g. '.') should be rejected."""
        with pytest.raises(ValueError, match="empty after normalization"):
            db.register_file(".")

    def test_register_rejects_absolute_path(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="project-relative"):
            db.register_file("/etc/passwd")

    def test_register_explicit_empty_metadata_clears_existing(self, db: FiligreeDB) -> None:
        """filigree-822d514ec7: register_file(metadata={}) must clear stored metadata.

        `metadata=None` means leave unchanged; `metadata={}` means set to empty.
        """
        f1 = db.register_file("src/main.py", metadata={"owner": "team-a"})
        assert f1.metadata == {"owner": "team-a"}
        f2 = db.register_file("src/main.py", metadata={})
        assert f2.metadata == {}

    def test_register_metadata_none_preserves_existing(self, db: FiligreeDB) -> None:
        """metadata=None on re-register must preserve existing metadata."""
        db.register_file("src/main.py", metadata={"owner": "team-a"})
        f = db.register_file("src/main.py")
        assert f.metadata == {"owner": "team-a"}


class TestDeleteFileRecord:
    """Tests for deleting file records and their file-domain dependents."""

    def test_delete_unreferenced_file_record(self, db: FiligreeDB) -> None:
        file_record = db.register_file("src/delete_me.py")

        result = db.delete_file_record(file_record.id)

        assert result["status"] == "deleted"
        assert result["file_id"] == file_record.id
        assert result["deleted_findings"] == 0
        with pytest.raises(KeyError):
            db.get_file(file_record.id)

    def test_delete_refuses_associations_without_force(self, db: FiligreeDB) -> None:
        file_record = db.register_file("src/linked.py")
        issue = db.create_issue("Linked issue")
        db.add_file_association(file_record.id, issue.id, "mentioned_in")

        with pytest.raises(ValueError, match="1 association"):
            db.delete_file_record(file_record.id)

        assert db.get_file(file_record.id).id == file_record.id

    def test_delete_refuses_open_findings_without_force(self, db: FiligreeDB) -> None:
        scan = db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/open_finding.py", "rule_id": "R1", "message": "Still open"}],
        )
        file_record = db.get_file_by_path("src/open_finding.py")
        assert file_record is not None

        with pytest.raises(ValueError, match="1 open finding"):
            db.delete_file_record(file_record.id)

        assert db.get_finding(scan["new_finding_ids"][0])["file_id"] == file_record.id

    def test_delete_allows_terminal_findings_without_force(self, db: FiligreeDB) -> None:
        scan = db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/terminal_finding.py", "rule_id": "R1", "message": "Dismissed"}],
        )
        file_record = db.get_file_by_path("src/terminal_finding.py")
        assert file_record is not None
        db.update_finding(scan["new_finding_ids"][0], status="false_positive")

        result = db.delete_file_record(file_record.id)

        assert result["deleted_findings"] == 1
        with pytest.raises(KeyError):
            db.get_file(file_record.id)

    def test_force_delete_cascades_associations_and_open_findings(self, db: FiligreeDB) -> None:
        scan = db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/force_delete.py", "rule_id": "R1", "message": "Open"}],
        )
        file_record = db.get_file_by_path("src/force_delete.py")
        assert file_record is not None
        issue = db.create_issue("Force linked issue")
        db.add_file_association(file_record.id, issue.id, "mentioned_in")

        result = db.delete_file_record(file_record.id, force=True)

        assert result["deleted_associations"] == 1
        assert result["deleted_findings"] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM file_associations WHERE file_id = ?", (file_record.id,)).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings WHERE file_id = ?", (file_record.id,)).fetchone()[0] == 0
        with pytest.raises(KeyError):
            db.get_finding(scan["new_finding_ids"][0])

    def test_delete_removes_annotation_file_target_links(self, tmp_path: Path) -> None:
        db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
        db.initialize()
        db.project_root = tmp_path
        try:
            file_record = db.register_file("src/linked_target.py")
            scan = db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "src/linked_target.py", "rule_id": "R1", "message": "Open"}],
            )
            finding_id = scan["new_finding_ids"][0]
            anchor = tmp_path / "src" / "annotation_anchor.py"
            anchor.parent.mkdir()
            anchor.write_text("anchor\n")
            db.annotate_file(
                "src/annotation_anchor.py",
                "This annotation points at a file record.",
                links=[{"target_type": "file", "target_id": file_record.id, "relationship": "relevant_to"}],
            )
            db.annotate_file(
                "src/annotation_anchor.py",
                "This annotation points at a finding record.",
                links=[{"target_type": "finding", "target_id": finding_id, "relationship": "evidence_for"}],
            )

            result = db.delete_file_record(file_record.id, force=True)

            assert result["deleted_annotation_links"] == 2
            assert (
                db.conn.execute(
                    "SELECT COUNT(*) FROM annotation_links WHERE "
                    "(target_type = 'file' AND target_id = ?) OR (target_type = 'finding' AND target_id = ?)",
                    (file_record.id, finding_id),
                ).fetchone()[0]
                == 0
            )
        finally:
            db.close()


class TestListFiles:
    """Tests for listing file records."""

    def test_list_empty(self, db: FiligreeDB) -> None:
        files = db.list_files()
        assert files == []

    def test_list_returns_all(self, db: FiligreeDB) -> None:
        db.register_file("a.py")
        db.register_file("b.py")
        files = db.list_files()
        assert len(files) == 2

    def test_list_with_limit(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.register_file(f"file{i}.py")
        files = db.list_files(limit=3)
        assert len(files) == 3

    def test_list_with_offset(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.register_file(f"file{i}.py")
        files = db.list_files(offset=3)
        assert len(files) == 2

    def test_list_with_language_filter(self, db: FiligreeDB) -> None:
        db.register_file("a.py", language="python")
        db.register_file("b.js", language="javascript")
        files = db.list_files(language="python")
        assert len(files) == 1
        assert files[0].language == "python"

    def test_list_with_path_prefix(self, db: FiligreeDB) -> None:
        db.register_file("src/core/a.py")
        db.register_file("src/core/b.py")
        db.register_file("vendor/src/core/c.py")
        db.register_file("tests/test_a.py")
        files = db.list_files(path_prefix="src/core/")
        assert {item.path for item in files} == {"src/core/a.py", "src/core/b.py"}

    def test_list_with_path_prefix_escapes_like_wildcards(self, db: FiligreeDB) -> None:
        """path_prefix containing SQL LIKE wildcards (% and _) must match literally."""
        db.register_file("src/file_test.py")
        db.register_file("src/filextest.py")  # _ wildcard would match this
        db.register_file("src/file%test.py")
        db.register_file("src/fileABCtest.py")  # % wildcard would match this

        # Underscore must be literal — only file_test.py should match
        files = db.list_files(path_prefix="src/file_test")
        assert len(files) == 1
        assert files[0].path == "src/file_test.py"

        # Percent must be literal — only file%test.py should match
        files = db.list_files(path_prefix="src/file%test")
        assert len(files) == 1
        assert files[0].path == "src/file%test.py"

    def test_list_sorted_by_path(self, db: FiligreeDB) -> None:
        db.register_file("z.py")
        db.register_file("a.py")
        files = db.list_files(sort="path")
        assert files[0].path == "a.py"
        assert files[1].path == "z.py"

    def test_list_files_invalid_sort_raises(self, db: FiligreeDB) -> None:
        """Invalid sort parameter must raise ValueError, not silently fall back."""
        db.register_file("a.py")
        with pytest.raises(ValueError, match="Invalid sort"):
            db.list_files(sort="nonexistent_column")


# ---------------------------------------------------------------------------
# Scan findings tests
# ---------------------------------------------------------------------------


class TestProcessScanResults:
    """Tests for ingesting scan findings."""

    def test_count_file_lines_logs_oserror_at_debug(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        missing = tmp_path / "missing.py"

        with caplog.at_level(logging.DEBUG, logger="filigree.db_files"):
            result = FiligreeDB._count_file_lines(missing)

        assert result is None
        assert "Could not count lines for" in caplog.text
        assert str(missing) in caplog.text
        records = [record for record in caplog.records if record.message.startswith("Could not count lines for")]
        assert records
        assert records[0].exc_info is not None

    def test_ingest_uses_registry_resolved_file_id(self, tmp_path: Path) -> None:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=FixedRegistry(
                file_id="core:file:abc123@src/main.py",
                content_hash="hash-ingest",
                registry_backend="clarion",
            ),
        )
        try:
            db.initialize()
            result = db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {
                        "path": "src/main.py",
                        "language": "python",
                        "rule_id": "E501",
                        "severity": "low",
                        "message": "Line too long",
                    },
                ],
            )

            assert result["files_created"] == 1
            file_record = db.get_file_by_path("src/main.py")
            assert file_record is not None
            assert file_record.id == "core:file:abc123@src/main.py"
            assert file_record.content_hash == "hash-ingest"
            assert file_record.registry_backend == "clarion"
            finding = db.get_finding(result["new_finding_ids"][0])
            assert finding["file_id"] == "core:file:abc123@src/main.py"
        finally:
            db.close()

    def test_ingest_refreshes_existing_displaced_registry_metadata(self, tmp_path: Path) -> None:
        class RefreshingRegistry:
            calls = 0

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                self.calls += 1
                return {
                    "file_id": make_entity_id("core:file:stable@src/refresh.py"),
                    "content_hash": f"sha256:scan-{self.calls}",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                return resolve_files_batch_via_loop(self, queries, actor=actor)

            def is_displaced(self) -> bool:
                return True

        registry = RefreshingRegistry()
        db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=registry, registry_backend="clarion")
        try:
            db.initialize()
            first = db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {
                        "path": "src/refresh.py",
                        "language": "python",
                        "rule_id": "E501",
                        "severity": "low",
                        "message": "first",
                    },
                ],
            )

            second = db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {
                        "path": "src/refresh.py",
                        "language": "python",
                        "rule_id": "E502",
                        "severity": "low",
                        "message": "second",
                    },
                ],
            )

            file_record = db.get_file_by_path("src/refresh.py")
            assert file_record is not None
            assert first["files_created"] == 1
            assert second["files_updated"] == 1
            assert registry.calls == 2
            assert file_record.content_hash == "sha256:scan-2"
            assert file_record.registry_backend == "clarion"
        finally:
            db.close()

    def test_ingest_resolves_new_files_before_write_transaction(self, tmp_path: Path) -> None:
        transaction_states: list[bool] = []

        class InspectingRegistry:
            db: FiligreeDB | None = None

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                assert self.db is not None
                transaction_states.append(self.db.conn.in_transaction)
                return {
                    "file_id": make_entity_id(f"core:file:{path.replace('/', ':')}"),
                    "content_hash": f"hash:{path}",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        registry = InspectingRegistry()
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=registry,
            registry_backend="clarion",
            clarion_config={"base_url": "http://clarion.test"},
        )
        registry.db = db
        try:
            db.initialize()

            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "src/first.py", "rule_id": "E501", "severity": "low", "message": "one"},
                    {"path": "src/second.py", "rule_id": "E502", "severity": "low", "message": "two"},
                ],
            )

            assert transaction_states == [False, False]
        finally:
            db.close()

    def test_ingest_recovers_when_registry_canonicalizes_path(self, tmp_path: Path) -> None:
        db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=_CasefoldingRegistry())
        try:
            db.initialize()
            first = db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "src/main.py", "rule_id": "E501", "severity": "low", "message": "one"}],
            )

            second = db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "SRC/Main.py", "rule_id": "E502", "severity": "low", "message": "two"}],
            )

            assert first["files_created"] == 1
            assert second["files_created"] == 0
            assert second["files_updated"] == 1
            assert db.conn.execute("SELECT count(*) FROM file_records").fetchone()[0] == 1
            file_record = db.get_file_by_path("src/main.py")
            assert file_record is not None
            assert len(db.get_findings(file_record.id)) == 2
        finally:
            db.close()

    def test_ingest_creates_file_and_findings(self, db: FiligreeDB) -> None:
        result = db.process_scan_results(
            scan_source="ruff",
            findings=[
                {
                    "path": "src/main.py",
                    "rule_id": "E501",
                    "severity": "low",
                    "message": "Line too long",
                    "line_start": 10,
                    "line_end": 10,
                },
            ],
        )
        assert result["files_created"] >= 1
        assert result["findings_created"] >= 1

    def test_ingest_and_update_preserve_finding_actor_attribution(self, db: FiligreeDB) -> None:
        result = db.process_scan_results(
            scan_source="agent",
            observation_actor="agent-a",
            findings=[{"path": "src/main.py", "rule_id": "A1", "severity": "low", "message": "m"}],
        )
        finding_id = result["new_finding_ids"][0]
        created = db.get_finding(finding_id)

        assert created["created_by"] == "agent-a"
        assert created["updated_by"] == "agent-a"

        updated = db.update_finding(finding_id, status="acknowledged", actor="reviewer-b")

        assert updated["created_by"] == "agent-a"
        assert updated["updated_by"] == "reviewer-b"

    def test_ingest_upserts_existing_finding(self, db: FiligreeDB) -> None:
        finding = {
            "path": "src/main.py",
            "rule_id": "E501",
            "severity": "low",
            "message": "Line too long",
            "line_start": 10,
        }
        db.process_scan_results(scan_source="ruff", findings=[finding])
        result = db.process_scan_results(scan_source="ruff", findings=[finding])
        # Second ingest should update, not create
        assert result["findings_created"] == 0
        assert result["findings_updated"] >= 1

    def test_ingest_normalizes_scan_path_separators(self, db: FiligreeDB) -> None:
        """Equivalent Windows/POSIX path forms should map to one file record."""
        first = {
            "path": r".\src\main.py",
            "rule_id": "E501",
            "severity": "low",
            "message": "Line too long",
            "line_start": 10,
        }
        second = {
            "path": "src/main.py",
            "rule_id": "E501",
            "severity": "low",
            "message": "Line too long",
            "line_start": 10,
        }
        db.process_scan_results(scan_source="ruff", findings=[first])
        result = db.process_scan_results(scan_source="ruff", findings=[second])

        assert result["files_created"] == 0
        assert result["files_updated"] == 1
        row = db.conn.execute("SELECT path FROM file_records").fetchall()
        assert len(row) == 1
        assert row[0]["path"] == "src/main.py"

    def test_ingest_with_language(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {
                    "path": "src/main.py",
                    "language": "python",
                    "rule_id": "E501",
                    "severity": "low",
                    "message": "Line too long",
                },
            ],
        )
        f = db.get_file_by_path("src/main.py")
        assert f is not None
        assert f.language == "python"

    def test_ingest_infers_file_language_when_missing(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "src/main.py", "rule_id": "E501", "severity": "low", "message": "Line too long"},
                {"path": "docs/readme.md", "rule_id": "MD001", "severity": "low", "message": "Heading"},
                {"path": "bin/tool.unknownext", "rule_id": "X1", "severity": "low", "message": "Unknown"},
                {
                    "path": "src/custom.py",
                    "language": "python3",
                    "rule_id": "C1",
                    "severity": "low",
                    "message": "Custom",
                },
            ],
        )

        py = db.get_file_by_path("src/main.py")
        md = db.get_file_by_path("docs/readme.md")
        unknown = db.get_file_by_path("bin/tool.unknownext")
        explicit = db.get_file_by_path("src/custom.py")

        assert py is not None
        assert md is not None
        assert unknown is not None
        assert explicit is not None
        assert py.language == "python"
        assert md.language == "markdown"
        assert unknown.language == ""
        assert explicit.language == "python3"

    def test_ingest_missing_language_does_not_overwrite_existing_language(self, db: FiligreeDB) -> None:
        db.register_file("src/custom.py", language="python3")

        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/custom.py", "rule_id": "E501", "severity": "low", "message": "Line too long"}],
        )

        file_record = db.get_file_by_path("src/custom.py")
        assert file_record is not None
        assert file_record.language == "python3"

    def test_ingest_unknown_severity_maps_to_info(self, db: FiligreeDB) -> None:
        """Unknown severity strings are mapped to 'info' with a warning."""
        result = db.process_scan_results(
            scan_source="ruff",
            findings=[
                {
                    "path": "src/main.py",
                    "rule_id": "E501",
                    "severity": "extreme",
                    "message": "Bad",
                },
            ],
        )
        assert result["findings_created"] == 1
        assert any("extreme" in w for w in result["warnings"])
        finding = db.conn.execute("SELECT severity FROM scan_findings").fetchone()
        assert finding["severity"] == "info"

    def test_ingest_rejects_escaping_registry_canonical_path(self, tmp_path: Path) -> None:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=FixedRegistry(
                file_id="core:file:escape",
                canonical_path="/etc/passwd",
                registry_backend="clarion",
            ),
        )
        try:
            db.initialize()

            with pytest.raises(ValueError, match="project-relative"):
                db.process_scan_results(
                    scan_source="ruff",
                    findings=[{"path": "src/input.py", "rule_id": "E1", "severity": "low", "message": "m"}],
                )

            assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0
            assert db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 0
        finally:
            db.close()

    @pytest.mark.parametrize("scan_source", ["", "   ", 123])
    def test_process_scan_results_rejects_invalid_scan_source(self, db: FiligreeDB, scan_source: Any) -> None:
        with pytest.raises(ValueError, match="scan_source must be a non-empty string"):
            db.process_scan_results(
                scan_source=scan_source,
                findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"}],
            )

        assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 0

    def test_process_scan_results_rejects_non_string_scan_run_id(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="scan_run_id must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                scan_run_id=cast(Any, 123),
                findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"}],
            )

        assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 0

    def test_ingest_empty_findings(self, db: FiligreeDB) -> None:
        result = db.process_scan_results(scan_source="ruff", findings=[])
        assert result["files_created"] == 0
        assert result["findings_created"] == 0
        assert result["new_finding_ids"] == []
        assert result["observations_created"] == 0

    def test_create_observations_promotes_finding_to_observation(self, db: FiligreeDB) -> None:
        result = db.process_scan_results(
            scan_source="codex",
            create_observations=True,
            findings=[
                {
                    "path": "src/main.py",
                    "rule_id": "logic-error",
                    "severity": "high",
                    "message": "Off-by-one in pagination loop",
                    "line_start": 42,
                },
            ],
        )
        assert result["observations_created"] == 1
        obs = db.list_observations()
        assert len(obs) == 1
        assert "[codex]" in obs[0]["summary"]
        assert obs[0]["priority"] == 1  # high -> P1

    def test_create_observations_does_not_backfill_existing(self, db: FiligreeDB) -> None:
        finding = {
            "path": "src/main.py",
            "rule_id": "logic-error",
            "severity": "high",
            "message": "Off-by-one in pagination loop",
            "line_start": 42,
        }
        db.process_scan_results(scan_source="codex", create_observations=False, findings=[finding])
        # Re-ingest — finding already exists, so no new observation
        result = db.process_scan_results(scan_source="codex", create_observations=True, findings=[finding])
        assert result["observations_created"] == 0

    def test_update_finding_marks_it_fixed(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        file_record = db.get_file_by_path("a.py")
        assert file_record is not None
        finding = db.get_findings(file_record.id)[0]

        updated = db.update_finding(finding.id, file_id=file_record.id, status="fixed")
        assert updated["status"] == "fixed"
        summary = db.get_file_findings_summary(file_record.id)
        assert summary["open_findings"] == 0

    def test_update_finding_unseen_in_latest_preserves_linked_observation(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            create_observations=True,
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        file_record = db.get_file_by_path("a.py")
        assert file_record is not None
        finding = db.get_findings(file_record.id)[0]
        assert len(db.list_observations(file_id=file_record.id)) == 1

        updated = db.update_finding(finding.id, file_id=file_record.id, status="unseen_in_latest")

        assert updated["status"] == "unseen_in_latest"
        assert len(db.list_observations(file_id=file_record.id)) == 1
        summary = db.get_file_findings_summary(file_record.id)
        assert summary["open_findings"] == 1

    def test_update_finding_links_issue_and_creates_association(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        issue = db.create_issue("Fix lint finding", type="bug")
        file_record = db.get_file_by_path("a.py")
        assert file_record is not None
        finding = db.get_findings(file_record.id)[0]

        updated = db.update_finding(finding.id, file_id=file_record.id, status="fixed", issue_id=issue.id)
        assert updated["status"] == "fixed"
        assert updated["issue_id"] == issue.id
        associations = db.get_file_associations(file_record.id)
        assert any(a["issue_id"] == issue.id and a["assoc_type"] == "bug_in" for a in associations)

    def test_update_finding_rejects_no_kwargs(self, db: FiligreeDB) -> None:
        """Calling update_finding with no status or issue_id must raise ValueError."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        finding = db.get_findings(f.id)[0]
        with pytest.raises(ValueError, match="At least one of"):
            db.update_finding(finding.id, file_id=f.id)

    def test_update_finding_rejects_invalid_status(self, db: FiligreeDB) -> None:
        """Passing an invalid status to update_finding must raise ValueError."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        finding = db.get_findings(f.id)[0]
        with pytest.raises(ValueError, match="Invalid finding status"):
            db.update_finding(finding.id, file_id=f.id, status=cast(Any, "bogus"))

    def test_ingest_finding_missing_path(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="path"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"severity": "low", "message": "No path key"}],
            )

    def test_ingest_finding_missing_rule_id(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="rule_id"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "a.py", "severity": "low", "message": "No rule_id key"}],
            )

    def test_ingest_finding_missing_message(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="message"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "a.py", "rule_id": "E1", "severity": "low"}],
            )

    def test_ingest_finding_blank_rule_id_rejected(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "a.py", "rule_id": "  ", "severity": "low", "message": "m"}],
            )

    def test_ingest_finding_blank_message_rejected(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "  "}],
            )

    def test_ingest_finding_path_dot_rejected_after_normalization(self, db: FiligreeDB) -> None:
        """Bug filigree-0911b35955: path='.' normalizes to '' and must be rejected, not inserted."""
        with pytest.raises(ValueError, match="empty after normalization"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": ".", "rule_id": "R1", "severity": "low", "message": "m"}],
            )
        assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0

    def test_ingest_finding_is_string(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="dict"):
            db.process_scan_results(scan_source="ruff", findings=["not-a-dict"])  # type: ignore[list-item]

    def test_ingest_finding_is_number(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="dict"):
            db.process_scan_results(scan_source="ruff", findings=[42])  # type: ignore[list-item]

    def test_bad_finding_at_end_does_not_persist_earlier_writes(self, db: FiligreeDB) -> None:
        """A bad finding later in the list must not leave earlier writes pending."""
        with pytest.raises(ValueError, match="severity must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "good.py", "rule_id": "E501", "severity": "low", "message": "ok"},
                    {"path": "bad.py", "rule_id": "E999", "severity": 42, "message": "bad"},
                ],
            )
        # Neither file nor finding should have been persisted
        assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 0

    def test_runtime_exception_rolls_back_pending_scan_writes(self, db: FiligreeDB) -> None:
        """Mid-batch runtime exceptions must rollback partial writes."""
        with pytest.raises(ValueError, match="suggestion must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "good.py", "rule_id": "E501", "severity": "low", "message": "ok"},
                    {
                        "path": "bad.py",
                        "rule_id": "E999",
                        "severity": "low",
                        "message": "bad",
                        "suggestion": 123,  # upfront validation catches non-string
                    },
                ],
            )

        # Force a separate successful write+commit and confirm dirty scan writes
        # were not accidentally committed.
        db.create_issue("post-error commit probe")
        assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 0

    def test_non_string_path_rejected(self, db: FiligreeDB) -> None:
        """Bug filigree-0dbe1a: non-string path must raise ValueError, not crash."""
        with pytest.raises(ValueError, match="path must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": 123, "rule_id": "E1", "severity": "low", "message": "m"}],
            )

    @pytest.mark.parametrize("path", ["../outside.py", "src/../../outside.py"])
    def test_scan_result_paths_must_be_project_relative(self, db: FiligreeDB, path: str) -> None:
        with pytest.raises(ValueError, match="project-relative"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": path, "rule_id": "E1", "severity": "low", "message": "m"}],
            )

    def test_scan_result_rejects_absolute_path(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="project-relative"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "/etc/passwd", "rule_id": "E1", "severity": "low", "message": "m"}],
            )

    def test_non_integer_line_start_rejected(self, db: FiligreeDB) -> None:
        """Bug filigree-0dbe1a: non-integer line_start must raise ValueError."""
        with pytest.raises(ValueError, match="line_start must be"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_start": "ten"},
                ],
            )

    def test_non_integer_line_end_rejected(self, db: FiligreeDB) -> None:
        """Bug filigree-0dbe1a: non-integer line_end must raise ValueError."""
        with pytest.raises(ValueError, match="line_end must be"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_end": "twenty"},
                ],
            )

    def test_bool_line_start_rejected(self, db: FiligreeDB) -> None:
        """Bug filigree-f08a57b68f: bool passes isinstance(int) so was silently stored as 1/0."""
        with pytest.raises(ValueError, match="line_start must be"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_start": True},
                ],
            )

    def test_bool_line_end_rejected(self, db: FiligreeDB) -> None:
        """Bug filigree-f08a57b68f: bool passes isinstance(int) so was silently stored as 1/0."""
        with pytest.raises(ValueError, match="line_end must be"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_end": False},
                ],
            )

    def test_negative_line_start_rejected(self, db: FiligreeDB) -> None:
        """filigree-8383bb4462: negative line_start collides with -1 dedup sentinel."""
        with pytest.raises(ValueError, match="line_start must be >= 1"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_start": -1},
                ],
            )

    def test_zero_line_start_rejected(self, db: FiligreeDB) -> None:
        """Scan finding line numbers are 1-based across public contracts."""
        with pytest.raises(ValueError, match="line_start must be >= 1"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_start": 0},
                ],
            )

    def test_negative_line_end_rejected(self, db: FiligreeDB) -> None:
        """filigree-8383bb4462: negative line_end likewise rejected for symmetry."""
        with pytest.raises(ValueError, match="line_end must be >= 1"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_end": -5},
                ],
            )

    def test_zero_line_end_rejected(self, db: FiligreeDB) -> None:
        """Scan finding line ranges must not store a zero end line."""
        with pytest.raises(ValueError, match="line_end must be >= 1"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "line_end": 0},
                ],
            )

    def test_line_end_before_line_start_rejected(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="line_end must be >= line_start"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {
                        "path": "a.py",
                        "rule_id": "E1",
                        "severity": "low",
                        "message": "m",
                        "line_start": 10,
                        "line_end": 2,
                    },
                ],
            )

    def test_out_of_range_line_start_for_existing_file_is_cleared(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Scanner evidence line numbers must not point outside the target file."""
        db.project_root = tmp_path
        (tmp_path / "target.py").write_text("one\n")

        result = db.process_scan_results(
            scan_source="codex",
            findings=[
                {
                    "path": "target.py",
                    "rule_id": "cross-file-evidence",
                    "severity": "medium",
                    "message": "Finding cites evidence from another file",
                    "line_start": 389,
                    "line_end": 391,
                },
            ],
        )

        file_record = db.get_file_by_path("target.py")
        assert file_record is not None
        finding = db.get_findings(file_record.id)[0]
        assert finding.line_start is None
        assert finding.line_end is None
        assert any("line_start 389" in warning and "target.py has 1 line" in warning for warning in result["warnings"])

    def test_non_dict_metadata_rejected(self, db: FiligreeDB) -> None:
        """filigree-ff98665ca3: list metadata must be rejected at ingest."""
        with pytest.raises(ValueError, match="metadata must be a JSON object"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "metadata": [1, 2]},
                ],
            )

    def test_non_string_suggestion_rejected(self, db: FiligreeDB) -> None:
        """Bug filigree-0dbe1a: non-string suggestion must raise ValueError."""
        with pytest.raises(ValueError, match="suggestion must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "suggestion": 42},
                ],
            )

    def test_null_suggestion_rejected(self, db: FiligreeDB) -> None:
        """filigree-e74fecddd4: explicit None suggestion must raise ValueError, not crash _upsert_finding with TypeError."""
        with pytest.raises(ValueError, match="suggestion must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "suggestion": None},
                ],
            )

    def test_non_string_language_rejected(self, db: FiligreeDB) -> None:
        """filigree-2134b23fb9: non-string language must raise ValueError, not leak sqlite errors."""
        with pytest.raises(ValueError, match="language must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "language": ["py"]},
                ],
            )
        with pytest.raises(ValueError, match="language must be a string"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[
                    {"path": "b.py", "rule_id": "E1", "severity": "low", "message": "m", "language": 42},
                ],
            )

    def test_null_language_normalized_to_empty(self, db: FiligreeDB) -> None:
        """filigree-2134b23fb9: explicit None language is treated like missing (becomes "")."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "c.py", "rule_id": "E1", "severity": "low", "message": "m", "language": None},
            ],
        )
        f = db.get_file_by_path("c.py")
        assert f is not None
        assert f.language == ""

    def test_scan_metadata_persisted_on_create(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {
                    "path": "a.py",
                    "rule_id": "E1",
                    "severity": "low",
                    "message": "m",
                    "metadata": {"url": "https://example.com", "tags": ["style"]},
                },
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        assert findings[0].metadata == {"url": "https://example.com", "tags": ["style"]}

    def test_scan_metadata_persisted_on_update(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m", "metadata": {"v": 1}},
            ],
        )
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m2", "metadata": {"v": 2}},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        assert findings[0].metadata == {"v": 2}

    def test_scan_metadata_defaults_empty_dict(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        assert findings[0].metadata == {}


class TestSeverityFallback:
    """Tests for severity normalization and fallback behavior."""

    @pytest.mark.parametrize(
        ("input_severity", "expected_severity", "expect_warning"),
        [
            ("major", "info", True),
            ("High", "high", False),
            (" low ", "low", False),
            ("", "info", True),
        ],
        ids=["unknown-maps-to-info", "normalizes-case", "strips-whitespace", "empty-string"],
    )
    def test_severity_normalization(self, db: FiligreeDB, input_severity: str, expected_severity: str, expect_warning: bool) -> None:
        result = db.process_scan_results(
            scan_source="ai",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": input_severity, "message": "m"}],
        )
        assert result["findings_created"] == 1
        if expect_warning:
            assert len(result["warnings"]) > 0
        else:
            assert result["warnings"] == []
        row = db.conn.execute("SELECT severity FROM scan_findings").fetchone()
        assert row["severity"] == expected_severity

    @pytest.mark.parametrize("bad_value", [None, 42], ids=["none", "numeric"])
    def test_severity_non_string_raises(self, db: FiligreeDB, bad_value: object) -> None:
        with pytest.raises(ValueError, match="severity must be a string"):
            db.process_scan_results(
                scan_source="ai",
                findings=[{"path": "a.py", "rule_id": "R1", "severity": bad_value, "message": "m"}],
            )


class TestScanRunId:
    """Tests for scan_run_id storage and attribution semantics."""

    def test_scan_run_id_stored_on_insert(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-001",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        row = db.conn.execute("SELECT scan_run_id FROM scan_findings").fetchone()
        assert row["scan_run_id"] == "run-001"

    def test_scan_run_id_first_attribution_wins(self, db: FiligreeDB) -> None:
        """First-attribution-wins: re-scan must NOT overwrite the original scan_run_id."""
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-001",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-002",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m2"}],
        )
        row = db.conn.execute("SELECT scan_run_id FROM scan_findings").fetchone()
        assert row["scan_run_id"] == "run-001"

    def test_scan_run_id_late_attribution(self, db: FiligreeDB) -> None:
        """Empty scan_run_id can be updated to non-empty on re-ingest."""
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-001",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m2"}],
        )
        row = db.conn.execute("SELECT scan_run_id FROM scan_findings").fetchone()
        assert row["scan_run_id"] == "run-001"

    def test_scan_run_id_empty_does_not_overwrite(self, db: FiligreeDB) -> None:
        """Re-ingesting with empty scan_run_id never clears existing attribution."""
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-001",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m2"}],
        )
        row = db.conn.execute("SELECT scan_run_id FROM scan_findings").fetchone()
        assert row["scan_run_id"] == "run-001"

    def test_findings_count_reflects_observations_not_attribution(self, db: FiligreeDB) -> None:
        """filigree-f84f141e86: scan_runs.findings_count must reflect findings observed
        by THIS run, not findings whose first-attribution scan_run_id matches.

        With first-attribution-wins on scan_findings.scan_run_id, a re-scan that only
        re-sees existing findings would otherwise report findings_count=0 even though
        it observed them.
        """
        db.create_scan_run(
            scan_run_id="run-A",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=[],
        )
        db.update_scan_run_status("run-A", "running")
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-A",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        db.create_scan_run(
            scan_run_id="run-B",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=[],
        )
        db.update_scan_run_status("run-B", "running")
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-B",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m2"}],
        )
        run_a = db.conn.execute("SELECT findings_count FROM scan_runs WHERE id='run-A'").fetchone()
        run_b = db.conn.execute("SELECT findings_count FROM scan_runs WHERE id='run-B'").fetchone()
        assert run_a["findings_count"] == 1
        assert run_b["findings_count"] == 1, f"run-B observed 1 finding but reports findings_count={run_b['findings_count']}"

    def test_existing_scan_run_rejects_mismatched_scan_source(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-source",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=[],
        )
        db.update_scan_run_status("run-source", "running")

        with pytest.raises(ValueError, match="scan_source"):
            db.process_scan_results(
                scan_source="ruff",
                scan_run_id="run-source",
                findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            )

        findings = db.conn.execute("SELECT COUNT(*) AS c FROM scan_findings").fetchone()
        run = db.get_scan_run("run-source")
        assert findings["c"] == 0
        assert run["status"] == "running"


class TestSuggestionField:
    """Tests for suggestion storage and size cap."""

    def test_suggestion_stored(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ai",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m", "suggestion": "Fix it"}],
        )
        row = db.conn.execute("SELECT suggestion FROM scan_findings").fetchone()
        assert row["suggestion"] == "Fix it"

    def test_suggestion_defaults_to_empty(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ai",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        row = db.conn.execute("SELECT suggestion FROM scan_findings").fetchone()
        assert row["suggestion"] == ""

    def test_suggestion_truncated_at_10000(self, db: FiligreeDB) -> None:
        long_suggestion = "x" * 15_000
        db.process_scan_results(
            scan_source="ai",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m", "suggestion": long_suggestion}],
        )
        row = db.conn.execute("SELECT suggestion FROM scan_findings").fetchone()
        assert len(row["suggestion"]) == 10_000 + len("\n[truncated]")
        assert row["suggestion"].endswith("\n[truncated]")

    def test_suggestion_updated_on_re_ingest(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ai",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m", "suggestion": "Fix v1"}],
        )
        db.process_scan_results(
            scan_source="ai",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m", "suggestion": "Fix v2"}],
        )
        row = db.conn.execute("SELECT suggestion FROM scan_findings").fetchone()
        assert row["suggestion"] == "Fix v2"


class TestNewFindingIds:
    """Tests for new_finding_ids in process_scan_results return."""

    def test_new_finding_ids_on_create(self, db: FiligreeDB) -> None:
        result = db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg1"},
                {"path": "a.py", "rule_id": "E502", "severity": "low", "message": "msg2"},
            ],
        )
        assert len(result["new_finding_ids"]) == 2
        assert result["findings_created"] == 2

    def test_new_finding_ids_empty_on_update(self, db: FiligreeDB) -> None:
        finding = {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}
        db.process_scan_results(scan_source="ruff", findings=[finding])
        result = db.process_scan_results(scan_source="ruff", findings=[finding])
        assert result["new_finding_ids"] == []
        assert result["findings_updated"] == 1

    def test_new_finding_ids_mixed(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "old"}],
        )
        result = db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "old"},  # update
                {"path": "a.py", "rule_id": "E502", "severity": "low", "message": "new"},  # create
            ],
        )
        assert len(result["new_finding_ids"]) == 1
        assert result["findings_created"] == 1
        assert result["findings_updated"] == 1


class TestFindingReopenOnRescan:
    """Reappearing findings must reopen if previously fixed or unseen."""

    def test_fixed_finding_reopens_on_rescan(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE file_id = ?", (f.id,))
        db.conn.commit()
        # Re-scan with the same finding
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        finding = db.get_findings(f.id)[0]
        assert finding.status == "open"

    def test_unseen_in_latest_reopens_on_rescan(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        db.conn.execute("UPDATE scan_findings SET status = 'unseen_in_latest' WHERE file_id = ?", (f.id,))
        db.conn.commit()
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        finding = db.get_findings(f.id)[0]
        assert finding.status == "open"

    def test_acknowledged_stays_acknowledged_on_rescan(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        db.conn.execute("UPDATE scan_findings SET status = 'acknowledged' WHERE file_id = ?", (f.id,))
        db.conn.commit()
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        finding = db.get_findings(f.id)[0]
        assert finding.status == "acknowledged"

    def test_false_positive_stays_false_positive_on_rescan(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        db.conn.execute("UPDATE scan_findings SET status = 'false_positive' WHERE file_id = ?", (f.id,))
        db.conn.commit()
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        finding = db.get_findings(f.id)[0]
        assert finding.status == "false_positive"


class TestMarkUnseen:
    """Tests for mark_unseen soft status behavior."""

    def test_mark_unseen_flags_missing_findings(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"},
                {"path": "a.py", "rule_id": "E502", "severity": "low", "message": "m2"},
            ],
        )
        # Second scan only includes E501 — E502 should become unseen_in_latest
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"}],
            mark_unseen=True,
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        statuses = {finding.rule_id: finding.status for finding in findings}
        assert statuses["E501"] == "open"
        assert statuses["E502"] == "unseen_in_latest"

    def test_mark_unseen_does_not_affect_other_files(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"},
                {"path": "b.py", "rule_id": "E501", "severity": "low", "message": "m1"},
            ],
        )
        # Scan only a.py — b.py findings should NOT be affected
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"}],
            mark_unseen=True,
        )
        fb = db.get_file_by_path("b.py")
        assert fb is not None
        findings_b = db.get_findings(fb.id)
        assert all(f.status == "open" for f in findings_b)

    def test_mark_unseen_does_not_affect_other_sources(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        db.process_scan_results(
            scan_source="eslint",
            findings=[{"path": "a.py", "rule_id": "no-unused-vars", "severity": "high", "message": "m"}],
        )
        # Scan ruff only — eslint findings should NOT be affected
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
            mark_unseen=True,
        )
        fa = db.get_file_by_path("a.py")
        assert fa is not None
        findings = db.get_findings(fa.id)
        eslint_finding = next(f for f in findings if f.scan_source == "eslint")
        assert eslint_finding.status == "open"

    def test_mark_unseen_preserves_fixed(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"},
                {"path": "a.py", "rule_id": "E502", "severity": "low", "message": "m2"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        # Manually mark E502 as fixed
        e502 = next(fi for fi in findings if fi.rule_id == "E502")
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (e502.id,))
        db.conn.commit()

        # Scan only E501 — E502 should stay fixed, not become unseen_in_latest
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"}],
            mark_unseen=True,
        )
        findings = db.get_findings(f.id)
        statuses = {fi.rule_id: fi.status for fi in findings}
        assert statuses["E502"] == "fixed"

    def test_mark_unseen_false_does_nothing(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"},
                {"path": "a.py", "rule_id": "E502", "severity": "low", "message": "m2"},
            ],
        )
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"}],
            mark_unseen=False,
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        assert all(fi.status == "open" for fi in findings)

    def test_last_seen_at_updated_on_rescan(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        first_last_seen = db.get_findings(f.id)[0].last_seen_at

        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        second_last_seen = db.get_findings(f.id)[0].last_seen_at
        assert second_last_seen is not None
        assert second_last_seen >= (first_last_seen or "")


class TestCleanStaleFindings:
    """Tests for the clean_stale_findings command."""

    def test_cleans_old_unseen_findings(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        finding = db.get_findings(f.id)[0]
        # Mark as unseen with an old last_seen_at
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding.id,),
        )
        db.conn.commit()

        result = db.clean_stale_findings(days=30)
        assert result["findings_fixed"] == 1
        updated = db.get_findings(f.id)[0]
        assert updated.status == "fixed"

    def test_does_not_clean_recent_unseen(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        finding = db.get_findings(f.id)[0]
        # Mark as unseen but with recent last_seen_at
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest' WHERE id = ?",
            (finding.id,),
        )
        db.conn.commit()

        result = db.clean_stale_findings(days=30)
        assert result["findings_fixed"] == 0

    def test_filters_by_scan_source(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        db.process_scan_results(
            scan_source="eslint",
            findings=[{"path": "a.py", "rule_id": "no-unused-vars", "severity": "high", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        for fi in findings:
            db.conn.execute(
                "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                (fi.id,),
            )
        db.conn.commit()

        result = db.clean_stale_findings(days=30, scan_source="ruff")
        assert result["findings_fixed"] == 1
        # eslint finding should still be unseen
        findings = db.get_findings(f.id)
        eslint = next(fi for fi in findings if fi.scan_source == "eslint")
        assert eslint.status == "unseen_in_latest"

    def test_cleans_when_last_seen_at_is_null(self, db: FiligreeDB) -> None:
        """coalesce(last_seen_at, updated_at) fallback when last_seen_at is NULL (M13)."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        finding = db.get_findings(f.id)[0]
        # Set last_seen_at to NULL and updated_at to old date
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', "
            "last_seen_at = NULL, updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding.id,),
        )
        db.conn.commit()

        result = db.clean_stale_findings(days=30)
        assert result["findings_fixed"] == 1
        updated = db.get_findings(f.id)[0]
        assert updated.status == "fixed"

    def test_null_last_seen_at_recent_updated_at_not_cleaned(self, db: FiligreeDB) -> None:
        """NULL last_seen_at with recent updated_at should NOT be cleaned."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        finding = db.get_findings(f.id)[0]
        # Set last_seen_at to NULL but keep updated_at recent
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = NULL WHERE id = ?",
            (finding.id,),
        )
        db.conn.commit()

        result = db.clean_stale_findings(days=30)
        assert result["findings_fixed"] == 0

    def test_cleans_linked_observations_for_stale_findings(self, db: FiligreeDB) -> None:
        result = db.process_scan_results(
            scan_source="ruff",
            create_observations=True,
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        finding_id = result["new_finding_ids"][0]
        observation = db.conn.execute(
            "SELECT id FROM observations WHERE source_finding_id = ?",
            (finding_id,),
        ).fetchone()
        assert observation is not None

        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()

        cleanup = db.clean_stale_findings(days=30, actor="janitor")

        live = db.conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE source_finding_id = ?",
            (finding_id,),
        ).fetchone()
        dismissed = db.conn.execute(
            "SELECT actor, reason FROM dismissed_observations WHERE obs_id = ?",
            (observation["id"],),
        ).fetchone()
        assert cleanup["findings_fixed"] == 1
        assert live["c"] == 0
        assert dismissed is not None
        assert dismissed["actor"] == "janitor"
        assert "stale" in dismissed["reason"]


class TestGetFindings:
    """Tests for retrieving findings for a file."""

    def test_get_findings_for_file(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Too long"},
                {"path": "a.py", "rule_id": "E502", "severity": "high", "message": "Bad import"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        assert len(findings) == 2

    def test_get_findings_with_severity_filter(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Low"},
                {"path": "a.py", "rule_id": "E502", "severity": "high", "message": "High"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id, severity="high")
        assert len(findings) == 1
        assert findings[0].severity == "high"

    def test_get_findings_with_status_filter(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "A"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id, status="open")
        assert len(findings) == 1
        findings = db.get_findings(f.id, status="fixed")
        assert len(findings) == 0

    def test_get_findings_pagination(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": f"E{i}", "severity": "low", "message": f"msg{i}"} for i in range(10)],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        page1 = db.get_findings(f.id, limit=5)
        assert len(page1) == 5
        page2 = db.get_findings(f.id, limit=5, offset=5)
        assert len(page2) == 5


# ---------------------------------------------------------------------------
# File association tests
# ---------------------------------------------------------------------------


class TestFileAssociations:
    """Tests for linking files to issues."""

    def test_add_association(self, db: FiligreeDB) -> None:
        f = db.register_file("src/main.py")
        issue = db.create_issue("Fix bug")
        db.add_file_association(f.id, issue.id, "bug_in")
        assocs = db.get_file_associations(f.id)
        assert len(assocs) == 1
        assert assocs[0]["issue_id"] == issue.id
        assert assocs[0]["assoc_type"] == "bug_in"

    def test_add_duplicate_association_is_idempotent(self, db: FiligreeDB) -> None:
        f = db.register_file("src/main.py")
        issue = db.create_issue("Fix bug")
        db.add_file_association(f.id, issue.id, "bug_in")
        db.add_file_association(f.id, issue.id, "bug_in")
        assocs = db.get_file_associations(f.id)
        assert len(assocs) == 1

    def test_invalid_assoc_type(self, db: FiligreeDB) -> None:
        f = db.register_file("src/main.py")
        issue = db.create_issue("Fix bug")
        with pytest.raises(ValueError, match="assoc_type"):
            db.add_file_association(f.id, issue.id, cast(Any, "invalid_type"))

    def test_multiple_association_types(self, db: FiligreeDB) -> None:
        f = db.register_file("src/main.py")
        issue = db.create_issue("Fix bug")
        db.add_file_association(f.id, issue.id, "bug_in")
        db.add_file_association(f.id, issue.id, "task_for")
        assocs = db.get_file_associations(f.id)
        assert len(assocs) == 2

    def test_nonexistent_issue_id_raises_valueerror(self, db: FiligreeDB) -> None:
        f = db.register_file("src/main.py")
        with pytest.raises(ValueError, match="Issue not found"):
            db.add_file_association(f.id, "nonexistent-issue-id", "bug_in")


# ---------------------------------------------------------------------------
# Bidirectional navigation tests (issue -> files/findings)
# ---------------------------------------------------------------------------


class TestIssueFiles:
    """Tests for getting files associated with an issue."""

    def test_get_issue_files(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Fix bug")
        f = db.register_file("src/main.py")
        db.add_file_association(f.id, issue.id, "bug_in")
        files = db.get_issue_files(issue.id)
        assert len(files) == 1
        assert files[0]["file_id"] == f.id
        assert files[0]["assoc_type"] == "bug_in"

    def test_get_issue_files_empty(self, db: FiligreeDB) -> None:
        issue = db.create_issue("No files")
        files = db.get_issue_files(issue.id)
        assert files == []

    def test_get_issue_files_includes_file_path(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Fix bug")
        f = db.register_file("src/main.py", language="python")
        db.add_file_association(f.id, issue.id, "bug_in")
        files = db.get_issue_files(issue.id)
        assert files[0]["file_path"] == "src/main.py"
        assert files[0]["file_language"] == "python"


class TestIssueFindings:
    """Tests for getting scan findings linked to an issue."""

    def test_get_issue_findings_via_association(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Fix bug")
        f = db.register_file("src/main.py")
        db.add_file_association(f.id, issue.id, "scan_finding")
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/main.py", "rule_id": "E501", "severity": "low", "message": "Too long"}],
        )
        findings = db.get_issue_findings(issue.id)
        assert len(findings) >= 1

    def test_get_issue_findings_via_direct_link(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Fix bug")
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/main.py", "rule_id": "E501", "severity": "low", "message": "Too long"}],
        )
        f = db.get_file_by_path("src/main.py")
        assert f is not None
        # Directly link a finding to the issue
        finding = db.get_findings(f.id)[0]
        db.conn.execute("UPDATE scan_findings SET issue_id = ? WHERE id = ?", (issue.id, finding.id))
        db.conn.commit()
        findings = db.get_issue_findings(issue.id)
        assert len(findings) >= 1

    def test_get_issue_findings_empty(self, db: FiligreeDB) -> None:
        issue = db.create_issue("No findings")
        findings = db.get_issue_findings(issue.id)
        assert findings == []


class TestFileDetailCore:
    """Tests for get_file_findings_summary() and get_file_detail()."""

    def test_summary_empty_file(self, db: FiligreeDB) -> None:
        f = db.register_file("empty.py")
        summary = db.get_file_findings_summary(f.id)
        assert summary["total_findings"] == 0
        assert summary["open_findings"] == 0
        for sev in ("critical", "high", "medium", "low", "info"):
            assert summary[sev] == 0

    def test_summary_counts_by_severity(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S101", "severity": "critical", "message": "Assert"},
                {"path": "a.py", "rule_id": "E501", "severity": "high", "message": "Long"},
                {"path": "a.py", "rule_id": "E302", "severity": "high", "message": "Space"},
                {"path": "a.py", "rule_id": "W291", "severity": "low", "message": "Trail"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        summary = db.get_file_findings_summary(f.id)
        assert summary["total_findings"] == 4
        assert summary["open_findings"] == 4
        assert summary["critical"] == 1
        assert summary["high"] == 2
        assert summary["low"] == 1

    def test_summary_excludes_fixed_and_false_positive(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "high", "message": "Long"},
                {"path": "a.py", "rule_id": "E302", "severity": "medium", "message": "Space"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        # Mark one as fixed
        findings = db.get_findings(f.id)
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (findings[0].id,))
        db.conn.commit()
        summary = db.get_file_findings_summary(f.id)
        assert summary["total_findings"] == 2  # total includes all
        assert summary["open_findings"] == 1  # open excludes fixed

    def test_get_file_detail_structure(self, db: FiligreeDB) -> None:
        f = db.register_file("src/main.py", language="python")
        detail = db.get_file_detail(f.id)
        assert set(detail.keys()) == {"file", "associations", "recent_findings", "summary", "observation_count"}
        assert detail["file"]["path"] == "src/main.py"
        assert detail["associations"] == []
        assert detail["recent_findings"] == []
        assert detail["summary"]["total_findings"] == 0

    def test_get_file_detail_with_data(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Fix bug")
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "src/main.py", "rule_id": "E501", "severity": "high", "message": "Long"},
            ],
        )
        f = db.get_file_by_path("src/main.py")
        assert f is not None
        db.add_file_association(f.id, issue.id, "bug_in")
        detail = db.get_file_detail(f.id)
        assert len(detail["associations"]) == 1
        assert detail["associations"][0]["issue_title"] == "Fix bug"
        assert len(detail["recent_findings"]) == 1
        assert detail["recent_findings"][0]["severity"] == "high"
        assert detail["summary"]["high"] == 1

    def test_get_file_detail_raises_for_missing(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_file_detail("nonexistent")

    def test_get_file_detail_propagates_non_table_operational_error(self, db: FiligreeDB) -> None:
        """Non-'no such table' OperationalErrors must propagate, not be swallowed."""
        import sqlite3

        db.process_scan_results(scan_source="ruff", findings=[{"path": "err.py", "rule_id": "E001", "severity": "low", "message": "x"}])
        f = db.get_file_by_path("err.py")
        assert f is not None

        real_conn = db.conn

        class _ConnProxy:
            """Proxy that raises 'database is locked' for observation queries."""

            def execute(self, sql: str, params: tuple[Any, ...] = ()) -> object:
                if "observations" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return real_conn.execute(sql, params)

            def __getattr__(self, name: str) -> object:
                return getattr(real_conn, name)

        db._conn = _ConnProxy()  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                db.get_file_detail(f.id)
        finally:
            db._conn = real_conn

    def test_recent_findings_capped_at_10(self, db: FiligreeDB) -> None:
        findings = [{"path": "big.py", "rule_id": f"E{i:03d}", "severity": "low", "message": f"Finding {i}"} for i in range(15)]
        db.process_scan_results(scan_source="ruff", findings=findings)
        f = db.get_file_by_path("big.py")
        assert f is not None
        detail = db.get_file_detail(f.id)
        assert len(detail["recent_findings"]) == 10
        assert detail["summary"]["total_findings"] == 15


class TestCreateObservationsFailure:
    """Test that observation failures don't abort finding ingestion."""

    def test_observation_failure_does_not_abort_findings(self, db: FiligreeDB) -> None:
        """When create_observations=True and observation creation fails,
        findings are still persisted (observations are best-effort)."""
        import sqlite3
        from unittest.mock import patch

        with patch.object(db, "create_observation", side_effect=sqlite3.OperationalError("DB write failed")):
            result = db.process_scan_results(
                scan_source="codex",
                create_observations=True,
                findings=[
                    {
                        "path": "src/bad.py",
                        "rule_id": "logic-error",
                        "severity": "high",
                        "message": "Off-by-one in loop",
                        "line_start": 10,
                    },
                ],
            )

        assert result["findings_created"] == 1
        assert result["observations_created"] == 0


class TestHotspots:
    """Tests for the hotspots (triage prioritization) feature."""

    def test_hotspots_empty(self, db: FiligreeDB) -> None:
        result = db.get_file_hotspots()
        assert result == []

    def test_hotspots_ranks_by_severity_score(self, db: FiligreeDB) -> None:
        # File with critical findings should rank higher
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "critical.py", "rule_id": "S001", "severity": "critical", "message": "Security"},
                {"path": "low.py", "rule_id": "E501", "severity": "low", "message": "Style"},
                {"path": "low.py", "rule_id": "E502", "severity": "low", "message": "Style 2"},
                {"path": "low.py", "rule_id": "E503", "severity": "low", "message": "Style 3"},
            ],
        )
        result = db.get_file_hotspots()
        assert len(result) == 2
        # critical=10 > 3*low=3
        assert result[0]["file"]["path"] == "critical.py"
        assert result[0]["score"] > result[1]["score"]

    def test_hotspots_with_limit(self, db: FiligreeDB) -> None:
        for i in range(10):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": f"file{i}.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
            )
        result = db.get_file_hotspots(limit=5)
        assert len(result) == 5

    def test_hotspots_includes_breakdown(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S001", "severity": "critical", "message": "Critical"},
                {"path": "a.py", "rule_id": "S002", "severity": "high", "message": "High"},
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "Low"},
            ],
        )
        result = db.get_file_hotspots()
        assert result[0]["findings_breakdown"]["critical"] == 1
        assert result[0]["findings_breakdown"]["high"] == 1
        assert result[0]["findings_breakdown"]["low"] == 1

    def test_hotspots_only_counts_open_findings(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "high", "message": "High"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        # Mark finding as fixed
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (findings[0].id,))
        db.conn.commit()
        result = db.get_file_hotspots()
        assert result == []


class TestHotspotsIncludesAcknowledgedFindings:
    """L3 bugfix: hotspots should count all non-terminal findings, not just 'open'."""

    def test_acknowledged_findings_count_in_hotspots(self, db: FiligreeDB) -> None:
        """Acknowledged findings should contribute to hotspot score."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S001", "severity": "high", "message": "Vuln"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        db.update_finding(findings[0].id, file_id=f.id, status="acknowledged")
        result = db.get_file_hotspots()
        assert len(result) == 1, "Acknowledged finding should still appear in hotspots"
        assert result[0]["score"] > 0


class TestTerminalFindingStatusesConstant:
    """L3 bugfix: single source of truth for terminal statuses."""

    def test_terminal_statuses_constant_exists(self) -> None:
        from filigree.db_files import TERMINAL_FINDING_STATUSES

        assert isinstance(TERMINAL_FINDING_STATUSES, frozenset)
        assert frozenset({"fixed", "false_positive"}) == TERMINAL_FINDING_STATUSES

    def test_open_findings_filter_uses_terminal_constant(self) -> None:
        """SQL filters should be derived from the constant, not hardcoded."""
        from filigree.db_files import TERMINAL_FINDING_STATUSES, FilesMixin

        for status in TERMINAL_FINDING_STATUSES:
            assert status in FilesMixin._OPEN_FINDINGS_FILTER

    def test_mark_unseen_preserves_false_positive(self, db: FiligreeDB) -> None:
        """false_positive findings must survive mark_unseen (not just fixed)."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"},
                {"path": "a.py", "rule_id": "E502", "severity": "low", "message": "m2"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        e502 = next(fi for fi in findings if fi.rule_id == "E502")
        db.conn.execute("UPDATE scan_findings SET status = 'false_positive' WHERE id = ?", (e502.id,))
        db.conn.commit()

        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m1"}],
            mark_unseen=True,
        )
        findings = db.get_findings(f.id)
        statuses = {fi.rule_id: fi.status for fi in findings}
        assert statuses["E502"] == "false_positive"


class TestCorruptFindingMetadata:
    """L2 bugfix: corrupt metadata should signal corruption out-of-band."""

    def test_corrupt_metadata_sets_corrupt_flag(self, db: FiligreeDB) -> None:
        """When finding metadata is corrupt JSON, the result carries the out-of-band flag."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        # Corrupt the metadata in the DB directly
        db.conn.execute(
            "UPDATE scan_findings SET metadata = '{bad json' WHERE id = ?",
            (findings[0].id,),
        )
        db.conn.commit()

        findings = db.get_findings(f.id)
        assert findings[0].metadata == {}
        assert getattr(findings[0].metadata, "_filigree_corrupt", False) is True

    def test_corrupt_file_record_metadata_does_not_crash(self, db: FiligreeDB) -> None:
        """When file_records.metadata is corrupt JSON, _build_file_record should not crash."""
        fr = db.register_file("corrupt_meta.py")
        # Corrupt the metadata in the DB directly
        db.conn.execute(
            "UPDATE file_records SET metadata = '{bad json' WHERE id = ?",
            (fr.id,),
        )
        db.conn.commit()

        result = db.get_file_by_path("corrupt_meta.py")
        assert result is not None
        assert result.metadata == {}
        assert getattr(result.metadata, "_filigree_corrupt", False) is True

    def test_corrupt_timeline_data_json_does_not_crash(self, db: FiligreeDB) -> None:
        """When timeline data_json is corrupt, get_file_timeline should not crash."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "tl.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("tl.py")
        assert f is not None
        findings = db.get_findings(f.id)
        assert findings
        # Corrupt the scan_findings metadata so the timeline CTE produces bad JSON
        db.conn.execute(
            "UPDATE scan_findings SET metadata = '{bad' WHERE id = ?",
            (findings[0].id,),
        )
        db.conn.commit()

        # Should not raise — gracefully degrade
        timeline = db.get_file_timeline(f.id)
        assert isinstance(timeline["results"], list)


class TestToDictDataWarnings:
    """Verify to_dict() surfaces _metadata_error as data_warnings."""

    def test_corrupt_file_record_to_dict_surfaces_warning(self, db: FiligreeDB) -> None:
        fr = db.register_file("warn_test.py")
        db.conn.execute(
            "UPDATE file_records SET metadata = '{bad json' WHERE id = ?",
            (fr.id,),
        )
        db.conn.commit()

        result = db.get_file_by_path("warn_test.py")
        assert result is not None
        d = result.to_dict()
        assert "_metadata_error" not in d["metadata"]
        assert len(d["data_warnings"]) == 1
        assert "corrupt" in d["data_warnings"][0].lower()

    def test_clean_file_record_to_dict_no_warnings(self, db: FiligreeDB) -> None:
        fr = db.register_file("clean_test.py")
        d = fr.to_dict()
        assert d["data_warnings"] == []

    def test_corrupt_finding_to_dict_surfaces_warning(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "w.py", "rule_id": "E1", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("w.py")
        assert f is not None
        findings = db.get_findings(f.id)
        db.conn.execute(
            "UPDATE scan_findings SET metadata = '{bad json' WHERE id = ?",
            (findings[0].id,),
        )
        db.conn.commit()

        findings = db.get_findings(f.id)
        d = findings[0].to_dict()
        assert "_metadata_error" not in d["metadata"]
        assert len(d["data_warnings"]) == 1
        assert "corrupt" in d["data_warnings"][0].lower()

    def test_clean_finding_to_dict_no_warnings(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "c.py", "rule_id": "E2", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("c.py")
        assert f is not None
        findings = db.get_findings(f.id)
        d = findings[0].to_dict()
        assert d["data_warnings"] == []


class TestMetaModuleLogger:
    """L4 bugfix: db_meta.py should use 'logger' not '_logger' for consistency."""

    def test_db_meta_uses_standard_logger_name(self) -> None:
        import filigree.db_meta as db_meta

        assert hasattr(db_meta, "logger"), "db_meta should export 'logger' like other db_* modules"


class TestSortBySeverity:
    """Tests for sort=severity on findings."""

    def test_sort_findings_by_severity(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E001", "severity": "low", "message": "Low"},
                {"path": "a.py", "rule_id": "S001", "severity": "critical", "message": "Critical"},
                {"path": "a.py", "rule_id": "E501", "severity": "medium", "message": "Medium"},
                {"path": "a.py", "rule_id": "H001", "severity": "high", "message": "High"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        results = db.get_findings(f.id, sort="severity")
        severities = [r.severity for r in results]
        assert severities == ["critical", "high", "medium", "low"]

    def test_sort_findings_by_severity_paginated(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E001", "severity": "info", "message": "Info"},
                {"path": "a.py", "rule_id": "S001", "severity": "critical", "message": "Critical"},
                {"path": "a.py", "rule_id": "H001", "severity": "high", "message": "High"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        result = db.get_findings_paginated(f.id, sort="severity")
        severities = [r["severity"] for r in result["results"]]
        assert severities == ["critical", "high", "info"]

    def test_sort_findings_invalid_raises(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E001", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        with pytest.raises(ValueError, match="Invalid sort field"):
            db.get_findings(f.id, sort="bogus")
        with pytest.raises(ValueError, match="Invalid sort field"):
            db.get_findings_paginated(f.id, sort="bogus")

    def test_sort_findings_default_is_updated_at(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E001", "severity": "low", "message": "Low"},
                {"path": "a.py", "rule_id": "S001", "severity": "critical", "message": "Critical"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        # Default sort should not be by severity
        results = db.get_findings(f.id)
        # Should not crash, just returns in updated_at order
        assert len(results) == 2


class TestMinFindingsFilter:
    """Tests for min_findings filter on list_files."""

    def test_min_findings_filters_files(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "many.py", "rule_id": f"E{i}", "severity": "low", "message": f"msg{i}"} for i in range(5)],
        )
        db.register_file("empty.py")
        result = db.list_files_paginated(min_findings=3)
        assert result["total"] == 1
        assert result["results"][0]["path"] == "many.py"

    def test_min_findings_only_counts_open(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m1"},
                {"path": "a.py", "rule_id": "E2", "severity": "low", "message": "m2"},
                {"path": "a.py", "rule_id": "E3", "severity": "low", "message": "m3"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        # Mark 2 as fixed, leaving only 1 open
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (findings[0].id,))
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (findings[1].id,))
        db.conn.commit()
        result = db.list_files_paginated(min_findings=2)
        assert result["total"] == 0

    def test_min_findings_counts_acknowledged(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m1"},
                {"path": "a.py", "rule_id": "E2", "severity": "low", "message": "m2"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        # Mark one as acknowledged — should still count as active
        db.conn.execute("UPDATE scan_findings SET status = 'acknowledged' WHERE id = ?", (findings[0].id,))
        db.conn.commit()
        result = db.list_files_paginated(min_findings=2)
        assert result["total"] == 1  # Both findings are non-terminal

    def test_min_findings_counts_unseen_in_latest(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m1"},
                {"path": "a.py", "rule_id": "E2", "severity": "low", "message": "m2"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        # Mark one as unseen_in_latest — should still count as active
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest' WHERE id = ?",
            (findings[0].id,),
        )
        db.conn.commit()
        result = db.list_files_paginated(min_findings=2)
        assert result["total"] == 1  # Both findings are non-terminal

    def test_min_findings_zero_returns_all(self, db: FiligreeDB) -> None:
        db.register_file("a.py")
        db.register_file("b.py")
        result = db.list_files_paginated(min_findings=0)
        assert result["total"] == 2


class TestHasSeverityFilter:
    """Tests for has_severity filter on list_files."""

    def test_has_severity_critical_only_returns_critical_files(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "critical.py", "rule_id": "S1", "severity": "critical", "message": "bad"},
                {"path": "lowonly.py", "rule_id": "E1", "severity": "low", "message": "minor"},
                {"path": "highonly.py", "rule_id": "H1", "severity": "high", "message": "warn"},
            ],
        )
        result = db.list_files_paginated(has_severity="critical")
        assert result["total"] == 1
        assert result["results"][0]["path"] == "critical.py"

    def test_has_severity_high_returns_high_and_not_low(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "H1", "severity": "high", "message": "warn"},
                {"path": "b.py", "rule_id": "E1", "severity": "low", "message": "minor"},
            ],
        )
        result = db.list_files_paginated(has_severity="high")
        assert result["total"] == 1
        assert result["results"][0]["path"] == "a.py"

    def test_has_severity_ignores_fixed_findings(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S1", "severity": "critical", "message": "bad"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (findings[0].id,))
        db.conn.commit()
        result = db.list_files_paginated(has_severity="critical")
        assert result["total"] == 0

    def test_has_severity_none_returns_all(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S1", "severity": "critical", "message": "bad"},
                {"path": "b.py", "rule_id": "E1", "severity": "low", "message": "minor"},
            ],
        )
        db.register_file("empty.py")
        result = db.list_files_paginated(has_severity=None)
        assert result["total"] == 3

    def test_has_severity_invalid_raises(self, db: FiligreeDB) -> None:
        db.register_file("a.py")
        with pytest.raises(ValueError, match="Invalid severity"):
            db.list_files_paginated(has_severity="bogus")


class TestListFilesScanSourceFilter:
    """list_files_paginated scan_source filter shows only files with findings from that source."""

    def test_scan_source_filters_to_matching_files(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "low", "message": "m"}],
        )
        result = db.list_files_paginated(scan_source="codex")
        assert result["total"] == 1
        assert result["results"][0]["path"] == "a.py"

    def test_scan_source_none_returns_all(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "low", "message": "m"}],
        )
        result = db.list_files_paginated(scan_source=None)
        assert result["total"] == 2

    def test_scan_source_no_match_returns_empty(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        result = db.list_files_paginated(scan_source="codex")
        assert result["total"] == 0


class TestListFilesEnrichment:
    """list_files_paginated should return summary + associations_count per file."""

    def test_list_includes_summary_with_severity_counts(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E1", "severity": "critical", "message": "m1"},
                {"path": "a.py", "rule_id": "E2", "severity": "high", "message": "m2"},
                {"path": "a.py", "rule_id": "E3", "severity": "medium", "message": "m3"},
                {"path": "b.py", "rule_id": "E4", "severity": "low", "message": "m4"},
            ],
        )
        result = db.list_files_paginated()
        by_path = {r["path"]: r for r in result["results"]}
        a = by_path["a.py"]
        assert "summary" in a
        assert a["summary"]["critical"] == 1
        assert a["summary"]["high"] == 1
        assert a["summary"]["medium"] == 1
        assert a["summary"]["low"] == 0
        assert a["summary"]["open_findings"] == 3
        b = by_path["b.py"]
        assert b["summary"]["low"] == 1
        assert b["summary"]["critical"] == 0

    def test_list_includes_associations_count(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        issue = db.create_issue("Bug in a.py")
        db.add_file_association(f.id, issue.id, "bug_in")
        result = db.list_files_paginated()
        assert result["results"][0]["associations_count"] == 1

    def test_list_summary_excludes_fixed_findings(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "E1", "severity": "critical", "message": "m1"},
                {"path": "a.py", "rule_id": "E2", "severity": "high", "message": "m2"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (findings[0].id,))
        db.conn.commit()
        result = db.list_files_paginated()
        s = result["results"][0]["summary"]
        assert s["open_findings"] == 1
        # The fixed finding should not be counted in severity buckets
        assert s["critical"] == 0
        assert s["high"] == 1

    def test_list_summary_empty_file(self, db: FiligreeDB) -> None:
        db.register_file("empty.py")
        result = db.list_files_paginated()
        s = result["results"][0]["summary"]
        assert s["open_findings"] == 0
        assert s["critical"] == 0
        assert result["results"][0]["associations_count"] == 0


class TestFileTimeline:
    """Tests for get_file_timeline() in core."""

    def test_timeline_empty_file(self, db: FiligreeDB) -> None:
        f = db.register_file("empty.py")
        result = db.get_file_timeline(f.id)
        assert result["results"] == []
        assert result["total"] == 0
        assert result["has_more"] is False

    def test_timeline_includes_finding_created(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "high", "message": "Long"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        result = db.get_file_timeline(f.id)
        assert result["total"] >= 1
        types = [e["type"] for e in result["results"]]
        assert "finding_created" in types

    def test_timeline_includes_finding_updated(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "high", "message": "Long"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        # Trigger an update by changing status
        findings = db.get_findings(f.id)
        db.conn.execute(
            "UPDATE scan_findings SET status = 'acknowledged', updated_at = '2099-01-01T00:00:00+00:00' WHERE id = ?",
            (findings[0].id,),
        )
        db.conn.commit()
        result = db.get_file_timeline(f.id)
        types = [e["type"] for e in result["results"]]
        assert "finding_updated" in types

    def test_timeline_includes_association(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py")
        issue = db.create_issue("Fix bug")
        db.add_file_association(f.id, issue.id, "bug_in")
        result = db.get_file_timeline(f.id)
        types = [e["type"] for e in result["results"]]
        assert "association_created" in types
        assoc_entry = next(e for e in result["results"] if e["type"] == "association_created")
        assert assoc_entry["data"]["issue_title"] == "Fix bug"

    def test_timeline_entries_have_deterministic_ids(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        r1 = db.get_file_timeline(f.id)
        r2 = db.get_file_timeline(f.id)
        ids1 = [e["id"] for e in r1["results"]]
        ids2 = [e["id"] for e in r2["results"]]
        assert ids1 == ids2
        # IDs should be 12-char hex strings
        for eid in ids1:
            assert len(eid) == 12
            int(eid, 16)  # must be valid hex

    def test_timeline_sorted_newest_first(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py")
        issue = db.create_issue("First")
        db.add_file_association(f.id, issue.id, "bug_in")
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
        )
        result = db.get_file_timeline(f.id)
        timestamps = [e["timestamp"] for e in result["results"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_timeline_pagination(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": f"E{i:03d}", "severity": "low", "message": f"msg{i}"} for i in range(10)],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        result = db.get_file_timeline(f.id, limit=3)
        assert len(result["results"]) == 3
        assert result["total"] == 10
        assert result["has_more"] is True

        page2 = db.get_file_timeline(f.id, limit=3, offset=3)
        assert len(page2["results"]) == 3
        # No duplicate IDs across pages
        ids1 = {e["id"] for e in result["results"]}
        ids2 = {e["id"] for e in page2["results"]}
        assert ids1.isdisjoint(ids2)

    def test_timeline_raises_for_missing_file(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_file_timeline("nonexistent")

    def test_timeline_event_type_filter(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py")
        issue = db.create_issue("Fix it")
        db.add_file_association(f.id, issue.id, "bug_in")
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"}],
        )
        # Unfiltered should have both types
        all_events = db.get_file_timeline(f.id)
        types = {e["type"] for e in all_events["results"]}
        assert "finding_created" in types
        assert "association_created" in types

        # Filter to associations only
        assoc_only = db.get_file_timeline(f.id, event_type="association")
        for e in assoc_only["results"]:
            assert e["type"] == "association_created"
        assert assoc_only["total"] < all_events["total"]

        # Filter to findings only
        findings_only = db.get_file_timeline(f.id, event_type="finding")
        for e in findings_only["results"]:
            assert e["type"].startswith("finding_")
        assert findings_only["total"] < all_events["total"]

    def test_timeline_can_include_associated_issue_events(self, db: FiligreeDB) -> None:
        f = db.register_file("issue-events.py")
        issue = db.create_issue("Timeline issue")
        db.add_file_association(f.id, issue.id, "mentioned_in")
        db.update_issue(issue.id, status="in_progress")

        default_timeline = db.get_file_timeline(f.id)
        with_issue_events = db.get_file_timeline(f.id, include_issue_events=True)
        issue_only = db.get_file_timeline(f.id, event_type="issue_event")

        assert all(e["type"] != "issue_event" for e in default_timeline["results"])
        issue_events = [e for e in with_issue_events["results"] if e["type"] == "issue_event"]
        assert issue_events
        assert issue_events[0]["data"]["issue_id"] == issue.id
        assert issue_events[0]["data"]["issue_title"] == "Timeline issue"
        assert issue_events[0]["data"]["event_type"] == "status_changed"
        assert issue_only["results"]
        assert all(e["type"] == "issue_event" for e in issue_only["results"])

    def test_timeline_invalid_event_type_raises(self, db: FiligreeDB) -> None:
        """Unknown event_type must raise ValueError, not return empty results."""
        f = db.register_file("a.py")
        with pytest.raises(ValueError, match="Invalid event_type"):
            db.get_file_timeline(f.id, event_type="bogus_type")


class TestFileMetadataEvents:
    """register_file should emit file_metadata_update events on field changes."""

    def test_metadata_event_on_language_change(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py", language="python")
        db.register_file("a.py", language="python3")
        tl = db.get_file_timeline(f.id)
        meta_events = [e for e in tl["results"] if e["type"] == "file_metadata_update"]
        assert len(meta_events) == 1
        assert meta_events[0]["data"]["field"] == "language"
        assert meta_events[0]["data"]["old_value"] == "python"
        assert meta_events[0]["data"]["new_value"] == "python3"

    def test_metadata_event_on_metadata_change(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py", metadata={"k": "v1"})
        db.register_file("a.py", metadata={"k": "v2"})
        tl = db.get_file_timeline(f.id)
        meta_events = [e for e in tl["results"] if e["type"] == "file_metadata_update"]
        assert len(meta_events) == 1
        assert meta_events[0]["data"]["field"] == "metadata"

    def test_no_event_when_no_change(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py", language="python")
        db.register_file("a.py", language="python")
        tl = db.get_file_timeline(f.id)
        meta_events = [e for e in tl["results"] if e["type"] == "file_metadata_update"]
        assert len(meta_events) == 0

    def test_no_event_when_metadata_key_order_differs(self, db: FiligreeDB) -> None:
        """JSON key ordering should not cause spurious metadata events."""
        f = db.register_file("a.py", metadata={"a": 1, "b": 2})
        db.register_file("a.py", metadata={"b": 2, "a": 1})
        tl = db.get_file_timeline(f.id)
        meta_events = [e for e in tl["results"] if e["type"] == "file_metadata_update"]
        assert len(meta_events) == 0

    def test_no_event_on_first_registration(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py", language="python")
        tl = db.get_file_timeline(f.id)
        meta_events = [e for e in tl["results"] if e["type"] == "file_metadata_update"]
        assert len(meta_events) == 0

    def test_timeline_filter_file_metadata_update(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py", language="python")
        db.register_file("a.py", language="python3")
        issue = db.create_issue("Fix it")
        db.add_file_association(f.id, issue.id, "bug_in")
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"}],
        )
        # All events
        all_tl = db.get_file_timeline(f.id)
        assert all_tl["total"] >= 3  # finding + association + metadata

        # Filter to metadata only
        meta_tl = db.get_file_timeline(f.id, event_type="file_metadata_update")
        assert meta_tl["total"] == 1
        assert all(e["type"] == "file_metadata_update" for e in meta_tl["results"])

    def test_unknown_event_type_raises(self, db: FiligreeDB) -> None:
        f = db.register_file("a.py")
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"}],
        )
        with pytest.raises(ValueError, match="Invalid event_type"):
            db.get_file_timeline(f.id, event_type="bogus_type")


class TestGlobalFindingsStats:
    """Tests for get_global_findings_stats()."""

    def test_global_stats_empty(self, db: FiligreeDB) -> None:
        stats = db.get_global_findings_stats()
        assert stats["total_findings"] == 0
        assert stats["open_findings"] == 0
        assert stats["files_with_findings"] == 0
        assert stats["critical"] == 0

    def test_global_stats_counts_all_files(self, db: FiligreeDB) -> None:
        # Create 15 files with findings — more than hotspot limit of 10
        for i in range(15):
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": f"file{i}.py", "rule_id": "E1", "severity": "low", "message": "m"}],
            )
        stats = db.get_global_findings_stats()
        assert stats["files_with_findings"] == 15
        assert stats["total_findings"] == 15
        assert stats["open_findings"] == 15
        assert stats["low"] == 15

    def test_global_stats_severity_breakdown(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S1", "severity": "critical", "message": "m"},
                {"path": "a.py", "rule_id": "S2", "severity": "high", "message": "m"},
                {"path": "b.py", "rule_id": "E1", "severity": "medium", "message": "m"},
                {"path": "c.py", "rule_id": "E2", "severity": "low", "message": "m"},
            ],
        )
        stats = db.get_global_findings_stats()
        assert stats["critical"] == 1
        assert stats["high"] == 1
        assert stats["medium"] == 1
        assert stats["low"] == 1
        assert stats["files_with_findings"] == 3

    def test_global_stats_excludes_fixed(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[
                {"path": "a.py", "rule_id": "S1", "severity": "critical", "message": "m"},
                {"path": "a.py", "rule_id": "E1", "severity": "low", "message": "m"},
            ],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        findings = db.get_findings(f.id)
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (findings[0].id,))
        db.conn.commit()
        stats = db.get_global_findings_stats()
        assert stats["open_findings"] == 1
        assert stats["critical"] == 0
        assert stats["low"] == 1


class TestPaginationMetadata:
    """Tests for paginated response format."""

    def test_list_files_returns_total(self, db: FiligreeDB) -> None:
        for i in range(10):
            db.register_file(f"file{i}.py")
        result = db.list_files_paginated(limit=3)
        assert result["total"] == 10
        assert result["limit"] == 3
        assert result["offset"] == 0
        assert result["has_more"] is True
        assert len(result["results"]) == 3

    def test_list_files_last_page(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.register_file(f"file{i}.py")
        result = db.list_files_paginated(limit=3, offset=3)
        assert result["total"] == 5
        assert result["has_more"] is False
        assert len(result["results"]) == 2

    def test_list_files_empty(self, db: FiligreeDB) -> None:
        result = db.list_files_paginated()
        assert result["total"] == 0
        assert result["has_more"] is False
        assert result["results"] == []

    def test_get_findings_returns_total(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": f"E{i}", "severity": "low", "message": f"msg{i}"} for i in range(10)],
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        result = db.get_findings_paginated(f.id, limit=5)
        assert result["total"] == 10
        assert result["has_more"] is True
        assert len(result["results"]) == 5

    def test_list_files_with_filter_and_total(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.register_file(f"src/file{i}.py", language="python")
        for i in range(3):
            db.register_file(f"lib/file{i}.js", language="javascript")
        result = db.list_files_paginated(language="python")
        assert result["total"] == 5
        assert len(result["results"]) == 5

    def test_list_files_paginated_escapes_like_wildcards(self, db: FiligreeDB) -> None:
        """path_prefix LIKE wildcards must be escaped in paginated variant too."""
        db.register_file("src/file_test.py")
        db.register_file("src/filextest.py")
        db.register_file("src/file%test.py")
        db.register_file("src/fileABCtest.py")

        result = db.list_files_paginated(path_prefix="src/file_test")
        assert result["total"] == 1
        assert result["results"][0]["path"] == "src/file_test.py"

        result = db.list_files_paginated(path_prefix="src/file%test")
        assert result["total"] == 1
        assert result["results"][0]["path"] == "src/file%test.py"

    def test_list_files_paginated_path_prefix_matches_only_prefix(self, db: FiligreeDB) -> None:
        db.register_file("src/a.py")
        db.register_file("vendor/src/b.py")

        result = db.list_files_paginated(path_prefix="src/")

        assert result["total"] == 1
        assert result["results"][0]["path"] == "src/a.py"

    def test_list_files_paginated_invalid_sort_raises(self, db: FiligreeDB) -> None:
        """Invalid sort parameter must raise ValueError, not silently fall back."""
        with pytest.raises(ValueError, match="Invalid sort"):
            db.list_files_paginated(sort="bogus")

    def test_list_files_paginated_invalid_has_severity_raises(self, db: FiligreeDB) -> None:
        """Invalid has_severity must raise ValueError, not silently skip the filter."""
        db.register_file("a.py")
        with pytest.raises(ValueError, match="Invalid severity"):
            db.list_files_paginated(has_severity="ultra_critical")

    def test_list_files_paginated_invalid_direction_raises(self, db: FiligreeDB) -> None:
        """filigree-e53ce98110: invalid direction must raise ValueError, not silently fall back to default order."""
        db.register_file("a.py")
        with pytest.raises(ValueError, match="Invalid direction"):
            db.list_files_paginated(direction="SIDEWAYS")

    def test_list_files_paginated_valid_directions_accepted(self, db: FiligreeDB) -> None:
        """ASC/DESC/asc/desc must all be accepted for direction."""
        db.register_file("a.py")
        for d in ("ASC", "DESC", "asc", "desc"):
            db.list_files_paginated(direction=d)
        # None still means "use default"
        db.list_files_paginated(direction=None)


# ---------------------------------------------------------------------------
# _normalize_scan_path edge cases (filigree-7bff85)
# ---------------------------------------------------------------------------


class TestNormalizeScanPath:
    """Direct unit tests for _normalize_scan_path edge cases."""

    def test_empty_string(self) -> None:
        assert _normalize_scan_path("") == ""

    def test_dot_path(self) -> None:
        assert _normalize_scan_path(".") == ""

    def test_trailing_slash(self) -> None:
        assert _normalize_scan_path("src/main.py/") == "src/main.py"

    def test_double_slash(self) -> None:
        result = _normalize_scan_path("src//main.py")
        assert result == "src/main.py"

    def test_backslash_path(self) -> None:
        assert _normalize_scan_path(r"src\main.py") == "src/main.py"

    def test_dot_backslash_prefix(self) -> None:
        assert _normalize_scan_path(r".\src\main.py") == "src/main.py"

    def test_parent_traversal(self) -> None:
        assert _normalize_scan_path("src/../main.py") == "main.py"

    def test_absolute_path_keeps_leading_slash_for_validation(self) -> None:
        assert _normalize_scan_path("/etc/passwd") == "/etc/passwd"

    def test_normal_path_unchanged(self) -> None:
        assert _normalize_scan_path("src/main.py") == "src/main.py"


# ---------------------------------------------------------------------------
# get_scan_runs core-level tests (filigree-694a75)
# ---------------------------------------------------------------------------


class TestGetScanRunsCore:
    """Core-level unit tests for FiligreeDB.get_scan_runs()."""

    def test_empty_table(self, db: FiligreeDB) -> None:
        assert db.get_scan_runs() == []

    def test_excludes_empty_scan_run_id(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="ruff",
            scan_run_id="",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        assert db.get_scan_runs() == []

    def test_single_run(self, db: FiligreeDB) -> None:
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-1",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        runs = db.get_scan_runs()
        assert len(runs) == 1
        assert runs[0]["scan_run_id"] == "run-1"
        assert runs[0]["scan_source"] == "codex"
        assert runs[0]["total_findings"] == 1
        assert runs[0]["files_scanned"] == 1

    def test_multi_file_scan_run(self, db: FiligreeDB) -> None:
        """files_scanned should count distinct files, not total findings."""
        db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-multi",
            findings=[
                {"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"},
                {"path": "a.py", "rule_id": "R2", "severity": "high", "message": "n"},
                {"path": "b.py", "rule_id": "R1", "severity": "low", "message": "m"},
            ],
        )
        runs = db.get_scan_runs()
        assert len(runs) == 1
        assert runs[0]["total_findings"] == 3
        assert runs[0]["files_scanned"] == 2

    def test_limit_parameter(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.process_scan_results(
                scan_source="ruff",
                scan_run_id=f"run-{i}",
                findings=[{"path": f"f{i}.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            )
        runs = db.get_scan_runs(limit=2)
        assert len(runs) == 2

    def test_clean_run_with_zero_findings_appears_in_history(self, db: FiligreeDB) -> None:
        """filigree-ff340d965f: a completed clean scan run must show in history.

        Pre-fix, get_scan_runs() derived history from scan_findings only, so a
        run that completed with zero findings was invisible despite the
        scan_runs row existing.
        """
        db.create_scan_run(
            scan_run_id="clean-run",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("clean-run", "running")
        db.process_scan_results(
            scan_source="codex",
            findings=[],
            scan_run_id="clean-run",
            complete_scan_run=True,
        )
        runs = db.get_scan_runs()
        assert len(runs) == 1
        assert runs[0]["scan_run_id"] == "clean-run"
        assert runs[0]["total_findings"] == 0
        assert runs[0]["files_scanned"] == 0
        assert runs[0]["completed_at"]  # non-empty


class TestCreateObservationsPartialFailure:
    """Test that create_observations=True handles mid-batch failures correctly."""

    def test_create_observations_succeeds_for_two_findings(self, db: FiligreeDB) -> None:
        """Baseline: create_observations=True for multiple findings creates all observations."""
        result = db.process_scan_results(
            scan_source="codex",
            create_observations=True,
            findings=[
                {
                    "path": "src/a.py",
                    "rule_id": "logic-error-1",
                    "severity": "high",
                    "message": "Off by one",
                    "line_start": 10,
                },
                {
                    "path": "src/b.py",
                    "rule_id": "logic-error-2",
                    "severity": "critical",
                    "message": "Null deref",
                    "line_start": 20,
                },
            ],
        )
        assert result["observations_created"] == 2
        assert result["findings_created"] == 2


# ---------------------------------------------------------------------------
# _safe_json_loads edge cases
# ---------------------------------------------------------------------------


class TestSafeJsonLoads:
    """filigree-ef3925404b, filigree-769a192252, filigree-7ea6b80f3b: _safe_json_loads edge cases.

    ``_safe_json_loads`` returns a ``_ParsedJson`` (dict subclass) carrying an
    out-of-band ``_filigree_corrupt`` flag instead of an in-band sentinel key.
    These assertions cover both the parsed payload and the flag.
    """

    def test_none_input_returns_empty_dict(self) -> None:
        result = _safe_json_loads(None, "test")
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is False

    def test_empty_string_returns_empty_dict(self) -> None:
        result = _safe_json_loads("", "test")
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is False

    def test_non_dict_json_array_surfaces_corrupt_flag(self) -> None:
        result = _safe_json_loads("[1,2,3]", "test")
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is True

    def test_non_dict_json_scalar_surfaces_corrupt_flag(self) -> None:
        result = _safe_json_loads("42", "test")
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is True

    def test_valid_dict_returns_parsed(self) -> None:
        result = _safe_json_loads('{"key": "value", "num": 42}', "test")
        assert result == {"key": "value", "num": 42}
        assert getattr(result, "_filigree_corrupt", False) is False

    def test_invalid_json_returns_corrupt_flag(self) -> None:
        result = _safe_json_loads("{not valid json}", "test")
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is True

    def test_undecodable_bytes_become_corrupt_flag(self) -> None:
        """Bug 2 (filigree-7ea6b80f3b): UnicodeDecodeError must not leak.

        SQLite-flexible-typed JSON columns can return ``bytes`` for BLOB
        rows. Invalid-UTF-8 input previously raised ``UnicodeDecodeError``
        past the ``(JSONDecodeError, TypeError)`` arm; the contract says
        all corruption should surface as the flag.
        """
        result = _safe_json_loads(b"\xff", "test")
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is True

    def test_user_field_named_like_legacy_sentinel_round_trips(self, db: FiligreeDB) -> None:
        """Bug 1 (filigree-7ea6b80f3b): a custom field named ``_fields_error`` must NOT be stripped."""
        issue = db.create_issue("repro", type="bug", fields={"_fields_error": True, "keep": "value"})
        out = db.get_issue(issue.id).to_dict()
        assert out["fields"] == {"_fields_error": True, "keep": "value"}
        assert out["data_warnings"] == []

    def test_user_metadata_named_like_legacy_sentinel_round_trips(self, db: FiligreeDB) -> None:
        """Bug 1 (filigree-7ea6b80f3b): file metadata key ``_metadata_error`` must NOT be stripped."""
        fr = db.register_file("user_meta.py", metadata={"_metadata_error": True, "owner": "bot"})
        out = db.get_file(fr.id)
        assert out is not None
        d = out.to_dict()
        assert d["metadata"] == {"_metadata_error": True, "owner": "bot"}
        assert d["data_warnings"] == []

    def test_non_dict_file_metadata_surfaces_data_warning(self, db: FiligreeDB) -> None:
        """End-to-end: array JSON in file_records.metadata must yield a data_warning."""
        fr = db.register_file("array_meta.py")
        db.conn.execute(
            "UPDATE file_records SET metadata = '[1,2,3]' WHERE id = ?",
            (fr.id,),
        )
        db.conn.commit()

        result = db.get_file_by_path("array_meta.py")
        assert result is not None
        d = result.to_dict()
        assert "_metadata_error" not in d["metadata"]
        assert len(d["data_warnings"]) == 1
        assert "corrupt" in d["data_warnings"][0].lower()

    def test_blob_metadata_surfaces_data_warning(self, db: FiligreeDB) -> None:
        """End-to-end (Bug 2): BLOB-typed garbage in metadata must become a data_warning, not raise."""
        fr = db.register_file("blob_meta.py")
        db.conn.execute(
            "UPDATE file_records SET metadata = ? WHERE id = ?",
            (sqlite3.Binary(b"\xff\xfe\xfd"), fr.id),
        )
        db.conn.commit()

        result = db.get_file_by_path("blob_meta.py")
        assert result is not None
        d = result.to_dict()
        assert d["metadata"] == {}
        assert len(d["data_warnings"]) == 1
        assert "corrupt" in d["data_warnings"][0].lower()


# ---------------------------------------------------------------------------
# ScanFinding.__post_init__ validation
# ---------------------------------------------------------------------------


class TestScanFindingPostInit:
    """filigree-bd79437073: ScanFinding.__post_init__ validates severity and status."""

    def test_invalid_severity_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid severity"):
            ScanFinding(id="x", file_id="y", severity="banana")  # type: ignore[arg-type]

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid finding status"):
            ScanFinding(id="x", file_id="y", status="banana")  # type: ignore[arg-type]

    def test_valid_severity_accepted(self) -> None:
        for sev in ("critical", "high", "medium", "low", "info"):
            f = ScanFinding(id="x", file_id="y", severity=sev)
            assert f.severity == sev

    def test_valid_status_accepted(self) -> None:
        for status in ("open", "acknowledged", "fixed", "false_positive", "unseen_in_latest"):
            f = ScanFinding(id="x", file_id="y", status=status)
            assert f.status == status


class TestProcessScanResultsCreateObservations:
    def test_creates_observations_for_new_findings(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py")
        result = db.process_scan_results(
            scan_source="test",
            findings=[
                {"path": "src/main.py", "rule_id": "r1", "severity": "high", "message": "Bug found", "line_start": 10},
            ],
            create_observations=True,
        )
        assert result["findings_created"] == 1
        obs = db.list_observations()
        assert len(obs) == 1
        assert "[test]" in obs[0]["summary"]
        assert obs[0]["priority"] == 1  # high -> P1

    def test_no_observations_when_flag_false(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py")
        db.process_scan_results(
            scan_source="test",
            findings=[
                {"path": "src/main.py", "rule_id": "r1", "severity": "high", "message": "Bug"},
            ],
            create_observations=False,
        )
        obs = db.list_observations()
        assert len(obs) == 0
