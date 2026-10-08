"""Shared FiligreeDB factory for test fixtures.

Importable by any conftest.py or test file in the test suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from filigree.core import (
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    FiligreeDB,
    write_config,
)


def make_db(
    tmp_path: Path,
    *,
    packs: list[str] | None = None,
    prefix: str = "test",
    check_same_thread: bool = True,
) -> FiligreeDB:
    """Factory for FiligreeDB instances in tests.

    Centralizes construction so pack-specific fixtures reduce to one-liners.
    When *packs* is provided, a .filigree/ directory with config.json is
    created to match how production code discovers enabled packs.
    """
    if packs is None:
        d = FiligreeDB(
            tmp_path / "filigree.db",
            prefix=prefix,
            check_same_thread=check_same_thread,
        )
    else:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir(exist_ok=True)
        write_config(filigree_dir, {"prefix": prefix, "version": 1, "enabled_packs": packs})
        d = FiligreeDB(
            filigree_dir / DB_FILENAME,
            prefix=prefix,
            enabled_packs=packs,
            check_same_thread=check_same_thread,
        )
    d.initialize()
    return d


def set_scan_ingest_accept_kinds(db_or_dir: FiligreeDB | Path, kinds: list[str]) -> None:
    """Write ``scan_ingest.accept_kinds`` into the project's config.json.

    Accepts a ``FiligreeDB`` (writes to its ``meta_dir``) or the store dir
    itself (for tests that construct the DB afterwards). ``["*"]`` restores the
    pre-Stage-0 behaviour: every wardline finding kind is ingested.
    """
    meta_dir = db_or_dir.meta_dir if isinstance(db_or_dir, FiligreeDB) else db_or_dir
    config_path = meta_dir / "config.json"
    # Merge into the RAW file (not ``read_config``'s defaults-filled view) so a
    # test DB without a config.json does not suddenly acquire a default prefix.
    config: dict[str, Any] = json.loads(config_path.read_text()) if config_path.exists() else {}
    config["scan_ingest"] = {"accept_kinds": list(kinds)}
    write_config(meta_dir, config)
