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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import APIRouter
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Receive, Scope, Send

from filigree import __version__
from filigree.core import (
    CONF_FILENAME,
    CONFIG_FILENAME,
    FILIGREE_DIR_NAME,
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

logger = logging.getLogger(__name__)

_EXPECTED_PROJECT_CONFIG_ERRORS = (ProjectNotInitialisedError, ValueError, TypeError, KeyError)

# ---------------------------------------------------------------------------
# Module-level state — set by main() or test fixtures
# ---------------------------------------------------------------------------

_db: FiligreeDB | None = None
_config: dict[str, Any] = {}

# 2.1.0 §1.1: opt-in gate for accepting ``force=true`` on HTTP batch-close
# routes. Default-off — HTTP callers can't bypass the workflow validator
# without the operator explicitly starting the dashboard with
# ``--allow-http-force-close``. Toggled by ``main()`` at process startup.
_allow_http_force_close: bool = False


def _get_allow_http_force_close() -> bool:
    """Accessor so route handlers read the current flag at request time.

    A plain ``from filigree.dashboard import _allow_http_force_close``
    would bind the bool by value at import; tests and ``main()`` both
    mutate this module attribute, so the routes need a function-level
    read instead.
    """
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
    Clarion even though the operator just asked for fallback at startup.
    """
    conf_path = filigree_dir.parent / CONF_FILENAME
    if conf_path.is_file():
        return FiligreeDB.from_conf(
            conf_path,
            check_same_thread=check_same_thread,
            allow_local_fallback_override=allow_local_fallback_override,
        )
    return FiligreeDB.from_filigree_dir(
        filigree_dir,
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
        # Serialises ALL reads and writes of (_projects, _dbs, _evicted_dbs):
        # - get_db lazy-open and cache lookup (filigree-732f6b31e4: serialise
        #   first opens; filigree-e43edbc067: removed unlocked fast path so a
        #   reader can never hand out a handle that reload just popped).
        # - reload's atomic state swap (filigree-e43edbc067).
        # - close_all drain.
        self._lock = threading.Lock()

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
                    raise ValueError(
                        f"Corrupt server config {SERVER_CONFIG_FILE}: project entry for {project_path!r} must be an object"
                    )
                prefix = meta.get("prefix")
                if not isinstance(prefix, str):
                    raise ValueError(
                        f"Corrupt server config {SERVER_CONFIG_FILE}: project prefix for {project_path!r} must be a string"
                    )

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

        The lock guards the whole operation: membership check, cache lookup,
        and lazy open all happen under the same lock that ``reload()`` uses
        for its atomic state swap. This means a reader either observes the
        old (consistent) ``(_projects, _dbs)`` pair or the new pair, never a
        torn view where ``_projects[key]`` points at a new path while
        ``_dbs[key]`` is still the handle for the old path.
        (filigree-e43edbc067)
        """
        self._drain_evicted_dbs()
        with self._lock:
            if key not in self._projects:
                raise KeyError(key)
            cached = self._dbs.get(key)
            if cached is not None:
                return cached
            info = self._projects[key]
            filigree_path = Path(info["path"])
            db: FiligreeDB | None = None
            try:
                db = _open_db_for_filigree_dir(filigree_path, check_same_thread=False)
                self._dbs[key] = db
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
            return self._dbs[key]

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


_project_store: ProjectStore | None = None


def _get_db() -> FiligreeDB:
    """Return the active database connection.

    In server mode (``_project_store`` set): resolves the project from
    the per-request ``_current_project_key`` ContextVar.  Falls back to
    ``default_key`` when the var is empty (un-prefixed ``/api/`` route).

    In ethereal mode: returns the module-level ``_db``.
    """
    from fastapi import HTTPException

    from filigree.types.api import ErrorCode

    if _project_store is not None:
        key = _current_project_key.get() or _project_store.default_key
        if not key:
            raise HTTPException(status_code=503, detail="No projects registered")
        try:
            return _project_store.get_db(key)
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
    if _db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    return _db


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
      the full path becomes ``/api/loom/<endpoint>`` after the
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

    # Loom generation — new in 2.0 under /loom. Empty in Phase B.
    router.include_router(analytics.create_loom_router(), prefix="/loom")
    router.include_router(issues.create_loom_router(), prefix="/loom")
    router.include_router(files.create_loom_router(), prefix="/loom")
    router.include_router(releases.create_loom_router(), prefix="/loom")

    # Living surface — un-prefixed loom aliases; per-endpoint adoption.
    router.include_router(files.create_living_surface_router())

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

    # --- MCP streamable-HTTP setup (optional) ---
    _mcp_handler: ASGIApp | None = None
    _mcp_lifespan_factory: Callable[..., Any] | None = None
    try:
        from filigree.mcp_server import create_mcp_app

        if server_mode:
            # Closure reads ContextVar — no changes to mcp_server.py needed
            def _server_db_resolver() -> FiligreeDB | None:
                if _project_store is None:
                    return None
                key = _current_project_key.get() or _project_store.default_key
                if not key:
                    return None
                return _project_store.get_db(key)

            _mcp_handler, _mcp_lifespan_factory = create_mcp_app(db_resolver=_server_db_resolver)
        else:
            _mcp_handler, _mcp_lifespan_factory = create_mcp_app(db_resolver=lambda: _db)
    except ImportError:
        logger.debug("MCP streamable-HTTP not available (SDK not installed or import error)", exc_info=True)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        if _mcp_lifespan_factory is not None:
            async with _mcp_lifespan_factory():
                yield
        else:
            yield

    app = FastAPI(title="Filigree Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)

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

        # Middleware: extract project_key from path and set ContextVar
        from starlette.middleware.base import BaseHTTPMiddleware

        class ProjectMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
                path = request.url.path
                # Match /api/p/{key}/… — extract the key segment
                if path.startswith("/api/p/"):
                    parts = path.split("/", 5)  # ['', 'api', 'p', key, ...]
                    if len(parts) >= 4 and parts[3]:
                        token = _current_project_key.set(parts[3])
                        try:
                            return await call_next(request)
                        finally:
                            _current_project_key.reset(token)
                return await call_next(request)

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
        if server_mode and _project_store is not None:
            return JSONResponse(
                {
                    "status": "ok",
                    "mode": "server",
                    "projects": len(_project_store.list_projects()),
                    "version": __version__,
                }
            )
        return JSONResponse({"status": "ok", "mode": "ethereal", "version": __version__})

    @app.get("/api/projects")
    async def api_projects() -> JSONResponse:
        if server_mode and _project_store is not None:
            return JSONResponse(_project_store.list_projects())
        # Ethereal mode: single project with empty key so setProject("")
        # routes to /api (not /api/p/prefix/ which would 404).
        name = _config.get("name") or (_db.prefix if _db is not None else "")
        return JSONResponse([{"key": "", "name": name, "path": ""}])

    if server_mode:

        @app.post("/api/reload")
        async def api_reload() -> JSONResponse:
            if _project_store is None:
                return JSONResponse({"status": "error", "detail": "Not in server mode"}, status_code=500)
            diff = _project_store.reload()
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
                    "projects": len(_project_store.list_projects()),
                    **diff,
                }
            )

    # Serve static JS modules (ES modules for dashboard components)
    from starlette.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Mount MCP streamable-HTTP endpoint.
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
                            token = _current_project_key.set(project_vals[0])
                            try:
                                await self._inner(scope, receive, send)
                                return
                            finally:
                                _current_project_key.reset(token)
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
    ``POST /api/loom/batch/close``. Without it those routes reject
    ``force=true`` with 400/VALIDATION — the workflow escape lane can only
    be used by the CLI or MCP, never by a passing HTTP client.

    ``allow_local_fallback`` is an ADR-014 recovery flag for single-project
    ethereal mode: when the project is configured for Clarion registry mode
    but Clarion is unavailable, auto-create paths use ``LocalRegistry``.
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
            project_root, _conf_path = find_filigree_anchor()
            filigree_dir = project_root / FILIGREE_DIR_NAME
            config = read_config(filigree_dir)
            _config.update(config)
            db = _open_db_for_filigree_dir(
                filigree_dir,
                check_same_thread=False,
                allow_local_fallback_override=True if allow_local_fallback else None,
            )
            if allow_local_fallback and db.registry_backend == "clarion":
                logger.warning("dashboard started with --allow-local-fallback; clarion registry is bypassed for auto-creates")
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
