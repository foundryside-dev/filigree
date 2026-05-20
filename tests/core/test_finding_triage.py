"""Tests for finding triage DB methods."""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


def _seed_findings(db: FiligreeDB) -> dict[str, str]:
    """Create a file with 3 findings and return {name: finding_id}."""
    db.register_file("src/main.py", language="python")
    result = db.process_scan_results(
        scan_source="test-scanner",
        findings=[
            {"path": "src/main.py", "rule_id": "logic-error", "severity": "high", "message": "Off by one"},
            {"path": "src/main.py", "rule_id": "type-error", "severity": "medium", "message": "Wrong return type", "line_start": 42},
            {"path": "src/main.py", "rule_id": "injection", "severity": "critical", "message": "SQL injection", "line_start": 100},
        ],
    )
    ids = result["new_finding_ids"]
    return {"obo": ids[0], "type": ids[1], "sqli": ids[2]}


class TestGetFinding:
    def test_get_by_id(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        finding = db.get_finding(ids["obo"])
        assert finding["rule_id"] == "logic-error"
        assert finding["severity"] == "high"

    def test_not_found_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_finding("no-such-id")


class TestListFindingsGlobal:
    def test_returns_all_findings(self, db: FiligreeDB) -> None:
        _seed_findings(db)
        result = db.list_findings_global()
        assert len(result["findings"]) == 3

    def test_filter_by_severity(self, db: FiligreeDB) -> None:
        _seed_findings(db)
        result = db.list_findings_global(severity="critical")
        assert len(result["findings"]) == 1
        assert result["findings"][0]["rule_id"] == "injection"

    def test_filter_by_status(self, db: FiligreeDB) -> None:
        _seed_findings(db)
        result = db.list_findings_global(status="open")
        assert len(result["findings"]) == 3

    def test_filter_by_scan_run_id(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py")
        db.process_scan_results(
            scan_source="s1",
            scan_run_id="run-1",
            findings=[{"path": "src/main.py", "rule_id": "r1", "severity": "info", "message": "m1"}],
        )
        db.process_scan_results(
            scan_source="s1",
            scan_run_id="run-2",
            findings=[{"path": "src/main.py", "rule_id": "r2", "severity": "info", "message": "m2"}],
        )
        result = db.list_findings_global(scan_run_id="run-2")
        assert len(result["findings"]) == 1
        assert result["findings"][0]["rule_id"] == "r2"

    def test_filter_by_issue_id(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        issue = db.create_issue("Test bug", type="bug")
        db.update_finding(ids["sqli"], issue_id=issue.id)
        result = db.list_findings_global(issue_id=issue.id)
        assert len(result["findings"]) == 1

    def test_pagination(self, db: FiligreeDB) -> None:
        _seed_findings(db)
        result = db.list_findings_global(limit=2, offset=0)
        assert len(result["findings"]) == 2
        assert result["total"] == 3

    def test_invalid_severity_raises(self, db: FiligreeDB) -> None:
        _seed_findings(db)
        with pytest.raises(ValueError, match="Invalid severity filter"):
            db.list_findings_global(severity="hgih")

    def test_invalid_status_raises(self, db: FiligreeDB) -> None:
        _seed_findings(db)
        with pytest.raises(ValueError, match="Invalid status filter"):
            db.list_findings_global(status="bogus")


class TestUpdateFinding:
    def test_update_status(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        updated = db.update_finding(ids["obo"], status="acknowledged")
        assert updated["status"] == "acknowledged"

    def test_update_issue_id(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        issue = db.create_issue("Test bug", type="bug")
        updated = db.update_finding(ids["obo"], issue_id=issue.id)
        assert updated["issue_id"] == issue.id

    def test_invalid_status_raises(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        with pytest.raises(ValueError, match="Invalid finding status"):
            db.update_finding(ids["obo"], status="bogus")

    def test_not_found_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.update_finding("no-such-id", status="fixed")

    def test_mismatched_file_id_raises(self, db: FiligreeDB) -> None:
        """Providing a file_id that doesn't match the finding should raise KeyError."""
        ids = _seed_findings(db)
        db.register_file("src/other.py")
        other_file = db.conn.execute("SELECT id FROM file_records WHERE path = 'src/other.py'").fetchone()["id"]
        with pytest.raises(KeyError, match="Finding not found"):
            db.update_finding(ids["obo"], file_id=other_file, status="acknowledged")

    def test_update_without_file_id(self, db: FiligreeDB) -> None:
        """file_id=None path looks up file_id from the finding record."""
        ids = _seed_findings(db)
        # Call without file_id — should resolve it from the DB
        updated = db.update_finding(ids["obo"], status="acknowledged")
        assert updated["status"] == "acknowledged"
        assert updated["file_id"]  # file_id should be populated from DB

    def test_dismiss_reason_persists_in_metadata(self, db: FiligreeDB) -> None:
        """dismiss_reason is stored in finding metadata JSON."""
        ids = _seed_findings(db)
        updated = db.update_finding(ids["obo"], status="false_positive", dismiss_reason="not a real bug")
        assert updated["status"] == "false_positive"
        meta = updated.get("metadata") or {}
        if isinstance(meta, str):
            import json

            meta = json.loads(meta)
        assert meta["dismiss_reason"] == "not a real bug"

    def test_dismiss_reason_without_status_raises(self, db: FiligreeDB) -> None:
        """dismiss_reason requires status to also be provided."""
        ids = _seed_findings(db)
        with pytest.raises(ValueError, match="dismiss_reason requires status"):
            db.update_finding(ids["obo"], dismiss_reason="reason only")

    def test_dismiss_with_legacy_non_dict_metadata_recovers(self, db: FiligreeDB) -> None:
        """filigree-ff98665ca3: legacy rows with JSON-array metadata must not crash dismiss.

        Pre-fix, `old_meta["dismiss_reason"] = ...` ran outside the parse
        try/except and raised TypeError on list/scalar metadata. The fix uses
        `_safe_json_loads` which coerces non-dict top-levels to {}.
        """
        ids = _seed_findings(db)
        # Simulate a legacy row whose metadata is a JSON array (valid JSON but
        # not an object) — bypass validation by writing the column directly.
        db.conn.execute(
            "UPDATE scan_findings SET metadata = ? WHERE id = ?",
            ("[1, 2, 3]", ids["obo"]),
        )
        db.conn.commit()
        updated = db.update_finding(ids["obo"], status="false_positive", dismiss_reason="recovered")
        assert updated["status"] == "false_positive"
        meta = updated.get("metadata") or {}
        assert isinstance(meta, dict)
        assert meta["dismiss_reason"] == "recovered"


class TestPromoteFindingToObservation:
    def test_creates_observation(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        obs = db.promote_finding_to_observation(ids["sqli"])
        assert obs["summary"].startswith("[test-scanner]")
        assert "SQL injection" in obs["summary"]
        assert obs["file_path"] == "src/main.py"
        assert obs["line"] == 100

    def test_priority_from_severity(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        obs = db.promote_finding_to_observation(ids["sqli"])
        assert obs["priority"] == 0  # critical -> P0

    def test_not_found_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.promote_finding_to_observation("no-such-id")

    @pytest.mark.parametrize(
        ("severity", "expected_priority"),
        [("critical", 0), ("high", 1), ("medium", 2), ("low", 3), ("info", 3)],
    )
    def test_severity_to_priority_mapping(self, db: FiligreeDB, severity: str, expected_priority: int) -> None:
        """Each severity level maps to the correct priority."""
        db.register_file("src/sev.py")
        result = db.process_scan_results(
            scan_source="test",
            findings=[{"path": "src/sev.py", "rule_id": "r1", "severity": severity, "message": "msg"}],
        )
        finding_id = result["new_finding_ids"][0]
        obs = db.promote_finding_to_observation(finding_id)
        assert obs["priority"] == expected_priority


class TestPromoteFindingToIssue:
    def test_creates_issue_and_links_finding(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        result = db.promote_finding_to_issue(ids["sqli"])
        issue = result["issue"]

        assert issue.type == "bug"
        assert issue.priority == 0
        assert "SQL injection" in issue.title
        assert issue.fields["source_finding_id"] == ids["sqli"]
        assert db.get_finding(ids["sqli"])["issue_id"] == issue.id
        labels = db.conn.execute("SELECT label FROM labels WHERE issue_id = ?", (issue.id,)).fetchall()
        assert any(row["label"] == "from-finding" for row in labels)

    def test_reuses_existing_issue_on_retry(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        first = db.promote_finding_to_issue(ids["sqli"])

        second = db.promote_finding_to_issue(ids["sqli"])

        assert second["issue"].id == first["issue"].id
        assert "warnings" in second
        assert any("already linked" in warning for warning in second["warnings"])

    def test_rejects_non_list_labels(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.promote_finding_to_issue(ids["sqli"], labels="cluster:test")  # type: ignore[arg-type]

    def test_rejects_non_string_label_items(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.promote_finding_to_issue(ids["sqli"], labels=["cluster:test", 123])  # type: ignore[list-item]

    def test_rejects_non_string_actor(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        with pytest.raises(ValueError, match="actor must be a string"):
            db.promote_finding_to_issue(ids["sqli"], actor=123)  # type: ignore[arg-type]


class TestProcessScanResultsBreakingChange:
    """The old create_issues parameter was removed — callers must use create_observations."""

    def test_old_create_issues_kwarg_raises_type_error(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py")
        with pytest.raises(TypeError):
            db.process_scan_results(
                scan_source="test",
                findings=[{"path": "src/main.py", "rule_id": "r1", "severity": "info", "message": "m"}],
                create_issues=True,  # type: ignore[call-arg]
            )
