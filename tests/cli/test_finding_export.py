"""``filigree finding export`` — archive (and optionally drop) stored telemetry rows (Task 0.5c).

Stage 0 stopped Wardline telemetry (``fact`` / ``classification`` / ``metric`` /
``suggestion`` kinds, on any path including ``<engine>``) from entering the tracker.
This verb disposes of the rows already stored: it writes every non-defect
``scan_findings`` row (with its ``file_records`` join) to JSONL plus
a sha256sum-format sidecar, and — only with ``--delete`` — drops those rows and
any file record they leave unreferenced. FIL-1: a row whose kind is missing,
corrupt, or unknown is defect-side and is never selected.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.cli_common import get_db
from filigree.core import FiligreeDB
from tests._db_factory import set_scan_ingest_accept_kinds

DEFAULT_OUT = Path("archive") / "telemetry-3x.jsonl"


def _finding(path: str, rule_id: str, *, kind: str | None) -> dict[str, Any]:
    f: dict[str, Any] = {
        "path": path,
        "rule_id": rule_id,
        "message": f"{rule_id} at {path}",
        "severity": "info",
        "fingerprint": f"fp-{rule_id}",
    }
    if kind is not None:
        f["metadata"] = {"wardline": {"kind": kind}}
    return f


def _seed(db: FiligreeDB, findings: list[dict[str, Any]]) -> dict[str, str]:
    """Ingest *findings* (all kinds accepted) and map rule_id -> finding id."""
    set_scan_ingest_accept_kinds(db, ["*"])
    result = db.process_scan_results(scan_source="wardline", findings=findings)
    ids = list(result["new_finding_ids"])
    assert len(ids) == len(findings), result
    return {f["rule_id"]: fid for f, fid in zip(findings, ids, strict=True)}


def _invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(cli, ["finding", "export", *args], catch_exceptions=False)


def _finding_ids(db: FiligreeDB) -> set[str]:
    return {row["id"] for row in db.conn.execute("SELECT id FROM scan_findings").fetchall()}


def _file_paths(db: FiligreeDB) -> set[str]:
    return {row["path"] for row in db.conn.execute("SELECT path FROM file_records").fetchall()}


def _table_count(db: FiligreeDB, table: str) -> int:
    return int(db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - fixed table names


def test_export_writes_jsonl_and_sidecar(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, root = cli_in_project
    with get_db() as db:
        ids = _seed(
            db,
            [
                _finding("src/a.py", "R-METRIC", kind="metric"),
                _finding("<engine>", "R-ENGINE", kind="fact"),
                _finding("src/a.py", "R-DEFECT", kind="defect"),
            ],
        )
        expected_rows = {
            fid: dict(db.conn.execute("SELECT * FROM scan_findings WHERE id = ?", (fid,)).fetchone())
            for fid in (ids["R-METRIC"], ids["R-ENGINE"])
        }

    result = _invoke(runner, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    out = root / DEFAULT_OUT
    assert out.is_file()
    data = out.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    sidecar = out.with_name(out.name + ".sha256")
    assert sidecar.read_text() == f"{digest}  {out.name}\n"

    assert payload["selected"] == 2
    assert payload["exported"] == 2
    assert payload["deleted"] == 0
    assert payload["sha256"] == digest
    assert Path(payload["out"]).resolve() == out.resolve()

    lines = [json.loads(line) for line in data.decode().splitlines()]
    # Deterministic: ordered by finding id.
    assert [line["finding"]["id"] for line in lines] == sorted(expected_rows)
    for line in lines:
        # Full stored row (raw column values, metadata as its stored text) + the joined file row.
        assert line["finding"] == expected_rows[line["finding"]["id"]]
        assert line["file"]["id"] == line["finding"]["file_id"]
        assert {"path", "language", "first_seen", "metadata"} <= set(line["file"])
    assert {line["file"]["path"] for line in lines} == {"src/a.py", "<engine>"}


def test_export_without_delete_leaves_rows(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, root = cli_in_project
    with get_db() as db:
        _seed(db, [_finding("src/a.py", "R-METRIC", kind="metric"), _finding("<engine>", "R-ENGINE", kind="fact")])
        before_findings, before_files = _finding_ids(db), _file_paths(db)

    result = _invoke(runner)
    assert result.exit_code == 0, result.output
    assert "2" in result.output

    with get_db() as db:
        assert _finding_ids(db) == before_findings
        assert _file_paths(db) == before_files

    # The existing archive is never silently overwritten ...
    out = root / DEFAULT_OUT
    original = out.read_bytes()
    refused = _invoke(runner, "--json")
    assert refused.exit_code == 1
    assert json.loads(refused.output)["code"] == "CONFLICT"
    assert out.read_bytes() == original
    # ... unless --force is passed.
    forced = _invoke(runner, "--json", "--force")
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.output)["exported"] == 2


def test_dry_run_counts_and_writes_nothing(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, root = cli_in_project
    with get_db() as db:
        _seed(db, [_finding("src/a.py", "R-METRIC", kind="metric"), _finding("src/a.py", "R-DEFECT", kind="defect")])
        before = _finding_ids(db)

    result = _invoke(runner, "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "selected": 1,
        "exported": 0,
        "deleted": 0,
        "deleted_file_records": 0,
        "skipped_linked": 0,
        "out": str((root / DEFAULT_OUT).resolve()),
        "sha256": None,
        "dry_run": True,
    }
    assert not (root / "archive").exists()
    with get_db() as db:
        assert _finding_ids(db) == before

    clash = runner.invoke(cli, ["finding", "export", "--dry-run", "--delete"])
    assert clash.exit_code == 2
    assert "mutually exclusive" in clash.output


def test_export_delete_removes_rows_and_orphan_files(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, root = cli_in_project
    with get_db() as db:
        ids = _seed(
            db,
            [
                # src/only_telemetry.py: every finding is telemetry -> file record dropped.
                _finding("src/only_telemetry.py", "R-ONLY-1", kind="metric"),
                _finding("src/only_telemetry.py", "R-ONLY-2", kind="suggestion"),
                # src/mixed.py keeps a defect -> file record kept.
                _finding("src/mixed.py", "R-MIXED-METRIC", kind="classification"),
                _finding("src/mixed.py", "R-MIXED-DEFECT", kind="defect"),
                # src/linked.py has an issue association -> file record kept.
                _finding("src/linked.py", "R-LINKED", kind="fact"),
                _finding("<engine>", "R-ENGINE", kind="metric"),
            ],
        )
        only_file = db.get_file_by_path("src/only_telemetry.py")
        linked_file = db.get_file_by_path("src/linked.py")
        assert only_file is not None
        assert linked_file is not None
        issue = db.create_issue("Tracked file", priority=2)
        db.add_file_association(linked_file.id, issue.id, "bug_in")
        # A file_events row (NOT NULL FK, no ON DELETE) must not block dropping the record.
        db.conn.execute(
            "INSERT INTO file_events (file_id, field, old_value, new_value, created_at) VALUES (?, 'language', '', 'python', ?)",
            (only_file.id, "2026-01-01T00:00:00+00:00"),
        )
        db.conn.commit()
        issues_before = _table_count(db, "issues")
        events_before = _table_count(db, "events")

    result = _invoke(runner, "--delete", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["selected"] == 5
    assert payload["exported"] == 5
    assert payload["deleted"] == 5
    assert payload["deleted_file_records"] == 2

    out = root / DEFAULT_OUT
    exported = [json.loads(line)["finding"]["id"] for line in out.read_text().splitlines()]
    assert sorted(exported) == sorted(fid for rule, fid in ids.items() if rule != "R-MIXED-DEFECT")
    assert payload["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()

    with get_db() as db:
        assert _finding_ids(db) == {ids["R-MIXED-DEFECT"]}
        assert _file_paths(db) == {"src/mixed.py", "src/linked.py"}
        assert _table_count(db, "file_events") == 0
        assert _table_count(db, "file_associations") == 1
        # The disposal records nothing in the issue tracker itself.
        assert _table_count(db, "issues") == issues_before
        assert _table_count(db, "events") == events_before


def test_export_never_selects_defect_rows(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, root = cli_in_project
    with get_db() as db:
        ids = _seed(
            db,
            [
                _finding("src/a.py", "R-METRIC", kind="metric"),
                _finding("src/a.py", "R-DEFECT", kind="defect"),
                _finding("src/a.py", "R-EMPTY", kind=None),  # stored as the '{}' column default
                _finding("src/a.py", "R-NULL", kind=None),
                _finding("src/a.py", "R-CORRUPT", kind="metric"),
                _finding("src/a.py", "R-ARRAY", kind="metric"),
                _finding("src/a.py", "R-WLSTR", kind="metric"),
                _finding("src/a.py", "R-UNKNOWN", kind="frobnicate"),
            ],
        )
        assert db.conn.execute("SELECT metadata FROM scan_findings WHERE id = ?", (ids["R-EMPTY"],)).fetchone()[0] == "{}"
        for rule, raw in (("R-NULL", None), ("R-CORRUPT", "{not json"), ("R-ARRAY", "[1, 2]"), ("R-WLSTR", '{"wardline": "x"}')):
            db.conn.execute("UPDATE scan_findings SET metadata = ? WHERE id = ?", (raw, ids[rule]))
        db.conn.commit()

    result = _invoke(runner, "--delete", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["selected"] == payload["deleted"] == 1

    exported = [json.loads(line)["finding"]["id"] for line in (root / DEFAULT_OUT).read_text().splitlines()]
    assert exported == [ids["R-METRIC"]]
    with get_db() as db:
        assert _finding_ids(db) == {fid for rule, fid in ids.items() if rule != "R-METRIC"}
        assert _file_paths(db) == {"src/a.py"}


def test_engine_path_rows_are_selected_only_for_non_defect_kinds(cli_in_project: tuple[CliRunner, Path]) -> None:
    """Final-review ruling (I1): ``<engine>`` is selected by kind like any other path.
    Wardline emits real defects at ``<engine>`` (WLN-ENGINE-LINELESS-DEFECT, ...), so a
    defect-side row there (defect, missing or unknown kind -- FIL-1) is never selected."""
    runner, root = cli_in_project
    with get_db() as db:
        ids = _seed(
            db,
            [
                _finding("<engine>", "R-ENGINE-DEFECT", kind="defect"),
                _finding("<engine>", "R-ENGINE-BARE", kind=None),
                _finding("<engine>", "R-ENGINE-UNKNOWN", kind="frobnicate"),
                _finding("<engine>", "R-ENGINE-METRIC", kind="metric"),
            ],
        )

    result = _invoke(runner, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["selected"] == 1
    exported = [json.loads(line)["finding"]["id"] for line in (root / DEFAULT_OUT).read_text().splitlines()]
    assert exported == [ids["R-ENGINE-METRIC"]]


def test_engine_defect_is_ingested_and_never_exported_engine_metric_is_rejected_and_exported(
    cli_in_project: tuple[CliRunner, Path],
) -> None:
    """End to end under the default policy (``accept_kinds = ["defect"]``): an ``<engine>``
    defect lands as a finding and the export never selects it; an ``<engine>`` metric is
    refused at ingest, and one a 3.3 ingest already stored is selected by the export."""
    runner, root = cli_in_project
    with get_db() as db:
        result = db.process_scan_results(
            scan_source="wardline",
            findings=[
                _finding("<engine>", "WLN-ENGINE-LINELESS-DEFECT", kind="defect"),
                _finding("<engine>", "WLN-ENGINE-RUN", kind="metric"),
            ],
        )
        assert [(f["index"], f["code"]) for f in result["failed"]] == [(1, "KIND_NOT_ACCEPTED")]
        assert result["findings_created"] == 1
        defect_id = result["new_finding_ids"][0]
        # A legacy (3.3) ingest stored the metric before Stage 0.
        legacy = _seed(db, [_finding("<engine>", "WLN-ENGINE-RUN-3X", kind="metric")])

    payload = json.loads(_invoke(runner, "--delete", "--json").output)
    assert payload["selected"] == payload["deleted"] == 1
    exported = [json.loads(line)["finding"]["id"] for line in (root / DEFAULT_OUT).read_text().splitlines()]
    assert exported == [legacy["WLN-ENGINE-RUN-3X"]]
    with get_db() as db:
        assert _finding_ids(db) == {defect_id}
        assert _file_paths(db) == {"<engine>"}


def test_delete_refused_when_export_not_fsynced(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, root = cli_in_project
    with get_db() as db:
        _seed(db, [_finding("src/a.py", "R-METRIC", kind="metric")])
        before_findings, before_files = _finding_ids(db), _file_paths(db)

    import filigree.finding_export as finding_export

    def _boom(fd: int) -> None:
        raise OSError(5, "simulated fsync failure")

    monkeypatch.setattr(finding_export.os, "fsync", _boom)
    result = _invoke(runner, "--delete", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "IO"

    monkeypatch.undo()
    assert not (root / DEFAULT_OUT).exists()
    with get_db() as db:
        assert _finding_ids(db) == before_findings
        assert _file_paths(db) == before_files


def test_issue_linked_telemetry_is_kept_and_counted(cli_in_project: tuple[CliRunner, Path]) -> None:
    """Controller ruling (0.5c fix 1): a bridged finding (``issue_id`` set) is deliberate
    evidence — Phase 2 WP-2.6 converts it into an evidence reference — so the export
    neither archives nor deletes it, and reports it as ``skipped_linked``."""
    runner, root = cli_in_project
    with get_db() as db:
        ids = _seed(
            db,
            [
                _finding("src/linked_only.py", "R-BRIDGED", kind="metric"),
                _finding("<engine>", "R-ENGINE-BRIDGED", kind="fact"),
                _finding("src/other.py", "R-FREE", kind="metric"),
            ],
        )
        issue_id = db.promote_finding_to_issue(ids["R-BRIDGED"], actor="test")["issue"].id
        engine_issue = db.create_issue("Engine evidence", priority=2)
        db.conn.execute("UPDATE scan_findings SET issue_id = ? WHERE id = ?", (engine_issue.id, ids["R-ENGINE-BRIDGED"]))
        db.conn.commit()
        evidence_before = {f.id for f in db.get_issue_findings(issue_id)}
        engine_evidence_before = {f.id for f in db.get_issue_findings(engine_issue.id)}
        assert ids["R-BRIDGED"] in evidence_before

    dry = json.loads(_invoke(runner, "--dry-run", "--json").output)
    assert dry["selected"] == 1
    assert dry["skipped_linked"] == 2

    result = _invoke(runner, "--delete", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["selected"] == payload["exported"] == payload["deleted"] == 1
    assert payload["skipped_linked"] == 2

    exported = [json.loads(line)["finding"]["id"] for line in (root / DEFAULT_OUT).read_text().splitlines()]
    assert exported == [ids["R-FREE"]]
    with get_db() as db:
        assert _finding_ids(db) == {ids["R-BRIDGED"], ids["R-ENGINE-BRIDGED"]}
        # Files still holding a kept linked finding are never orphaned.
        assert _file_paths(db) == {"src/linked_only.py", "<engine>"}
        assert {f.id for f in db.get_issue_findings(issue_id)} == evidence_before
        assert {f.id for f in db.get_issue_findings(engine_issue.id)} == engine_evidence_before

    human = runner.invoke(cli, ["finding", "export", "--dry-run"])
    assert "2 issue-linked" in human.output
