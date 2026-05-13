"""CLI tests for shared file annotation commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli
from filigree.cli_common import get_db


def _assert_validation_envelope(output: str) -> None:
    data = json.loads(output)
    assert data["code"] == "VALIDATION"
    assert data["error"]


def test_annotate_list_get_and_resolve_json(initialized_project: Path) -> None:
    runner = CliRunner()
    source = initialized_project / "src" / "cli_ann.py"
    source.parent.mkdir()
    source.write_text("alpha\nbeta\n")
    original = os.getcwd()
    os.chdir(initialized_project)
    try:
        create = runner.invoke(
            cli,
            [
                "annotate-file",
                "src/cli_ann.py",
                "Remember beta.",
                "--line",
                "2",
                "--intent",
                "warning",
                "--critical",
                "--json",
            ],
        )
        assert create.exit_code == 0, create.output
        annotation = json.loads(create.output)
        assert annotation["annotation_id"].startswith("test-ann-")
        assert "id" not in annotation

        listed = runner.invoke(cli, ["list-annotations", "--file", "src/cli_ann.py", "--json"])
        assert listed.exit_code == 0, listed.output
        list_data = json.loads(listed.output)
        assert list_data["items"][0]["annotation_id"] == annotation["annotation_id"]
        assert "provenance" not in list_data["items"][0]

        fetched = runner.invoke(cli, ["get-annotation", annotation["annotation_id"], "--json"])
        assert fetched.exit_code == 0, fetched.output
        assert json.loads(fetched.output)["provenance"]["file_checksum"]

        resolved = runner.invoke(
            cli,
            ["resolve-annotation", annotation["annotation_id"], "--reason", "Handled", "--json"],
        )
        assert resolved.exit_code == 0, resolved.output
        assert json.loads(resolved.output)["status"] == "resolved"
    finally:
        os.chdir(original)


def test_annotation_cli_invalid_options_emit_json_envelopes(initialized_project: Path) -> None:
    runner = CliRunner()
    original = os.getcwd()
    os.chdir(initialized_project)
    try:
        cases = [
            ["annotate-file", "x.py", "note", "--intent", "bad", "--json"],
            ["list-annotations", "--limit", "0", "--json"],
            ["list-annotations", "--offset", "-1", "--json"],
            ["list-annotations", "--status", "bad", "--json"],
            ["get-annotation", "test-ann-missing", "--detail", "bad", "--json"],
        ]

        for args in cases:
            result = runner.invoke(cli, args)
            assert result.exit_code == 1, f"{args}: {result.output}"
            _assert_validation_envelope(result.output)
    finally:
        os.chdir(original)


def test_cli_close_json_includes_annotation_warnings(initialized_project: Path) -> None:
    runner = CliRunner()
    (initialized_project / "warn.py").write_text("x = 1\n")
    original = os.getcwd()
    os.chdir(initialized_project)
    try:
        with get_db() as db:
            issue = db.create_issue("Close with annotation")
        created = runner.invoke(
            cli,
            [
                "annotate-file",
                "warn.py",
                "Must consider this.",
                "--critical",
                "--link",
                f"issue:{issue.id}:must_consider",
                "--json",
            ],
        )
        assert created.exit_code == 0, created.output
        ann_id = json.loads(created.output)["annotation_id"]

        closed = runner.invoke(cli, ["close", issue.id, "--reason", "done", "--json"])
        assert closed.exit_code == 0, closed.output
        data = json.loads(closed.output)
        assert data["succeeded"][0]["annotation_warnings"][0]["annotation_id"] == ann_id
    finally:
        os.chdir(original)


def test_cli_plain_close_prints_annotation_warning(initialized_project: Path) -> None:
    runner = CliRunner()
    (initialized_project / "plain.py").write_text("x = 1\n")
    original = os.getcwd()
    os.chdir(initialized_project)
    try:
        with get_db() as db:
            issue = db.create_issue("Close plain")
            db.annotate_file(
                "plain.py",
                "Plain warning.",
                critical=True,
                links=[{"target_type": "issue", "target_id": issue.id, "relationship": "must_consider"}],
            )

        closed = runner.invoke(cli, ["close", issue.id, "--reason", "done"])
        assert closed.exit_code == 0, closed.output
        assert "Annotation warning" in closed.output
    finally:
        os.chdir(original)
