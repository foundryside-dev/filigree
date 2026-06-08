"""Regression tests for MCP startup and request context with v2.0
``.filigree.conf``-relocated databases.

Covers two bugs:

* filigree-7adcb01a60: stdio ``_run`` / ``_attempt_startup`` ignores
  ``.filigree.conf`` ``db`` and always opens ``.filigree/filigree.db``.
* filigree-8311f59c0f: HTTP ``_handle_mcp`` derives the project metadata
  directory from ``db_path.parent``, which widens the ``_safe_path()``
  sandbox by one directory for relocated layouts (``db: "track.db"``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from filigree.core import (
    CONF_FILENAME,
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    FiligreeDB,
    write_conf,
    write_config,
)
from filigree.registry import EXPECTED_LOOMWEAVE_API_VERSION
from filigree.types.api import ErrorCode
from tests._fakes.clarion_http import clarion_stub


def _make_relocated_project(tmp_path: Path) -> tuple[Path, Path]:
    """Build a v2.0 project with ``db: "track.db"`` (relocated to root).

    Returns ``(project_root, db_path)``.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    # Metadata dir still exists for logs / summary, but DB lives at the root.
    (project_root / FILIGREE_DIR_NAME).mkdir()
    write_config(project_root / FILIGREE_DIR_NAME, {"prefix": "rel", "version": 1})
    write_conf(
        project_root / CONF_FILENAME,
        {"version": 1, "project_name": "rel", "prefix": "rel", "db": "track.db"},
    )
    db_path = project_root / "track.db"
    seed = FiligreeDB(db_path, prefix="rel", project_root=project_root)
    seed.initialize()
    seed.close()
    return project_root, db_path


class TestStdioStartupHonoursConf:
    """Bug filigree-7adcb01a60: stdio MCP must respect ``.filigree.conf``."""

    def test_attempt_startup_with_conf_path_opens_relocated_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When given a conf path, ``_attempt_startup`` must open the DB
        declared by ``.filigree.conf``, not the legacy ``.filigree/filigree.db``.
        """
        import filigree.mcp_server as mcp_mod

        project_root, db_path = _make_relocated_project(tmp_path)
        filigree_dir = project_root / FILIGREE_DIR_NAME

        monkeypatch.setattr(mcp_mod, "db", None)
        monkeypatch.setattr(mcp_mod, "_filigree_dir", None)
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", None)
        monkeypatch.setattr(mcp_mod, "_db_open_error", None)

        mcp_mod._attempt_startup(filigree_dir, conf_path=project_root / CONF_FILENAME)
        try:
            assert mcp_mod._db_open_error is None, mcp_mod._db_open_error
            assert mcp_mod._schema_mismatch is None
            assert mcp_mod.db is not None
            assert mcp_mod.db.db_path == db_path.resolve()
            assert mcp_mod._filigree_dir == filigree_dir
        finally:
            if mcp_mod.db is not None:
                mcp_mod.db.close()

    def test_attempt_startup_legacy_layout_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legacy projects (no ``.filigree.conf``) must keep using
        ``from_filigree_dir`` — the fix must not regress them.
        """
        import filigree.mcp_server as mcp_mod
        from filigree.core import DB_FILENAME

        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "leg", "version": 1})
        d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="leg")
        d.initialize()
        d.close()

        monkeypatch.setattr(mcp_mod, "db", None)
        monkeypatch.setattr(mcp_mod, "_filigree_dir", None)
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", None)
        monkeypatch.setattr(mcp_mod, "_db_open_error", None)

        mcp_mod._attempt_startup(filigree_dir)
        try:
            assert mcp_mod._db_open_error is None
            assert mcp_mod.db is not None
            assert mcp_mod.db.db_path == (filigree_dir / DB_FILENAME).resolve()
        finally:
            if mcp_mod.db is not None:
                mcp_mod.db.close()

    def test_run_without_project_requires_conf_not_legacy_ancestor(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Agent stdio startup must not attach to a legacy ancestor."""
        import filigree.mcp_server as mcp_mod

        legacy_dir = tmp_path / FILIGREE_DIR_NAME
        legacy_dir.mkdir()
        d = FiligreeDB(legacy_dir / DB_FILENAME, prefix="filigree")
        d.initialize()
        d.close()
        workdir = tmp_path / "scratch" / "leaf"
        workdir.mkdir(parents=True)
        monkeypatch.chdir(workdir)

        def _forbid_startup(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("stdio startup should not open a legacy ancestor")

        monkeypatch.setattr(mcp_mod, "_attempt_startup", _forbid_startup)

        with pytest.raises(SystemExit) as excinfo:
            asyncio.run(mcp_mod._run(None))

        assert excinfo.value.code == 1
        assert ".filigree.conf" in capsys.readouterr().err

    def test_attempt_startup_records_invalid_conf_as_open_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Malformed/unsafe .filigree.conf should use the clean startup-open
        failure path instead of escaping as a raw ValueError traceback.
        """
        import filigree.mcp_server as mcp_mod

        project_root = tmp_path / "bad-conf"
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir(parents=True)
        write_config(filigree_dir, {"prefix": "bad", "version": 1})
        write_conf(
            project_root / CONF_FILENAME,
            {"version": 1, "project_name": "bad", "prefix": "bad", "db": "../outside.db"},
        )

        monkeypatch.setattr(mcp_mod, "db", None)
        monkeypatch.setattr(mcp_mod, "_filigree_dir", None)
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", None)
        monkeypatch.setattr(mcp_mod, "_db_open_error", None)

        mcp_mod._attempt_startup(filigree_dir, conf_path=project_root / CONF_FILENAME)

        assert mcp_mod.db is None
        assert mcp_mod._schema_mismatch is None
        assert isinstance(mcp_mod._db_open_error, ValueError)
        assert "must resolve under the project root" in str(mcp_mod._db_open_error)

    def test_attempt_startup_loomweave_version_mismatch_degrades_with_public_envelope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Loomweave API mismatch must not escape MCP startup as a raw RuntimeError."""
        import asyncio

        import filigree.mcp_server as mcp_mod

        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        with clarion_stub(api_version=EXPECTED_LOOMWEAVE_API_VERSION + 1) as (base_url, _state):
            write_config(
                filigree_dir,
                {
                    "prefix": "mcp",
                    "version": 1,
                    "registry_backend": "loomweave",
                    "loomweave": {"base_url": base_url, "timeout_seconds": 1},
                },
            )

            monkeypatch.setattr(mcp_mod, "db", None)
            monkeypatch.setattr(mcp_mod, "_filigree_dir", None)
            monkeypatch.setattr(mcp_mod, "_schema_mismatch", None)
            monkeypatch.setattr(mcp_mod, "_registry_startup_error", None)
            monkeypatch.setattr(mcp_mod, "_db_open_error", None)

            mcp_mod._attempt_startup(filigree_dir)

            assert mcp_mod.db is None
            assert mcp_mod._schema_mismatch is None
            assert mcp_mod._registry_startup_error is not None
            assert mcp_mod._db_open_error is None

            result = asyncio.run(mcp_mod.call_tool("issue_list", {}))
            payload = json.loads(result[0].text)
            assert payload["code"] == ErrorCode.LOOMWEAVE_REGISTRY_VERSION_MISMATCH
            assert payload["details"] == {
                "cause": "loomweave_registry_version_mismatch",
                "url": f"{base_url}/api/v1/_capabilities",
                "expected": EXPECTED_LOOMWEAVE_API_VERSION,
                "advertised": EXPECTED_LOOMWEAVE_API_VERSION + 1,
            }

            status_result = asyncio.run(mcp_mod.call_tool("mcp_status_get", {}))
            status_payload = json.loads(status_result[0].text)
            assert status_payload["status"] == "registry_version_mismatch"
            assert status_payload["code"] == ErrorCode.LOOMWEAVE_REGISTRY_VERSION_MISMATCH


class TestRequestFiligreeDirSandbox:
    """Bug filigree-8311f59c0f: HTTP request dir must equal project_root/.filigree."""

    def test_resolve_request_dir_uses_project_root_for_relocated_db(self, tmp_path: Path) -> None:
        """For a conf-built DB with ``project_root`` set, the helper must
        return ``project_root/.filigree``, NOT ``db_path.parent``.

        With the buggy ``db_path.parent`` derivation, ``_safe_path()`` ends
        up using the project's parent directory as its sandbox base.
        """
        from filigree.mcp_server import _resolve_request_filigree_dir

        project_root, _ = _make_relocated_project(tmp_path)
        db = FiligreeDB.from_conf(project_root / CONF_FILENAME, check_same_thread=False)
        try:
            resolved = _resolve_request_filigree_dir(db)
            # The metadata dir, not the DB's parent.
            assert resolved == project_root.resolve() / FILIGREE_DIR_NAME
            # Specifically, NOT the project root itself (which is what
            # db_path.parent would yield for ``db: "track.db"``).
            assert resolved != project_root.resolve()
        finally:
            db.close()

    def test_resolve_request_dir_legacy_layout_unchanged(self, tmp_path: Path) -> None:
        """Legacy ``.filigree/filigree.db`` projects must still resolve to
        ``.filigree/`` so the sandbox base remains ``project_root``.
        """
        from filigree.mcp_server import _resolve_request_filigree_dir

        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "leg", "version": 1})
        db = FiligreeDB.from_filigree_dir(filigree_dir, check_same_thread=False)
        try:
            resolved = _resolve_request_filigree_dir(db)
            assert resolved == filigree_dir.resolve()
            # And db_path.parent happens to equal filigree_dir too — confirm
            # the legacy path keeps producing the same answer either way.
            assert resolved == db.db_path.parent
        finally:
            db.close()

    def test_safe_path_rejects_sibling_escape_for_relocated_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: with the request context set up via the resolver,
        ``_safe_path("../sibling/...")`` must raise — proving the sandbox
        boundary is the project root, not its parent.
        """
        import filigree.mcp_server as mcp_mod

        project_root, _ = _make_relocated_project(tmp_path)
        # Create a "sibling" file alongside the project root. Without the
        # fix, ``_safe_path`` would accept this path because the sandbox
        # base widens to ``project_root.parent``.
        sibling = tmp_path / "sibling.jsonl"
        sibling.write_text("[]")

        db = FiligreeDB.from_conf(project_root / CONF_FILENAME, check_same_thread=False)
        try:
            db_token = mcp_mod._request_db.set(db)
            dir_token = mcp_mod._request_filigree_dir.set(mcp_mod._resolve_request_filigree_dir(db))
            try:
                with pytest.raises(ValueError, match="escapes"):
                    mcp_mod._safe_path("../sibling.jsonl")
            finally:
                mcp_mod._request_filigree_dir.reset(dir_token)
                mcp_mod._request_db.reset(db_token)
        finally:
            db.close()
