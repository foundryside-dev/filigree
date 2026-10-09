"""Federation token: the published file is reconciled to the active env token.

HTTP F14 / M-6. Siblings (Wardline, Loomweave) discover Filigree's inbound
bearer by reading ``<store>/federation_token``. A daemon started with
``WEFT_FEDERATION_TOKEN`` set while that file held a different, stale value
enforced the env token and 401'd every sibling that sent the file token. At
daemon boot the file is now rewritten to the active env token, ``/api/health``
reports the auth source and whether the published file matches, and
``doctor --fix`` reconciles the same mismatch (tests/test_doctor.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import DB_FILENAME, FiligreeAnchor, FiligreeDB, write_config
from filigree.dashboard import create_app
from filigree.federation_token import (
    FEDERATION_TOKEN_FILENAME,
    WEFT_FEDERATION_ENV_VAR,
    ReconcileStatus,
    read_token_file,
    reconcile_token_file,
)

ENV_TOKEN = "env-active-token-AAAA"  # noqa: S105 — test fixture  # secret-scan: allow-this-line
STALE_TOKEN = "stale-file-token-BBBB"  # noqa: S105 — test fixture  # secret-scan: allow-this-line


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _make_store(tmp_path: Path) -> Path:
    """A minimal single-project store (``.filigree/``) with an initialised DB."""
    store = tmp_path / "proj" / ".filigree"
    store.mkdir(parents=True)
    write_config(store, {"prefix": "rec", "version": 1})
    db = FiligreeDB(store / DB_FILENAME, prefix="rec", check_same_thread=False)
    db.initialize()
    db.close()
    return store


def _token_mode(store: Path) -> int:
    return stat.S_IMODE((store / FEDERATION_TOKEN_FILENAME).stat().st_mode)


async def _probe(app: FastAPI, bearer: str) -> dict[str, Any]:
    async with _client(app) as c:
        health = await c.get("/api/health")
        with_file_token = await c.get("/api/weft/issues", params={"limit": 1}, headers={"Authorization": f"Bearer {bearer}"})
        with_stale_token = await c.get("/api/weft/issues", params={"limit": 1}, headers={"Authorization": f"Bearer {STALE_TOKEN}"})
    return {
        "health": health.json(),
        "file_token_status": with_file_token.status_code,
        "stale_token_status": with_stale_token.status_code,
    }


class TestBootReconciliation:
    def test_env_token_rewrites_stale_file_at_boot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """In-process equivalent of "restart the daemon and curl it": run the
        real ephemeral boot path (``main``) with uvicorn stubbed, then drive the
        app it would have served. The stale file is realigned to the env token,
        health reports it, and a sibling sending the *file* token gets 200."""
        store = _make_store(tmp_path)
        (store / FEDERATION_TOKEN_FILENAME).write_text(STALE_TOKEN + "\n")
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, ENV_TOKEN)
        monkeypatch.setattr(dash_module, "find_filigree_anchor", lambda: FiligreeAnchor(store.parent, None, store))
        monkeypatch.setattr(dash_module, "_idle_watchdog", lambda *_a: None)
        monkeypatch.setattr("filigree.dashboard.webbrowser.open", lambda *a, **kw: None)

        captured: dict[str, Any] = {}

        def fake_uvicorn_run(app: FastAPI, *args: object, **kwargs: object) -> None:
            # The DB is open only while "serving" — probe inside the run window.
            file_token = read_token_file(store)
            captured["file_token"] = file_token
            captured["mode"] = _token_mode(store)
            captured.update(asyncio.run(_probe(app, file_token)))

        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
        try:
            dash_module.main(port=9999, no_browser=True, server_mode=False)
        finally:
            dash_module._db = None
            dash_module._project_store = None

        # (a) the published file now holds the active env token, still 0600.
        assert captured["file_token"] == ENV_TOKEN
        assert captured["mode"] == 0o600
        # (b) health reports the source and that the published file matches.
        auth = captured["health"]["auth"]
        assert auth["mode"] == "bearer"
        assert auth["source"] == "env"
        assert auth["file_matches_active"] is True
        # (c) a sibling that reads the file and sends its contents is accepted;
        # the old stale value is (correctly) rejected.
        assert captured["file_token_status"] == 200
        assert captured["stale_token_status"] == 401

    def test_server_mode_boot_reconciles_config_dir_without_env_pin_promotion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Server-mode boot (``allow_env_pin=False``) reconciles the server config
        dir too — env → file only; it never writes the process env (F1)."""
        config_dir = tmp_path / "srvcfg"
        config_dir.mkdir()
        (config_dir / FEDERATION_TOKEN_FILENAME).write_text(STALE_TOKEN + "\n")
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, ENV_TOKEN)

        pinned = dash_module._mint_and_guard_federation_token(config_dir, allow_env_pin=False)

        assert pinned is False
        assert read_token_file(config_dir) == ENV_TOKEN
        assert _token_mode(config_dir) == 0o600


class TestHealthAuthSource:
    async def test_health_reports_auth_source_and_file_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``create_app`` stays read-only: with a stale file and no boot step it
        honestly reports the mismatch (and leaves the file alone)."""
        store = _make_store(tmp_path)
        db = FiligreeDB(store / DB_FILENAME, prefix="rec", check_same_thread=False)
        dash_module._db = db
        try:
            # env active, file stale → mismatch reported, file untouched.
            (store / FEDERATION_TOKEN_FILENAME).write_text(STALE_TOKEN + "\n")
            monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, ENV_TOKEN)
            async with _client(create_app()) as c:
                stale = (await c.get("/api/health")).json()["auth"]
            assert stale["mode"] == "bearer"
            assert stale["source"] == "env"
            assert stale["file_matches_active"] is False
            assert read_token_file(store) == STALE_TOKEN

            # env active, file aligned → match.
            (store / FEDERATION_TOKEN_FILENAME).write_text(ENV_TOKEN + "\n")
            async with _client(create_app()) as c:
                aligned = (await c.get("/api/health")).json()["auth"]
            assert aligned["source"] == "env"
            assert aligned["file_matches_active"] is True

            # no env, file token → source file (trivially matches).
            monkeypatch.delenv(WEFT_FEDERATION_ENV_VAR)
            async with _client(create_app()) as c:
                file_src = (await c.get("/api/health")).json()["auth"]
            assert file_src["mode"] == "bearer"
            assert file_src["source"] == "file"
            assert file_src["file_matches_active"] is True

            # no env, no file → auth off.
            (store / FEDERATION_TOKEN_FILENAME).unlink()
            async with _client(create_app()) as c:
                off = (await c.get("/api/health")).json()["auth"]
            assert off["mode"] == "off"
            assert off["source"] == "none"
            assert off["file_matches_active"] is True
            # Additive: the pre-existing posture keys are still present.
            assert off["federation"]["enabled"] is False
        finally:
            dash_module._db = None
            db.close()

    async def test_health_file_match_is_live_after_boot_drift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The match flag is read per request, so a file rewritten after boot to a
        value the daemon will not accept shows up as a mismatch immediately."""
        store = _make_store(tmp_path)
        (store / FEDERATION_TOKEN_FILENAME).write_text(ENV_TOKEN + "\n")
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, ENV_TOKEN)
        db = FiligreeDB(store / DB_FILENAME, prefix="rec", check_same_thread=False)
        dash_module._db = db
        try:
            app = create_app()
            async with _client(app) as c:
                before = (await c.get("/api/health")).json()["auth"]["file_matches_active"]
                (store / FEDERATION_TOKEN_FILENAME).write_text(STALE_TOKEN + "\n")
                after = (await c.get("/api/health")).json()["auth"]["file_matches_active"]
        finally:
            dash_module._db = None
            db.close()
        assert before is True
        assert after is False


class TestFileTokenWithoutEnv:
    async def test_file_token_accepted_when_no_env(self, tmp_path: Path) -> None:
        """Tier 2 unchanged: with no env token the file token is the credential."""
        store = _make_store(tmp_path)
        (store / FEDERATION_TOKEN_FILENAME).write_text("file-only-token\n")
        db = FiligreeDB(store / DB_FILENAME, prefix="rec", check_same_thread=False)
        dash_module._db = db
        try:
            async with _client(create_app()) as c:
                ok = await c.get("/api/weft/issues", params={"limit": 1}, headers={"Authorization": "Bearer file-only-token"})
                missing = await c.get("/api/weft/issues", params={"limit": 1})
        finally:
            dash_module._db = None
            db.close()
        assert ok.status_code == 200
        assert missing.status_code == 401
        # No env → nothing to reconcile; the file is untouched.
        assert read_token_file(store) == "file-only-token"


class TestReconcileTokenFile:
    def test_rewrites_differing_file_and_logs_without_token_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / FEDERATION_TOKEN_FILENAME).write_text(STALE_TOKEN + "\n")
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, ENV_TOKEN)

        with caplog.at_level(logging.INFO, logger="filigree.federation_token"):
            result = reconcile_token_file(tmp_path)

        assert result.status is ReconcileStatus.REWRITTEN
        assert result.env_name == WEFT_FEDERATION_ENV_VAR
        assert read_token_file(tmp_path) == ENV_TOKEN
        assert _token_mode(tmp_path) == 0o600
        assert not list(tmp_path.glob("*.tmp"))  # atomic publish left no temp behind

        records = [r for r in caplog.records if r.getMessage() == "token_file_reconciled"]
        assert len(records) == 1
        args_data = records[0].__dict__["args_data"]
        assert args_data["new_fingerprint"] == hashlib.sha256(ENV_TOKEN.encode()).hexdigest()[:8]
        assert args_data["old_fingerprint"] == hashlib.sha256(STALE_TOKEN.encode()).hexdigest()[:8]
        # No token value anywhere in any captured record.
        for rec in caplog.records:
            blob = repr(rec.__dict__)
            assert ENV_TOKEN not in blob
            assert STALE_TOKEN not in blob

    @pytest.mark.parametrize(
        ("env", "file", "expected"),
        [
            (None, STALE_TOKEN, ReconcileStatus.NO_ENV),
            (ENV_TOKEN, None, ReconcileStatus.NO_FILE),
            (ENV_TOKEN, ENV_TOKEN, ReconcileStatus.ALREADY_MATCHES),
        ],
    )
    def test_noop_cases_do_not_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str | None, file: str | None, expected: ReconcileStatus
    ) -> None:
        if env is not None:
            monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, env)
        if file is not None:
            (tmp_path / FEDERATION_TOKEN_FILENAME).write_text(file + "\n")

        result = reconcile_token_file(tmp_path)

        assert result.status is expected
        assert read_token_file(tmp_path) == (file or "")

    def test_write_failure_is_reported_not_raised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / FEDERATION_TOKEN_FILENAME).write_text(STALE_TOKEN + "\n")
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, ENV_TOKEN)

        def _boom(*_a: object, **_kw: object) -> tuple[int, str]:
            raise OSError("read-only file system")

        monkeypatch.setattr("filigree.federation_token.tempfile.mkstemp", _boom)

        result = reconcile_token_file(tmp_path)

        assert result.status is ReconcileStatus.WRITE_FAILED
        assert read_token_file(tmp_path) == STALE_TOKEN
