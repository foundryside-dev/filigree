"""MCP coverage for explicit SQLite WAL checkpointing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from tests.mcp._helpers import _parse


def _wal_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + "-wal")


def _grow_wal(db: FiligreeDB, rows: int = 1000) -> None:
    db.conn.execute("PRAGMA wal_autocheckpoint=0")
    db.conn.execute("CREATE TABLE IF NOT EXISTS wal_checkpoint_probe (id INTEGER PRIMARY KEY, value TEXT)")
    db.conn.commit()
    for i in range(rows):
        db.conn.execute("INSERT INTO wal_checkpoint_probe (value) VALUES (?)", (f"row-{i}",))
    db.conn.commit()


async def test_db_checkpoint_tool_truncates_wal(mcp_db: FiligreeDB) -> None:
    _grow_wal(mcp_db)
    assert _wal_path(mcp_db.db_path).stat().st_size > 0

    payload = _parse(await call_tool("db_checkpoint", {}))

    assert payload["status"] == "checkpointed"
    assert payload["busy"] is False
    assert payload["wal_size_before"] > 0
    assert payload["wal_size_after"] == 0
    assert payload["database"] == str(mcp_db.db_path)


async def test_db_checkpoint_tool_reports_busy(mcp_db: FiligreeDB) -> None:
    _grow_wal(mcp_db, rows=1)
    reader = sqlite3.connect(str(mcp_db.db_path), isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM wal_checkpoint_probe").fetchone()
        _grow_wal(mcp_db)

        payload = _parse(await call_tool("db_checkpoint", {}))
    finally:
        reader.rollback()
        reader.close()

    assert payload["status"] == "busy"
    assert payload["busy"] is True
    assert payload["checkpoint_busy"] > 0
    assert payload["wal_size_after"] > 0
