"""Core tests for shared file annotations."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from filigree.db_schema import CURRENT_SCHEMA_VERSION, SCHEMA_SQL
from filigree.migrations import apply_pending_migrations
from tests._db_factory import make_db


def _project_db(tmp_path: Path) -> FiligreeDB:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = make_db(tmp_path)
    db.project_root = tmp_path
    return db


class TestAnnotationSchema:
    def test_current_schema_creates_annotation_tables(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "schema.db")
        try:
            conn.executescript(SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

            assert CURRENT_SCHEMA_VERSION >= 10
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            }
            assert {
                "annotations",
                "annotation_provenance",
                "annotation_links",
                "annotation_events",
                "annotation_closeout_acknowledgements",
            }.issubset(tables)
        finally:
            conn.close()

    def test_v9_to_v10_migration_creates_annotation_tables(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "migration.db")
        try:
            conn.executescript(SCHEMA_SQL)
            for table in (
                "annotation_closeout_acknowledgements",
                "annotation_events",
                "annotation_links",
                "annotation_provenance",
                "annotations",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("PRAGMA user_version = 9")
            conn.commit()

            applied = apply_pending_migrations(conn, 10)

            assert applied == 1
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
            assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'annotations'").fetchone() is not None
        finally:
            conn.close()


class TestAnnotationCrud:
    def test_annotate_file_records_file_anchor_provenance_and_link(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            source = tmp_path / "src" / "example.py"
            source.parent.mkdir()
            source.write_text("one\nsecret_token = 'abc123'\nthree\n")
            issue = db.create_issue("Read me")

            annotation = db.annotate_file(
                "src/example.py",
                "Token assignment is intentionally redacted in provenance.",
                line_start=2,
                intent="warning",
                critical=True,
                links=[{"target_type": "issue", "target_id": issue.id, "relationship": "must_consider"}],
                actor="tester",
            )

            assert annotation["annotation_id"].startswith("test-ann-")
            assert annotation["file_path"] == "src/example.py"
            assert annotation["line_start"] == 2
            assert annotation["line_end"] == 2
            assert annotation["anchor_state"] == "current"
            assert annotation["critical"] is True
            assert annotation["links"][0]["annotation_link_id"].startswith("test-annlink-")
            assert annotation["links"][0]["target_id"] == issue.id
            assert "id" not in annotation
            provenance = annotation["provenance"]
            assert provenance["file_checksum"]
            assert provenance["file_size"] == source.stat().st_size
            assert "secret_token" not in provenance.get("file_diff", "")
        finally:
            db.close()

    def test_line_ranges_are_one_based_and_ordered(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            path = tmp_path / "a.py"
            path.write_text("print('hi')\n")
            with pytest.raises(ValueError, match="line_start must be >= 1"):
                db.annotate_file("a.py", "bad", line_start=0)
            with pytest.raises(ValueError, match="line_end must be >= line_start"):
                db.annotate_file("a.py", "bad", line_start=2, line_end=1)
        finally:
            db.close()

    def test_annotate_file_rejects_absolute_path(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            with pytest.raises(ValueError, match="project-relative"):
                db.annotate_file("/etc/passwd", "bad path")
        finally:
            db.close()

    def test_anchor_drift_is_computed_on_read(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            source = tmp_path / "src" / "drift.py"
            source.parent.mkdir()
            source.write_text("alpha\nbeta\ngamma\n")
            annotation = db.annotate_file("src/drift.py", "Watch beta", line_start=2)

            source.write_text("intro\nalpha\nbeta\ngamma\n")
            drifted = db.get_annotation(annotation["annotation_id"])

            assert drifted["anchor_state"] == "line_drifted"
            assert drifted["current_line_start"] == 3
            assert drifted["current_line_end"] == 3
        finally:
            db.close()

    def test_stored_annotation_path_escape_is_treated_as_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("outside anchor\n")
        db = _project_db(project)
        try:
            now = "2026-01-01T00:00:00+00:00"
            db.conn.execute(
                "INSERT INTO annotations "
                "(id, file_path, line_start, line_end, anchor_snippet, note, intent, critical, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'breadcrumb', 0, 'active', ?, ?)",
                ("test-ann-escape", "../outside.py", 1, 1, "outside anchor\n", "imported bad path", now, now),
            )
            db.conn.commit()

            annotation = db.get_annotation("test-ann-escape")

            assert annotation["anchor_state"] == "file_missing"
            assert annotation["current_line_start"] is None
            assert annotation["current_line_end"] is None
        finally:
            db.close()

    def test_anchor_drift_counts_final_line_without_trailing_newline(self, tmp_path: Path) -> None:
        """A two-line snippet without a final newline still spans two lines after drift."""
        db = _project_db(tmp_path)
        try:
            source = tmp_path / "multi.py"
            source.write_text("aa\nbb")
            annotation = db.annotate_file("multi.py", "Watch two final lines", line_start=1, line_end=2)

            source.write_text("intro\naa\nbb")
            drifted = db.get_annotation(annotation["annotation_id"])

            assert drifted["anchor_state"] == "line_drifted"
            assert drifted["current_line_start"] == 2
            assert drifted["current_line_end"] == 3
        finally:
            db.close()

    def test_list_annotations_filters_by_relationship_without_target(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "must.py").write_text("x = 1\n")
            (tmp_path / "relevant.py").write_text("x = 2\n")
            (tmp_path / "plain.py").write_text("x = 3\n")
            issue = db.create_issue("Linked")
            must_consider = db.annotate_file(
                "must.py",
                "Must consider this.",
                links=[{"target_type": "issue", "target_id": issue.id, "relationship": "must_consider"}],
            )
            db.annotate_file(
                "relevant.py",
                "Relevant but not required.",
                links=[{"target_type": "issue", "target_id": issue.id, "relationship": "relevant_to"}],
            )
            db.annotate_file("plain.py", "No relationship link.")

            result = db.list_annotations(relationship="must_consider")

            assert [item["annotation_id"] for item in result["items"]] == [must_consider["annotation_id"]]
            assert result["has_more"] is False
        finally:
            db.close()

    def test_annotate_file_rejects_line_end_beyond_eof(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "short.py").write_text("one\n")

            with pytest.raises(ValueError, match=r"line_end exceeds file length"):
                db.annotate_file("short.py", "impossible range", line_start=1, line_end=99)
        finally:
            db.close()

    def test_failed_annotation_range_does_not_register_file(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "short.py").write_text("one\n")

            with pytest.raises(ValueError, match=r"line_start exceeds file length"):
                db.annotate_file("short.py", "bad range", line_start=3)

            paths = [row["path"] for row in db.conn.execute("SELECT path FROM file_records").fetchall()]
            assert "short.py" not in paths
        finally:
            db.close()

    def test_carry_forward_acknowledges_old_critical_warning(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "src.py").write_text("x = 1\n")
            old_issue = db.create_issue("Old")
            new_issue = db.create_issue("New")
            annotation = db.annotate_file(
                "src.py",
                "Must survive closeout.",
                line_start=1,
                critical=True,
                links=[{"target_type": "issue", "target_id": old_issue.id, "relationship": "must_consider"}],
            )

            warnings = db.get_annotation_closeout_warnings(old_issue.id)
            assert [w["annotation_id"] for w in warnings] == [annotation["annotation_id"]]

            result = db.carry_forward_annotation(
                annotation["annotation_id"],
                from_target_id=old_issue.id,
                to_target_id=new_issue.id,
                reason="Still relevant",
                actor="tester",
            )

            assert result["annotation"]["status"] == "active"
            assert result["link"]["target_id"] == new_issue.id
            assert db.get_annotation_closeout_warnings(old_issue.id) == []
            assert [w["annotation_id"] for w in db.get_annotation_closeout_warnings(new_issue.id)] == [annotation["annotation_id"]]
        finally:
            db.close()

    def test_carry_forward_requires_active_source_link(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "src.py").write_text("x = 1\n")
            linked_issue = db.create_issue("Linked")
            unrelated_issue = db.create_issue("Unrelated")
            new_issue = db.create_issue("New")
            annotation = db.annotate_file(
                "src.py",
                "Must survive closeout.",
                line_start=1,
                critical=True,
                links=[{"target_type": "issue", "target_id": linked_issue.id, "relationship": "must_consider"}],
            )

            with pytest.raises(ValueError, match="not actively linked"):
                db.carry_forward_annotation(
                    annotation["annotation_id"],
                    from_target_id=unrelated_issue.id,
                    to_target_id=new_issue.id,
                    reason="Still relevant",
                    actor="tester",
                )

            assert [w["annotation_id"] for w in db.get_annotation_closeout_warnings(linked_issue.id)] == [annotation["annotation_id"]]
            assert db.get_annotation_closeout_warnings(unrelated_issue.id) == []
            assert db.get_annotation_closeout_warnings(new_issue.id) == []
        finally:
            db.close()

    def test_promote_annotation_keeps_active_by_default_and_links_target(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "src.py").write_text("x = 1\n")
            annotation = db.annotate_file("src.py", "This should become work.", line_start=1)

            result = db.promote_annotation(
                annotation["annotation_id"],
                target_type="issue",
                title="Follow up on annotation",
                reason="Actionable",
                actor="tester",
            )

            promoted = db.get_annotation(annotation["annotation_id"])
            assert promoted["status"] == "active"
            assert result["target_type"] == "issue"
            assert result["target_id"].startswith("test-")
            assert any(link["relationship"] == "promoted_to" for link in promoted["links"])
        finally:
            db.close()

    def test_promote_annotation_rolls_back_issue_target_when_audit_event_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "src.py").write_text("x = 1\n")
            annotation = db.annotate_file("src.py", "This should become issue work.", line_start=1)
            before_issue_ids = {row["id"] for row in db.conn.execute("SELECT id FROM issues").fetchall()}
            original_event = db._record_annotation_event

            def fail_promote_event(annotation_id: str, event_type: str, **kwargs: object) -> dict[str, object]:
                if event_type == "promoted":
                    raise sqlite3.OperationalError("simulated annotation event failure")
                return original_event(annotation_id, event_type, **kwargs)

            monkeypatch.setattr(db, "_record_annotation_event", fail_promote_event)

            with pytest.raises(sqlite3.OperationalError, match="simulated annotation event failure"):
                db.promote_annotation(annotation["annotation_id"], target_type="issue", title="Promoted target")

            after_issue_ids = {row["id"] for row in db.conn.execute("SELECT id FROM issues").fetchall()}
            promoted_links = db.conn.execute(
                "SELECT COUNT(*) FROM annotation_links WHERE annotation_id = ? AND relationship = 'promoted_to'",
                (annotation["annotation_id"],),
            ).fetchone()[0]
            assert after_issue_ids == before_issue_ids
            assert promoted_links == 0
        finally:
            db.close()

    def test_promote_annotation_rolls_back_observation_target_when_audit_event_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "src.py").write_text("x = 1\n")
            annotation = db.annotate_file("src.py", "This should become observation work.", line_start=1)
            before_observation_ids = {row["id"] for row in db.conn.execute("SELECT id FROM observations").fetchall()}
            original_event = db._record_annotation_event

            def fail_promote_event(annotation_id: str, event_type: str, **kwargs: object) -> dict[str, object]:
                if event_type == "promoted":
                    raise sqlite3.OperationalError("simulated annotation event failure")
                return original_event(annotation_id, event_type, **kwargs)

            monkeypatch.setattr(db, "_record_annotation_event", fail_promote_event)

            with pytest.raises(sqlite3.OperationalError, match="simulated annotation event failure"):
                db.promote_annotation(annotation["annotation_id"], target_type="observation", title="Promoted target")

            after_observation_ids = {row["id"] for row in db.conn.execute("SELECT id FROM observations").fetchall()}
            promoted_links = db.conn.execute(
                "SELECT COUNT(*) FROM annotation_links WHERE annotation_id = ? AND relationship = 'promoted_to'",
                (annotation["annotation_id"],),
            ).fetchone()[0]
            assert after_observation_ids == before_observation_ids
            assert promoted_links == 0
        finally:
            db.close()

    def test_provenance_flags_redaction_generated_binary_and_file_delete(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True)
        generated = tmp_path / "docs" / "bugs" / "generated" / "report.py"
        generated.parent.mkdir(parents=True)
        generated.write_text("safe = 1\n")
        subprocess.run(["git", "add", "docs/bugs/generated/report.py"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, check=True, capture_output=True)
        generated.write_text("safe = 1\napi_key = 'secret'\n")
        binary = tmp_path / "asset.bin"
        binary.write_bytes(b"\x00\x01\x02")

        db = _project_db(tmp_path)
        try:
            ann = db.annotate_file("docs/bugs/generated/report.py", "Generated dirty file", line_start=2)
            flags = set(ann["provenance"]["provenance_flags"])
            assert {"generated_file", "dirty_worktree", "redacted"}.issubset(flags)
            assert "secret" not in ann["provenance"]["file_diff"]
            assert "api_key" not in ann["provenance"]["file_diff"]

            binary_ann = db.annotate_file("asset.bin", "Binary context")
            assert "binary_file" in binary_ann["provenance"]["provenance_flags"]

            file_id = ann["file_id"]
            db.delete_file_record(file_id)
            assert db.get_annotation(ann["annotation_id"])["file_id"] is None
        finally:
            db.close()

    def test_jsonl_round_trip_includes_annotations_and_rejects_foreign_issue_links(self, tmp_path: Path) -> None:
        db = _project_db(tmp_path)
        try:
            (tmp_path / "src.py").write_text("x = 1\n")
            issue = db.create_issue("Linked")
            annotation = db.annotate_file(
                "src.py",
                "Round trip me",
                line_start=1,
                links=[{"target_type": "issue", "target_id": issue.id, "relationship": "relevant_to"}],
            )
            out = tmp_path / "annotations.jsonl"
            db.export_jsonl(out)
        finally:
            db.close()

        fresh = _project_db(tmp_path / "fresh")
        try:
            result = fresh.import_jsonl(out, merge=True)
            assert result["count"] > 0
            assert fresh.get_annotation(annotation["annotation_id"])["note"] == "Round trip me"
        finally:
            fresh.close()

        bad = tmp_path / "foreign.jsonl"
        records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        for record in records:
            if record.get("_type") == "annotation_link" and record.get("target_type") == "issue":
                record["target_id"] = "foreign-aaaaaaaaaa"
        bad.write_text("\n".join(json.dumps(record) for record in records) + "\n")

        reject = _project_db(tmp_path / "reject")
        try:
            with pytest.raises(ValueError, match="foreign project"):
                reject.import_jsonl(bad, merge=True)
        finally:
            reject.close()

    def test_jsonl_import_rejects_missing_annotation_link_targets(self, tmp_path: Path) -> None:
        imported = tmp_path / "dangling-annotation-link.jsonl"
        now = "2026-01-01T00:00:00+00:00"
        records = [
            {
                "_type": "annotation",
                "id": "test-ann-dangling",
                "file_path": "src.py",
                "note": "imported annotation",
                "created_at": now,
                "updated_at": now,
            },
            {
                "_type": "annotation_link",
                "id": "test-annlink-dangling",
                "annotation_id": "test-ann-dangling",
                "target_type": "issue",
                "target_id": "test-missingissue",
                "relationship": "must_consider",
                "created_at": now,
            },
        ]
        imported.write_text("\n".join(json.dumps(record) for record in records) + "\n")

        db = _project_db(tmp_path / "import-reject")
        try:
            with pytest.raises(ValueError, match=r"annotation_link.*missing issue target"):
                db.import_jsonl(imported, merge=True)
            assert db.conn.execute("SELECT COUNT(*) FROM annotations WHERE id = 'test-ann-dangling'").fetchone()[0] == 0
            assert db.conn.execute("SELECT COUNT(*) FROM annotation_links WHERE id = 'test-annlink-dangling'").fetchone()[0] == 0
        finally:
            db.close()

    def test_jsonl_import_rejects_missing_closeout_ack_issue_targets(self, tmp_path: Path) -> None:
        imported = tmp_path / "dangling-closeout-ack.jsonl"
        now = "2026-01-01T00:00:00+00:00"
        records = [
            {
                "_type": "annotation",
                "id": "test-ann-ack-dangling",
                "file_path": "src.py",
                "note": "imported annotation",
                "created_at": now,
                "updated_at": now,
            },
            {
                "_type": "annotation_closeout_acknowledgement",
                "annotation_id": "test-ann-ack-dangling",
                "target_id": "test-missingissue",
                "carried_to_target_id": "test-othermissing",
                "actor": "import",
                "reason": "carry it",
                "acknowledged_at": now,
            },
        ]
        imported.write_text("\n".join(json.dumps(record) for record in records) + "\n")

        db = _project_db(tmp_path / "closeout-import-reject")
        try:
            with pytest.raises(ValueError, match=r"closeout.*missing issue target"):
                db.import_jsonl(imported, merge=True)
            assert db.conn.execute("SELECT COUNT(*) FROM annotations WHERE id = 'test-ann-ack-dangling'").fetchone()[0] == 0
            assert (
                db.conn.execute(
                    "SELECT COUNT(*) FROM annotation_closeout_acknowledgements WHERE annotation_id = 'test-ann-ack-dangling'"
                ).fetchone()[0]
                == 0
            )
        finally:
            db.close()
