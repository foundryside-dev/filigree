"""CLI wiring for ``filigree sei-backfill``.

The backfill logic itself is exercised against a Loomweave stub in
``tests/federation/test_sei_conformance_oracle.py``. This module covers the CLI
adapter: the verb is registered, and the clean precondition refusal
(``SeiBackfillError`` → exit 1 + a VALIDATION error envelope) is wired through
``get_db`` → ``run_sei_backfill`` → output, in both human and JSON modes. A
default local-mode project has no Loomweave authority, which is exactly the
refusal path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.cli_common import get_db
from filigree.registry import RegistryUnavailableError
from tests._fakes.clarion_http import clarion_stub


def test_sei_prefix_is_loomweave() -> None:
    """v26/T0: the emitter-match constant uses the loomweave:eid: namespace."""
    from filigree.sei_backfill import SEI_PREFIX

    assert SEI_PREFIX == "loomweave:eid:"
    assert "loomweave:eid:abc".startswith(SEI_PREFIX)


def _switch_to_loomweave_mode(project: Path, base_url: str) -> None:
    """Repoint a local-mode project's conf at a (live-stub) Loomweave authority."""
    conf_path = project / ".filigree.conf"
    conf = json.loads(conf_path.read_text())
    conf["registry_backend"] = "loomweave"
    conf["loomweave"] = {"base_url": base_url, "timeout_seconds": 2}
    conf_path.write_text(json.dumps(conf))


def test_sei_backfill_refuses_in_local_mode_human(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill"])
    assert result.exit_code == 1
    assert "Loomweave" in result.output


def test_sei_backfill_refuses_in_local_mode_json(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "VALIDATION"
    assert "Loomweave" in payload["error"]


def test_sei_backfill_help_lists_execute_flag(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill", "--help"])
    assert result.exit_code == 0
    assert "--execute" in result.output


# --- applied run against a live Loomweave stub (success + human output) ----------


def test_sei_backfill_execute_human_reports_applied_and_lists_orphans(cli_in_project: tuple[CliRunner, Path]) -> None:
    """``--execute`` against a SEI-capable Loomweave: a resolvable locator migrates,
    an unresolvable one is reported ORPHAN. Covers the applied branch and the
    human-output formatter including the orphan-review listing."""
    runner, project = cli_in_project
    migrate_loc = "py:func:mod::kept"
    orphan_loc = "py:func:mod::gone"
    sei = "loomweave:eid:00000000000000000000000000000abc"

    # Seed two associations on a real issue while still in local mode.
    with get_db() as db:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, migrate_loc, content_hash="sha256:a")
        db.add_entity_association(issue.id, orphan_loc, content_hash="sha256:b")
        issue_id = issue.id

    with clarion_stub(sei_supported=True, sei_by_locator={migrate_loc: sei}) as (base_url, _state):
        _switch_to_loomweave_mode(project, base_url)
        result = runner.invoke(cli, ["sei-backfill", "--execute"])

        assert result.exit_code == 0, result.output
        assert "APPLIED" in result.output
        assert "1 migrated" in result.output
        assert "1 orphaned" in result.output
        assert "ORPHANS NEEDING REVIEW" in result.output
        assert orphan_loc in result.output

        # The applied run actually wrote: the resolvable locator is now the SEI,
        # the orphan is kept verbatim and stamped.
        with get_db() as db:
            rows = {
                r["loomweave_entity_id"]: r["migration_orphaned_at"]
                for r in db.conn.execute(
                    "SELECT loomweave_entity_id, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
                    (issue_id,),
                ).fetchall()
            }
        assert sei in rows
        assert orphan_loc in rows
        assert migrate_loc not in rows
        assert rows[orphan_loc] is not None  # orphan stamped


def test_sei_backfill_execute_json_reports_migration(cli_in_project: tuple[CliRunner, Path]) -> None:
    """``--execute --json`` emits the structured report with ``dry_run`` false."""
    runner, project = cli_in_project
    loc = "py:func:mod::f"
    sei = "loomweave:eid:00000000000000000000000000000def"

    with get_db() as db:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, loc, content_hash="sha256:a")

    with clarion_stub(sei_supported=True, sei_by_locator={loc: sei}) as (base_url, _state):
        _switch_to_loomweave_mode(project, base_url)
        result = runner.invoke(cli, ["sei-backfill", "--execute", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["associations_migrated"] == 1
    assert payload["associations_orphaned"] == 0


# --- exception → error-envelope mapping (sei.py:47-50) -------------------------


def test_sei_backfill_maps_registry_unavailable_to_error_envelope(
    cli_in_project: tuple[CliRunner, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Loomweave availability failure maps to a REGISTRY_UNAVAILABLE envelope, exit 1."""
    runner, _project = cli_in_project

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RegistryUnavailableError("loomweave down", url="http://x", path="", cause_kind="network")

    monkeypatch.setattr("filigree.cli_commands.sei.run_sei_backfill", _boom)
    result = runner.invoke(cli, ["sei-backfill", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "REGISTRY_UNAVAILABLE"
    assert "loomweave down" in payload["error"]


def test_sei_backfill_maps_sqlite_error_to_io_envelope(
    cli_in_project: tuple[CliRunner, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sqlite failure during the migration maps to an IO envelope, exit 1."""
    runner, _project = cli_in_project

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("filigree.cli_commands.sei.run_sei_backfill", _boom)
    result = runner.invoke(cli, ["sei-backfill", "--execute", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "IO"
    assert "database is locked" in payload["error"]


def test_sei_backfill_maps_out_of_sync_error_to_code_3_and_envelope(
    cli_in_project: tuple[CliRunner, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LoomweaveOutOfSyncError maps to exit code 3 and the LOOMWEAVE_OUT_OF_SYNC envelope."""
    runner, _project = cli_in_project
    from filigree.sei_backfill import LoomweaveOutOfSyncError

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise LoomweaveOutOfSyncError("Loomweave DB is out of sync")

    monkeypatch.setattr("filigree.cli_commands.sei.run_sei_backfill", _boom)
    result = runner.invoke(cli, ["sei-backfill", "--json"])

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["code"] == "LOOMWEAVE_OUT_OF_SYNC"
    assert "Loomweave DB is out of sync" in payload["error"]
    assert payload["remediation_command"] == "loomweave analyze"


def test_sei_backfill_sync_check_missing_db(
    cli_in_project: tuple[CliRunner, Path],
) -> None:
    """If db.project_root is set but .loomweave/loomweave.db is missing, it raises LoomweaveOutOfSyncError."""
    runner, project = cli_in_project
    (project / ".git").mkdir(exist_ok=True)
    with clarion_stub(sei_supported=True) as (base_url, _state):
        _switch_to_loomweave_mode(project, base_url)
        # Run command — since `.loomweave/loomweave.db` does not exist, it should raise LoomweaveOutOfSyncError, exit with code 3.
        result = runner.invoke(cli, ["sei-backfill", "--json"])
        assert result.exit_code == 3
        payload = json.loads(result.output)
        assert payload["code"] == "LOOMWEAVE_OUT_OF_SYNC"
        assert "Loomweave database not found" in payload["error"]


def test_sei_backfill_sync_check_hash_mismatch(
    cli_in_project: tuple[CliRunner, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If .loomweave/loomweave.db exists but analyzed_at_commit does not match git HEAD, it raises LoomweaveOutOfSyncError."""
    runner, project = cli_in_project
    (project / ".git").mkdir(exist_ok=True)

    # 1. Create a dummy .loomweave/loomweave.db
    loomweave_dir = project / ".loomweave"
    loomweave_dir.mkdir(parents=True, exist_ok=True)
    loomweave_db_path = loomweave_dir / "loomweave.db"

    import sqlite3

    conn = sqlite3.connect(str(loomweave_db_path))
    conn.execute("CREATE TABLE runs (status TEXT, analyzed_at_commit TEXT, started_at TEXT)")
    # Insert a run with a mismatched commit hash
    conn.execute("INSERT INTO runs VALUES ('completed', 'mismatched_commit_hash', '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()

    # 2. Mock git rev-parse HEAD to return a fixed commit hash
    import subprocess

    class MockCompletedProcess:
        def __init__(self) -> None:
            self.stdout = "expected_git_head_commit_hash\n"
            self.stderr = ""
            self.returncode = 0

    def mock_run(*args: object, **kwargs: object) -> MockCompletedProcess:
        return MockCompletedProcess()

    monkeypatch.setattr(subprocess, "run", mock_run)

    with clarion_stub(sei_supported=True) as (base_url, _state):
        _switch_to_loomweave_mode(project, base_url)

        result = runner.invoke(cli, ["sei-backfill", "--json"])
        assert result.exit_code == 3
        payload = json.loads(result.output)
        assert payload["code"] == "LOOMWEAVE_OUT_OF_SYNC"
        assert "out of sync with git HEAD" in payload["error"]
        assert "mismatched_commit_hash" in payload["error"]
        assert "expected_git_head_commit_hash" in payload["error"]


def test_sei_backfill_sync_check_passes_with_synced_db(
    cli_in_project: tuple[CliRunner, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for filigree-0e4bc3d81a's sibling bug: a project WITH a
    .git dir and a .loomweave/loomweave.db whose latest completed run matches git
    HEAD must PASS the sync gate and proceed to backfill. Pre-fix the gate read a
    stale .clarion/clarion.db that never exists, so every real rebranded project
    raised LoomweaveOutOfSyncError — and no test exercised the pass branch against
    the real path."""
    runner, project = cli_in_project
    (project / ".git").mkdir(exist_ok=True)

    synced_head = "synced_head_commit_hash"
    loomweave_dir = project / ".loomweave"
    loomweave_dir.mkdir(parents=True, exist_ok=True)

    import sqlite3

    conn = sqlite3.connect(str(loomweave_dir / "loomweave.db"))
    conn.execute("CREATE TABLE runs (status TEXT, analyzed_at_commit TEXT, started_at TEXT)")
    conn.execute("INSERT INTO runs VALUES ('completed', ?, '2026-01-01T00:00:00Z')", (synced_head,))
    conn.commit()
    conn.close()

    import subprocess

    class MockCompletedProcess:
        def __init__(self) -> None:
            self.stdout = f"{synced_head}\n"
            self.stderr = ""
            self.returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: MockCompletedProcess())

    loc = "py:func:mod::f"
    sei = "loomweave:eid:0000000000000000000000000000ace0"
    with get_db() as db:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, loc, content_hash="sha256:a")

    with clarion_stub(sei_supported=True, sei_by_locator={loc: sei}) as (base_url, _state):
        _switch_to_loomweave_mode(project, base_url)
        result = runner.invoke(cli, ["sei-backfill", "--execute", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload.get("code") != "LOOMWEAVE_OUT_OF_SYNC"
    assert payload["associations_migrated"] == 1
