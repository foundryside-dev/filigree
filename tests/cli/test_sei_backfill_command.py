"""CLI wiring for ``filigree sei-backfill``.

The backfill logic itself is exercised against a Clarion stub in
``tests/federation/test_sei_conformance_oracle.py``. This module covers the CLI
adapter: the verb is registered, and the clean precondition refusal
(``SeiBackfillError`` → exit 1 + a VALIDATION error envelope) is wired through
``get_db`` → ``run_sei_backfill`` → output, in both human and JSON modes. A
default local-mode project has no Clarion authority, which is exactly the
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


def _switch_to_clarion_mode(project: Path, base_url: str) -> None:
    """Repoint a local-mode project's conf at a (live-stub) Clarion authority."""
    conf_path = project / ".filigree.conf"
    conf = json.loads(conf_path.read_text())
    conf["registry_backend"] = "clarion"
    conf["clarion"] = {"base_url": base_url, "timeout_seconds": 2}
    conf_path.write_text(json.dumps(conf))


def test_sei_backfill_refuses_in_local_mode_human(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill"])
    assert result.exit_code == 1
    assert "Clarion" in result.output


def test_sei_backfill_refuses_in_local_mode_json(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "VALIDATION"
    assert "Clarion" in payload["error"]


def test_sei_backfill_help_lists_execute_flag(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill", "--help"])
    assert result.exit_code == 0
    assert "--execute" in result.output


# --- applied run against a live Clarion stub (success + human output) ----------


def test_sei_backfill_execute_human_reports_applied_and_lists_orphans(cli_in_project: tuple[CliRunner, Path]) -> None:
    """``--execute`` against a SEI-capable Clarion: a resolvable locator migrates,
    an unresolvable one is reported ORPHAN. Covers the applied branch and the
    human-output formatter including the orphan-review listing."""
    runner, project = cli_in_project
    migrate_loc = "py:func:mod::kept"
    orphan_loc = "py:func:mod::gone"
    sei = "clarion:eid:00000000000000000000000000000abc"

    # Seed two associations on a real issue while still in local mode.
    with get_db() as db:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, migrate_loc, content_hash="sha256:a")
        db.add_entity_association(issue.id, orphan_loc, content_hash="sha256:b")
        issue_id = issue.id

    with clarion_stub(sei_supported=True, sei_by_locator={migrate_loc: sei}) as (base_url, _state):
        _switch_to_clarion_mode(project, base_url)
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
                r["clarion_entity_id"]: r["migration_orphaned_at"]
                for r in db.conn.execute(
                    "SELECT clarion_entity_id, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
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
    sei = "clarion:eid:00000000000000000000000000000def"

    with get_db() as db:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, loc, content_hash="sha256:a")

    with clarion_stub(sei_supported=True, sei_by_locator={loc: sei}) as (base_url, _state):
        _switch_to_clarion_mode(project, base_url)
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
    """A Clarion availability failure maps to a REGISTRY_UNAVAILABLE envelope, exit 1."""
    runner, _project = cli_in_project

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RegistryUnavailableError("clarion down", url="http://x", path="", cause_kind="network")

    monkeypatch.setattr("filigree.cli_commands.sei.run_sei_backfill", _boom)
    result = runner.invoke(cli, ["sei-backfill", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "REGISTRY_UNAVAILABLE"
    assert "clarion down" in payload["error"]


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
