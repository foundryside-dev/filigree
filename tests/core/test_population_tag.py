"""Project population tag: config key, ``init --population``, ``config set``, banner."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import CONFIG_FILENAME, find_filigree_anchor, read_config, write_config
from filigree.hooks import generate_session_context


def _init(runner: CliRunner, root: Path, *args: str) -> None:
    os.chdir(root)
    result = runner.invoke(cli, ["init", "--prefix", "t", *args])
    assert result.exit_code == 0, result.output


def _config(root: Path) -> dict[str, object]:
    return json.loads((find_filigree_anchor(root).store_dir / CONFIG_FILENAME).read_text())


def test_init_writes_population_default_in_non_tty(tmp_path: Path, cli_runner: CliRunner) -> None:
    _init(cli_runner, tmp_path)
    assert _config(tmp_path)["population"] == "product-use"


def test_init_population_flag_is_recorded(tmp_path: Path, cli_runner: CliRunner) -> None:
    _init(cli_runner, tmp_path, "--population", "suite-construction")
    assert _config(tmp_path)["population"] == "suite-construction"


def test_init_rejects_unknown_population(tmp_path: Path, cli_runner: CliRunner) -> None:
    os.chdir(tmp_path)
    result = cli_runner.invoke(cli, ["init", "--population", "bogus"])
    assert result.exit_code != 0


def test_init_in_tty_prompts_when_population_absent(tmp_path: Path, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    import filigree.cli_commands.admin as admin_mod

    monkeypatch.setattr(admin_mod, "_stdin_is_tty", lambda: True)
    os.chdir(tmp_path)
    result = cli_runner.invoke(cli, ["init", "--prefix", "t"], input="suite-construction\n")
    assert result.exit_code == 0, result.output
    assert _config(tmp_path)["population"] == "suite-construction"


def test_reinit_does_not_overwrite_existing_population(tmp_path: Path, cli_runner: CliRunner) -> None:
    _init(cli_runner, tmp_path, "--population", "suite-construction")
    _init(cli_runner, tmp_path)
    assert _config(tmp_path)["population"] == "suite-construction"


def test_config_set_population(initialized_project: Path, cli_runner: CliRunner) -> None:
    os.chdir(initialized_project)
    result = cli_runner.invoke(cli, ["config", "set", "population", "suite-construction"])
    assert result.exit_code == 0, result.output
    assert _config(initialized_project)["population"] == "suite-construction"


def test_config_set_rejects_invalid_population(initialized_project: Path, cli_runner: CliRunner) -> None:
    os.chdir(initialized_project)
    result = cli_runner.invoke(cli, ["config", "set", "population", "nope"])
    assert result.exit_code != 0
    assert _config(initialized_project)["population"] == "product-use"


def test_config_set_rejects_unknown_key(initialized_project: Path, cli_runner: CliRunner) -> None:
    os.chdir(initialized_project)
    result = cli_runner.invoke(cli, ["config", "set", "prefix", "x"])
    assert result.exit_code != 0


def test_read_config_preserves_population(initialized_project: Path) -> None:
    store = find_filigree_anchor(initialized_project).store_dir
    assert read_config(store)["population"] == "product-use"


def test_banner_reports_population(initialized_project: Path) -> None:
    os.chdir(initialized_project)
    context = generate_session_context()
    assert context is not None
    assert "POPULATION: product-use" in context


def test_banner_reports_unset_population(initialized_project: Path) -> None:
    store = find_filigree_anchor(initialized_project).store_dir
    config = _config(initialized_project)
    del config["population"]
    write_config(store, config)
    os.chdir(initialized_project)
    context = generate_session_context()
    assert context is not None
    assert "POPULATION: unset" in context
