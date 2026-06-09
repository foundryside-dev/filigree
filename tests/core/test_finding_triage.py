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


class TestFindingSuppressionState:
    """Wardline's suppression verdict is lifted from
    ``metadata.wardline.suppression_state`` onto the top-level finding read
    surface (``get_finding`` / ``list_findings_global``) so triage can tell an
    accepted/suppressed defect from open work without parsing nested metadata.
    Independent of issue-linkage: a finding can be baselined while unlinked."""

    def _seed_suppressed(self, db: FiligreeDB, state: str = "baselined") -> str:
        db.register_file("src/s.py", language="python")
        result = db.process_scan_results(
            scan_source="wardline",
            findings=[
                {
                    "path": "src/s.py",
                    "rule_id": "WLN-EXAMPLE",
                    "severity": "high",
                    "message": "an accepted defect",
                    "metadata": {"wardline": {"suppression_state": state}},
                }
            ],
        )
        return result["new_finding_ids"][0]

    def test_finding_without_wardline_meta_has_null_suppression_state(self, db: FiligreeDB) -> None:
        ids = _seed_findings(db)
        finding = db.get_finding(ids["obo"])
        assert finding["suppression_state"] is None

    def test_suppressed_finding_surfaces_state_via_get_and_list(self, db: FiligreeDB) -> None:
        fid = self._seed_suppressed(db, state="baselined")
        finding = db.get_finding(fid)
        # Surfaced at the top level — no nested-metadata parsing needed.
        assert finding["suppression_state"] == "baselined"
        # ...and it is independent of issue-linkage (unlinked but still suppressed).
        assert finding["issue_id"] is None
        # The same lift rides the project-wide finding_list read path.
        listed = db.list_findings_global(file_id=finding["file_id"])["findings"]
        assert len(listed) == 1
        assert listed[0]["suppression_state"] == "baselined"


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


def _seed_wardline_mix(db: FiligreeDB) -> None:
    """Seed the dogfood-3 shape: metric telemetry noise, a real defect, a
    baselined defect, and a non-wardline finding with no ``wardline`` metadata.

    Mirrors the live distribution (FIL-2/X-5): ``kind:metric`` rows drown the
    handful of real ``kind:defect`` rows, and a baselined defect must be
    excludable from the actionable view.
    """
    db.register_file("src/app.py", language="python")
    db.process_scan_results(
        scan_source="wardline",
        findings=[
            {
                "path": "src/app.py",
                "rule_id": "WLN-L3-LOW-RESOLUTION",
                "severity": "info",
                "message": "engine telemetry",
                "line_start": 1,
                "metadata": {"wardline": {"kind": "metric"}},
            },
            {
                "path": "src/app.py",
                "rule_id": "PY-WL-101",
                "severity": "high",
                "message": "a real un-suppressed defect",
                "line_start": 10,
                "metadata": {"wardline": {"kind": "defect", "qualname": "app.handler"}},
            },
            {
                "path": "src/app.py",
                "rule_id": "PY-WL-102",
                "severity": "high",
                "message": "a baselined (accepted) defect",
                "line_start": 20,
                "metadata": {"wardline": {"kind": "defect", "suppression_state": "baselined"}},
            },
        ],
    )
    # A non-wardline, agent-reported finding: carries no ``wardline`` metadata.
    db.process_scan_results(
        scan_source="agent",
        findings=[
            {"path": "src/app.py", "rule_id": "api-misuse", "severity": "medium", "message": "agent finding", "line_start": 30},
        ],
    )


class TestListFindingsGlobalKindSuppression:
    """FIL-2/X-5: ``finding_list`` must filter on the nested wardline axes
    (``kind``, ``suppression``) and on ``rule_id``/``qualname`` so an agent can
    answer 'show me the real un-suppressed defects' server-side instead of
    pulling everything and filtering nested metadata client-side."""

    def test_filter_by_kind_defect(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        result = db.list_findings_global(kind="defect")
        rules = sorted(f["rule_id"] for f in result["findings"])
        # Only the two wardline-classified defects; metric telemetry and the
        # kind-less agent finding are excluded.
        assert rules == ["PY-WL-101", "PY-WL-102"]
        # The COUNT path must agree with the rows path under the metadata filter.
        assert result["total"] == 2

    def test_filter_by_kind_metric(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        result = db.list_findings_global(kind="metric")
        assert [f["rule_id"] for f in result["findings"]] == ["WLN-L3-LOW-RESOLUTION"]
        assert result["total"] == 1

    def test_filter_by_suppression_active_excludes_baselined_and_keeps_metadata_less(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        result = db.list_findings_global(suppression="active")
        rules = sorted(f["rule_id"] for f in result["findings"])
        # Everything EXCEPT the baselined defect — including the metadata-less
        # agent finding (absent suppression => active, per the promote-guard contract).
        assert rules == ["PY-WL-101", "WLN-L3-LOW-RESOLUTION", "api-misuse"]
        assert result["total"] == 3

    def test_filter_by_suppression_baselined(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        result = db.list_findings_global(suppression="baselined")
        assert [f["rule_id"] for f in result["findings"]] == ["PY-WL-102"]
        assert result["total"] == 1

    def test_no_arg_default_returns_everything_including_suppressed(self, db: FiligreeDB) -> None:
        """Core-contract lock (filigree-2bdb878bd2): the core primitive's default
        is 'return everything' — suppressed rows are NOT hidden here. The
        default-hide lives only at the agent-facing surfaces (MCP/CLI), so
        internal callers (e.g. scanner_reporting re-finding a just-ingested row)
        keep seeing every row. If this ever flips, every internal caller silently
        loses rows."""
        _seed_wardline_mix(db)
        result = db.list_findings_global()
        rules = sorted(f["rule_id"] for f in result["findings"])
        assert "PY-WL-102" in rules  # the baselined defect is present by default
        assert result["total"] == 4

    def test_suppression_all_is_noop_returns_everything(self, db: FiligreeDB) -> None:
        """``suppression='all'`` is the explicit 'no suppression filter' sentinel
        the surfaces pass to opt back in to suppressed rows. It must behave
        identically to omitting the filter."""
        _seed_wardline_mix(db)
        explicit_all = db.list_findings_global(suppression="all")
        no_arg = db.list_findings_global()
        assert sorted(f["rule_id"] for f in explicit_all["findings"]) == sorted(f["rule_id"] for f in no_arg["findings"])
        assert explicit_all["total"] == no_arg["total"] == 4

    @pytest.mark.parametrize("offcontract", ["baselined", ["baselined"], 42])
    def test_offcontract_nondict_wardline_classifies_as_active(self, db: FiligreeDB, offcontract: object) -> None:
        """``metadata.wardline`` is external payload stored verbatim and may be a
        valid-JSON-but-non-dict value (string/list/int). ``json_extract`` of
        ``$.wardline.suppression_state`` on a non-object node returns NULL, so the
        row must read as active (present under ``suppression='active'``, absent
        under ``suppression='baselined'``) and must NOT raise — mirroring the
        promote-guard's 'absent => active' contract on the query side."""
        db.register_file("src/oc.py", language="python")
        db.process_scan_results(
            scan_source="wardline",
            findings=[
                {
                    "path": "src/oc.py",
                    "rule_id": "OC-1",
                    "severity": "high",
                    "message": "off-contract",
                    "metadata": {"wardline": offcontract},
                }
            ],
        )
        active = db.list_findings_global(suppression="active")
        assert "OC-1" in {f["rule_id"] for f in active["findings"]}
        baselined = db.list_findings_global(suppression="baselined")
        assert "OC-1" not in {f["rule_id"] for f in baselined["findings"]}

    def test_pagination_applies_after_suppression_filter(self, db: FiligreeDB) -> None:
        """LIMIT/OFFSET apply to the already-suppression-filtered set (the clause
        is part of the shared WHERE), so the baselined row never leaks onto any
        page and the pages tile the active set with no gap or overlap."""
        _seed_wardline_mix(db)
        active_total = db.list_findings_global(suppression="active")["total"]  # 3 of the 4 rows
        seen: list[str] = []
        for offset in range(active_total):
            page = db.list_findings_global(suppression="active", limit=1, offset=offset)
            seen.extend(f["rule_id"] for f in page["findings"])
            assert page["total"] == active_total  # total reflects the filtered population
        assert "PY-WL-102" not in seen  # baselined never paged in
        assert len(seen) == len(set(seen)) == active_total  # no overlap, no gap

    def test_filter_real_unsuppressed_defects_combined(self, db: FiligreeDB) -> None:
        # The headline query the finding exists to enable.
        _seed_wardline_mix(db)
        result = db.list_findings_global(kind="defect", suppression="active")
        assert [f["rule_id"] for f in result["findings"]] == ["PY-WL-101"]
        assert result["total"] == 1

    def test_filter_by_rule_id(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        result = db.list_findings_global(rule_id="PY-WL-101")
        assert [f["rule_id"] for f in result["findings"]] == ["PY-WL-101"]
        assert result["total"] == 1

    def test_filter_by_qualname(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        result = db.list_findings_global(qualname="app.handler")
        assert [f["rule_id"] for f in result["findings"]] == ["PY-WL-101"]
        assert result["total"] == 1

    def test_invalid_kind_raises(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        with pytest.raises(ValueError, match="Invalid kind filter"):
            db.list_findings_global(kind="bogus")

    def test_invalid_suppression_raises(self, db: FiligreeDB) -> None:
        _seed_wardline_mix(db)
        with pytest.raises(ValueError, match="Invalid suppression filter"):
            db.list_findings_global(suppression="bogus")

    def test_corrupt_metadata_row_survives_filters(self, db: FiligreeDB) -> None:
        """A single malformed-metadata row must not raise OperationalError under
        the json_extract filters (the json_valid guard contract) and must
        classify as kind-less + active."""
        _seed_wardline_mix(db)
        db.conn.execute("UPDATE scan_findings SET metadata = ? WHERE rule_id = ?", ("{not json", "api-misuse"))
        db.conn.commit()
        # kind=defect must still run and must exclude the corrupt row.
        defects = db.list_findings_global(kind="defect")
        assert sorted(f["rule_id"] for f in defects["findings"]) == ["PY-WL-101", "PY-WL-102"]
        # suppression=active must still run and must INCLUDE the corrupt row
        # (corrupt/absent => active).
        active = db.list_findings_global(suppression="active")
        assert "api-misuse" in {f["rule_id"] for f in active["findings"]}

    def test_active_classification_shared_with_unbridged_stats(self, db: FiligreeDB) -> None:
        """``suppression=active`` and ``unbridged_finding_stats`` share the SAME
        suppression predicate, so the suppressed/active *classification* is
        identical. They do NOT share the base population: ``unbridged_finding_stats``
        restricts to open + un-bridged, while ``finding_list(suppression="active")``
        applies no such filter — so the latter's total is a *superset*. Pin both
        facts so the docstrings can't quietly over- or under-claim."""
        _seed_wardline_mix(db)
        # When every finding is open + un-bridged, the only difference between the
        # populations is the suppressed/active split — and that split is shared,
        # so the counts coincide here.
        stats = db.unbridged_finding_stats()
        active = db.list_findings_global(suppression="active")
        assert stats["actionable"] == active["total"]

        # Now introduce a terminal (fixed) active finding and a bridged active
        # finding — both EXCLUDED from ``actionable`` (terminal / has an issue)
        # but still un-suppressed, so still counted by ``suppression="active"``.
        # Create the issue first (it commits) before the raw UPDATEs so we don't
        # open a nested transaction.
        issue = db.create_issue("bridged", type="bug")
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE rule_id = ?", ("PY-WL-101",))
        db.conn.execute("UPDATE scan_findings SET issue_id = ? WHERE rule_id = ?", (issue.id, "api-misuse"))
        db.conn.commit()
        stats2 = db.unbridged_finding_stats()
        active2 = db.list_findings_global(suppression="active")
        # actionable shrinks (both rows leave the open+unbridged base); active is
        # a strict superset (it never filtered them out).
        assert active2["total"] > stats2["actionable"]
        # The suppressed classification is unchanged on both surfaces: the
        # baselined defect is the only suppressed row everywhere.
        assert stats2["suppressed"] == 1
        assert "PY-WL-102" not in {f["rule_id"] for f in active2["findings"]}


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
