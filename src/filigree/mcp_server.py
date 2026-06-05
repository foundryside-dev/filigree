"""MCP server for the filigree issue tracker.

Primary interface for agents. Direct SQLite in stdio mode (no daemon).
Also mountable as streamable-HTTP handler inside the dashboard daemon for server mode.
Exposes filigree operations as MCP tools.

Usage:
    filigree-mcp                              # Auto-discover .filigree/ from cwd
    filigree-mcp --project /path/to/project   # Explicit project root
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
import weakref
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
    ToolAnnotations,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from filigree.core import (
    CONF_FILENAME,
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    FiligreeDB,
    find_filigree_conf,
)
from filigree.db_schema import CURRENT_SCHEMA_VERSION
from filigree.install_support.version_marker import format_schema_mismatch_guidance
from filigree.mcp_runtime import McpRuntimeContext, McpToolMetadata, set_runtime_context
from filigree.mcp_tools.common import (  # noqa: F401  — re-exported for backward compat
    _MAX_LIST_RESULTS,
    _text,
)
from filigree.registry import RegistryVersionMismatchError
from filigree.registry_errors import registry_error_response
from filigree.summary import generate_summary, write_summary
from filigree.types.api import ErrorCode, ErrorResponse, SchemaVersionMismatchError, errorcode_to_http_status

# ---------------------------------------------------------------------------
# Module globals (state accessors depend on these)
# ---------------------------------------------------------------------------

server = Server("filigree")
db: FiligreeDB | None = None
_filigree_dir: Path | None = None
_logger: logging.Logger | None = None

# Deprecation telemetry (rollout plan §5.3): count how often callers reach a
# tool via its DEPRECATED OLD name (keyed by the inbound wire name) so we can
# PROVE, before old names are removed in Phase 2, that consumers have migrated.
# The count is exact: ``call_tool`` runs as an asyncio coroutine on the single
# event-loop thread, and there is no ``await`` between the Counter read and
# write, so each ``+= 1`` is uninterruptible — the GIL is not relied upon.
_deprecated_tool_calls: Counter[str] = Counter()

_request_db: ContextVar[FiligreeDB | None] = ContextVar("filigree_request_db", default=None)
_request_filigree_dir: ContextVar[Path | None] = ContextVar("filigree_request_dir", default=None)

# Set when startup detects an on-disk schema newer than the installed
# filigree (forward mismatch). When non-None the server stays up — list_tools
# still works for introspection — but every call_tool short-circuits to a
# structured ErrorResponse(code=SCHEMA_MISMATCH). Cleared on successful init.
_schema_mismatch: SchemaVersionMismatchError | None = None

# Set when Loomweave advertises an incompatible registry API version at startup.
# Mirrors schema-mismatch degraded mode: list_tools stays available, while
# call_tool surfaces a structured CLARION_REGISTRY_VERSION_MISMATCH envelope.
_registry_startup_error: RegistryVersionMismatchError | None = None

# Set when startup hits a non-mismatch DB-open failure (locked file, missing
# file, permission denied, on-disk corruption). The server cannot run without
# a DB; ``_run`` checks this and exits cleanly with a structured log line and
# a stderr message — no Python traceback. F3-followup, GH PR #33 review.
_db_open_error: Exception | None = None

# Per-DB async lock serialising ``call_tool`` execution. The MCP SDK dispatches
# tool invocations concurrently via ``tg.start_soon``; without serialisation two
# coroutines share the single cached ``sqlite3.Connection`` on ``FiligreeDB``
# and the ``finally`` rollback of one can wipe another's uncommitted writes.
# See filigree-33a938b515.
_tool_locks: weakref.WeakKeyDictionary[FiligreeDB, asyncio.Lock] = weakref.WeakKeyDictionary()


def _lock_for(db_obj: FiligreeDB) -> asyncio.Lock:
    lock = _tool_locks.get(db_obj)
    if lock is None:
        lock = asyncio.Lock()
        _tool_locks[db_obj] = lock
    return lock


# ---------------------------------------------------------------------------
# State accessors (used by domain modules via deferred import)
# ---------------------------------------------------------------------------


def _get_db() -> FiligreeDB:
    active_db = _request_db.get() or db
    if active_db is None:
        msg = "Database not initialized"
        raise RuntimeError(msg)
    return active_db


def _get_filigree_dir() -> Path | None:
    return _request_filigree_dir.get() or _filigree_dir


def _resolve_request_filigree_dir(active_db: FiligreeDB) -> Path:
    """Return the project metadata directory (``project_root/.filigree``)
    for the active per-request DB, used to anchor ``_safe_path()``.

    For v2.0 conf-built DBs the ``db`` may be relocated outside ``.filigree/``,
    so ``db_path.parent`` is the project root, not the metadata dir; using it
    as the anchor would let ``_safe_path()`` resolve up one level into the
    project's parent. ``FiligreeDB.project_root`` is the source of truth — both
    ``from_filigree_dir`` and ``from_conf`` set it. Fall back to
    ``db_path.parent`` only for legacy direct ``FiligreeDB(...)`` constructions
    that did not set ``project_root`` (chiefly older tests).
    """
    if active_db.project_root is not None:
        return active_db.project_root / FILIGREE_DIR_NAME
    return active_db.db_path.parent


def _refresh_summary() -> None:
    """Regenerate context.md after mutations (best-effort, never fatal)."""
    filigree_dir = _get_filigree_dir()
    if filigree_dir is not None:
        try:
            write_summary(_get_db(), filigree_dir / SUMMARY_FILENAME)
        except OSError:
            (_logger or logging.getLogger(__name__)).warning("Failed to write context.md", exc_info=True)
        except Exception:
            (_logger or logging.getLogger(__name__)).error(
                "BUG in summary generation — context.md not updated. This is likely a code defect, not a database problem.",
                exc_info=True,
            )


def _find_venv_root(executable: Path) -> Path | None:
    candidates = [executable]
    try:
        resolved = executable.resolve()
    except OSError:
        resolved = None
    if resolved is not None and resolved != executable:
        candidates.append(resolved)

    for candidate in candidates:
        for parent in (candidate.parent, *candidate.parents):
            if (parent / "pyvenv.cfg").is_file():
                return parent
    return None


def _runtime_diagnostics() -> dict[str, str | None]:
    executable = Path(sys.executable)
    try:
        resolved = executable.resolve()
    except OSError:
        resolved = executable
    module_file = Path(__file__).resolve()
    venv_root = _find_venv_root(executable)
    install_context = "system_or_unknown"
    if venv_root is not None:
        install_context = "uv_tool" if ".local/share/uv/tools" in str(venv_root) else "venv"
    return {
        "python_executable": sys.executable,
        "python_executable_resolved": str(resolved),
        "entrypoint": sys.argv[0] if sys.argv else None,
        "module_file": str(module_file),
        "package_root": str(module_file.parent),
        "venv_root": str(venv_root) if venv_root is not None else None,
        "install_context": install_context,
    }


def _safe_path(raw: str) -> Path:
    """Resolve a user-supplied path safely within the project root.

    Raises ValueError for paths that escape the project directory.
    Delegates to :func:`filigree.paths.safe_path` so the same logic is
    shared with the CLI surface.
    """
    from filigree.paths import safe_path

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        msg = "Project directory not initialized"
        raise ValueError(msg)
    return safe_path(raw, filigree_dir.parent)


def _record_deprecated_tool_call(wire_name: str, arguments: dict[str, Any]) -> None:
    """Record that a caller reached a tool via its deprecated OLD name.

    Best-effort: increment the per-wire-name counter and emit a structured
    ``deprecated_tool_name`` log event (matching the ``tool_call``/``tool_error``
    style elsewhere in this module). ``wire_name`` is guaranteed to be a key of
    ``RENAME_MAP`` by the caller's detection guard.

    Runs before the handler's try/except in ``call_tool``, so it is wrapped in a
    blanket guard: telemetry must NEVER break a real tool call.
    """
    try:
        _deprecated_tool_calls[wire_name] += 1
        if _logger:
            _logger.info(
                "deprecated_tool_name",
                extra={
                    "tool": wire_name,
                    "canonical": RENAME_MAP[wire_name],
                    "actor": arguments.get("actor"),
                },
            )
    except Exception:
        # Telemetry is best-effort; never break a tool call.
        pass


def get_mcp_status_payload() -> dict[str, Any]:
    """Return read-only MCP server health without requiring a usable DB.

    This is intentionally safe in warm-but-degraded schema-mismatch mode:
    agents can inspect why the connector is degraded without mutating DB
    metadata or restarting the MCP process.
    """
    active_db = _request_db.get() or db
    filigree_dir = _get_filigree_dir()
    installed = CURRENT_SCHEMA_VERSION

    if _schema_mismatch is not None:
        return {
            "status": "schema_mismatch",
            "db_initialized": False,
            "schema_compatible": False,
            "installed_schema_version": _schema_mismatch.installed,
            "database_schema_version": _schema_mismatch.database,
            "code": ErrorCode.SCHEMA_MISMATCH,
            "error": str(_schema_mismatch),
            "guidance": format_schema_mismatch_guidance(_schema_mismatch.installed, _schema_mismatch.database),
            "filigree_dir": str(filigree_dir) if filigree_dir is not None else None,
            "runtime": _runtime_diagnostics(),
        }

    if _registry_startup_error is not None:
        response = registry_error_response(_registry_startup_error, action="opening project database")
        return {
            "status": "registry_version_mismatch",
            "db_initialized": False,
            "schema_compatible": True,
            "installed_schema_version": installed,
            "database_schema_version": None,
            "code": response["code"],
            "error": response["error"],
            "details": response.get("details"),
            "guidance": "Upgrade Filigree or Loomweave so their registry API versions match.",
            "filigree_dir": str(filigree_dir) if filigree_dir is not None else None,
            "runtime": _runtime_diagnostics(),
        }

    if _db_open_error is not None:
        return {
            "status": "db_open_error",
            "db_initialized": False,
            "schema_compatible": False,
            "installed_schema_version": installed,
            "database_schema_version": None,
            "code": ErrorCode.IO,
            "error": str(_db_open_error),
            "guidance": "Run `filigree doctor` for diagnosis.",
            "filigree_dir": str(filigree_dir) if filigree_dir is not None else None,
            "runtime": _runtime_diagnostics(),
        }

    if active_db is None:
        return {
            "status": "not_initialized",
            "db_initialized": False,
            "schema_compatible": False,
            "installed_schema_version": installed,
            "database_schema_version": None,
            "code": ErrorCode.NOT_INITIALIZED,
            "error": "Database not initialized",
            "guidance": "Run `filigree init` in the project, then restart MCP.",
            "filigree_dir": str(filigree_dir) if filigree_dir is not None else None,
            "runtime": _runtime_diagnostics(),
        }

    try:
        database_version = active_db.get_schema_version()
    except sqlite3.Error as exc:
        return {
            "status": "db_open_error",
            "db_initialized": True,
            "schema_compatible": False,
            "installed_schema_version": installed,
            "database_schema_version": None,
            "code": ErrorCode.IO,
            "error": str(exc),
            "guidance": "Run `filigree doctor` for diagnosis.",
            "filigree_dir": str(filigree_dir) if filigree_dir is not None else None,
            "runtime": _runtime_diagnostics(),
        }

    compatible = database_version <= installed
    return {
        "status": "ok" if compatible else "schema_mismatch",
        "db_initialized": True,
        "schema_compatible": compatible,
        "installed_schema_version": installed,
        "database_schema_version": database_version,
        "code": None if compatible else ErrorCode.SCHEMA_MISMATCH,
        "error": None if compatible else f"Database schema v{database_version} is newer than installed v{installed}",
        "guidance": None if compatible else format_schema_mismatch_guidance(installed, database_version),
        "filigree_dir": str(filigree_dir) if filigree_dir is not None else None,
        "runtime": _runtime_diagnostics(),
        # Deprecation-readiness signal (plan §5.3): old-name usage counts.
        # Surfaced only on the healthy path; degraded payloads above report
        # *why* the connector is degraded, not migration telemetry.
        "deprecated_tool_name_calls": {
            "total": sum(_deprecated_tool_calls.values()),
            "by_name": dict(_deprecated_tool_calls),
        },
    }


# ---------------------------------------------------------------------------
# Tool aggregation from domain modules
# ---------------------------------------------------------------------------

from filigree.mcp_tools import (  # noqa: E402, I001  — must come after globals
    annotations as _annotations_mod,
    entities as _entities_mod,
    files as _files_mod,
    issues as _issues_mod,
    meta as _meta_mod,
    observations as _observations_mod,
    planning as _planning_mod,
    scanners as _scanners_mod,
    workflow as _workflow_mod,
)
from filigree.mcp_tools.rename import NEW_TO_OLD, RENAME_MAP  # noqa: E402
from filigree.mcp_tools.tiers import tier_for  # noqa: E402

_all_tools: list[Tool] = []
_all_handlers: dict[str, Callable[..., Any]] = {}

# Subsystem (= owning tool module's short name) per tool, captured during
# assembly so it is complete-by-construction: a new tool registered by a
# module can never be missing a subsystem. Consumed by the curated tool
# catalogue surfaced from get_workflow_guide.
_tool_subsystem: dict[str, str] = {}


def _record_subsystem(tools: list[Tool], module: Any) -> None:
    subsystem = module.__name__.rsplit(".", 1)[-1]
    for _tool in tools:
        _tool_subsystem[_tool.name] = subsystem


for _mod in (
    _issues_mod,
    _planning_mod,
    _files_mod,
    _annotations_mod,
    _workflow_mod,
    _meta_mod,
    _observations_mod,
    _entities_mod,
):
    _tools, _handlers = _mod.register()
    _record_subsystem(_tools, _mod)
    _all_tools.extend(_tools)
    _all_handlers.update(_handlers)

# Scanner module uses include_legacy=True to own list_scanners + trigger_scan
_tools, _handlers = _scanners_mod.register(include_legacy=True)
_record_subsystem(_tools, _scanners_mod)
_all_tools.extend(_tools)
_all_handlers.update(_handlers)


# ---------------------------------------------------------------------------
# Tier tagging (MCP tool discoverability)
# ---------------------------------------------------------------------------
# Post-process the assembled tool list ONCE, at import, before anything reads
# descriptions or input schemas. This is the single central seam: list_tools()
# returns _all_tools verbatim (in both normal and schema-mismatch degraded
# mode), so tagging here covers every served path without per-call work and
# without risk of double-appending the marker. inputSchema is untouched, so
# _tool_argument_names (below) and arg validation are unaffected.

# Pure getters get readOnlyHint; the one hard-destructive tool gets
# destructiveHint. Anything ambiguous is left without an annotation on
# purpose — a wrong hint is worse than none.
_READ_ONLY_PREFIXES: tuple[str, ...] = ("get_", "list_", "search_", "explain_", "preview_")
_DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({"delete_issue", "delete_file_record"})


def _is_read_only(name: str) -> bool:
    # session_context is deliberately NOT here: it can opportunistically
    # restart the dashboard process (_build_context -> ensure_dashboard_running),
    # so it is not unambiguously side-effect free. Skip-if-uncertain.
    return name.startswith(_READ_ONLY_PREFIXES) or name in {
        "get_critical_path",
        "validate_issue",
    }


def _apply_tier_metadata(tools: list[Tool]) -> None:
    for tool in tools:
        tier = tier_for(tool.name)
        base = tool.description or ""
        marker = f" [tier: {tier}]"
        if not base.endswith(marker):
            tool.description = f"{base}{marker}"

        # Only set MCP ToolAnnotations where cheap and clearly correct.
        read_only = _is_read_only(tool.name)
        destructive = tool.name in _DESTRUCTIVE_TOOLS
        if not (read_only or destructive):
            continue
        existing = tool.annotations or ToolAnnotations()
        if read_only:
            existing.readOnlyHint = True
        if destructive:
            existing.destructiveHint = True
        tool.annotations = existing


_apply_tier_metadata(_all_tools)


# ---------------------------------------------------------------------------
# Served tool surface (namespaced wire names)
# ---------------------------------------------------------------------------
# ``_all_tools`` keeps its OLD/canonical names — TIER_MAP, _tool_argument_names,
# _all_handlers, and get_schema's derivation all key off them. The wire surface
# served by list_tools() is a one-time projection that renames ONLY ``.name`` to
# the namespaced ``<entity>_<verb>`` form (RENAME_MAP). model_copy preserves the
# already-applied tier markers and annotations. Built AFTER _apply_tier_metadata
# so those carry across the copy. Inbound new names are canonicalized back to the
# old name at the top of call_tool (NEW_TO_OLD), so every downstream guard and
# _all_handlers.get keeps operating on the canonical identity.
_served_tools: list[Tool] = [tool.model_copy(update={"name": RENAME_MAP[tool.name]}) for tool in _all_tools]


def _allowed_tool_arguments(tool: Tool) -> set[str]:
    schema = tool.inputSchema
    if not isinstance(schema, dict):
        return set()
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return set()
    return {key for key in properties if isinstance(key, str)}


_tool_argument_names: dict[str, set[str]] = {tool.name: _allowed_tool_arguments(tool) for tool in _all_tools}
_tool_input_schemas: dict[str, dict[str, Any]] = {
    tool.name: tool.inputSchema if isinstance(tool.inputSchema, dict) else {} for tool in _all_tools
}


def _runtime_tool_metadata() -> McpToolMetadata:
    return McpToolMetadata(tools=tuple(_all_tools), tool_subsystem=dict(_tool_subsystem))


set_runtime_context(
    McpRuntimeContext(
        get_db=_get_db,
        get_filigree_dir=_get_filigree_dir,
        safe_path=_safe_path,
        refresh_summary=_refresh_summary,
        get_status_payload=get_mcp_status_payload,
        get_tool_metadata=_runtime_tool_metadata,
        resolve_request_filigree_dir=_resolve_request_filigree_dir,
    )
)


def _unknown_argument_error(tool_name: str, arguments: object) -> ErrorResponse | None:
    if not isinstance(arguments, dict):
        return ErrorResponse(error=f"Arguments for {tool_name} must be an object", code=ErrorCode.VALIDATION)
    allowed = _tool_argument_names.get(tool_name, set())
    unknown = sorted(key for key in arguments if isinstance(key, str) and key not in allowed)
    if not unknown:
        return None
    unknown_label = ", ".join(unknown)
    return ErrorResponse(
        error=f"Unknown parameter(s) for {tool_name}: {unknown_label}",
        code=ErrorCode.VALIDATION,
    )


def _json_type_label(type_spec: object) -> str:
    labels = {
        "string": "a string",
        "integer": "an integer",
        "number": "a number",
        "boolean": "a boolean",
        "array": "an array",
        "object": "an object",
        "null": "null",
    }
    if isinstance(type_spec, str):
        return labels.get(type_spec, type_spec)
    if isinstance(type_spec, list):
        names = [item for item in type_spec if isinstance(item, str)]
        return " or ".join(labels.get(name, name) for name in names) if names else "valid JSON type"
    return "valid JSON type"


def _json_type_matches(value: object, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "null":
        return value is None
    return True


def _validate_schema_value(value: object, schema: dict[str, Any], path: str) -> str | None:
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        errors: list[str] = []
        for option in one_of:
            if not isinstance(option, dict):
                continue
            error = _validate_schema_value(value, option, path)
            if error is None:
                return None
            errors.append(error)
        return errors[0] if errors else f"{path} does not match any allowed schema"

    type_spec = schema.get("type")
    if isinstance(type_spec, str):
        allowed_types = [type_spec]
    elif isinstance(type_spec, list):
        allowed_types = [item for item in type_spec if isinstance(item, str)]
    else:
        allowed_types = []

    if allowed_types and not any(_json_type_matches(value, type_name) for type_name in allowed_types):
        return f"{path} must be {_json_type_label(type_spec)}"

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            return f"{path} length must be >= {min_length}"

    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, int | float) and value < minimum:
            return f"{path} must be >= {minimum}"
        maximum = schema.get("maximum")
        if isinstance(maximum, int | float) and value > maximum:
            return f"{path} must be <= {maximum}"

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    child_path = f"{path}.{key}" if path else key
                    return f"{child_path} is required"

        properties = schema.get("properties")
        if isinstance(properties, dict):
            if schema.get("additionalProperties") is False:
                for key in value:
                    if isinstance(key, str) and key not in properties:
                        return f"Unknown parameter(s) for {path}: {key}" if path else f"Unknown parameter(s): {key}"
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                property_schema = properties.get(key)
                if not isinstance(property_schema, dict):
                    continue
                child_path = f"{path}.{key}" if path else key
                error = _validate_schema_value(item, property_schema, child_path)
                if error is not None:
                    return error

    return None


def _schema_validation_error(tool_name: str, arguments: object) -> ErrorResponse | None:
    if not isinstance(arguments, dict):
        return ErrorResponse(error=f"Arguments for {tool_name} must be an object", code=ErrorCode.VALIDATION)
    schema = _tool_input_schemas.get(tool_name)
    if not isinstance(schema, dict):
        return None
    error = _validate_schema_value(arguments, schema, "")
    if error is None:
        return None
    return ErrorResponse(error=error, code=ErrorCode.VALIDATION)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

CONTEXT_URI = "filigree://context"


def _context_resource_error() -> ErrorResponse | None:
    if _schema_mismatch is not None:
        return ErrorResponse(
            error=format_schema_mismatch_guidance(
                _schema_mismatch.installed,
                _schema_mismatch.database,
            ),
            code=ErrorCode.SCHEMA_MISMATCH,
        )
    if _registry_startup_error is not None:
        return registry_error_response(_registry_startup_error, action="opening project database")
    if _db_open_error is not None:
        return ErrorResponse(error=f"Database not initialized: {_db_open_error}", code=ErrorCode.IO)

    active_db = _request_db.get() or db
    if active_db is not None:
        try:
            db_version = active_db.get_schema_version()
        except sqlite3.Error:
            db_version = None
        if db_version is not None and db_version > CURRENT_SCHEMA_VERSION:
            return ErrorResponse(
                error=format_schema_mismatch_guidance(CURRENT_SCHEMA_VERSION, db_version),
                code=ErrorCode.SCHEMA_MISMATCH,
            )
    return None


@server.list_resources()  # type: ignore[untyped-decorator,no-untyped-call]
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri=CONTEXT_URI,  # type: ignore[arg-type]
            name="Project Pulse",
            description="Auto-generated project summary: vitals, ready work, blockers, recent activity",
            mimeType="text/markdown",
        ),
    ]


@server.read_resource()  # type: ignore[untyped-decorator,no-untyped-call]
async def read_context(uri: str) -> str:
    if str(uri) == CONTEXT_URI:
        degraded = _context_resource_error()
        if degraded is not None:
            return json.dumps(degraded)
        return generate_summary(_get_db())
    msg = f"Unknown resource: {uri}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_WORKFLOW_TEXT_STATIC = """\
# Filigree Workflow

You are working in a project that uses **filigree** for issue tracking.
Filigree data lives in `.filigree/` and is accessed via these MCP tools.

## Quick start
1. Read `filigree://context` resource for current project state (vitals, ready work, blockers)
2. Use `work_ready` to find unblocked tasks sorted by priority
3. Use `work_start` or `work_start_next` to atomically claim and transition a task into work
4. Use `workflow_transition_list` to see allowed status changes before manual updates
5. Work on the task, use `comment_add` to log progress
6. Use `issue_close` when done — response includes newly-unblocked items

## Key tools
- **issue_get / issue_list / issue_search** — read project state
- **issue_create / issue_update / issue_close** — mutate issues
- **work_start / work_start_next** — usual path: atomic claim plus transition to work
- `work_claim` / `work_claim_next` — claim-only, niche path with optimistic locking
- **workflow_transition_list / issue_validate** — workflow-aware status management
- **type_list / type_get / workflow_status_explain** — discover type workflows
- **pack_list / workflow_guide_get** — workflow pack documentation
- **dependency_add / dependency_remove** — manage blockers
- **plan_get / plan_create** — milestone/phase/step hierarchies
- **issue_batch_close / issue_batch_update** — bulk operations (per-issue error handling)
- **change_list** — events since a timestamp (session resumption)
- **template_get** — field schemas for issue types
- **stats_get / summary_get** — project analytics
- **metrics_get** — flow metrics (cycle time, lead time, throughput)
- **dependency_critical_path** — longest dependency chain among open issues
- **admin_reload_templates** — refresh templates after editing .filigree/templates/

## Conventions
- Issue IDs: `{prefix}-{10hex}` (e.g., `myproj-a3f9b2e1c0`)
- Priorities: P0 (critical) through P4 (low)
- Each type has its own status workflow — use `type_list` to discover
- Use `workflow_transition_list <id>` before status changes
"""


def _build_workflow_text() -> str:
    """Build dynamic workflow prompt from template registry if available."""
    if (_request_db.get() or db) is None:
        return _WORKFLOW_TEXT_STATIC

    try:
        tracker = _get_db()
        types_list = tracker.templates.list_types()
        if not types_list:
            return _WORKFLOW_TEXT_STATIC

        lines = [_WORKFLOW_TEXT_STATIC, "\n## Registered Types\n"]
        for tpl in sorted(types_list, key=lambda t: t.type):
            states = " → ".join(s.name for s in tpl.states)
            lines.append(f"- **{tpl.type}** ({tpl.display_name}): {states}")

        packs = tracker.templates.list_packs()
        if packs:
            lines.append("\n## Enabled Packs\n")
            for pack in sorted(packs, key=lambda p: p.pack):
                type_names = ", ".join(sorted(pack.types.keys()))
                lines.append(f"- **{pack.pack}** v{pack.version}: {type_names}")

        # Observation awareness (read-only, guarded for pre-v7 DBs)
        try:
            obs_stats = tracker.observation_stats(sweep=False)
            if obs_stats["count"] > 0:
                lines.append("\n## Observations\n")
                if obs_stats["stale_count"] > 0:
                    lines.append(f"- {obs_stats['stale_count']} stale observation(s) (>48h old). Run `observation_list` to triage.")
                else:
                    lines.append(f"- {obs_stats['count']} pending observation(s). Use `observation_list` to review.")
        except sqlite3.OperationalError:
            logging.getLogger(__name__).debug("observation stats unavailable in MCP prompt", exc_info=True)

        return "\n".join(lines) + "\n"
    except sqlite3.Error:
        logging.getLogger(__name__).error(
            "Database error building workflow text — database may need repair",
            exc_info=True,
        )
        return (
            _WORKFLOW_TEXT_STATIC + "\n\n> **WARNING:** Database error prevented loading "
            "workflow types. Run `filigree doctor` to diagnose.\n"
        )
    except Exception:
        logging.getLogger(__name__).error(
            "BUG: Unexpected error building dynamic workflow prompt — this is likely a code defect, not a configuration issue",
            exc_info=True,
        )
        return (
            _WORKFLOW_TEXT_STATIC + "\n\n> **ERROR:** Failed to load workflow types "
            "due to an unexpected error. Run `filigree doctor` to diagnose. "
            "Use `type_list` and `pack_list` directly.\n"
        )


@server.list_prompts()  # type: ignore[untyped-decorator,no-untyped-call]
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="filigree-workflow",
            description="Filigree workflow guide with current project context. Use at session start.",
            arguments=[
                PromptArgument(
                    name="include_context",
                    description="Include current project summary (default: true)",
                    required=False,
                ),
            ],
        ),
    ]


@server.get_prompt()  # type: ignore[untyped-decorator,no-untyped-call]
async def get_workflow_prompt(name: str, arguments: dict[str, str] | None = None) -> GetPromptResult:
    if name != "filigree-workflow":
        msg = f"Unknown prompt: {name}"
        raise ValueError(msg)
    messages: list[PromptMessage] = [
        PromptMessage(role="user", content=TextContent(type="text", text=_build_workflow_text())),
    ]
    include_ctx = (arguments or {}).get("include_context", "true").lower() != "false"
    if include_ctx:
        try:
            summary = generate_summary(_get_db())
            messages.append(
                PromptMessage(role="user", content=TextContent(type="text", text=summary)),
            )
        except RuntimeError as exc:
            if "not initialized" in str(exc):
                logging.getLogger(__name__).debug("DB not yet initialized; prompt context omitted")
            else:
                logging.getLogger(__name__).error("Unexpected RuntimeError building prompt context", exc_info=True)
    return GetPromptResult(description="Filigree workflow guide with project context", messages=messages)


# ---------------------------------------------------------------------------
# Tool definitions & dispatch
# ---------------------------------------------------------------------------


@server.list_tools()  # type: ignore[untyped-decorator,no-untyped-call]
async def list_tools() -> list[Tool]:
    # Serve namespaced names only. _served_tools is the one-time projection of
    # _all_tools with .name renamed via RENAME_MAP (tier markers + annotations
    # preserved). Returned in both normal and schema-mismatch degraded mode —
    # introspection needs no DB.
    return _served_tools


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    t0 = time.monotonic()

    # Canonicalize at the very top: callers may use the namespaced wire name
    # (served by list_tools) OR the legacy old name. Resolve the new name back
    # to its canonical (old) identity so EVERY downstream guard, dispatch, arg
    # check, and log line operates on one name. Critically, this keeps the three
    # ``name != "get_mcp_status"`` degraded-mode exemptions reachable via the new
    # ``mcp_status_get`` name. Unknown names pass through unchanged to the
    # NOT_FOUND fast-path below.
    #
    # Deprecation telemetry (plan §5.3): capture the inbound wire name BEFORE
    # canonicalizing, then detect the deprecated-old-name case. A caller using
    # the NEW name (``issue_get``) resolves to a different canonical name
    # (``get_issue``) — not deprecated. A caller using the OLD name
    # (``get_issue``) resolves to itself AND is a key of ``RENAME_MAP`` (has a
    # successor) — deprecated. An unknown name resolves to itself but is not in
    # ``RENAME_MAP``, so it is not recorded (it falls through to the NOT_FOUND
    # fast-path). Recorded here, before the degraded-mode guards, so old-name
    # usage is counted regardless of the call's eventual outcome.
    wire_name = name
    name = NEW_TO_OLD.get(name, name)
    if name == wire_name and wire_name in RENAME_MAP:
        _record_deprecated_tool_call(wire_name, arguments)

    # Warm-but-degraded mode: if startup detected a v+1 DB, every call_tool
    # short-circuits to a structured SCHEMA_MISMATCH envelope. list_tools
    # still works (introspection needs no DB), so agents get a clean signal
    # instead of seeing a connection drop. See F3 of the 2.0 release plan.
    if _schema_mismatch is not None and name != "get_mcp_status":
        from filigree.mcp_tools.common import _text as _common_text

        return _common_text(
            ErrorResponse(
                error=format_schema_mismatch_guidance(
                    _schema_mismatch.installed,
                    _schema_mismatch.database,
                ),
                code=ErrorCode.SCHEMA_MISMATCH,
            )
        )

    if _registry_startup_error is not None and name != "get_mcp_status":
        from filigree.mcp_tools.common import _text as _common_text

        return _common_text(registry_error_response(_registry_startup_error, action="opening project database"))

    # Runtime drift gate: a long-running MCP session can have its DB
    # forward-migrated under it (sibling MCP at a newer version, manual
    # migration, etc.). Initialization succeeded with version N, but
    # ``PRAGMA user_version`` now reports N+1 — every write goes through
    # because the init-time gate already fell through. Re-check on every
    # call; cheap (a single pragma read) and fail-closed only when the
    # DB is strictly newer than the installed binary. ``get_mcp_status``
    # is exempted so the diagnostic stays available. Senior-user MCP
    # review run e P2.5.
    active_db_for_schema = _request_db.get() or db
    if name != "get_mcp_status" and active_db_for_schema is not None:
        try:
            db_version = active_db_for_schema.get_schema_version()
        except sqlite3.Error:
            db_version = None
        if db_version is not None and db_version > CURRENT_SCHEMA_VERSION:
            from filigree.mcp_tools.common import _text as _common_text

            return _common_text(
                ErrorResponse(
                    error=format_schema_mismatch_guidance(CURRENT_SCHEMA_VERSION, db_version),
                    code=ErrorCode.SCHEMA_MISMATCH,
                )
            )

    # Fast-path: unknown tool returns an error response before any DB contact
    # and without holding the serialisation lock.
    handler = _all_handlers.get(name)
    if handler is None:
        from filigree.mcp_tools.common import _text as _common_text

        return _common_text({"error": f"Unknown tool: {name}", "code": ErrorCode.NOT_FOUND})
    unknown_argument_error = _unknown_argument_error(name, arguments)
    if unknown_argument_error is not None:
        from filigree.mcp_tools.common import _text as _common_text

        return _common_text(unknown_argument_error)
    schema_validation_error = _schema_validation_error(name, arguments)
    if schema_validation_error is not None:
        from filigree.mcp_tools.common import _text as _common_text

        return _common_text(schema_validation_error)

    # Serialise tool execution per-DB. The MCP SDK dispatches tool calls
    # concurrently; the shared ``sqlite3.Connection`` on ``FiligreeDB`` has
    # no transaction isolation between coroutines, and the finally-rollback
    # below would otherwise erase a sibling coroutine's uncommitted writes.
    # See filigree-33a938b515.
    active_db = _request_db.get() or db
    lock = _lock_for(active_db) if active_db is not None else None

    async def _run() -> list[TextContent]:
        try:
            out: list[TextContent] = await handler(arguments)
            # ADR-012: surface a non-blocking actor mismatch in the response
            # envelope's ``warnings`` list. Best-effort — never break a tool call.
            try:
                from filigree import actor_identity
                from filigree.mcp_tools.common import _inject_warnings

                run_db = _request_db.get() or db
                if run_db is not None:
                    mismatch = actor_identity.actor_mismatch_warning(arguments.get("actor"), run_db._verified_actor)
                    if mismatch is not None:
                        out = _inject_warnings(out, [dict(mismatch)])
            except Exception:
                pass
            return out
        except Exception:
            if _logger:
                _logger.error("tool_error", extra={"tool": name, "args_data": arguments}, exc_info=True)
            raise
        finally:
            # Safety net: roll back any uncommitted transaction left by a
            # failed mutation. Re-resolve _get_db() in case the handler
            # switched the ContextVar-scoped DB.
            resolved = _request_db.get() or db
            if resolved is not None and resolved.conn.in_transaction:
                resolved.conn.rollback()

    if lock is None:
        result = await _run()
    else:
        async with lock:
            result = await _run()

    duration_ms = round((time.monotonic() - t0) * 1000, 1)
    if _logger:
        _logger.info("tool_call", extra={"tool": name, "args_data": arguments, "duration_ms": duration_ms})
    return result


# ---------------------------------------------------------------------------
# HTTP transport factory (for server-mode dashboard)
# ---------------------------------------------------------------------------


def create_mcp_app(
    db_resolver: Callable[[], FiligreeDB | None] | None = None,
) -> tuple[ASGIApp, Callable[..., Any]]:
    """Create an ASGI app + lifespan hook for MCP streamable-HTTP.

    Returns ``(asgi_app, lifespan_context_manager)`` where:

    * **asgi_app** is an ASGI callable to mount at ``/mcp`` in the
      dashboard.
    * **lifespan_context_manager** is an async-context-manager that
      must be entered during the parent application's lifespan so the
      underlying ``StreamableHTTPSessionManager`` task-group is
      running before the first request arrives.

    ``db_resolver`` — optional callable returning the active
    :class:`FiligreeDB`. When provided, each request gets an isolated
    request-local DB + project directory context.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=True,
    )

    async def _handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        db_token: Token[FiligreeDB | None] | None = None
        dir_token: Token[Path | None] | None = None
        if db_resolver is not None:
            from starlette.responses import JSONResponse

            try:
                resolved = db_resolver()
            except KeyError as exc:
                project_key = str(exc.args[0]) if exc.args else ""
                resp = JSONResponse(
                    {
                        "error": "Unknown project",
                        "code": ErrorCode.NOT_FOUND,
                        "project": project_key,
                    },
                    status_code=404,
                )
                await resp(scope, receive, send)
                return
            except SchemaVersionMismatchError as exc:
                resp = JSONResponse(
                    {
                        "error": format_schema_mismatch_guidance(exc.installed, exc.database),
                        "code": ErrorCode.SCHEMA_MISMATCH,
                    },
                    status_code=errorcode_to_http_status(ErrorCode.SCHEMA_MISMATCH),
                )
                await resp(scope, receive, send)
                return
            except RegistryVersionMismatchError as exc:
                response = registry_error_response(exc, action="opening project database")
                resp = JSONResponse(
                    response,
                    status_code=errorcode_to_http_status(response["code"]),
                )
                await resp(scope, receive, send)
                return
            except ValueError as exc:
                resp = JSONResponse(
                    {
                        "error": str(exc),
                        "code": ErrorCode.VALIDATION,
                    },
                    status_code=400,
                )
                await resp(scope, receive, send)
                return
            except FileNotFoundError as exc:
                resp = JSONResponse(
                    {
                        "error": str(exc),
                        "code": ErrorCode.NOT_INITIALIZED,
                    },
                    status_code=503,
                )
                await resp(scope, receive, send)
                return
            except (OSError, sqlite3.Error) as exc:
                resp = JSONResponse(
                    {
                        "error": str(exc),
                        "code": ErrorCode.IO,
                    },
                    status_code=500,
                )
                await resp(scope, receive, send)
                return

            if resolved is None:
                resp = JSONResponse(
                    {
                        "error": "Unable to resolve project database",
                        "code": ErrorCode.NOT_INITIALIZED,
                    },
                    status_code=503,
                )
                await resp(scope, receive, send)
                return
            db_token = _request_db.set(resolved)
            dir_token = _request_filigree_dir.set(_resolve_request_filigree_dir(resolved))
        try:
            await session_manager.handle_request(scope, receive, send)
        except RuntimeError as exc:
            if "not initialized" not in str(exc) and "Task group" not in str(exc):
                raise
            # Session manager not started (e.g. lifespan not triggered in
            # test or ethereal mode).  Return 503 so the route is visible
            # but clearly not ready.
            from starlette.responses import JSONResponse

            resp = JSONResponse(
                {
                    "error": "MCP session manager not initialized",
                    "code": ErrorCode.NOT_INITIALIZED,
                },
                status_code=503,
            )
            await resp(scope, receive, send)
        finally:
            try:
                if dir_token is not None:
                    _request_filigree_dir.reset(dir_token)
            finally:
                if db_token is not None:
                    _request_db.reset(db_token)

    return _handle_mcp, session_manager.run


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _attempt_startup(filigree_dir: Path, conf_path: Path | None = None) -> None:
    """Open the project DB, falling back to warm-but-degraded mode on v+1.

    When ``conf_path`` is provided, opens the DB declared by ``.filigree.conf``
    via :meth:`FiligreeDB.from_conf` so v2.0 relocated layouts (e.g.
    ``db: "track.db"``) are honoured. Otherwise opens the legacy
    ``.filigree/filigree.db`` via :meth:`FiligreeDB.from_filigree_dir`.
    ``filigree_dir`` always remains the metadata directory
    (``project_root/.filigree``) and anchors logs / summary / ephemeral PID
    regardless of where the DB itself lives.

    On a forward schema mismatch the server stays up: ``db`` remains ``None``,
    ``_schema_mismatch`` is set, and every ``call_tool`` short-circuits to a
    structured ``SCHEMA_MISMATCH`` envelope. ``list_tools`` continues to work
    (it touches no DB state). This lets MCP clients render a clean error
    instead of seeing a connection drop. See F3 of the 2.0 release plan.

    For non-mismatch open failures (locked file, permission denied, missing
    file, on-disk corruption) the helper records ``_db_open_error`` instead
    of letting the exception propagate — the F3 promise of "clean signal
    instead of connection drop" was one bug-class wide before this fix.
    ``_run`` consults the sentinel after calling us and exits cleanly.
    """
    global db, _filigree_dir, _schema_mismatch, _registry_startup_error, _db_open_error

    _filigree_dir = filigree_dir
    try:
        db = FiligreeDB.from_conf(conf_path) if conf_path is not None else FiligreeDB.from_filigree_dir(filigree_dir)
        # ADR-012 (schema v24): stamp the transport-verified OS identity onto the
        # session so every runtime insert records verified_actor. Resolution
        # never raises and never blocks; None leaves verified_actor NULL.
        from filigree import actor_identity

        db.set_verified_actor(actor_identity.resolve_os_actor())
        _schema_mismatch = None
        _registry_startup_error = None
        _db_open_error = None
    except SchemaVersionMismatchError as exc:
        db = None
        _schema_mismatch = exc
        _registry_startup_error = None
        _db_open_error = None
    except RegistryVersionMismatchError as exc:
        db = None
        _schema_mismatch = None
        _registry_startup_error = exc
        _db_open_error = None
    except (OSError, sqlite3.Error, ValueError) as exc:
        db = None
        _schema_mismatch = None
        _registry_startup_error = None
        _db_open_error = exc


async def _run(project_path: Path | None) -> None:
    global _logger

    if project_path:
        # Honour ``.filigree.conf`` even when ``--project`` is supplied: the
        # CLI surface (cli_common.get_db) does and stdio MCP must agree, or
        # a v2.0 conf-relocated project gets two divergent databases.
        conf_path: Path | None = (project_path / CONF_FILENAME) if (project_path / CONF_FILENAME).is_file() else None
        filigree_dir = project_path / FILIGREE_DIR_NAME
        if not filigree_dir.is_dir():
            print(f"Error: {filigree_dir} not found. Run 'filigree init' first.", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            conf_path = find_filigree_conf()
        except FileNotFoundError as exc:
            # ProjectNotInitialisedError carries a message that points at
            # `filigree init` and `filigree doctor`.
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        project_root = conf_path.parent
        filigree_dir = project_root / FILIGREE_DIR_NAME

    _attempt_startup(filigree_dir, conf_path=conf_path)

    from filigree.logging import setup_logging

    _logger = setup_logging(filigree_dir)
    _logger.info("mcp_server_start", extra={"tool": "server", "args_data": {"project": str(filigree_dir.parent)}})
    _log_startup_status(_logger)

    if _db_open_error is not None:
        # Locked DB / permission denied / missing file / corruption — the
        # server cannot proceed. Exit cleanly with a structured log line so
        # operators see a single failure event instead of a Python
        # traceback dumped to stderr by asyncio.
        print(f"Error opening project database: {_db_open_error}", file=sys.stderr)
        print("Run `filigree doctor` for diagnosis.", file=sys.stderr)
        sys.exit(1)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        if db is not None:
            db.close()


def _log_startup_status(logger: logging.Logger) -> None:
    """Emit a WARNING when the server is starting in degraded (v+1) mode.

    Operators tailing the MCP server log should immediately see that the
    process is up but degraded — without having to wait for a client to
    invoke a tool and read the ``SCHEMA_MISMATCH`` envelope. Split out as
    a tiny helper so a unit test can drive this branch synchronously
    without entering the async ``stdio_server`` event loop in :func:`_run`.
    """
    if _schema_mismatch is not None:
        logger.warning(
            "mcp_server_degraded",
            extra={
                "tool": "server",
                "args_data": {
                    "installed": _schema_mismatch.installed,
                    "database": _schema_mismatch.database,
                },
            },
        )
    elif _registry_startup_error is not None:
        logger.warning(
            "mcp_server_registry_version_mismatch",
            extra={
                "tool": "server",
                "args_data": {
                    "expected": _registry_startup_error.expected,
                    "advertised": _registry_startup_error.advertised,
                    "url": _registry_startup_error.url,
                },
            },
        )
    elif _db_open_error is not None:
        logger.warning(
            "mcp_server_db_open_failed",
            extra={
                "tool": "server",
                "args_data": {"error": str(_db_open_error)},
            },
        )


def main() -> None:
    import asyncio

    parser = argparse.ArgumentParser(description="Filigree MCP server")
    parser.add_argument("--project", type=Path, default=None, help="Project root (auto-discovers .filigree/ if omitted)")
    args = parser.parse_args()

    asyncio.run(_run(args.project))


if __name__ == "__main__":
    main()
