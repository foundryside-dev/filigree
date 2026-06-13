"""CLI coverage for explicit SQLite WAL checkpointing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import DB_FILENAME


def _db_path(project_root: Path) -> Path:
    return project_root / ".weft" / "filigree" / DB_FILENAME


def _wal_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + "-wal")


def _grow_wal(db_path: Path, rows: int = 1000) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE IF NOT EXISTS wal_checkpoint_probe (id INTEGER PRIMARY KEY, value TEXT)")
    conn.commit()
    for i in range(rows):
        conn.execute("INSERT INTO wal_checkpoint_probe (value) VALUES (?)", (f"row-{i}",))
    conn.commit()
    return conn


def test_db_checkpoint_command_truncates_wal(initialized_project: Path, monkeypatch, cli_runner: CliRunner) -> None:
    monkeypatch.chdir(initialized_project)
    db_path = _db_path(initialized_project)
    writer = _grow_wal(db_path)
    try:
        assert _wal_path(db_path).stat().st_size > 0

        result = cli_runner.invoke(cli, ["db", "checkpoint", "--json"])
    finally:
        writer.close()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "checkpointed"
    assert payload["busy"] is False
    assert payload["wal_size_before"] > 0
    assert payload["wal_size_after"] == 0
    assert payload["database"] == str(db_path)


def test_db_checkpoint_command_reports_busy(initialized_project: Path, monkeypatch, cli_runner: CliRunner) -> None:
    monkeypatch.chdir(initialized_project)
    db_path = _db_path(initialized_project)
    seed_writer = _grow_wal(db_path, rows=1)
    reader = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM wal_checkpoint_probe").fetchone()
        writer = _grow_wal(db_path)

        result = cli_runner.invoke(cli, ["db", "checkpoint", "--json"])
    finally:
        writer.close()
        reader.rollback()
        reader.close()
        seed_writer.close()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "busy"
    assert payload["busy"] is True
    assert payload["checkpoint_busy"] > 0
    assert payload["wal_size_after"] > 0
