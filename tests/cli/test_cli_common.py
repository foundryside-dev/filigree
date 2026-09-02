"""Unit tests for ``filigree.cli_common`` startup-failure rendering.

Covers ``_emit_registry_startup_failure`` (filigree-8fd300e2f7): both
registry protocol failures that can escape ``FiligreeDB.from_anchor`` at
startup — ``RegistryVersionMismatchError`` and ``RegistryUnavailableError`` —
render as the public envelope on ``--json`` and as plain stderr otherwise,
with an actionable hint line for the recoverable (unavailable) case.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from filigree import cli_common
from filigree.registry import RegistryUnavailableError, RegistryVersionMismatchError
from filigree.registry_errors import registry_startup_error_response, registry_startup_hint
from filigree.types.api import ErrorCode

UNAVAILABLE = RegistryUnavailableError(
    "Loomweave capability probe unreachable at http://127.0.0.1:1/api/v1/_capabilities: refused",
    url="http://127.0.0.1:1/api/v1/_capabilities",
    cause_kind="network",
)
MISMATCH = RegistryVersionMismatchError(
    "incompatible registry api version",
    url="http://127.0.0.1:1/api/v1/_capabilities",
    expected=1,
    advertised=2,
)


def _invoke(as_json: bool, exc: RegistryUnavailableError | RegistryVersionMismatchError) -> tuple[str, str]:
    """Run ``_emit_registry_startup_failure`` inside a real Click context."""

    @click.command()
    @click.option("--json", "as_json", is_flag=True)
    def cmd(as_json: bool) -> None:
        cli_common._emit_registry_startup_failure(exc)

    result = CliRunner().invoke(cmd, ["--json"] if as_json else [])
    assert result.exit_code == 0, result.output
    return result.stdout, result.stderr


class TestRegistryStartupHint:
    def test_unavailable_hint_names_both_remedies(self) -> None:
        hint = registry_startup_hint(UNAVAILABLE)
        assert hint is not None
        assert "loomweave serve" in hint
        assert "allow_local_fallback" in hint

    def test_mismatch_hint_is_upgrade_guidance(self) -> None:
        hint = registry_startup_hint(MISMATCH)
        assert hint is not None
        assert "Upgrade" in hint

    def test_startup_response_carries_hint_and_backend(self) -> None:
        response = registry_startup_error_response(UNAVAILABLE, action="opening project database")
        assert response["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        details = response["details"]
        assert details["backend"] == "loomweave"
        assert details["cause_kind"] == "network"
        assert details["url"] == UNAVAILABLE.url
        assert details["hint"] == registry_startup_hint(UNAVAILABLE)


class TestEmitRegistryStartupFailure:
    def test_unavailable_plain_prints_error_then_hint_on_stderr(self) -> None:
        stdout, stderr = _invoke(False, UNAVAILABLE)
        assert stdout == ""
        lines = stderr.splitlines()
        assert len(lines) == 2
        assert "Registry unavailable while opening project database" in lines[0]
        assert UNAVAILABLE.url in lines[0]
        assert "network" in lines[0]
        assert lines[1] == registry_startup_hint(UNAVAILABLE)

    def test_unavailable_json_prints_envelope_on_stdout(self) -> None:
        stdout, stderr = _invoke(True, UNAVAILABLE)
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload == registry_startup_error_response(UNAVAILABLE, action="opening project database")
        assert payload["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert payload["details"]["cause_kind"] == "network"

    def test_mismatch_plain_still_renders_its_envelope(self) -> None:
        stdout, stderr = _invoke(False, MISMATCH)
        assert stdout == ""
        lines = stderr.splitlines()
        assert "Loomweave registry API version mismatch" in lines[0]
        assert lines[1] == registry_startup_hint(MISMATCH)

    def test_mismatch_json_code_unchanged(self) -> None:
        stdout, _stderr = _invoke(True, MISMATCH)
        payload = json.loads(stdout)
        assert payload["code"] == ErrorCode.LOOMWEAVE_REGISTRY_VERSION_MISMATCH
        assert payload["details"]["expected"] == 1
        assert payload["details"]["advertised"] == 2


class TestGetDbCatchesRegistryUnavailable:
    def test_get_db_exits_1_instead_of_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.core import FiligreeDB

        def _raise(*_args: object, **_kwargs: object) -> FiligreeDB:
            raise UNAVAILABLE

        monkeypatch.setattr(cli_common, "find_filigree_anchor", lambda: object())
        monkeypatch.setattr(FiligreeDB, "from_anchor", staticmethod(_raise))

        @click.command()
        @click.option("--json", "as_json", is_flag=True)
        def cmd(as_json: bool) -> None:
            cli_common.get_db()

        result = CliRunner().invoke(cmd, ["--json"])
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        payload = json.loads(result.stdout)
        assert payload["code"] == ErrorCode.REGISTRY_UNAVAILABLE
