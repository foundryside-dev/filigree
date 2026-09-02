"""Registry degraded mode on the stdio MCP server recovers once Loomweave is back.

A fail-closed Loomweave failure at ``_attempt_startup`` (``registry_backend=
loomweave``, ``allow_local_fallback=false``) used to be latched in
``_registry_startup_error`` for the whole process lifetime: every tool call
answered ``REGISTRY_UNAVAILABLE`` forever, while its hint told the operator to
start Loomweave — which then changed nothing. The server now re-attempts
startup on a tool call at most once per ``_REGISTRY_RETRY_INTERVAL_SECONDS``
(monotonic clock), clears the latch on success, and reports the retry
schedule through ``mcp_status_get``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import filigree.mcp_server as mcp_mod
from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, FiligreeDB, write_config
from filigree.registry import RegistryUnavailableError, RegistryVersionMismatchError
from filigree.types.api import ErrorCode
from tests._fakes.clarion_http import ClarionStubState, clarion_stub


def _call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(asyncio.run(mcp_mod.call_tool(name, arguments or {}))[0].text)
    return payload


def _rewind_retry_clock() -> None:
    """Make the next tool call eligible for a startup retry without sleeping."""
    assert mcp_mod._registry_retry_last_monotonic is not None
    mcp_mod._registry_retry_last_monotonic -= mcp_mod._REGISTRY_RETRY_INTERVAL_SECONDS


@pytest.fixture
def reset_mcp_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for attr in ("db", "_filigree_dir", "_project_root", "_schema_mismatch", "_registry_startup_error", "_db_open_error"):
        monkeypatch.setattr(mcp_mod, attr, None)
    monkeypatch.setattr(mcp_mod, "_startup_args", None)
    monkeypatch.setattr(mcp_mod, "_registry_retry_last_monotonic", None)
    monkeypatch.setattr(mcp_mod, "_registry_retry_last_at", None)
    monkeypatch.setattr(mcp_mod, "_registry_retry_stamped_error", None)
    try:
        yield
    finally:
        if mcp_mod.db is not None:
            mcp_mod._tool_locks.pop(mcp_mod.db, None)
            mcp_mod.db.close()


@pytest.fixture
def declining_loomweave() -> Iterator[tuple[Path, ClarionStubState]]:
    """A fail-closed loomweave-mode project whose Loomweave declines the registry role.

    ``state.registry_backend`` can be flipped to ``True`` mid-test to simulate
    the operator fixing Loomweave; ``state.capability_requests`` counts probes.
    """
    with clarion_stub(registry_backend=False) as (base_url, state):
        yield base_url, state  # type: ignore[misc]


def _make_project(tmp_path: Path, base_url: str) -> Path:
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="proj")
    db.initialize()
    db.close()
    write_config(
        filigree_dir,
        {
            "prefix": "proj",
            "version": 1,
            "registry_backend": "loomweave",
            "loomweave": {"base_url": base_url, "timeout_seconds": 1, "allow_local_fallback": False},
        },
    )
    return filigree_dir


@pytest.mark.usefixtures("reset_mcp_globals")
class TestDegradedModeRecovers:
    def test_latch_clears_after_loomweave_comes_back(self, tmp_path: Path, declining_loomweave: tuple[str, ClarionStubState]) -> None:
        base_url, state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)

        mcp_mod._attempt_startup(filigree_dir)
        assert mcp_mod.db is None
        assert isinstance(mcp_mod._registry_startup_error, RegistryUnavailableError)
        assert mcp_mod._registry_startup_error.cause_kind == "role_declined"
        assert state.capability_requests == 1

        # Within the interval: no re-probe, still the envelope.
        payload = _call("issue_list")
        assert payload["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert state.capability_requests == 1

        # Interval elapsed, Loomweave still declining: ONE re-probe, still degraded.
        _rewind_retry_clock()
        payload = _call("issue_list")
        assert payload["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert state.capability_requests == 2
        assert mcp_mod.db is None

        # Operator fixes Loomweave; within the interval nothing changes yet.
        state.registry_backend = True
        payload = _call("issue_list")
        assert payload["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert state.capability_requests == 2

        # Next eligible call re-attempts startup, succeeds, and serves the tool.
        _rewind_retry_clock()
        payload = _call("issue_list")
        assert "items" in payload, payload
        assert state.capability_requests == 3
        assert mcp_mod._registry_startup_error is None
        assert mcp_mod.db is not None

        status = _call("mcp_status_get")
        assert status["status"] == "ok"
        assert status["db_initialized"] is True

    def test_status_reports_retry_schedule(self, tmp_path: Path, declining_loomweave: tuple[str, ClarionStubState]) -> None:
        base_url, state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)
        mcp_mod._attempt_startup(filigree_dir)

        status = _call("mcp_status_get")
        assert status["status"] == "registry_unavailable"
        retry = status["registry_retry"]
        assert retry["interval_seconds"] == mcp_mod._REGISTRY_RETRY_INTERVAL_SECONDS
        assert retry["last_retry_at"] is not None
        assert 0 < retry["next_retry_after"] <= mcp_mod._REGISTRY_RETRY_INTERVAL_SECONDS
        # Status is read-only: it never triggers the re-probe itself.
        assert state.capability_requests == 1

        _rewind_retry_clock()
        status = _call("mcp_status_get")
        assert status["registry_retry"]["next_retry_after"] == 0
        assert state.capability_requests == 1

    def test_guidance_follows_cause_kind(self, tmp_path: Path, declining_loomweave: tuple[str, ClarionStubState]) -> None:
        base_url, _state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)
        mcp_mod._attempt_startup(filigree_dir)

        status = _call("mcp_status_get")
        assert status["details"]["cause_kind"] == "role_declined"
        # A declined role is not fixed by starting Loomweave.
        assert "loomweave serve" not in status["guidance"]
        assert "registry_backend" in status["guidance"]
        assert status["details"]["hint"] == status["guidance"]

        payload = _call("issue_list")
        assert payload["details"]["hint"] == status["guidance"]

    def test_db_open_error_after_retry_is_an_envelope_not_a_crash(self, tmp_path: Path) -> None:
        """A retry that fails for a non-registry reason must not leave call_tool raising."""
        mcp_mod._db_open_error = OSError("database is locked")
        payload = _call("issue_list")
        assert payload["code"] == ErrorCode.IO
        assert "locked" in payload["error"]

    def test_transient_db_open_error_during_retry_keeps_retrying(
        self, tmp_path: Path, declining_loomweave: tuple[str, ClarionStubState], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Loomweave comes back but the DB open fails transiently: the retry must not latch forever.

        Genuine startup exits the process on ``_db_open_error`` so a supervisor
        can restart it; a retry has no such escape hatch, so the interval retry
        must keep covering the new sentinel until the open succeeds.
        """
        base_url, state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)
        mcp_mod._attempt_startup(filigree_dir)
        assert isinstance(mcp_mod._registry_startup_error, RegistryUnavailableError)

        state.registry_backend = True
        real_stamp = FiligreeDB.set_verified_actor

        def locked_stamp(self: FiligreeDB, actor: str | None) -> None:
            # Fails AFTER the capability probe succeeded, like a lock held by a
            # sibling writer at the moment the session row is stamped.
            self.close()
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(FiligreeDB, "set_verified_actor", locked_stamp)
        _rewind_retry_clock()
        payload = _call("issue_list")
        assert payload["code"] == ErrorCode.IO
        assert "locked" in payload["error"]
        assert mcp_mod._registry_startup_error is None
        assert mcp_mod._db_open_error is not None
        assert state.capability_requests == 2

        # Within the interval: no re-probe, same envelope.
        payload = _call("issue_list")
        assert payload["code"] == ErrorCode.IO
        assert state.capability_requests == 2
        status = _call("mcp_status_get")
        assert status["status"] == "db_open_error"
        assert status["registry_retry"]["next_retry_after"] > 0

        # The lock clears; the next eligible call recovers.
        monkeypatch.setattr(FiligreeDB, "set_verified_actor", real_stamp)
        _rewind_retry_clock()
        payload = _call("issue_list")
        assert "items" in payload, payload
        assert mcp_mod._db_open_error is None
        assert mcp_mod.db is not None
        assert state.capability_requests == 3

    def test_retry_helper_is_a_noop_without_startup_args(self, tmp_path: Path) -> None:
        mcp_mod._registry_startup_error = RegistryUnavailableError("down", url="http://127.0.0.1:1", cause_kind="network")
        assert mcp_mod._maybe_retry_registry_startup() is False
        assert mcp_mod._registry_startup_error is not None


@pytest.mark.usefixtures("reset_mcp_globals")
class TestHandSeededLatchNeverReprobes:
    """Only a latch stamped by ``_attempt_startup`` is ever retried.

    A latch seeded directly (unit tests, or a future code path that writes
    ``_registry_startup_error`` by hand) carries no retry stamp; if
    ``_startup_args`` still points at some earlier project the retry must NOT
    fire, or the seeded error is silently replaced by whatever that stale
    project's startup yields (observed as cross-test pollution in CI).
    """

    def test_seeded_latch_without_retry_stamp_is_not_retried(
        self, tmp_path: Path, declining_loomweave: tuple[str, ClarionStubState]
    ) -> None:
        base_url, state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)
        # Simulate leaked startup args from an earlier ``_attempt_startup`` on
        # another project, then a hand-seeded latch with no retry stamp.
        mcp_mod._startup_args = (filigree_dir, None, None)
        seeded = RegistryVersionMismatchError("incompatible", url=base_url, expected=1, advertised=2)
        mcp_mod._registry_startup_error = seeded
        assert mcp_mod._registry_retry_last_monotonic is None

        blocked = _call("issue_get", {"issue_id": "anything"})

        assert blocked["code"] == ErrorCode.LOOMWEAVE_REGISTRY_VERSION_MISMATCH
        assert mcp_mod._registry_startup_error is seeded
        assert mcp_mod.db is None
        assert state.capability_requests == 0

    def test_seeded_latch_with_leaked_stamp_is_not_retried(self, tmp_path: Path, declining_loomweave: tuple[str, ClarionStubState]) -> None:
        """A stamp left behind by an earlier real startup must not license a retry of a hand-seeded latch.

        Reproduces the cross-test pollution the reviewer observed: one test runs
        ``_attempt_startup`` against a tmp project (stamping the retry clock and
        leaving ``_startup_args`` behind), a later test hand-seeds a different
        error, and — once the interval has elapsed — the retry silently replaced
        the seeded error with whatever the torn-down project's startup yielded.
        """
        base_url, state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)
        mcp_mod._attempt_startup(filigree_dir)
        assert isinstance(mcp_mod._registry_startup_error, RegistryUnavailableError)
        assert state.capability_requests == 1
        assert mcp_mod._registry_retry_last_monotonic is not None
        # The stamp and startup args leak; only the latch is swapped by hand.
        seeded = RegistryVersionMismatchError("incompatible", url="http://localhost:9111", expected=1, advertised=2)
        mcp_mod._registry_startup_error = seeded
        _rewind_retry_clock()

        blocked = _call("issue_get", {"issue_id": "anything"})

        assert blocked["code"] == ErrorCode.LOOMWEAVE_REGISTRY_VERSION_MISMATCH
        assert mcp_mod._registry_startup_error is seeded
        assert mcp_mod.db is None
        assert state.capability_requests == 1

    def test_successful_startup_clears_retry_stamp(self, tmp_path: Path) -> None:
        with clarion_stub(registry_backend=True) as (base_url, _state):
            filigree_dir = _make_project(tmp_path, base_url)
            mcp_mod._registry_retry_last_monotonic = 1.0
            mcp_mod._registry_retry_last_at = "stale"
            mcp_mod._attempt_startup(filigree_dir)
        assert mcp_mod.db is not None
        assert mcp_mod._registry_retry_last_monotonic is None
        assert mcp_mod._registry_retry_last_at is None
        assert mcp_mod._registry_retry_stamped_error is None


@pytest.mark.usefixtures("reset_mcp_globals")
class TestRetryLogging:
    """A long outage must not turn the operator log into one WARNING per interval."""

    @pytest.fixture
    def retry_logger(self, monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
        logger = logging.getLogger("test.mcp.registry_retry")
        monkeypatch.setattr(mcp_mod, "_logger", logger)
        return logger

    def test_repeat_failure_with_same_cause_logs_below_warning(
        self,
        tmp_path: Path,
        declining_loomweave: tuple[str, ClarionStubState],
        retry_logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        base_url, state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)
        mcp_mod._attempt_startup(filigree_dir)

        with caplog.at_level(logging.DEBUG, logger=retry_logger.name):
            for _ in range(3):
                _rewind_retry_clock()
                assert _call("issue_list")["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert state.capability_requests == 4

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []
        retry_lines = [r for r in caplog.records if r.getMessage() == "mcp_server_registry_retry_failed"]
        assert len(retry_lines) == 3
        assert all(r.levelno == logging.INFO for r in retry_lines)
        assert retry_lines[0].args_data["cause_kind"] == "role_declined"  # type: ignore[attr-defined]

    def test_cause_change_logs_one_warning(
        self,
        tmp_path: Path,
        declining_loomweave: tuple[str, ClarionStubState],
        retry_logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        base_url, state = declining_loomweave
        filigree_dir = _make_project(tmp_path, base_url)
        mcp_mod._attempt_startup(filigree_dir)
        assert isinstance(mcp_mod._registry_startup_error, RegistryUnavailableError)
        assert mcp_mod._registry_startup_error.cause_kind == "role_declined"

        # Loomweave now demands a token Filigree does not send: the failure
        # signature changes (role_declined -> auth) and that transition is
        # worth exactly one WARNING; the identical failure afterwards is not.
        state.required_token = "secret"  # noqa: S105 — test fixture
        with caplog.at_level(logging.DEBUG, logger=retry_logger.name):
            _rewind_retry_clock()
            _call("issue_list")
            _rewind_retry_clock()
            _call("issue_list")

        assert isinstance(mcp_mod._registry_startup_error, RegistryUnavailableError)
        assert mcp_mod._registry_startup_error.cause_kind == "auth"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r.getMessage() for r in warnings] == ["mcp_server_registry_unavailable"]
        assert warnings[0].args_data["cause_kind"] == "auth"  # type: ignore[attr-defined]
        retry_lines = [r for r in caplog.records if r.getMessage() == "mcp_server_registry_retry_failed"]
        assert len(retry_lines) == 1
