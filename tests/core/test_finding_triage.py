"""Tests for finding triage DB methods."""

from __future__ import annotations

from typing import Any, cast

import pytest

from filigree.core import FiligreeDB
from filigree.types.core import make_issue_id


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


class TestFindingIssueStatus:
    """N6 (weft-c815d5e77d): the finding read surfaces carry the linked issue's
    status (and its ``close_reason`` resolution when closed), so a finding linked
    to a dismissed (``not_a_bug``) issue reads as triaged, not open work."""

    def test_unlinked_finding_has_null_issue_status(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        finding = db.get_finding(ids["obo"])
        assert finding["issue_status"] is None
        assert finding["issue_resolution"] is None

    def test_linked_finding_surfaces_issue_status(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        issue = db.promote_finding_to_issue(ids["sqli"], actor="t")["issue"]
        finding = db.get_finding(ids["sqli"])
        assert finding["issue_id"] == issue.id
        # Freshly promoted bug sits at its initial open state.
        assert finding["issue_status"] == issue.status
        assert finding["issue_resolution"] is None

    def test_dismissed_issue_surfaces_not_a_bug_and_resolution(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        issue = db.promote_finding_to_issue(ids["sqli"], actor="t")["issue"]
        db.close_issue(issue.id, status="not_a_bug", reason="intentional/by-design", actor="human")
        finding = db.get_finding(ids["sqli"])
        assert finding["issue_status"] == "not_a_bug"
        assert finding["issue_resolution"] == "intentional/by-design"

    def test_list_findings_global_carries_issue_status(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        issue = db.promote_finding_to_issue(ids["sqli"], actor="t")["issue"]
        db.close_issue(issue.id, status="not_a_bug", reason="dup", actor="human")
        listed = db.list_findings_global(issue_id=issue.id)
        assert len(listed["findings"]) == 1
        f0 = listed["findings"][0]
        assert f0["issue_status"] == "not_a_bug"
        assert f0["issue_resolution"] == "dup"

    def test_list_findings_global_status_filter_unambiguous_after_join(self, db: FiligreeDB) -> None:
        # Regression guard: the finding-status filter must not collide with
        # issues.status after the LEFT JOIN (both tables have a ``status`` column).
        ids = _seed_findings(db)
        db.promote_finding_to_issue(ids["sqli"], actor="t")
        listed = db.list_findings_global(status="open")
        assert len(listed["findings"]) == 3
        assert all(f["status"] == "open" for f in listed["findings"])

    def test_get_findings_paginated_carries_issue_status(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        issue = db.promote_finding_to_issue(ids["sqli"], actor="t")["issue"]
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        page = db.get_findings_paginated(file_id=file_id, limit=10)
        linked = next(f for f in page["results"] if f["id"] == ids["sqli"])
        assert linked["issue_status"] == issue.status
        unlinked = next(f for f in page["results"] if f["id"] == ids["obo"])
        assert unlinked["issue_status"] is None

    def test_missing_linked_issue_yields_null_status_not_crash(self, db: FiligreeDB) -> None:
        # A finding may carry an ``issue_id`` whose issue row no longer exists
        # (the FK is ON DELETE SET NULL, so this only arises off-contract). The
        # LEFT JOIN must yield null rather than crash.
        ids = _seed_findings(db)
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("UPDATE scan_findings SET issue_id = 'iss-ghost' WHERE id = ?", (ids["sqli"],))
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = ON")
        finding = db.get_finding(ids["sqli"])
        assert finding["issue_id"] == "iss-ghost"
        assert finding["issue_status"] is None
        assert finding["issue_resolution"] is None


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

    def test_non_string_status_raises_value_error(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        with pytest.raises(ValueError, match="status must be a string"):
            db.update_finding(ids["obo"], status=cast(Any, ["fixed"]))

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
        assert issue.fields["severity"] == "critical"
        assert "SQL injection" in issue.title
        assert issue.fields["source_finding_id"] == ids["sqli"]
        assert db.get_finding(ids["sqli"])["issue_id"] == issue.id
        labels = db.conn.execute("SELECT label FROM labels WHERE issue_id = ?", (issue.id,)).fetchall()
        assert any(row["label"] == "from-finding" for row in labels)

    @pytest.mark.parametrize(
        ("finding_severity", "bug_severity"),
        [
            ("critical", "critical"),
            ("high", "major"),
            ("medium", "major"),
            ("low", "minor"),
            ("info", "cosmetic"),
        ],
    )
    def test_maps_finding_severity_to_bug_workflow_severity(
        self,
        db: FiligreeDB,
        finding_severity: str,
        bug_severity: str,
    ) -> None:
        result = db.process_scan_results(
            scan_source="test-scanner",
            findings=[
                {
                    "path": f"src/{finding_severity}.py",
                    "rule_id": "R1",
                    "severity": finding_severity,
                    "message": "finding",
                }
            ],
        )
        issue = db.promote_finding_to_issue(result["new_finding_ids"][0])["issue"]

        assert issue.fields["severity"] == bug_severity

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


class TestPromoteFindingAndAttachEntity:
    def test_creates_issue_and_attaches_entity(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        result = db.promote_finding_and_attach_entity(ids["sqli"], "py:func:login", "hash-v1")

        assert result["created"] is True
        assert result["association"]["entity_id"] == "py:func:login"
        assert result["association"]["content_hash_at_attach"] == "hash-v1"
        assoc = db.list_entity_associations(make_issue_id(result["issue"].id))
        assert len(assoc) == 1

    def test_retry_converges_after_attach_failure(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """The promote+attach pair is non-atomic but idempotent: a failure in the
        attach step (after the issue is already promoted and committed) leaves a
        promoted-but-unassociated issue, and re-issuing the same request converges
        to that issue with the association now present. Guards the partial-state
        contract documented on ``promote_finding_and_attach_entity``.
        """
        ids = _seed_findings(db)
        real_attach = db.add_entity_association
        calls = {"n": 0}

        def flaky_attach(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                msg = "simulated attach failure"
                raise RuntimeError(msg)
            return real_attach(*args, **kwargs)

        monkeypatch.setattr(db, "add_entity_association", flaky_attach)

        # First attempt: promote commits, then attach raises.
        with pytest.raises(RuntimeError, match="simulated attach failure"):
            db.promote_finding_and_attach_entity(ids["sqli"], "py:func:login", "hash-v1")

        # The issue was promoted (finding linked) despite the attach failure...
        issue_id = db.get_finding(ids["sqli"])["issue_id"]
        assert issue_id
        # ...but no association exists yet — the partial state the contract warns about.
        assert db.list_entity_associations(make_issue_id(issue_id)) == []

        # Retry: promote reuses the existing issue, attach now succeeds.
        result = db.promote_finding_and_attach_entity(ids["sqli"], "py:func:login", "hash-v1")
        assert result["issue"].id == issue_id
        assert result["created"] is False
        assoc = db.list_entity_associations(make_issue_id(issue_id))
        assert len(assoc) == 1
        assert assoc[0]["entity_id"] == "py:func:login"
        assert assoc[0]["content_hash_at_attach"] == "hash-v1"


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
