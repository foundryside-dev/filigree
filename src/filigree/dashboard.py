"""Web dashboard for filigree — interactive project management UI.

Full-featured local web server: kanban board, dependency graph, metrics,
activity feed, workflow visualization. Supports issue management (create,
update, close, reopen, claim, dependency management), batch operations,
and real-time auto-refresh.

**Ethereal mode** (default): single-project.  A module-level ``_db`` is
set at startup and injected via ``Depends(_get_db)``.

**Server mode** (``--server-mode``): multi-project.  A ``ProjectStore``
reads ``server.json``, manages per-project ``FiligreeDB`` connections,
and resolves the active project via a ``ContextVar`` set by middleware.

Usage:
    filigree dashboard                    # Opens browser at localhost:8377
    filigree dashboard --port 9000        # Custom port
    filigree dashboard --no-browser       # Skip auto-open
    filigree dashboard --server-mode      # Multi-project server mode
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import webbrowser
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import APIRouter
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response
    from starlette.types import ASGIApp, Receive, Scope, Send

from starlette.requests import Request

from filigree import __version__
from filigree.core import (
    CONF_FILENAME,
    CONFIG_FILENAME,
    FiligreeDB,
    ProjectNotInitialisedError,
    find_filigree_anchor,
    read_config,
)

# Re-export so test imports continue to work.
from filigree.dashboard_routes.common import _safe_bounded_int as _safe_bounded_int
from filigree.install_support.version_marker import format_schema_mismatch_guidance
from filigree.types.api import SchemaVersionMismatchError

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8377
# Canonical inbound federation bearer env var (3.0.0). Gates Filigree's own
# /api/weft/* + /mcp HTTP surface. Federation plumbing → Weft prefix. The two
# FILIGREE_*_API_TOKEN names are deprecated aliases read as a soft fallback so
# existing exports keep working; scheduled for removal post-1.0. (Distinct from
# the OUTBOUND registry token WEFT_TOKEN in registry.py.)
WEFT_FEDERATION_ENV_VAR = "WEFT_FEDERATION_TOKEN"
FEDERATION_API_ENV_VAR = "FILIGREE_FEDERATION_API_TOKEN"  # deprecated alias
LEGACY_API_ENV_VAR = "FILIGREE_API_TOKEN"  # deprecated alias
# Read order: canonical first, then deprecated aliases.
FEDERATION_TOKEN_ENV_VARS = (WEFT_FEDERATION_ENV_VAR, FEDERATION_API_ENV_VAR, LEGACY_API_ENV_VAR)

logger = logging.getLogger(__name__)

_EXPECTED_PROJECT_CONFIG_ERRORS = (ProjectNotInitialisedError, ValueError, TypeError, KeyError)

# ---------------------------------------------------------------------------
# Module-level state — set by main() or test fixtures
# ---------------------------------------------------------------------------

_db: FiligreeDB | None = None
_config: dict[str, Any] = {}
_DASHBOARD_STATE_ATTR = "filigree_dashboard_state"


def _resolve_federation_api_token(store_dir: Path | None = None) -> tuple[str, str | None]:
    """Resolve the bearer token for the federation/MCP HTTP surfaces (read-only).

    3-tier: ``$WEFT_FEDERATION_TOKEN`` (+ deprecated aliases) → the daemon's
    minted ``<store_dir>/federation_token`` → off. *store_dir* is the served
    project's store dir (ethereal) or ``~/.config/filigree/`` (server mode); the
    file is minted at daemon boot (see :func:`run`), never here, so this stays a
    pure read. Returns ``(token, source)`` where *source* is the env-var name,
    the file-source label, or ``None``.
    """
    from filigree.federation_token import resolve_federation_token

    return resolve_federation_token(store_dir)


def _mint_and_guard_federation_token(mint_dir: Path, *, allow_env_pin: bool) -> bool:
    """Mint the daemon's federation token and guard against a *silently-open* serve.

    Fail-LOUD, not silently-open. :func:`mint_token_file` is best-effort: a write
    failure (read-only mount, full disk) logs quietly and returns the in-memory
    token — which :func:`run` would otherwise DISCARD, leaving ``create_app`` to
    re-resolve from the now-absent file → tier-3 → federation auth silently OFF on
    the (loopback) bind, exactly when the operator expects it ON. Either way the
    failure is now made loud (stderr *and* the structured logger launchers tail).

    *allow_env_pin* gates the in-memory tier-1 fallback, and is the ETHEREAL
    single-project posture only. There, on a persist failure with no operator env
    token, pinning the daemon's own token into the process env (tier 1) keeps the
    expected posture — that daemon serves exactly one project, so a process-scoped
    pin is correct and bounded. In SERVER mode it MUST be ``False``: the
    home/server token must never become a tier-1 env pin, which
    :func:`dashboard_auth.build_auth_middleware` would accept across EVERY project
    scope, breaking the per-project scoping invariant (F1 / filigree-23574069a1).
    Server mode therefore warns loudly but fails open on its unscoped surface
    rather than minting cross-project authority (per-project scoped tokens are
    resolved independently and are unaffected). Availability/observability, not
    hardening (C-8): a present env token already wins, and a persisted mint is
    untouched (no warning, no env mutation).

    Returns ``True`` iff it pinned the env var, so the caller can unset it in its
    ``finally`` for in-process cleanup symmetry.
    """
    from filigree.federation_token import (
        WEFT_FEDERATION_ENV_VAR,
        mint_token_file,
        read_env_token,
        read_token_file,
    )

    minted = mint_token_file(mint_dir)
    env_tok, _ = read_env_token()
    if not (minted and not env_tok and read_token_file(mint_dir) != minted):
        return False
    if allow_env_pin:
        posture = (
            "Federation auth will use an in-memory token for THIS daemon only; same-host siblings "
            "cannot read it and will get 401 until the store dir is writable."
        )
    else:
        posture = (
            "Federation auth on the daemon's unscoped surface is OFF until the store dir is writable "
            "(per-project scoped tokens are unaffected). The token is NOT promoted to a cross-project "
            "credential in server mode."
        )
    msg = (
        f"could not persist the federation token to {mint_dir}/federation_token. {posture} "
        "Fix the mount/permissions and restart, or export WEFT_FEDERATION_TOKEN."
    )
    # Dual-emit (stderr + structured log) so a launcher capturing only the
    # filigree log still sees it — mirroring run()'s schema-mismatch branch.
    print(f"WARNING: {msg}", file=sys.stderr)
    logger.warning("federation_token_persist_failed: %s", msg)
    if allow_env_pin:
        os.environ[WEFT_FEDERATION_ENV_VAR] = minted
        return True
    return False


def _dashboard_auth_scope(*, federation_enabled: bool, token_env: str | None) -> dict[str, Any]:
    return {
        "federation": {
            "enabled": federation_enabled,
            "token_env": token_env,
            "protected_paths": ["/api/weft/*", "/api/scan-results", "/api/observations", "/api/v1/scan-results", "/api/v1/observations"],
        },
        "mcp_http": {
            "enabled": federation_enabled,
            "token_env": token_env,
            "protected_paths": ["/mcp", "/mcp/*"],
        },
        "classic_api": {
            "enabled": False,
            "note": "Classic dashboard API routes remain open; federation scanner aliases are listed under federation.",
        },
        "dashboard_ui": {
            "enabled": False,
            "note": "The local dashboard UI remains open under the loopback trust boundary.",
        },
        # ADR-012 actor-verification posture for the HTTP transport. Writes via any
        # dashboard/loom HTTP route are unverified: the `actor` field is a
        # self-asserted claim and verified_actor/verified_author land NULL (only
        # CLI / MCP-stdio stamp a transport-verified actor). Surfaced here — the
        # canonical posture surface — so the dropped verification is discoverable
        # instead of silent. The federation token gates *access*, not *identity*
        # (C-8); transport-bound identity verification is deferred (filigree-81d3971467).
        "actor_verification": {
            "verified": False,
            "deferral": "filigree-81d3971467",
            "note": (
                "HTTP writes are unverified: 'actor' is an unauthenticated self-asserted claim and "
                "verified_actor/verified_author are NULL. Only CLI and MCP-stdio stamp a transport-verified "
                "actor. Transport-bound identity verification for HTTP is deferred to filigree-81d3971467."
            ),
        },
    }


@dataclass
class DashboardState:
    """Per-ASGI-app dashboard runtime state.

    The module globals remain as startup/test compatibility inputs, but each
    app captures its own state object so multiple apps in one process do not
    route requests through whichever global was assigned most recently.
    """

    db: FiligreeDB | None = None
    config: dict[str, Any] = field(default_factory=dict)
    project_store: ProjectStore | None = None
    allow_http_force_close: bool = False
    current_project_key: ContextVar[str] = field(default_factory=lambda: ContextVar("project_key", default=""))


# 2.1.0 §1.1: opt-in gate for accepting ``force=true`` on HTTP batch-close
# routes. Default-off — HTTP callers can't bypass the workflow validator
# without the operator explicitly starting the dashboard with
# ``--allow-http-force-close``. Toggled by ``main()`` at process startup.
_allow_http_force_close: bool = False


def _state_from_request(request: Request | None) -> DashboardState | None:
    if request is None:
        return None
    state = getattr(request.app.state, _DASHBOARD_STATE_ATTR, None)
    return state if isinstance(state, DashboardState) else None


def _get_allow_http_force_close(request: Request | None = None) -> bool:
    """Accessor so route handlers read the current flag at request time.

    A plain ``from filigree.dashboard import _allow_http_force_close``
    would bind the bool by value at import; tests and ``main()`` both
    mutate this module attribute, so the routes need a function-level
    read instead.
    """
    state = _state_from_request(request)
    if state is not None:
        return state.allow_http_force_close or _allow_http_force_close
    return _allow_http_force_close


# Idle auto-shutdown for ethereal mode (seconds)
IDLE_TIMEOUT_SECONDS = 3600  # 1 hour
IDLE_CHECK_INTERVAL = 60  # check every minute
_last_request_time: float = 0.0  # monotonic clock; set at startup

# Server mode: per-request project key set by middleware
_current_project_key: ContextVar[str] = ContextVar("project_key", default="")


def _open_db_for_filigree_dir(
    filigree_dir: Path,
    *,
    check_same_thread: bool = True,
    allow_local_fallback_override: bool | None = None,
) -> FiligreeDB:
    """Open the project DB for *filigree_dir*, honouring ``.filigree.conf``.

    Mirrors the canonical CLI pattern (``cli_common._build_db``): when a
    ``.filigree.conf`` sits next to the directory, use ``FiligreeDB.from_conf``
    so a relocated ``db`` field is honoured (e.g. ``db = "storage/track.db"``).
    Fall back to ``from_filigree_dir`` for legacy installs without a conf.
    Without this, the dashboard silently opened ``.filigree/filigree.db`` while
    the CLI/MCP — which goes through ``cli_common.py`` — opened the conf-
    declared path, producing a split-brain view. (filigree-da8d5aba0f)

    ``allow_local_fallback_override`` is forwarded so the dashboard's
    ``--allow-local-fallback`` flag flows into the ADR-014 capability probe
    *before* it runs at ``FiligreeDB.__init__`` — otherwise a project whose
    config disables fallback would fail to construct against an offline
    Loomweave even though the operator just asked for fallback at startup.
    """
    # *filigree_dir* is a resolved store dir (legacy ``.filigree/``, federation
    # ``.weft/filigree/``, or an arbitrary-depth weft.toml ``store_dir`` override).
    # The conf anchor sits at the PROJECT ROOT, which is NOT recoverable from the
    # store dir alone for a multi-segment override — reverse-deriving by stripping
    # segments yields the wrong root. Resolve through the canonical anchor walk
    # (which reads weft.toml) so the project root is correct for every layout (I2).
    project_root = find_filigree_anchor(filigree_dir).project_root
    conf_path = project_root / CONF_FILENAME
    if conf_path.is_file():
        return FiligreeDB.from_conf(
            conf_path,
            store_dir=filigree_dir,
            check_same_thread=check_same_thread,
            allow_local_fallback_override=allow_local_fallback_override,
        )
    return FiligreeDB.from_store_dir(
        filigree_dir,
        project_root=project_root,
        check_same_thread=check_same_thread,
        allow_local_fallback_override=allow_local_fallback_override,
    )


def _read_project_display_name(filigree_dir: Path, prefix: str) -> str:
    """Read only the optional display name, without validating the project."""
    config_path = filigree_dir / CONFIG_FILENAME
    if not config_path.exists():
        return prefix
    try:
        raw = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        logger.warning("Failed to read %s for dashboard display name; using prefix", config_path, exc_info=True)
        return prefix
    if not isinstance(raw, dict):
        return prefix
    name = raw.get("name")
    return name if isinstance(name, str) and name else prefix


class ProjectStore:
    """Manages multiple FiligreeDB connections for server mode.

    Reads ``server.json`` via :func:`read_server_config`, maps project
    prefixes to ``.filigree/`` paths, and lazily opens DB connections.
    """

    def __init__(self) -> None:
        self._projects: dict[str, dict[str, str]] = {}  # key -> {name, path}
        self._dbs: dict[str, FiligreeDB] = {}
        # Handles evicted by reload() (removed/path-changed projects). They are
        # NOT closed at eviction time because a concurrent request handler may
        # still be using one. A short grace-period drain bounds long-lived
        # server processes without closing under the request that just lost
        # the cache race.
        self._evicted_dbs: list[FiligreeDB] = []
        self._evicted_at: dict[FiligreeDB, float] = {}
        self._evicted_close_grace_seconds = 60.0
        # Serialises reads and writes of (_projects, _dbs, _evicted_dbs):
        # - get_db cache lookup/publish and project-map snapshots
        #   (filigree-e43edbc067: removed unlocked fast path so a reader can
        #   never hand out a handle that reload just popped).
        # - reload's atomic state swap (filigree-e43edbc067).
        # - close_all drain.
        self._lock = threading.Lock()
        # Per-project open locks keep concurrent first opens for the same key
        # serialized without making unrelated projects wait behind slow DB
        # initialization.
        self._db_open_locks: dict[str, Any] = {}

    def _pop_drainable_evicted_locked(self, *, force: bool = False) -> list[FiligreeDB]:
        now = time.monotonic()
        drainable: list[FiligreeDB] = []
        retained: list[FiligreeDB] = []
        for db in self._evicted_dbs:
            evicted_at = self._evicted_at.get(db, now)
            if force or now - evicted_at >= self._evicted_close_grace_seconds:
                drainable.append(db)
                self._evicted_at.pop(db, None)
            else:
                retained.append(db)
        self._evicted_dbs = retained
        return drainable

    def _close_evicted_handles(self, handles: list[FiligreeDB]) -> None:
        for db in handles:
            try:
                db.close()
            except Exception:
                logger.warning("Error closing evicted project DB", exc_info=True)

    def _drain_evicted_dbs(self) -> None:
        with self._lock:
            drainable = self._pop_drainable_evicted_locked()
        self._close_evicted_handles(drainable)

    # -- public API --

    def _compute_projects(self) -> dict[str, dict[str, str]]:
        """Read server.json and return a fresh project map.

        Pure: never assigns to self. ``load()`` and ``reload()`` use this to
        decouple "build the new map" (slow, can fail) from the atomic state
        swap. Skips directories that don't exist (logs warning). Raises
        ``ValueError`` on corrupt JSON or prefix collision so ``reload()`` can
        retain existing state.
        """
        from filigree.server import SERVER_CONFIG_FILE, read_server_config

        # Fail fast on corrupt JSON so reload() can retain current state.
        if SERVER_CONFIG_FILE.exists():
            try:
                raw = SERVER_CONFIG_FILE.read_text()
                parsed = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Corrupt server config {SERVER_CONFIG_FILE}: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"Corrupt server config {SERVER_CONFIG_FILE}: expected JSON object")
            projects_node = parsed.get("projects", {})
            if not isinstance(projects_node, dict):
                raise ValueError(f"Corrupt server config {SERVER_CONFIG_FILE}: 'projects' must be an object")
            for project_path, meta in projects_node.items():
                if not isinstance(meta, dict):
                    raise ValueError(f"Corrupt server config {SERVER_CONFIG_FILE}: project entry for {project_path!r} must be an object")
                prefix = meta.get("prefix")
                if not isinstance(prefix, str):
                    raise ValueError(f"Corrupt server config {SERVER_CONFIG_FILE}: project prefix for {project_path!r} must be a string")

        config = read_server_config()
        projects: dict[str, dict[str, str]] = {}
        for filigree_path_str, meta in config.projects.items():
            filigree_path = Path(filigree_path_str)
            if not filigree_path.is_dir():
                logger.warning("Skipping registered project (dir missing): %s", filigree_path)
                continue
            prefix = meta.get("prefix", "filigree")
            if prefix in projects:
                existing = projects[prefix]["path"]
                raise ValueError(f"Prefix collision: {prefix!r} claimed by both {existing} and {filigree_path_str}")
            display_name = _read_project_display_name(filigree_path, prefix)
            projects[prefix] = {"name": display_name, "path": filigree_path_str}
        return projects

    def load(self) -> None:
        """Read server.json and populate the project map.

        Skips directories that don't exist (logs warning).
        Raises ``ValueError`` on prefix collision or corrupt JSON.
        """
        new_projects = self._compute_projects()
        with self._lock:
            self._projects = new_projects

    def get_db(self, key: str) -> FiligreeDB:
        """Return (lazily opening) the DB for *key*. Raises ``KeyError``.

        The global lock guards membership checks, cache lookup/publish, and
        reload's atomic state swap. Slow DB initialization happens outside
        that global lock, behind a per-project open lock, so unrelated project
        requests are not serialized behind the first open for another key.
        Before publishing a newly opened DB, the current project-map path is
        rechecked so reload cannot produce a torn ``(_projects, _dbs)`` pair.
        (filigree-e43edbc067, filigree-732f6b31e4)
        """
        while True:
            self._drain_evicted_dbs()
            with self._lock:
                info = self._projects.get(key)
                if info is None:
                    raise KeyError(key)
                cached = self._dbs.get(key)
                if cached is not None:
                    return cached
                open_lock = self._db_open_locks.get(key)
                if open_lock is None:
                    open_lock = threading.Lock()
                    self._db_open_locks[key] = open_lock

            with open_lock:
                with self._lock:
                    info = self._projects.get(key)
                    if info is None:
                        raise KeyError(key)
                    cached = self._dbs.get(key)
                    if cached is not None:
                        return cached
                    filigree_path = Path(info["path"])

                db: FiligreeDB | None = None
                try:
                    db = _open_db_for_filigree_dir(filigree_path, check_same_thread=False)
                except SchemaVersionMismatchError as exc:
                    # Operator-visible expected condition (project DB written by a
                    # newer filigree); log at WARNING and re-raise so the FastAPI
                    # exception handler converts it to a 409 SCHEMA_MISMATCH for
                    # this project only — other projects in the server keep
                    # working.
                    logger.warning(
                        "Project DB schema mismatch for key=%r path=%s: installed=v%d database=v%d",
                        key,
                        filigree_path,
                        exc.installed,
                        exc.database,
                    )
                    if db is not None:
                        db.close()
                    raise
                except _EXPECTED_PROJECT_CONFIG_ERRORS as exc:
                    logger.warning("Invalid project configuration for key=%r path=%s: %s", key, filigree_path, exc)
                    if db is not None:
                        db.close()
                    raise
                except Exception:
                    logger.error("Failed to open project DB for key=%r path=%s", key, filigree_path, exc_info=True)
                    if db is not None:
                        db.close()
                    raise

                if db is None:  # pragma: no cover - _open_db_for_filigree_dir returns or raises
                    msg = f"Project DB open returned no handle for key={key!r} path={filigree_path}"
                    raise RuntimeError(msg)
                cached_after_open: FiligreeDB | None = None
                stale_open = False
                missing_after_open = False
                with self._lock:
                    current_info = self._projects.get(key)
                    if current_info is None:
                        missing_after_open = True
                    elif current_info.get("path") != str(filigree_path):
                        stale_open = True
                    else:
                        cached_after_open = self._dbs.get(key)
                        if cached_after_open is None:
                            self._dbs[key] = db
                            return db

                db.close()
                if cached_after_open is not None:
                    return cached_after_open
                if missing_after_open:
                    raise KeyError(key)
                if stale_open:
                    continue

    def list_projects(self) -> list[dict[str, str]]:
        """Return ``[{key, name, path}]`` for the frontend."""
        self._drain_evicted_dbs()
        with self._lock:
            return [{"key": k, **v} for k, v in self._projects.items()]

    def reload(self) -> dict[str, Any]:
        """Re-read server.json. On read failure, retains existing state.

        Atomic: builds the new project map locally, then under one lock
        acquisition (a) drains older evicted handles, (b) swaps ``_projects``,
        and (c) evicts stale ``_dbs`` entries. Newly evicted handles get a
        short grace period before later runtime calls close them.
        """
        try:
            new_projects = self._compute_projects()
        except Exception as exc:
            logger.error("Failed to reload server.json — retaining existing state", exc_info=True)
            return {"added": [], "removed": [], "error": str(exc)}

        with self._lock:
            drainable = self._pop_drainable_evicted_locked()
            old_projects = self._projects
            old_keys = set(old_projects)
            new_keys = set(new_projects)
            removed = sorted(old_keys - new_keys)
            path_changed = sorted(key for key in (old_keys & new_keys) if old_projects[key].get("path") != new_projects[key].get("path"))
            self._projects = new_projects
            for key in [*removed, *path_changed]:
                handle = self._dbs.pop(key, None)
                if handle is not None:
                    self._evicted_dbs.append(handle)
                    self._evicted_at[handle] = time.monotonic()
        self._close_evicted_handles(drainable)

        return {
            "added": sorted(new_keys - old_keys),
            "removed": removed,
            "error": "",
        }

    def close_all(self) -> None:
        """Close all open DB connections, including handles previously
        evicted by ``reload()``.

        Shutdown drain for SQLite handles managed by the store. Runtime calls
        also drain evicted handles after a grace period.
        """
        with self._lock:
            handles: list[tuple[str, FiligreeDB]] = list(self._dbs.items())
            evicted = self._pop_drainable_evicted_locked(force=True)
            self._dbs.clear()
            self._evicted_at.clear()
        for key, db in handles:
            try:
                db.close()
            except Exception:
                logger.warning("Error closing DB for project %s", key, exc_info=True)
        for db in evicted:
            try:
                db.close()
            except Exception:
                logger.warning("Error closing evicted project DB", exc_info=True)

    @property
    def default_key(self) -> str:
        """First loaded project's key, or ``""`` if empty."""
        with self._lock:
            if not self._projects:
                return ""
            return next(iter(self._projects))

    def store_dir_for(self, key: str) -> Path | None:
        """Return the CANONICAL store dir for *key* (where its federation token
        lives), or ``None`` if unknown.

        The per-project federation token lives in the *canonical* store dir, which
        after WEFT consolidation is ``.weft/filigree/`` — NOT necessarily the
        ``.filigree/`` path registered in ``server.json``. Resolve it through the
        project anchor exactly as ``get_db`` opens the DB, so the auth middleware
        reads the token from the same store the DB lives in. Reading the registered
        legacy path directly silently broke per-project token auth for any project
        whose token only lives in the consolidated store — the F1 resolver
        (filigree-23574069a1) predated consolidation (follow-up: weft-…).
        """
        with self._lock:
            info = self._projects.get(key)
        if info is None:
            return None
        registered = Path(info["path"])
        # find_filigree_anchor does filesystem I/O — resolve OUTSIDE the lock.
        try:
            return find_filigree_anchor(registered).store_dir
        except Exception:
            logger.warning("store_dir_for(%s): anchor resolution failed; using registered path", key, exc_info=True)
            return registered


_project_store: ProjectStore | None = None


def _get_db(request: Request) -> FiligreeDB:
    """Return the active database connection.

    In server mode (``DashboardState.project_store`` set): resolves the project
    from the app-local per-request ContextVar.  Falls back to ``default_key``
    when the var is empty (un-prefixed ``/api/`` route).

    In ethereal mode: returns the app-local single-project DB captured by
    ``create_app``.
    """
    from fastapi import HTTPException

    from filigree.types.api import ErrorCode

    state = _state_from_request(request)
    if state is None:
        state = DashboardState(db=_db, config=_config, project_store=_project_store, allow_http_force_close=_allow_http_force_close)

    if state.project_store is not None:
        key = state.current_project_key.get() or state.project_store.default_key
        if not key:
            raise HTTPException(status_code=503, detail="No projects registered")
        try:
            db = state.project_store.get_db(key)
            db._dashboard_server_mode = True  # type: ignore[attr-defined]
            return db
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {key!r}") from None
        except SchemaVersionMismatchError:
            raise
        except (ProjectNotInitialisedError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Invalid project configuration for {key!r}: {exc}",
                    "code": ErrorCode.VALIDATION,
                },
            ) from None
    if state.db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    state.db._dashboard_server_mode = False  # type: ignore[attr-defined]
    return state.db


# ---------------------------------------------------------------------------
# Project-scoped router — all issue, workflow, and file endpoints
# ---------------------------------------------------------------------------


def _create_project_router() -> APIRouter:
    """Build the APIRouter containing all project-scoped endpoints.

    Composes two named API generations per ADR-002:

    - **classic** — every currently-existing endpoint at its existing
      path (mostly unprefixed, with the ``POST /v1/scan-results``
      outlier). Frozen; no URL moves, no shape changes.
    - **loom** — new in 2.0, attached under a ``/loom`` sub-prefix so
      the full path becomes ``/api/weft/<endpoint>`` after the
      app-level ``/api`` prefix. Empty in Phase B of the federation
      work package; Phase C fills it endpoint-by-endpoint.
    - **living surface** — un-prefixed ``/api/<endpoint>`` aliases of
      the current recommended generation (loom as of 2026-04-26), per
      ``docs/federation/contracts.md``. Added per-endpoint in Phase C
      where the path does not collide with classic. Each module
      contributes only the aliases it owns; only ``files`` participates
      in Phase C1.

    Server-mode and ethereal-mode ``/api`` mounts (and the
    ``/api/p/{project_key}`` server-mode mount) both include this
    router, so the generation split is inherited by every mount point
    automatically.
    """
    from fastapi import APIRouter

    from filigree.dashboard_routes import analytics, entities, files, issues, releases

    router = APIRouter()

    # Classic generation — existing routes at their existing paths.
    router.include_router(analytics.create_classic_router())
    router.include_router(issues.create_classic_router())
    router.include_router(files.create_classic_router())
    router.include_router(releases.create_classic_router())
    router.include_router(entities.create_classic_router())

    # Weft generation — new in 2.0 under /weft. Empty in Phase B.
    router.include_router(analytics.create_weft_router(), prefix="/weft")
    router.include_router(issues.create_weft_router(), prefix="/weft")
    router.include_router(files.create_weft_router(), prefix="/weft")
    router.include_router(releases.create_weft_router(), prefix="/weft")

    # Living surface — un-prefixed loom aliases; per-endpoint adoption.
    router.include_router(files.create_living_surface_router())
    router.include_router(analytics.create_living_surface_router())

    return router


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(*, server_mode: bool = False) -> ASGIApp:
    """Create the FastAPI application with all dashboard endpoints.

    When *server_mode* is ``True`` the app serves multiple projects via
    ``_project_store`` and adds ``/api/p/{key}/…`` routing + management
    endpoints.  Otherwise (ethereal mode) it behaves as a single-project
    dashboard backed by the module-level ``_db``.
    """
    import contextlib
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    from filigree.types.api import ErrorCode

    dashboard_state = DashboardState(
        db=_db,
        config=dict(_config),
        project_store=_project_store,
        allow_http_force_close=_allow_http_force_close,
    )

    # Resolve federation auth before MCP setup so the high-privilege
    # streamable-HTTP transport is only mounted when it can be protected. Tier-2
    # (the minted file) lives in the daemon's own store: the served project's
    # store dir (ethereal) or the server config dir (server mode). Read-only here
    # — the file is minted at daemon boot (run()), so create_app (which tests
    # invoke directly) never writes.
    if server_mode:
        from filigree.server import SERVER_CONFIG_DIR

        _token_store_dir: Path | None = SERVER_CONFIG_DIR
    else:
        _token_store_dir = dashboard_state.db.meta_dir if dashboard_state.db is not None else None
    _api_token, _api_token_env = _resolve_federation_api_token(_token_store_dir)

    # --- MCP streamable-HTTP setup (optional, token-protected only) ---
    _mcp_handler: ASGIApp | None = None
    _mcp_lifespan_factory: Callable[..., Any] | None = None
    if _api_token:
        try:
            from filigree.mcp_server import create_mcp_app

            if server_mode:
                # Closure reads ContextVar — no changes to mcp_server.py needed
                def _server_db_resolver() -> FiligreeDB | None:
                    if dashboard_state.project_store is None:
                        return None
                    key = dashboard_state.current_project_key.get() or dashboard_state.project_store.default_key
                    if not key:
                        return None
                    return dashboard_state.project_store.get_db(key)

                _mcp_handler, _mcp_lifespan_factory = create_mcp_app(db_resolver=_server_db_resolver)
            else:
                _mcp_handler, _mcp_lifespan_factory = create_mcp_app(db_resolver=lambda: dashboard_state.db)
        except ImportError:
            logger.debug("MCP streamable-HTTP not available (SDK not installed or import error)", exc_info=True)
    else:
        logger.debug("MCP streamable-HTTP endpoint disabled because federation bearer auth is not configured")

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        if _mcp_lifespan_factory is not None:
            async with _mcp_lifespan_factory():
                yield
        else:
            yield

    app = FastAPI(title="Filigree Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)
    setattr(app.state, _DASHBOARD_STATE_ATTR, dashboard_state)

    # HTTPException handler — rewrite FastAPI's default ``{"detail": "..."}``
    # to the 2.0 flat envelope ``{"error", "code", ...}``. Maps HTTP status
    # codes to ErrorCode members; preserves any explicit ``{"error","code"}``
    # detail dict a route may pass.
    from starlette.exceptions import HTTPException as _StarletteHTTPException

    _status_to_errorcode: dict[int, ErrorCode] = {
        400: ErrorCode.VALIDATION,
        401: ErrorCode.PERMISSION,
        403: ErrorCode.PERMISSION,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        422: ErrorCode.VALIDATION,
        500: ErrorCode.INTERNAL,
        503: ErrorCode.NOT_INITIALIZED,
    }

    @app.exception_handler(SchemaVersionMismatchError)
    async def _schema_mismatch_to_envelope(_request: Any, exc: SchemaVersionMismatchError) -> JSONResponse:
        # 409 Conflict — the request can't be served until the version
        # mismatch is resolved (upgrade filigree or use a matching project).
        # Server-mode: only the bad project's requests get this; others
        # continue serving normally.
        return JSONResponse(
            {
                "error": format_schema_mismatch_guidance(exc.installed, exc.database),
                "code": ErrorCode.SCHEMA_MISMATCH,
            },
            status_code=409,
        )

    @app.exception_handler(_StarletteHTTPException)
    async def _http_exception_to_envelope(_request: Any, exc: _StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail and "code" in detail:
            body: dict[str, Any] = dict(detail)
        else:
            code = _status_to_errorcode.get(exc.status_code)
            if code is None:
                # An unmapped status reaching this handler means either a new
                # Starlette/FastAPI status or a route raising an unusual code.
                # Log so it's discoverable rather than silently coerced to
                # INTERNAL — clients branching on ``code`` deserve to know.
                logger.warning(
                    "HTTPException with unmapped status_code=%s; using generic client/server error code",
                    exc.status_code,
                )
                code = ErrorCode.VALIDATION if 400 <= exc.status_code < 500 else ErrorCode.INTERNAL
            body = {
                "error": str(detail) if detail is not None else "Request failed",
                "code": code,
            }
        return JSONResponse(body, status_code=exc.status_code)

    # CORS — restrict to localhost origins only (this is a local dev tool)
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Opt-in bearer-token auth for the loom federation surface (ADR-018).
    # Active only when WEFT_FEDERATION_TOKEN (or a deprecated FILIGREE_*_API_TOKEN
    # alias) is set; otherwise the middleware is not installed for
    # loom routes. The MCP HTTP transport is never mounted without this token.
    # Added after CORS so CORS remains inner and still decorates
    # classic/dashboard responses; loom OPTIONS preflight passes through.
    app.state.auth_scope = _dashboard_auth_scope(federation_enabled=bool(_api_token), token_env=_api_token_env)
    if _api_token:
        from filigree.dashboard_auth import build_auth_middleware
        from filigree.federation_token import FEDERATION_TOKEN_ENV_VARS, read_token_file

        # Tier-1 operator pin: the daemon token counts as a cross-project pin only
        # when it was resolved from the environment, not from the home-store file.
        # A home-store file token is NOT a valid credential for a project-scoped
        # request (filigree-23574069a1) — only that project's own token or an env
        # pin is.
        _env_pin = _api_token if _api_token_env in FEDERATION_TOKEN_ENV_VARS else ""

        _resolver: Callable[[str], str] | None = None
        if server_mode:

            def _resolve_project_token(key: str) -> str:
                store = dashboard_state.project_store
                if store is None:
                    return ""
                store_dir = store.store_dir_for(key)
                return read_token_file(store_dir) if store_dir is not None else ""

            _resolver = _resolve_project_token

        app.add_middleware(
            build_auth_middleware(_api_token, env_pin=_env_pin, project_token_resolver=_resolver),
        )
        logger.info(
            "federation bearer auth enabled via %s; dashboard UI and classic API routes remain open",
            _api_token_env,
        )

    # Idle-tracking middleware (ethereal mode only — server mode runs indefinitely)
    if not server_mode:
        from starlette.middleware.base import BaseHTTPMiddleware

        class IdleTrackingMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
                global _last_request_time
                _last_request_time = time.monotonic()
                return await call_next(request)

        app.add_middleware(IdleTrackingMiddleware)

    router = _create_project_router()

    if server_mode:
        # Dual mount: /api/p/{key}/… for explicit project, /api/… for default
        app.include_router(router, prefix="/api/p/{project_key}")
        app.include_router(router, prefix="/api")

        # Middleware: resolve the project scope (path /api/p/{key}/… OR a
        # ?project= query on a federation path) and set the ContextVar that
        # _get_db and _require_federation_scope read. Honoring ?project= for the
        # whole federation API makes /api/weft/… scope the same way /mcp does.
        from starlette.middleware.base import BaseHTTPMiddleware

        from filigree.dashboard_auth import extract_federation_scope, is_loom_scoped_path
        from filigree.dashboard_routes.common import _error_response
        from filigree.types.api import ErrorCode

        class ProjectMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
                path = request.url.path
                key = extract_federation_scope(path, request.query_params.get("project"))
                if key is not None:
                    token = dashboard_state.current_project_key.set(key)
                    try:
                        response = await call_next(request)
                        # Name the project a federation request actually resolved
                        # to, so a misroute (client scoped the wrong key) cannot
                        # read as success. An unknown key 404s in _get_db before
                        # any write, so a 2xx here means key == the written
                        # project's prefix. Skip /mcp — its streaming transport
                        # owns its own response headers.
                        if is_loom_scoped_path(path) and not (path == "/mcp" or path.startswith("/mcp/")):
                            response.headers["X-Filigree-Project"] = key
                        return response
                    finally:
                        dashboard_state.current_project_key.reset(token)
                # Unscoped. A federation WRITE must not silently fall back to the
                # daemon's default project (the filigree-7a399b8124 contamination):
                # fail closed. Reads stay lenient (keep the default-project
                # fallback). /mcp is excluded — it carries protocol messages and
                # self-scopes via ?project= at the transport.
                is_federation = is_loom_scoped_path(path) and not (path == "/mcp" or path.startswith("/mcp/"))
                if request.method not in ("GET", "HEAD", "OPTIONS") and is_federation:
                    return _error_response(
                        "Ambiguous federation write in server mode: scope to a project — use POST /api/p/{project_key}/weft/… or add ?project={key}.",
                        ErrorCode.VALIDATION,
                        400,
                    )
                response = await call_next(request)
                # C-10(a) honest-seams: an unscoped federation READ silently
                # resolves to the daemon's default project; echo which project it
                # actually hit so a defaulted read is not silent about its
                # destination (uniform with the scoped branch above). Writes never
                # reach here — they fail closed — so this never masks a misroute.
                if request.method in ("GET", "HEAD") and is_federation:
                    store = dashboard_state.project_store
                    default_key = store.default_key if store is not None else ""
                    if default_key:
                        response.headers["X-Filigree-Project"] = default_key
                return response

        app.add_middleware(ProjectMiddleware)
    else:
        # Ethereal mode: single project at /api/
        app.include_router(router, prefix="/api")

    # Root-level endpoints (not project-scoped)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "dashboard.html").read_text()
        return HTMLResponse(html)

    @app.get("/api/health")
    async def api_health() -> JSONResponse:
        if server_mode and dashboard_state.project_store is not None:
            return JSONResponse(
                {
                    "status": "ok",
                    "mode": "server",
                    "projects": len(dashboard_state.project_store.list_projects()),
                    "version": __version__,
                    "auth": app.state.auth_scope,
                }
            )
        return JSONResponse({"status": "ok", "mode": "ethereal", "version": __version__, "auth": app.state.auth_scope})

    @app.get("/api/projects")
    async def api_projects() -> JSONResponse:
        if server_mode and dashboard_state.project_store is not None:
            return JSONResponse(dashboard_state.project_store.list_projects())
        # Ethereal mode: single project with empty key so setProject("")
        # routes to /api (not /api/p/prefix/ which would 404).
        name = dashboard_state.config.get("name") or (dashboard_state.db.prefix if dashboard_state.db is not None else "")
        return JSONResponse([{"key": "", "name": name, "path": ""}])

    if server_mode:

        @app.post("/api/reload")
        async def api_reload() -> JSONResponse:
            if dashboard_state.project_store is None:
                return JSONResponse({"status": "error", "detail": "Not in server mode"}, status_code=500)
            diff = dashboard_state.project_store.reload()
            if diff.get("error"):
                from filigree.dashboard_routes.common import _error_response
                from filigree.types.api import ErrorCode

                return _error_response(
                    f"Failed to reload project store: {diff['error']}",
                    ErrorCode.IO,
                    409,
                )
            logger.info("Project store reloaded: %s", diff)
            # Frontend ui.js reloadServer() reads ``data.ok`` and
            # ``data.projects``; without these it renders "Reload failed"
            # even on a successful backend reload. ``status`` retained for
            # any direct API consumer. (filigree-173e76a28a)
            return JSONResponse(
                {
                    "ok": True,
                    "status": "ok",
                    "projects": len(dashboard_state.project_store.list_projects()),
                    **diff,
                }
            )

    # Serve static JS modules (ES modules for dashboard components)
    from starlette.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Mount MCP streamable-HTTP endpoint only when bearer auth is configured.
    if _mcp_handler is not None:
        from starlette.routing import Mount

        if server_mode:
            # Wrap MCP handler to extract ?project= query param
            from urllib.parse import parse_qs

            class _McpProjectWrapper:
                """ASGI wrapper that sets _current_project_key from ?project= query param."""

                def __init__(self, inner: ASGIApp) -> None:
                    self._inner = inner

                async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                    if scope["type"] in ("http", "websocket"):
                        qs = scope.get("query_string", b"").decode()
                        params = parse_qs(qs)
                        project_vals = params.get("project", [])
                        if project_vals:
                            token = dashboard_state.current_project_key.set(project_vals[0])
                            try:
                                await self._inner(scope, receive, send)
                                return
                            finally:
                                dashboard_state.current_project_key.reset(token)
                    await self._inner(scope, receive, send)

            app.routes.append(Mount("/mcp", app=_McpProjectWrapper(_mcp_handler)))
        else:
            app.routes.append(Mount("/mcp", app=_mcp_handler))

    return app


def _idle_watchdog(timeout: float, check_interval: float) -> None:
    """Background thread that sends SIGTERM when no requests arrive for *timeout* seconds."""
    while True:
        time.sleep(check_interval)
        elapsed = time.monotonic() - _last_request_time
        if elapsed >= timeout:
            logger.info("Idle for %.0fs (threshold %.0fs), shutting down", elapsed, timeout)
            os.kill(os.getpid(), signal.SIGTERM)
            return


def _exit_dashboard_config_error(exc: BaseException) -> None:
    """Exit dashboard startup cleanly for expected project configuration errors."""
    logger.warning("dashboard_project_config_error", extra={"tool": "dashboard", "args_data": {"error": _safe_log_error(exc)}})
    print(f"Error loading dashboard project: {exc}", file=sys.stderr)
    print("Run `filigree doctor` for diagnosis.", file=sys.stderr)
    sys.exit(1)


def _safe_log_error(exc: BaseException) -> str:
    """Return a path/redaction-safe message for structured log payloads."""
    safe = getattr(exc, "safe_message", None)
    return safe if isinstance(safe, str) else str(exc)


def main(
    port: int = DEFAULT_PORT,
    *,
    no_browser: bool = False,
    server_mode: bool = False,
    allow_http_force_close: bool = False,
    allow_local_fallback: bool = False,
) -> None:
    """Start the dashboard server.

    In server mode, reads ``server.json`` for multi-project routing.
    In ethereal mode (default), serves the single local project.
    Ethereal servers auto-shutdown after IDLE_TIMEOUT_SECONDS of inactivity.

    ``allow_http_force_close`` (2.1.0 §1.1) opts the dashboard into
    accepting ``force=true`` on ``POST /api/batch/close`` and
    ``POST /api/weft/batch/close``. Without it those routes reject
    ``force=true`` with 400/VALIDATION — the workflow escape lane can only
    be used by the CLI or MCP, never by a passing HTTP client.

    ``allow_local_fallback`` is an ADR-014 recovery flag for single-project
    ethereal mode: when the project is configured for Loomweave registry mode
    but Loomweave is unavailable, auto-create paths use ``LocalRegistry``.
    """
    import uvicorn

    global _db, _last_request_time, _project_store, _allow_http_force_close

    filigree_dir: Path | None = None

    # Clear any leftover globals from a previous in-process run so ``_get_db``
    # routes to the intended mode (filigree-bff063de18). Without this, a
    # server-mode run followed by an ethereal run (or vice versa) can serve
    # the wrong database because ``_get_db`` keys off ``_project_store``.
    # ``_config`` is dict-mutable (so no ``global`` declaration); clearing it
    # here prevents stale keys (notably ``name``, which read_config does not
    # default) from leaking into the next run's /api/projects response.
    # (filigree-154a23794c)
    _project_store = None
    _db = None
    _config.clear()
    _allow_http_force_close = allow_http_force_close

    if server_mode:
        try:
            store = ProjectStore()
            store.load()
            _project_store = store
        except _EXPECTED_PROJECT_CONFIG_ERRORS as exc:
            _exit_dashboard_config_error(exc)
        n = len(store.list_projects())
        logger.info("Server mode: loaded %d project(s)", n)
    else:
        try:
            anchor = find_filigree_anchor()
            filigree_dir = anchor.store_dir
            config = read_config(filigree_dir)
            _config.update(config)
            db = FiligreeDB.from_anchor(
                anchor,
                check_same_thread=False,
                allow_local_fallback_override=True if allow_local_fallback else None,
            )
            if allow_local_fallback and db.registry_backend == "loomweave":
                logger.warning("dashboard started with --allow-local-fallback; loomweave registry is bypassed for auto-creates")
                db.enable_local_registry_fallback()
            _db = db
        except SchemaVersionMismatchError as exc:
            # Forward schema mismatch — exit cleanly (code 3, matching
            # `filigree doctor`) with the shared guidance text instead of
            # dumping a Python stack trace. F1 owns the helper; F2 owns
            # this dashboard-startup branch. Log a WARNING with structured
            # fields so operators tailing the filigree log see the failure
            # even if stderr is captured / redirected by the launcher.
            logger.warning(
                "dashboard_schema_mismatch",
                extra={
                    "tool": "dashboard",
                    "args_data": {"installed": exc.installed, "database": exc.database},
                },
            )
            print(format_schema_mismatch_guidance(exc.installed, exc.database), file=sys.stderr)
            sys.exit(3)
        except _EXPECTED_PROJECT_CONFIG_ERRORS as exc:
            _exit_dashboard_config_error(exc)
        except (OSError, sqlite3.Error) as exc:
            # Locked DB / permission denied / on-disk corruption etc. The
            # F2 fix only covered v+1; this sibling branch keeps the same
            # "no Python traceback at startup" UX promise for the more
            # common adjacent failures. Exit 1 (generic failure) — exit 3
            # is reserved for forward schema mismatch.
            logger.warning(
                "dashboard_db_open_failed",
                extra={"tool": "dashboard", "args_data": {"error": _safe_log_error(exc)}},
            )
            print(f"Error opening project database: {exc}", file=sys.stderr)
            print("Run `filigree doctor` for diagnosis.", file=sys.stderr)
            sys.exit(1)

    # First-serve federation-token mint (tier 2). Auto-provision the daemon's own
    # token file so single-host federation auth works with zero operator toil; the
    # env var stays the cross-host override. Mints into the daemon's own subtree —
    # the served project's store dir (ethereal) or the server config dir (server
    # mode). Done here at real serve only, never in create_app (tests call that
    # directly and must not write to a shared/real dir).
    _pinned_token_env = False
    if server_mode:
        from filigree.server import SERVER_CONFIG_DIR

        # Server mode: never pin (F1 — a home/server token must not become a
        # cross-project tier-1 credential). Warn loudly only.
        _mint_and_guard_federation_token(SERVER_CONFIG_DIR, allow_env_pin=False)
    elif _db is not None:
        _pinned_token_env = _mint_and_guard_federation_token(_db.meta_dir, allow_env_pin=True)

    app = create_app(server_mode=server_mode)

    # Initialise idle timer and start watchdog (ethereal mode only)
    _last_request_time = time.monotonic()
    if not server_mode:
        watchdog = threading.Thread(
            target=_idle_watchdog,
            args=(IDLE_TIMEOUT_SECONDS, IDLE_CHECK_INTERVAL),
            daemon=True,
        )
        watchdog.start()

    browser_timer: threading.Timer | None = None
    if not no_browser:
        browser_timer = threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}"))
        browser_timer.start()

    mode_label = "Server" if server_mode else "Dashboard"
    print(f"Filigree {mode_label}: http://localhost:{port}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        if browser_timer is not None:
            browser_timer.cancel()
        if _project_store is not None:
            _project_store.close_all()
        if _db is not None:
            _db.close()
        # Clean up ephemeral PID/port files so next session starts fresh
        if filigree_dir is not None:
            for name in ("ephemeral.pid", "ephemeral.port"):
                (filigree_dir / name).unlink(missing_ok=True)
        # Reset both globals so a later in-process ``main()`` call starts
        # from a clean slate (filigree-bff063de18). Also clear ``_config``
        # so server-mode (or a subsequent ethereal run with a minimal config)
        # cannot serve a stale ``name`` (filigree-154a23794c).
        _project_store = None
        _db = None
        _allow_http_force_close = False
        _config.clear()
        # Cleanup symmetry: if THIS run synthesised an in-memory federation-token
        # env pin (ethereal persist-failure fallback), unset it so a later
        # in-process run() does not inherit a stale tier-1 token that masks a
        # since-recovered mount or a fresh file (matches the global reset above).
        if _pinned_token_env:
            from filigree.federation_token import WEFT_FEDERATION_ENV_VAR

            os.environ.pop(WEFT_FEDERATION_ENV_VAR, None)
