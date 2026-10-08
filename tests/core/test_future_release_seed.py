"""``filigree init`` no longer seeds a "Future" release, and doctor --fix retires empty legacy ones."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import FiligreeDB, find_filigree_anchor


def test_init_does_not_seed_future_release(tmp_path: Path, cli_runner: CliRunner) -> None:
    os.chdir(tmp_path)
    result = cli_runner.invoke(cli, ["init", "--prefix", "t"])
    assert result.exit_code == 0, result.output

    db = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
    try:
        assert db.list_issues(type="release") == []
        assert db.get_ready() == []
    finally:
        db.close()


def test_initialize_does_not_seed_future_release(db: FiligreeDB) -> None:
    assert db.list_issues(type="release") == []
