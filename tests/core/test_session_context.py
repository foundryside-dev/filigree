"""Honest session banner: actor-scoped claims, startable-first READY, stalled critical path, defect-only analyzer signal."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import FiligreeDB, find_filigree_anchor
from filigree.hooks import READY_CAP, _build_context, resolve_session_actor


@pytest.fixture(autouse=True)
def _no_ambient_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FILIGREE_ACTOR", raising=False)


def _section(context: str, header: str) -> str:
    """Return the banner block that starts at the line beginning with *header*."""
    lines = context.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == ""), len(lines))
    return "\n".join(lines[start:end])


def test_ready_lists_only_startable_first_and_marks_rest(db: FiligreeDB) -> None:
    triage_bug = db.create_issue("Triage me", type="bug", priority=0)  # P0 but not startable from triage
    task = db.create_issue("Plain task", type="task", priority=3)

    context = _build_context(db)
    ready = _section(context, "READY TO WORK")

    assert ready.splitlines()[0] == "READY TO WORK (1 startable of 2 ready):"
    lines = ready.splitlines()[1:]
    assert task.id in lines[0]
    assert "not startable" not in lines[0]
    assert triage_bug.id in lines[1]
    assert "— not startable: move to 'confirmed' first" in lines[1]


def test_ready_never_lists_containers(db: FiligreeDB) -> None:
    epic = db.create_issue("Big epic", type="epic", priority=0)
    task = db.create_issue("Leaf task", type="task", priority=2)

    context = _build_context(db)
    ready = _section(context, "READY TO WORK")

    assert epic.id not in ready
    assert task.id in ready
    # The container still counts as ready, it just is not startable.
    assert ready.splitlines()[0] == "READY TO WORK (1 startable of 2 ready):"


def test_ready_cap_applies_after_startable_first_ordering(db: FiligreeDB) -> None:
    for i in range(READY_CAP):
        db.create_issue(f"Triage {i}", type="bug", priority=0)
    task = db.create_issue("Late startable task", type="task", priority=4)

    ready = _section(_build_context(db), "READY TO WORK")

    # P4 task sorts after the P0 triage bugs by priority, but startable-first wins the cap.
    assert task.id in ready


def test_in_progress_split_by_actor(db: FiligreeDB) -> None:
    mine = db.start_work(db.create_issue("Mine", priority=1).id, assignee="alice").id
    theirs = db.start_work(db.create_issue("Theirs", priority=1).id, assignee="bob").id

    context = _build_context(db, actor="alice")

    claims = _section(context, "YOUR CLAIMS")
    assert claims.splitlines()[0] == "YOUR CLAIMS (actor=alice):"
    assert mine in claims
    assert theirs not in context
    assert "left" in claims  # lease remaining is shown
    assert "OTHERS ACTIVE: 1" in context


def test_in_progress_actor_unknown_prints_count_only(db: FiligreeDB) -> None:
    only = db.start_work(db.create_issue("Someone's work", priority=1).id, assignee="bob").id

    context = _build_context(db)

    assert "IN PROGRESS (1, actor unknown — pass --actor)" in context
    assert only not in context
    assert "YOUR CLAIMS" not in context


def test_in_progress_actor_with_no_claims(db: FiligreeDB) -> None:
    db.start_work(db.create_issue("Bob's", priority=1).id, assignee="bob")
    context = _build_context(db, actor="alice")
    assert "YOUR CLAIMS (actor=alice): none" in context
    assert "OTHERS ACTIVE: 1" in context


def test_critical_path_shows_stalled_age_after_14_days(db: FiligreeDB) -> None:
    blocker = db.create_issue("Old blocker", priority=1)
    downstream = db.create_issue("Waiting", priority=2)
    db.add_dependency(downstream.id, blocker.id)
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    db.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (old, blocker.id))
    db.conn.commit()

    context = _build_context(db)

    heading = next(line for line in context.splitlines() if line.startswith("CRITICAL PATH"))
    assert heading.endswith("(stalled 20d):")


def test_critical_path_fresh_head_is_not_flagged(db: FiligreeDB) -> None:
    blocker = db.create_issue("Fresh blocker", priority=1)
    downstream = db.create_issue("Waiting", priority=2)
    db.add_dependency(downstream.id, blocker.id)

    heading = next(line for line in _build_context(db).splitlines() if line.startswith("CRITICAL PATH"))
    assert "stalled" not in heading


def _wln(path: str, fp: str, **md: Any) -> dict[str, Any]:
    f: dict[str, Any] = {"path": path, "rule_id": "R", "message": "m", "severity": "high", "line_start": 1, "fingerprint": fp}
    if md:
        f["metadata"] = md
    return f


def test_banner_defect_count_and_hint_excludes_telemetry(db: FiligreeDB) -> None:
    db.process_scan_results(
        scan_source="wardline",
        findings=[
            _wln("a.py", "fp1", wardline={"kind": "defect"}),
            _wln("b.py", "fp2"),  # kind-less: counted defect-side
            _wln("c.py", "fp3", wardline={"kind": "metric"}),
            _wln("d.py", "fp4", wardline={"kind": "metric"}),
            _wln("e.py", "fp5", wardline={"kind": "metric"}),
        ],
    )

    context = _build_context(db)

    line = next(line for line in context.splitlines() if line.startswith("ANALYZER SIGNAL"))
    assert line.startswith("ANALYZER SIGNAL: 2 defect-signal finding(s) open")
    assert "(+3 telemetry rows, not work — see Task 0.5)" in line
    assert "`filigree finding list --kind defect --status open`" in line
    assert "finding_list kind=defect status=open suppression=active limit=25" in line
    assert "actionable" not in context
    assert "ANALYZER FINDINGS" not in context


def test_banner_omits_telemetry_line_when_zero(db: FiligreeDB) -> None:
    db.process_scan_results(scan_source="wardline", findings=[_wln("a.py", "fp1", wardline={"kind": "defect"})])

    line = next(line for line in _build_context(db).splitlines() if line.startswith("ANALYZER SIGNAL"))

    assert line.startswith("ANALYZER SIGNAL: 1 defect-signal finding(s) open")
    assert "telemetry" not in line


def test_banner_has_no_analyzer_line_without_findings(db: FiligreeDB) -> None:
    assert "ANALYZER" not in _build_context(db)


# ---------------------------------------------------------------------------
# Actor plumbing end to end: resolver precedence and the CLI pass-through
# ---------------------------------------------------------------------------


def test_resolve_session_actor_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_session_actor() is None
    assert resolve_session_actor("  ") is None
    monkeypatch.setenv("FILIGREE_ACTOR", " env-actor ")
    assert resolve_session_actor() == "env-actor"
    assert resolve_session_actor("") == "env-actor"
    assert resolve_session_actor("flag-actor") == "flag-actor"  # explicit beats env


def _project_with_claim(root: Path, runner: CliRunner, assignee: str) -> str:
    os.chdir(root)
    result = runner.invoke(cli, ["init", "--prefix", "t"])
    assert result.exit_code == 0, result.output
    db = FiligreeDB.from_anchor(find_filigree_anchor(root))
    try:
        return db.start_work(db.create_issue("Claimed work", priority=1).id, assignee=assignee).id
    finally:
        db.close()


def test_cli_actor_flag_scopes_session_context(tmp_path: Path, cli_runner: CliRunner) -> None:
    claimed = _project_with_claim(tmp_path, cli_runner, "alice")

    result = cli_runner.invoke(cli, ["--actor", "alice", "session-context"])

    assert result.exit_code == 0, result.output
    assert "YOUR CLAIMS (actor=alice):" in result.output
    assert claimed in result.output
    assert "actor unknown" not in result.output


def test_cli_without_actor_prints_count_only(tmp_path: Path, cli_runner: CliRunner) -> None:
    claimed = _project_with_claim(tmp_path, cli_runner, "alice")

    result = cli_runner.invoke(cli, ["session-context"])

    assert "IN PROGRESS (1, actor unknown — pass --actor)" in result.output
    assert claimed not in result.output


def test_cli_filigree_actor_env_scopes_session_context(tmp_path: Path, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    claimed = _project_with_claim(tmp_path, cli_runner, "alice")
    monkeypatch.setenv("FILIGREE_ACTOR", "alice")

    result = cli_runner.invoke(cli, ["session-context"])

    assert result.exit_code == 0, result.output
    assert "YOUR CLAIMS (actor=alice):" in result.output
    assert claimed in result.output
