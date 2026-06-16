"""CLI transport-bound actor identity tests (ADR-012, schema v24).

Click 8.2 removed ``CliRunner(mix_stderr=...)``; stderr is always captured
separately now, so these tests read ``result.stderr`` directly rather than
toggling the (gone) kwarg. ``result.output`` still merges stdout+stderr, so we
read ``result.stdout`` where clean stdout matters.
"""

from __future__ import annotations

from click.testing import CliRunner

from filigree.cli import cli


def _init_project(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["init"], catch_exceptions=False)
    assert result.exit_code == 0, result.output


def test_cli_write_stamps_verified_actor(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    runner = CliRunner()
    _init_project(runner)
    result = runner.invoke(cli, ["--actor", "alice", "create", "t", "--type", "task"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    from filigree.cli_common import get_db

    db = get_db()
    row = db.conn.execute("SELECT verified_actor FROM events WHERE event_type = 'created' LIMIT 1").fetchone()
    assert row["verified_actor"] == "alice"


def test_cli_mismatch_warns_on_stderr_but_does_not_block(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    runner = CliRunner()
    _init_project(runner)
    result = runner.invoke(cli, ["--actor", "agent-x", "create", "t", "--type", "task"], catch_exceptions=False)
    assert result.exit_code == 0  # never blocks
    assert "ACTOR_MISMATCH" in result.stderr
    # Production correctness: the warning lands on stderr ONLY, so a --json
    # payload on stdout stays parseable (Click 8.3.1 result.output merges both,
    # but result.stdout is the clean stream real pipelines consume).
    assert "ACTOR_MISMATCH" not in result.stdout


def test_cli_no_warning_for_placeholder_default_actor(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    runner = CliRunner()
    _init_project(runner)
    # No --actor → default "cli" (a placeholder), so no warning.
    result = runner.invoke(cli, ["create", "t", "--type", "task"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "ACTOR_MISMATCH" not in result.stderr


def test_cli_and_mcp_default_actors_are_placeholders() -> None:
    from filigree.actor_identity import _PLACEHOLDER_ACTORS

    # The CLI --actor default ("cli") must be suppressed; mcp default likewise.
    assert "cli" in _PLACEHOLDER_ACTORS
    assert "mcp" in _PLACEHOLDER_ACTORS
