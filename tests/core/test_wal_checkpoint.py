"""Explicit WAL checkpoint hygiene tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from filigree.core import FiligreeDB


def _wal_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + "-wal")


def _grow_wal(db: FiligreeDB, rows: int = 1000) -> None:
    db.conn.execute("PRAGMA wal_autocheckpoint=0")
    db.conn.execute("CREATE TABLE IF NOT EXISTS wal_checkpoint_probe (id INTEGER PRIMARY KEY, value TEXT)")
    db.conn.commit()
    for i in range(rows):
        db.conn.execute("INSERT INTO wal_checkpoint_probe (value) VALUES (?)", (f"row-{i}",))
    db.conn.commit()


def test_checkpoint_wal_truncates_idle_wal_and_reports_result(db: FiligreeDB) -> None:
    _grow_wal(db)
    wal = _wal_path(db.db_path)
    assert wal.stat().st_size > 0

    result = db.checkpoint_wal()

    assert result["status"] == "checkpointed"
    assert result["busy"] is False
    assert result["checkpoint_busy"] == 0
    assert result["wal_size_before"] > 0
    assert result["wal_size_after"] == 0
    assert result["database"] == str(db.db_path)
    assert db.conn.execute("SELECT count(*) FROM wal_checkpoint_probe").fetchone()[0] == 1000


def test_checkpoint_wal_reports_busy_without_raising(db: FiligreeDB) -> None:
    _grow_wal(db, rows=1)
    reader = sqlite3.connect(str(db.db_path), isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM wal_checkpoint_probe").fetchone()
        _grow_wal(db)

        result = db.checkpoint_wal()
    finally:
        reader.rollback()
        reader.close()

    assert result["status"] == "busy"
    assert result["busy"] is True
    assert result["checkpoint_busy"] > 0
    assert result["log_frames"] >= result["checkpointed_frames"]
    assert result["wal_size_after"] > 0
