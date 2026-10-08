"""Stage 0 telemetry cut (Task 0.5a): the scan ingest accepts defect kinds only by default.

Wardline emits engine telemetry (``fact`` / ``classification`` / ``metric`` /
``suggestion`` kinds, and ``<engine>`` pseudo-path rows) alongside defects. Those
are not work. By default (``scan_ingest.accept_kinds = ["defect"]``) the ingest
rejects them per-finding with ``KIND_NOT_ACCEPTED``; ``["*"]`` restores the 3.3
behaviour. Independently of the setting, the ``mark_unseen`` sweep never
transitions a stored non-defect row — otherwise a defects-only producer would
flip every previously-ingested telemetry row to ``unseen_in_latest`` (and the
close-on-fixed cascade would close their linked issues).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from filigree.core import FiligreeDB, read_scan_ingest_accept_kinds, write_config
from filigree.scanner_reporting import report_scanner_finding
from tests._db_factory import set_scan_ingest_accept_kinds

KIND_REASON = "telemetry kinds are not work; see Stage 0"


def _finding(path: str, rule_id: str, *, kind: str | None = "defect", fingerprint: str | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"path": path, "rule_id": rule_id, "message": f"{rule_id} at {path}", "severity": "medium"}
    if kind is not None:
        f["metadata"] = {"wardline": {"kind": kind}}
    if fingerprint is not None:
        f["fingerprint"] = fingerprint
    return f


def _status(db: FiligreeDB, finding_id: str) -> str:
    return str(db.conn.execute("SELECT status FROM scan_findings WHERE id = ?", (finding_id,)).fetchone()["status"])


class TestAcceptKindsConfig:
    def test_default_is_defect_only(self, tmp_path: Path) -> None:
        assert read_scan_ingest_accept_kinds(tmp_path) == ("defect",)

    def test_star_round_trips(self, tmp_path: Path) -> None:
        set_scan_ingest_accept_kinds(tmp_path, ["*"])
        assert read_scan_ingest_accept_kinds(tmp_path) == ("*",)

    def test_explicit_list_is_deduplicated_and_sorted(self, tmp_path: Path) -> None:
        set_scan_ingest_accept_kinds(tmp_path, ["fact", "defect", "fact"])
        assert read_scan_ingest_accept_kinds(tmp_path) == ("defect", "fact")

    @pytest.mark.parametrize(
        "section",
        [
            "defect",
            {"accept_kinds": "defect"},
            {"accept_kinds": []},
            {"accept_kinds": ["defect", 3]},
            {"accept_kinds": [""]},
            {"other": True},
        ],
    )
    def test_malformed_setting_falls_back_to_default(self, tmp_path: Path, section: object) -> None:
        write_config(tmp_path, {"prefix": "test", "version": 1, "scan_ingest": section})
        assert read_scan_ingest_accept_kinds(tmp_path) == ("defect",)

    def test_corrupt_config_falls_back_to_default(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{not json")
        assert read_scan_ingest_accept_kinds(tmp_path) == ("defect",)


class TestIngestRejectsNonDefectKinds:
    def test_ingest_rejects_non_defect_kind_per_finding_with_reason(self, db: FiligreeDB) -> None:
        findings = [
            _finding("src/a.py", "R-DEFECT", fingerprint="fp-defect"),
            _finding("src/a.py", "R-FACT", kind="fact", fingerprint="fp-fact"),
            _finding("src/b.py", "R-METRIC", kind="metric"),
            _finding("src/b.py", "R-CLASS", kind="classification", fingerprint="fp-class"),
            _finding("src/b.py", "R-SUGG", kind="suggestion", fingerprint="fp-sugg"),
        ]

        result = db.process_scan_results(scan_source="wardline", findings=findings)

        assert result["failed"] == [
            {"index": 1, "fingerprint": "fp-fact", "code": "KIND_NOT_ACCEPTED", "reason": KIND_REASON},
            {"index": 2, "fingerprint": None, "code": "KIND_NOT_ACCEPTED", "reason": KIND_REASON},
            {"index": 3, "fingerprint": "fp-class", "code": "KIND_NOT_ACCEPTED", "reason": KIND_REASON},
            {"index": 4, "fingerprint": "fp-sugg", "code": "KIND_NOT_ACCEPTED", "reason": KIND_REASON},
        ]
        assert result["rejected_by_kind"] == 4
        assert result["requested"] == 5
        assert result["findings_created"] == 1
        assert result["applied"] == 1
        stored = db.conn.execute("SELECT rule_id FROM scan_findings").fetchall()
        assert [r["rule_id"] for r in stored] == ["R-DEFECT"]
        # A path carrying ONLY rejected findings never becomes a tracked file.
        assert db.get_file_by_path("src/b.py") is None

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"wardline": {}},
            {"wardline": {"kind": None}},
            {"wardline": "not-a-dict"},
            {"wardline": {"kind": 7}},
            {"wardline": {"kind": "frobnicate"}},  # unknown kind -> defect (FIL-1)
            {"kind": "fact"},  # kind outside metadata.wardline is not the wardline axis
        ],
    )
    def test_ingest_missing_kind_treated_as_defect(self, db: FiligreeDB, metadata: dict[str, Any] | None) -> None:
        finding: dict[str, Any] = {"path": "src/a.py", "rule_id": "R1", "message": "m"}
        if metadata is not None:
            finding["metadata"] = metadata

        result = db.process_scan_results(scan_source="wardline", findings=[finding])

        assert result["failed"] == []
        assert result["rejected_by_kind"] == 0
        assert result["findings_created"] == 1

    @pytest.mark.parametrize("kind", ["defect", None])
    def test_ingest_engine_path_rejected(self, db: FiligreeDB, kind: str | None) -> None:
        findings = [
            _finding("<engine>", "WL-ENGINE", kind=kind, fingerprint="fp-engine"),
            _finding("src/a.py", "R1"),
        ]

        result = db.process_scan_results(scan_source="wardline", findings=findings)

        assert result["failed"] == [{"index": 0, "fingerprint": "fp-engine", "code": "KIND_NOT_ACCEPTED", "reason": KIND_REASON}]
        assert result["rejected_by_kind"] == 1
        assert result["findings_created"] == 1
        assert db.get_file_by_path("<engine>") is None

    def test_accept_kinds_star_restores_legacy(self, db: FiligreeDB) -> None:
        set_scan_ingest_accept_kinds(db, ["*"])
        findings = [
            _finding("src/a.py", "R-DEFECT"),
            _finding("src/a.py", "R-FACT", kind="fact"),
            _finding("<engine>", "WL-ENGINE", kind="metric"),
        ]

        result = db.process_scan_results(scan_source="wardline", findings=findings)

        assert result["failed"] == []
        assert result["rejected_by_kind"] == 0
        assert result["findings_created"] == 3
        assert result["applied"] == 3
        assert db.get_file_by_path("<engine>") is not None

    def test_explicit_kind_list_accepts_named_kinds_only(self, db: FiligreeDB) -> None:
        set_scan_ingest_accept_kinds(db, ["defect", "fact"])
        findings = [
            _finding("src/a.py", "R-DEFECT"),
            _finding("src/a.py", "R-FACT", kind="fact"),
            _finding("src/a.py", "R-METRIC", kind="metric"),
            _finding("<engine>", "WL-ENGINE", kind="fact"),
        ]

        result = db.process_scan_results(scan_source="wardline", findings=findings)

        # ``<engine>`` is rejected whenever ``*`` is not in the set, regardless of kind.
        assert [(f["index"], f["code"]) for f in result["failed"]] == [(2, "KIND_NOT_ACCEPTED"), (3, "KIND_NOT_ACCEPTED")]
        assert result["findings_created"] == 2

    def test_rejected_telemetry_line_range_does_not_fail_the_batch(self, tmp_path: Path) -> None:
        """A rejected finding is not ingested, so its line attribution is never checked."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x = 1\n")
        rooted = FiligreeDB(tmp_path / "filigree.db", prefix="test", project_root=tmp_path)
        rooted.initialize()
        try:
            telemetry = _finding("src/a.py", "R-FACT", kind="fact")
            telemetry["line_start"] = 999

            result = rooted.process_scan_results(scan_source="wardline", findings=[telemetry, _finding("src/a.py", "R1")])

            assert result["rejected_by_kind"] == 1
            assert result["findings_created"] == 1
        finally:
            rooted.close()

    def test_over_cap_index_stays_request_position_after_kind_rejection(self, db: FiligreeDB) -> None:
        """OVER_CAP indices still address the REQUEST array when earlier findings were kind-rejected."""
        from filigree.registry import LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE
        from tests.core.test_scan_ingest_registry import _ErrorChannelRegistry

        db.registry = _ErrorChannelRegistry(code=LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE, error_paths={"src/big.py"})  # type: ignore[assignment]
        findings = [
            _finding("src/a.py", "R-FACT", kind="fact"),
            _finding("src/ok.py", "R1"),
            _finding("src/big.py", "R2", fingerprint="fp-big"),
        ]

        result = db.process_scan_results(scan_source="wardline", findings=findings)

        assert sorted((f["index"], f["code"]) for f in result["failed"]) == [(0, "KIND_NOT_ACCEPTED"), (2, "OVER_CAP")]
        assert result["findings_created"] == 1


class TestSweepSkipsNonDefectRows:
    def test_sweep_skips_non_defect_rows(self, db: FiligreeDB) -> None:
        # Seed under the OLD (3.3) behaviour: telemetry rows land as findings.
        set_scan_ingest_accept_kinds(db, ["*"])
        seeded = db.process_scan_results(
            scan_source="wardline",
            findings=[
                _finding("src/a.py", "R-FACT", kind="fact", fingerprint="fp-fact"),
                _finding("src/a.py", "R-METRIC", kind="metric", fingerprint="fp-metric"),
                _finding("src/a.py", "R-GONE", fingerprint="fp-gone"),
                _finding("src/a.py", "R-NOMETA", kind=None, fingerprint="fp-nometa"),
                _finding("src/a.py", "R-STAYS", fingerprint="fp-stays"),
            ],
        )
        fact_id, metric_id, gone_id, nometa_id, stays_id = seeded["new_finding_ids"]
        # An issue-linked telemetry row: the close-on-fixed cascade must not fire for it.
        issue = db.create_issue("Tracked telemetry", priority=2)
        issue_status_before = issue.status
        # A legacy/third-party row whose metadata column is NULL must stay sweepable
        # (``json_valid(NULL)`` is NULL — the guard must not read that as "telemetry").
        db.conn.execute("UPDATE scan_findings SET metadata = NULL WHERE id = ?", (nometa_id,))
        db.conn.execute("UPDATE scan_findings SET issue_id = ? WHERE id = ?", (issue.id, metric_id))
        db.conn.commit()

        # Stage 0 producer: defects only, full scanned_paths, mark_unseen on.
        set_scan_ingest_accept_kinds(db, ["defect"])
        db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "R-STAYS", fingerprint="fp-stays")],
            mark_unseen=True,
            scanned_paths=["src/a.py"],
        )

        assert _status(db, fact_id) == "open"
        assert _status(db, metric_id) == "open"
        assert db.get_issue(issue.id).status == issue_status_before
        # Positive controls: defect-side rows absent from the batch are still swept.
        assert _status(db, gone_id) == "unseen_in_latest"
        assert _status(db, nometa_id) == "unseen_in_latest"
        assert _status(db, stays_id) == "open"

    def test_sweep_guard_holds_under_star(self, db: FiligreeDB) -> None:
        """Ruling: the guard is unconditional — ``["*"]`` does not re-enable sweeping telemetry."""
        set_scan_ingest_accept_kinds(db, ["*"])
        seeded = db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "R-FACT", kind="fact"), _finding("src/a.py", "R-GONE")],
        )
        fact_id, gone_id = seeded["new_finding_ids"]

        db.process_scan_results(scan_source="wardline", findings=[], mark_unseen=True, scanned_paths=["src/a.py"])

        assert _status(db, fact_id) == "open"
        assert _status(db, gone_id) == "unseen_in_latest"


class TestReportFindingKindRejection:
    def test_report_finding_with_telemetry_kind_raises_validation(self, db: FiligreeDB) -> None:
        finding = _finding("src/a.py", "R-FACT", kind="fact")

        with pytest.raises(ValueError, match="KIND_NOT_ACCEPTED"):
            report_scanner_finding(db, finding, create_observation=False)

        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 0

    def test_report_finding_defect_still_lands(self, db: FiligreeDB) -> None:
        outcome = report_scanner_finding(db, _finding("src/a.py", "R1"), create_observation=False)
        assert outcome.finding_record["rule_id"] == "R1"


def test_config_file_shape_is_documented_key(tmp_path: Path) -> None:
    """The setting lives under ``scan_ingest.accept_kinds`` in config.json (no schema change)."""
    set_scan_ingest_accept_kinds(tmp_path, ["*"])
    assert json.loads((tmp_path / "config.json").read_text()) == {"scan_ingest": {"accept_kinds": ["*"]}}
