"""MCP tools for scanner lifecycle — list, trigger, batch trigger, status, preview."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shlex
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mcp.types import TextContent, Tool

from filigree.bundled_scanners import BUNDLED_SCANNERS, bundled_scanner_matches, get_bundled_scanner, looks_like_stale_bundled_scanner
from filigree.core import VALID_SEVERITIES
from filigree.db_files import INGESTED_FILE_ID_KEY
from filigree.mcp_tools.common import _list_response, _parse_args, _registry_error_text, _text, _validate_int_range
from filigree.mcp_tools.payloads import finding_to_mcp
from filigree.registry import RegistryResolutionError, RegistryUnavailableError
from filigree.scanner_callback import ScannerApiUrlResolution, resolve_scanner_api_url_with_source
from filigree.scanner_prompts import PROMPT_PACKS, applicable_prompt_pack_names, expand_prompt_pack_names, list_prompt_packs
from filigree.scanner_runtime import ScannerSpawnError, _spawn_scan
from filigree.scanners import list_scanners as _list_scanners
from filigree.scanners import load_scanner, validate_scanner_command, validate_scanner_name
from filigree.types.api import ErrorCode, ErrorResponse
from filigree.types.files import ScanIngestResult
from filigree.types.inputs import (
    DisableScannerArgs,
    EnableScannerArgs,
    GetScanStatusArgs,
    ListPromptPacksArgs,
    PreviewScanArgs,
    ReportFindingArgs,
    TriggerScanArgs,
    TriggerScanBatchArgs,
)

_LOCALHOST_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
_ALLOWED_URL_SCHEMES = frozenset(("http", "https"))

_logger = logging.getLogger(__name__)
_SCAN_RUN_LIFECYCLE_ERRORS = (sqlite3.Error, KeyError, ValueError)


def _mark_scan_run_failed(
    tracker: Any,
    scan_run_id: str,
    *,
    error_message: str,
    context: str,
    exit_code: int | None = None,
) -> str | None:
    kwargs: dict[str, Any] = {"error_message": error_message}
    if exit_code is not None:
        kwargs["exit_code"] = exit_code
    try:
        tracker.update_scan_run_status(scan_run_id, "failed", **kwargs)
    except _SCAN_RUN_LIFECYCLE_ERRORS as exc:
        _logger.error(
            "Failed to mark scan run %s failed during %s: %s",
            scan_run_id,
            context,
            exc,
            exc_info=True,
        )
        return str(exc)
    return None


def _prompt_pack_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": sorted(PROMPT_PACKS),
        "default": "bug-hunt",
        "description": (
            "Bundled scanner prompt pack. See list_prompt_packs; only applies when the scanner's accepts_prompt field is true. "
            "Prompt packs are advisory only; the selected prompt does not restrict scanner file access. "
            "See scanner risk_metadata.prompt_pack_scope."
        ),
    }


def _validate_prompt_pack(prompt: str) -> ErrorResponse | None:
    try:
        expand_prompt_pack_names(prompt)
    except ValueError as exc:
        return ErrorResponse(error=str(exc), code=ErrorCode.VALIDATION)
    return None


def _validate_scanner_accepts_prompt(cfg: Any, prompt: str) -> ErrorResponse | None:
    if prompt == "bug-hunt" or cfg.accepts_prompt():
        return None
    return ErrorResponse(
        error=f"Scanner {cfg.name!r} does not accept prompt packs; its command template has no {{prompt}} placeholder.",
        code=ErrorCode.VALIDATION,
        details={"scanner": cfg.name, "prompt": prompt, "accepts_prompt": False},
    )


def _scanner_path(filigree_dir: Path, scanner_name: str) -> Path:
    return filigree_dir / "scanners" / f"{scanner_name}.toml"


def _bundled_scanner_matches(path: Path, scanner_name: str) -> bool:
    return bundled_scanner_matches(path.parent, scanner_name)


def _looks_like_stale_bundled_scanner(path: Path, scanner_name: str) -> bool:
    return looks_like_stale_bundled_scanner(path.parent, scanner_name)


def _available_scanner_items(filigree_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for scanner_name in sorted(BUNDLED_SCANNERS):
        bundled = BUNDLED_SCANNERS[scanner_name]
        path = _scanner_path(filigree_dir, scanner_name)
        command_path = shutil.which(bundled.command)
        items.append(
            {
                "name": bundled.name,
                "description": bundled.description,
                "command": bundled.command,
                "command_available": command_path is not None,
                "command_path": command_path,
                "file_types": list(bundled.file_types),
                "language_focus": list(bundled.language_focus),
                "applicable_prompts": applicable_prompt_pack_names(bundled.language_focus),
                "enabled": path.is_file() and _bundled_scanner_matches(path, scanner_name),
                "path": str(path),
            }
        )
    return items


def _report_finding_observation_ids(
    tracker: Any,
    *,
    file_id: str,
    finding_id: str,
) -> list[str]:
    """Find observation IDs paired with a given finding.

    Uses the ``source_finding_id`` foreign-key set on the observation at
    creation time. Previously this matched on summary + line + the literal
    ``scanner:agent`` actor; that meant changing the observation actor (e.g.
    to attribute a real agent identity per F3) would break the lookup. The
    FK is the durable correlation.
    """
    observations = tracker.list_observations(file_id=file_id, limit=10000)
    return [observation["id"] for observation in observations if observation.get("source_finding_id") == finding_id]


def _reported_finding_record(
    tracker: Any,
    result: ScanIngestResult,
    *,
    file_id: str | None,
    rule_id: str,
    line_start: int | None,
    message: str,
    severity: str,
) -> dict[str, Any] | None:
    for finding_id in result.get("new_finding_ids", []):
        try:
            return cast(dict[str, Any], tracker.get_finding(finding_id))
        except KeyError:
            continue

    matching_findings = cast(
        list[dict[str, Any]],
        tracker.list_findings_global(scan_source="agent", file_id=file_id, limit=10000)["findings"],
    )
    return next(
        (
            item
            for item in matching_findings
            if item["rule_id"] == rule_id
            and item.get("line_start") == line_start
            and item.get("message") == message
            and item.get("severity") == severity
        ),
        None,
    )


def register(
    *,
    include_legacy: bool = False,
) -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for scanner-domain tools.

    When *include_legacy* is True, all scanner tools including legacy are
    returned (adds ``list_scanners`` and single-file ``trigger_scan``).
    When False, only the batch/status/preview tools are returned.
    """
    new_tools = [
        Tool(
            name="report_finding",
            description=(
                "Report a single code finding (bug, smell, security issue) discovered by the agent. "
                "Auto-registers the file if not already tracked. No scanner config needed — "
                "one call, one finding, zero ceremony. Returns the flat ScanFinding record plus "
                "an ``observation_id`` when a paired triage observation was created. "
                "Paired observations are explicit: pass ``create_observation=true`` when the "
                "finding should also show up in ``list_observations`` for triage. Pass ``actor`` "
                "to attribute the report to a specific agent identity "
                "(otherwise the observation is recorded as ``scanner:agent``). "
                "Pass ``response_detail='full'`` for the legacy batch-style stats "
                "(``findings_created`` / ``findings_updated`` / ``file_created`` / "
                "``observations_created`` / ``observations_failed``)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Project-relative file path"},
                    "rule_id": {"type": "string", "description": "Finding identifier / title (e.g. 'unused-import', 'sql-injection')"},
                    "message": {"type": "string", "description": "Detailed description of the finding"},
                    "severity": {
                        "type": "string",
                        "enum": sorted(VALID_SEVERITIES),
                        "default": "info",
                        "description": "Finding severity",
                    },
                    "line_start": {"type": "integer", "minimum": 1, "description": "Start line number"},
                    "line_end": {"type": "integer", "minimum": 1, "description": "End line number"},
                    "category": {"type": "string", "description": "Optional grouping category"},
                    "actor": {
                        "type": "string",
                        "description": (
                            "Agent identity for audit attribution. When set, the paired "
                            "observation's ``actor`` field uses this value instead of the "
                            "default ``scanner:agent``."
                        ),
                    },
                    "create_observation": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, explicitly create a paired observation so the finding "
                            "shows up in ``list_observations`` for triage. Default false creates "
                            "only the finding."
                        ),
                    },
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": (
                            "'slim' (default) returns the flat ScanFinding plus ``finding_result`` "
                            "and ``observation_id`` (if a paired observation was created). 'full' "
                            "additionally includes the batch-style ingest stats."
                        ),
                    },
                },
                "required": ["file_path", "rule_id", "message"],
            },
        ),
        Tool(
            name="list_prompt_packs",
            description=(
                "List bundled scanner prompt packs. Prompt packs are advisory review-focus hints; "
                "they do not restrict what the scanner process can read or report. "
                "Use the optional language filter to hide language-specific packs that do not match a scanner's language_focus."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "description": (
                            "Optional scanner language focus, e.g. python. Returns language-specific packs for that language "
                            "plus language-agnostic packs."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="list_available_scanners",
            description=(
                "List bundled scanner registrations that can be enabled in this project. "
                "Returns command availability, command path, enabled state, and target TOML path."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="enable_scanner",
            description=(
                "Enable a bundled scanner in the current project by writing its managed TOML registration. "
                "Refuses to overwrite custom TOML unless force=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scanner": {"type": "string", "description": "Bundled scanner name, e.g. codex or claude"},
                    "force": {"type": "boolean", "default": False, "description": "Replace an existing custom or stale bundled TOML"},
                },
                "required": ["scanner"],
            },
        ),
        Tool(
            name="disable_scanner",
            description=(
                "Disable a scanner registration by removing its TOML file. "
                "For bundled scanner names, refuses to remove custom TOML unless force=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scanner": {"type": "string", "description": "Scanner name"},
                    "force": {"type": "boolean", "default": False, "description": "Remove a custom TOML that uses a bundled scanner name"},
                },
                "required": ["scanner"],
            },
        ),
        Tool(
            name="trigger_scan_batch",
            description=(
                "Trigger a scanner on multiple files in one call. Registers all files, "
                "spawns one scanner process per file, and returns a list of per-file "
                "scan_run_ids plus a batch_id for correlation. Rate-limited per scanner+file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scanner": {"type": "string", "description": "Scanner name (from list_scanners)"},
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to scan (relative to project root)",
                    },
                    "prompt": _prompt_pack_schema(),
                    "api_url": {
                        "type": "string",
                        "description": "Dashboard URL where scanner POSTs results. Defaults to the active local Filigree dashboard.",
                    },
                },
                "required": ["scanner", "file_paths"],
            },
        ),
        Tool(
            name="get_scan_status",
            description="Get the status of a scan run by ID, including live PID check and log tail.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scan_run_id": {"type": "string", "description": "Scan run ID"},
                    "log_lines": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Number of log lines to tail",
                    },
                },
                "required": ["scan_run_id"],
            },
        ),
        Tool(
            name="preview_scan",
            description="Preview the command that would be executed for a scan, without spawning a process.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scanner": {"type": "string", "description": "Scanner name"},
                    "file_path": {"type": "string", "description": "File path (relative to project root)"},
                    "prompt": _prompt_pack_schema(),
                },
                "required": ["scanner", "file_path"],
            },
        ),
    ]

    legacy_tools = [
        Tool(
            name="list_scanners",
            description=(
                "List registered scanners from .filigree/scanners/*.toml. Returns available scanner names, "
                "descriptions, supported file types, prompt support, and risk metadata. If this returns an empty "
                "items list, call list_available_scanners to see bundled scanners that can be enabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="trigger_scan",
            description=(
                "Trigger an async bug scan on a file. Registers the file, spawns a detached scanner process, "
                "and returns immediately with a scan_run_id for correlation. Check scan status with get_scan_status "
                "or file findings later for results. "
                "Note: results are POSTed to the dashboard API — ensure the dashboard is running at the target api_url. "
                "Rate-limited (30s cooldown per scanner+file, DB-persisted)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scanner": {"type": "string", "description": "Scanner name (from list_scanners)"},
                    "file_path": {"type": "string", "description": "File path to scan (relative to project root)"},
                    "prompt": _prompt_pack_schema(),
                    "api_url": {
                        "type": "string",
                        "description": "Dashboard URL where scanner POSTs results. Defaults to the active local Filigree dashboard.",
                    },
                },
                "required": ["scanner", "file_path"],
            },
        ),
    ]

    tools = list(new_tools)
    handlers: dict[str, Callable[..., Any]] = {
        "report_finding": _handle_report_finding,
        "list_prompt_packs": _handle_list_prompt_packs,
        "list_available_scanners": _handle_list_available_scanners,
        "enable_scanner": _handle_enable_scanner,
        "disable_scanner": _handle_disable_scanner,
        "trigger_scan_batch": _handle_trigger_scan_batch,
        "get_scan_status": _handle_get_scan_status,
        "preview_scan": _handle_preview_scan,
    }

    if include_legacy:
        tools.extend(legacy_tools)
        handlers["list_scanners"] = _handle_list_scanners
        handlers["trigger_scan"] = _handle_trigger_scan

    return tools, handlers


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_localhost_url(api_url: str) -> ErrorResponse | None:
    """Return an ErrorResponse if *api_url* is not a usable localhost HTTP URL, else ``None``.

    Rejects empty strings, URLs without an ``http``/``https`` scheme, and any
    hostname outside the fixed localhost set. Scanner helper code assembles
    ``f"{api_url}/api/v1/scan-results"`` unconditionally; a blank or scheme-less
    value there produces an unusable callback silently, so the check must fail
    closed.
    """
    from urllib.parse import urlparse

    if not isinstance(api_url, str) or not api_url.strip():
        return ErrorResponse(
            error="api_url is required and must be a non-empty http(s) URL pointing at localhost.",
            code=ErrorCode.INVALID_API_URL,
        )

    try:
        parsed = urlparse(api_url)
    except ValueError as exc:
        return ErrorResponse(
            error=f"api_url could not be parsed: {exc}",
            code=ErrorCode.INVALID_API_URL,
        )

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        return ErrorResponse(
            error=f"api_url scheme {scheme!r} not allowed; expected one of {sorted(_ALLOWED_URL_SCHEMES)}.",
            code=ErrorCode.INVALID_API_URL,
        )

    host = (parsed.hostname or "").lower()
    if host not in _LOCALHOST_HOSTS:
        return ErrorResponse(
            error=f"Non-localhost api_url not allowed: {host!r}. Scanner results would be sent to an external host.",
            code=ErrorCode.INVALID_API_URL,
        )
    return None


def _resolve_scanner_api_url_or_error(
    filigree_dir: Path,
    *,
    explicit_api_url: str | None = None,
) -> tuple[ScannerApiUrlResolution | None, ErrorResponse | None]:
    try:
        return resolve_scanner_api_url_with_source(filigree_dir, explicit_api_url=explicit_api_url), None
    except ValueError as exc:
        return None, ErrorResponse(error=str(exc), code=ErrorCode.VALIDATION)


def _load_scanner_or_error(filigree_dir: Path, scanner_name: str) -> tuple[Any | None, ErrorResponse | None]:
    """Load scanner config or return an ErrorResponse."""
    scanners_dir = filigree_dir / "scanners"
    cfg = load_scanner(scanners_dir, scanner_name)
    if cfg is None:
        available = [s.name for s in _list_scanners(scanners_dir)]
        bundled = get_bundled_scanner(scanner_name)
        details: dict[str, Any] = {"available_scanners": available}
        if bundled is not None:
            details.update(
                {
                    "bundled": True,
                    "enable_with": "enable_scanner",
                    "cli_enable_command": f"filigree scanner enable {scanner_name}",
                    "hint": (
                        "This is a bundled scanner, but it is not enabled in this project. "
                        "Call list_available_scanners, then enable_scanner to write the managed registration "
                        "(CLI: filigree scanner available, then filigree scanner enable)."
                    ),
                }
            )
            error = f"Bundled scanner {scanner_name!r} is not enabled in this project"
        else:
            details["hint"] = "Call list_available_scanners to see bundled scanners that can be enabled."
            error = f"Scanner {scanner_name!r} not found"
        return None, ErrorResponse(
            error=error,
            code=ErrorCode.NOT_FOUND,
            details=details,
        )
    return cfg, None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_list_scanners(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_filigree_dir

    filigree_dir = _get_filigree_dir()
    scanners_dir = filigree_dir / "scanners" if filigree_dir else None
    if scanners_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))
    load_errors: list[str] = []
    scanners = _list_scanners(scanners_dir, errors=load_errors)
    if load_errors:
        # Surface load errors via the logger now that the response envelope is
        # the strict ListResponse[T]. Drops the legacy ``errors`` and ``hint``
        # siblings per the loom precedent.
        for msg in load_errors:
            _logger.warning("list_scanners load error: %s", msg)
    items = [s.to_dict() for s in scanners]
    return _text(_list_response(items, has_more=False))


async def _handle_list_prompt_packs(arguments: dict[str, Any]) -> list[TextContent]:
    args = _parse_args(arguments, ListPromptPacksArgs)
    language = args.get("language")
    if language is not None and not isinstance(language, str):
        return _text(ErrorResponse(error="'language' must be a string", code=ErrorCode.VALIDATION))
    items = [pack.to_dict() for pack in list_prompt_packs(language=language)]
    return _text(_list_response(items, has_more=False))


async def _handle_list_available_scanners(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_filigree_dir

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))
    return _text(_list_response(_available_scanner_items(filigree_dir), has_more=False))


async def _handle_enable_scanner(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_filigree_dir

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))

    args = _parse_args(arguments, EnableScannerArgs)
    scanner_name = args["scanner"]
    force = args.get("force", False)
    if not isinstance(force, bool):
        return _text(ErrorResponse(error="'force' must be a boolean", code=ErrorCode.VALIDATION))

    bundled = get_bundled_scanner(scanner_name)
    if bundled is None:
        return _text(
            ErrorResponse(
                error=f"Bundled scanner {scanner_name!r} not found",
                code=ErrorCode.NOT_FOUND,
                details={"available_scanners": sorted(BUNDLED_SCANNERS)},
            )
        )

    scanners_dir = filigree_dir / "scanners"
    scanners_dir.mkdir(exist_ok=True)
    path = scanners_dir / f"{scanner_name}.toml"
    if path.exists() and not force and not _bundled_scanner_matches(path, scanner_name):
        if _looks_like_stale_bundled_scanner(path, scanner_name):
            msg = f"Existing scanner config does not match current bundled definition: {path}. Re-run with force=true (CLI: --force) to upgrade it."
            hint = "Re-run with force=true to upgrade this scanner registration to the current bundled definition."
            conflict_kind = "stale_bundled"
        else:
            msg = f"Refusing to overwrite custom scanner config: {path}. Re-run with force=true (CLI: --force) to replace it with the bundled scanner."
            hint = "Re-run with force=true to replace it with the bundled scanner."
            conflict_kind = "custom"
        return _text(
            ErrorResponse(
                error=msg,
                code=ErrorCode.CONFLICT,
                details={"path": str(path), "hint": hint, "conflict_kind": conflict_kind},
            )
        )

    path.write_text(bundled.toml(), encoding="utf-8")
    return _text({"status": "enabled", "scanner": scanner_name, "path": str(path), "managed": True})


async def _handle_disable_scanner(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_filigree_dir

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))

    args = _parse_args(arguments, DisableScannerArgs)
    scanner_name = args["scanner"]
    if name_error := validate_scanner_name(scanner_name):
        return _text(ErrorResponse(error=name_error, code=ErrorCode.VALIDATION, details={"scanner": scanner_name}))
    force = args.get("force", False)
    if not isinstance(force, bool):
        return _text(ErrorResponse(error="'force' must be a boolean", code=ErrorCode.VALIDATION))

    path = _scanner_path(filigree_dir, scanner_name)
    if not path.exists():
        return _text(
            ErrorResponse(
                error=f"Scanner {scanner_name!r} is not enabled",
                code=ErrorCode.NOT_FOUND,
                details={"path": str(path)},
            )
        )

    if scanner_name in BUNDLED_SCANNERS and not force and not _bundled_scanner_matches(path, scanner_name):
        if _looks_like_stale_bundled_scanner(path, scanner_name):
            msg = f"Existing scanner config does not match current bundled definition: {path}. Re-run with force=true (CLI: --force) to remove it."
            conflict_kind = "stale_bundled"
        else:
            msg = f"Refusing to remove custom scanner config: {path}. Re-run with force=true (CLI: --force) to remove it anyway."
            conflict_kind = "custom"
        return _text(
            ErrorResponse(
                error=msg,
                code=ErrorCode.CONFLICT,
                details={"path": str(path), "hint": "Re-run with force=true to remove it anyway.", "conflict_kind": conflict_kind},
            )
        )

    path.unlink()
    return _text({"status": "disabled", "scanner": scanner_name, "path": str(path)})


async def _handle_report_finding(arguments: dict[str, Any]) -> list[TextContent]:
    """Report a single agent-discovered finding via process_scan_results."""
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, ReportFindingArgs)
    file_path = args.get("file_path", "")
    rule_id = args.get("rule_id", "")
    message = args.get("message", "")
    if not file_path or not rule_id or not message:
        return _text(ErrorResponse(error="file_path, rule_id, and message are required", code=ErrorCode.VALIDATION))

    severity = args.get("severity", "info")
    if severity not in VALID_SEVERITIES:
        return _text(
            ErrorResponse(
                error=f"Invalid severity: {severity!r}. Valid: {', '.join(sorted(VALID_SEVERITIES))}",
                code=ErrorCode.VALIDATION,
            )
        )

    finding: dict[str, Any] = {
        "path": file_path,
        "rule_id": rule_id,
        "message": message,
        "severity": severity,
    }
    if args.get("line_start") is not None:
        finding["line_start"] = args["line_start"]
    if args.get("line_end") is not None:
        finding["line_end"] = args["line_end"]
    if args.get("category"):
        finding["metadata"] = {"category": args["category"]}

    actor_raw = args.get("actor", "")
    actor = actor_raw.strip() if isinstance(actor_raw, str) else ""
    create_observation = args.get("create_observation", False)
    if not isinstance(create_observation, bool):
        return _text(ErrorResponse(error="'create_observation' must be a boolean", code=ErrorCode.VALIDATION))
    response_detail_raw = args.get("response_detail", "slim")
    if response_detail_raw not in ("slim", "full"):
        return _text(
            ErrorResponse(error="'response_detail' must be 'slim' or 'full'", code=ErrorCode.VALIDATION),
        )

    tracker = _get_db()
    try:
        result = tracker.process_scan_results(
            scan_source="agent",
            findings=[finding],
            scan_run_id="",
            create_observations=create_observation,
            observation_actor=actor,
        )
    except RegistryResolutionError as exc:
        _logger.error("report_finding registry resolution failed: %s", exc)
        return _registry_error_text(exc, action="reporting finding")
    except RegistryUnavailableError as exc:
        _logger.error("report_finding registry unavailable: %s", exc)
        return _registry_error_text(exc, action="reporting finding")
    except ValueError as exc:
        _logger.warning("report_finding validation failed: %s", exc)
        return _text(ErrorResponse(error=f"Failed to report finding: {exc}", code=ErrorCode.VALIDATION))
    except sqlite3.Error as exc:
        _logger.error("report_finding storage failure: %s", exc)
        return _text(ErrorResponse(error=f"Failed to report finding: {exc}", code=ErrorCode.IO))

    reported_file_id = finding.get(INGESTED_FILE_ID_KEY)
    reported_line_start = finding.get("line_start")
    lookup_line_start = (
        reported_line_start if isinstance(reported_line_start, int) and not isinstance(reported_line_start, bool) else None
    )
    finding_record = _reported_finding_record(
        tracker,
        result,
        file_id=reported_file_id if isinstance(reported_file_id, str) else None,
        rule_id=rule_id,
        line_start=lookup_line_start,
        message=message,
        severity=severity,
    )
    if finding_record is None:
        return _text(ErrorResponse(error="Reported finding was not found after ingestion", code=ErrorCode.IO))

    observation_ids = (
        _report_finding_observation_ids(tracker, file_id=finding_record["file_id"], finding_id=finding_record["id"])
        if create_observation
        else []
    )
    response: dict[str, Any] = {
        **finding_to_mcp(finding_record),
        "finding_result": "created" if result["findings_created"] else "updated",
    }
    if observation_ids:
        response["observation_id"] = observation_ids[0]
    if response_detail_raw == "full":
        # Legacy batch-style stats — useful when a caller wants the ingest summary
        # numbers (e.g. for multi-finding ingestion). Slim default drops these
        # because for a single-finding write they're constants that read like noise.
        response["findings_created"] = result["findings_created"]
        response["findings_updated"] = result["findings_updated"]
        response["file_created"] = result["files_created"] > 0
        response["observations_created"] = result["observations_created"]
        response["observations_failed"] = result["observations_failed"]
        response["observation_ids"] = observation_ids
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return _text(response)


async def _handle_trigger_scan(arguments: dict[str, Any]) -> list[TextContent]:
    from datetime import UTC, datetime

    from filigree.mcp_server import _get_db, _get_filigree_dir, _safe_path

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))

    args = _parse_args(arguments, TriggerScanArgs)
    tracker = _get_db()
    scanner_name = args.get("scanner")
    if not isinstance(scanner_name, str):
        return _text(ErrorResponse(error="scanner must be a string", code=ErrorCode.VALIDATION))
    file_path = args.get("file_path")
    if not isinstance(file_path, str):
        return _text(ErrorResponse(error="file_path must be a string", code=ErrorCode.VALIDATION))
    prompt = args.get("prompt", "bug-hunt")
    if not isinstance(prompt, str):
        return _text(ErrorResponse(error="prompt must be a string", code=ErrorCode.VALIDATION))
    api_url_arg = args.get("api_url")
    if api_url_arg is not None and not isinstance(api_url_arg, str):
        return _text(ErrorResponse(error="api_url must be a string", code=ErrorCode.VALIDATION))
    prompt_err = _validate_prompt_pack(prompt)
    if prompt_err is not None:
        return _text(prompt_err)
    api_resolution, api_resolution_err = _resolve_scanner_api_url_or_error(filigree_dir, explicit_api_url=api_url_arg)
    if api_resolution_err is not None:
        return _text(api_resolution_err)
    assert api_resolution is not None  # noqa: S101
    api_url = api_resolution.url

    url_err = _validate_localhost_url(api_url)
    if url_err is not None:
        return _text(url_err)

    try:
        target = _safe_path(file_path)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))

    cfg, err = _load_scanner_or_error(filigree_dir, scanner_name)
    if err is not None:
        return _text(err)
    assert cfg is not None  # noqa: S101  -- narrowing after error-check
    prompt_support_err = _validate_scanner_accepts_prompt(cfg, prompt)
    if prompt_support_err is not None:
        return _text(prompt_support_err)

    if not target.is_file():
        return _text(ErrorResponse(error=f"File not found: {file_path}", code=ErrorCode.NOT_FOUND))

    file_type_warning = ""
    if cfg.file_types:
        ext = Path(file_path).suffix.lstrip(".")
        if ext and ext not in cfg.file_types:
            file_type_warning = f"Warning: file extension {ext!r} not in scanner's declared file_types {cfg.file_types}. Proceeding anyway."

    canonical_path = str(target.relative_to(filigree_dir.resolve().parent))

    try:
        file_record = tracker.register_file(canonical_path)
    except (RegistryResolutionError, RegistryUnavailableError) as exc:
        return _registry_error_text(exc, action="triggering scan")
    project_root = filigree_dir.parent
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    scan_run_id = f"{scanner_name}-{ts}-{secrets.token_hex(3)}"

    # Reserve the scan run BEFORE spawning. ``reserve_scan_run`` atomically
    # checks cooldown and inserts a pending row; a concurrent trigger will
    # see the reservation and get rate_limited. This closes the TOCTOU
    # between check_scan_cooldown and create_scan_run.
    try:
        created, blocking_run = tracker.reserve_scan_run(
            scan_run_id=scan_run_id,
            scanner_name=scanner_name,
            scan_source=scanner_name,
            file_path=canonical_path,
            file_id=file_record.id,
            api_url=api_url,
        )
    except (sqlite3.Error, ValueError) as exc:
        _logger.error("Failed to reserve scan run %s: %s", scan_run_id, exc)
        return _text(
            ErrorResponse(
                error=f"Failed to reserve scan run: {exc}",
                code=ErrorCode.IO,
            )
        )
    if blocking_run is not None:
        # Cooldown conflict: a prior run for this (scanner, file) is still
        # within the cooldown window. CONFLICT (not IO) is the correct
        # retriable-soon semantic — clients can poll the blocking run's
        # status via details.blocking_run_id and retry when it completes.
        return _text(
            ErrorResponse(
                error=(
                    f"Scanner {scanner_name!r} was already triggered for {file_path!r} recently. Retry after the blocking run completes."
                ),
                code=ErrorCode.CONFLICT,
                details={"blocking_run_id": blocking_run["id"]},
            )
        )
    assert created is not None  # noqa: S101  -- exactly one of (created, blocking) is set

    try:
        spawn_result = _spawn_scan(
            cfg=cfg,
            canonical_path=canonical_path,
            api_url=api_url,
            project_root=project_root,
            scan_run_id=scan_run_id,
            filigree_dir=filigree_dir,
            prompt=prompt,
        )
    except ScannerSpawnError as exc:
        status_update_error = _mark_scan_run_failed(
            tracker,
            scan_run_id,
            error_message="Scanner process failed to spawn",
            context="single spawn failure",
        )
        err_resp = ErrorResponse(error=str(exc), code=exc.code)
        if exc.details:
            err_resp["details"] = exc.details
        if status_update_error:
            spawn_failure_details = dict(err_resp.get("details", {}))
            spawn_failure_details["status_update_error"] = status_update_error
            err_resp["details"] = spawn_failure_details
        return _text(err_resp)

    proc = spawn_result["proc"]
    scan_log_path = spawn_result["scan_log_path"]
    log_rel = str(scan_log_path.relative_to(filigree_dir.parent))

    # Backfill PID/log onto the reservation and transition to running.
    try:
        tracker.set_scan_run_spawn_info(scan_run_id, pid=proc.pid, log_path=log_rel)
        tracker.update_scan_run_status(scan_run_id, "running")
    except (sqlite3.Error, KeyError, ValueError) as exc:
        with contextlib.suppress(OSError):
            proc.kill()
        status_update_error = _mark_scan_run_failed(
            tracker,
            scan_run_id,
            error_message=f"Scanner process terminated after DB tracking failed: {exc}",
            context="single DB tracking failure",
        )
        _logger.error(
            "Failed to finalize scan run %s (pid %d killed): %s",
            scan_run_id,
            proc.pid,
            exc,
        )
        err_resp = ErrorResponse(
            error=f"Scan process spawned but DB tracking failed: {exc}. Process (pid={proc.pid}) terminated.",
            code=ErrorCode.IO,
        )
        if status_update_error:
            err_resp["details"] = {"status_update_error": status_update_error}
        return _text(err_resp)

    await asyncio.sleep(0.2)
    exit_code = proc.poll()
    if exit_code is not None and exit_code != 0:
        status_update_error = _mark_scan_run_failed(
            tracker,
            scan_run_id,
            context="single immediate exit",
            exit_code=exit_code,
            error_message=f"Scanner exited immediately with code {exit_code}",
        )
        log_hint = ""
        if scan_log_path.exists() and scan_log_path.stat().st_size > 0:
            log_hint = f" Check log: {log_rel}"
        elif spawn_result.get("log_warning"):
            log_hint = f" Note: {spawn_result['log_warning']}"
        details: dict[str, Any] = {
            "scanner": scanner_name,
            "file_id": file_record.id,
            "scan_run_id": scan_run_id,
            "exit_code": exit_code,
            "log_path": log_rel,
        }
        if status_update_error:
            details["status_update_error"] = status_update_error
        return _text(
            ErrorResponse(
                error=f"Scanner process exited immediately with code {exit_code}.{log_hint}",
                code=ErrorCode.IO,
                details=details,
            )
        )

    _logger.info(
        "Spawned scanner %s for %s (pid=%d, run_id=%s)",
        scanner_name,
        file_path,
        proc.pid,
        scan_run_id,
    )

    scan_result: dict[str, Any] = {
        "status": "triggered",
        "scanner": scanner_name,
        "file_path": file_path,
        "file_id": file_record.id,
        "scan_run_id": scan_run_id,
        "pid": proc.pid,
        "log_path": log_rel,
        "api_url": api_url,
        "api_url_source": api_resolution.source,
        "sandbox_summary": cfg.sandbox_summary(),
        "sandbox_class": cfg.sandbox_class(),
        **cfg.risk_metadata(),
        "message": (
            f"Scan triggered with run_id={scan_run_id!r}. "
            f"Results will be POSTed to {api_url}. "
            f"Poll findings via file_id={file_record.id!r} or status via get_scan_status. "
            f"Scanner log: {log_rel}"
        ),
    }
    warnings: list[str] = []
    if file_type_warning:
        warnings.append(file_type_warning)
    if spawn_result.get("log_warning"):
        warnings.append(spawn_result["log_warning"])
    if warnings:
        scan_result["warnings"] = warnings
    return _text(scan_result)


async def _handle_trigger_scan_batch(arguments: dict[str, Any]) -> list[TextContent]:
    from datetime import UTC, datetime

    from filigree.mcp_server import _get_db, _get_filigree_dir, _safe_path

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))

    args = _parse_args(arguments, TriggerScanBatchArgs)
    tracker = _get_db()
    scanner_name = args.get("scanner")
    if not isinstance(scanner_name, str):
        return _text(ErrorResponse(error="scanner must be a string", code=ErrorCode.VALIDATION))
    file_paths = args.get("file_paths", [])
    prompt = args.get("prompt", "bug-hunt")
    if not isinstance(prompt, str):
        return _text(ErrorResponse(error="prompt must be a string", code=ErrorCode.VALIDATION))
    api_url_arg = args.get("api_url")
    if api_url_arg is not None and not isinstance(api_url_arg, str):
        return _text(ErrorResponse(error="api_url must be a string", code=ErrorCode.VALIDATION))
    prompt_err = _validate_prompt_pack(prompt)
    if prompt_err is not None:
        return _text(prompt_err)
    api_resolution, api_resolution_err = _resolve_scanner_api_url_or_error(filigree_dir, explicit_api_url=api_url_arg)
    if api_resolution_err is not None:
        return _text(api_resolution_err)
    assert api_resolution is not None  # noqa: S101
    api_url = api_resolution.url

    if not isinstance(file_paths, list) or not file_paths:
        return _text(ErrorResponse(error="file_paths must be a non-empty list", code=ErrorCode.VALIDATION))
    if any(not isinstance(fp, str) for fp in file_paths):
        return _text(ErrorResponse(error="file_paths entries must be strings", code=ErrorCode.VALIDATION))

    max_batch_size = 500
    if len(file_paths) > max_batch_size:
        return _text(
            ErrorResponse(
                error=f"file_paths length {len(file_paths)} exceeds maximum of {max_batch_size}",
                code=ErrorCode.VALIDATION,
            )
        )

    url_err = _validate_localhost_url(api_url)
    if url_err is not None:
        return _text(url_err)

    cfg, err = _load_scanner_or_error(filigree_dir, scanner_name)
    if err is not None:
        return _text(err)
    assert cfg is not None  # noqa: S101  -- narrowing after error-check
    prompt_support_err = _validate_scanner_accepts_prompt(cfg, prompt)
    if prompt_support_err is not None:
        return _text(prompt_support_err)

    # Validate and resolve all paths. Dedupe repeated file_paths in the
    # request so we don't attempt to reserve the same (scanner, file) twice —
    # the second reservation would block itself via cooldown.
    canonical_paths: list[str] = []
    file_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    seen_canonical: set[str] = set()
    for fp in file_paths:
        try:
            target = _safe_path(fp)
        except ValueError as e:
            skipped.append({"file_path": fp, "reason": str(e)})
            continue
        if not target.is_file():
            skipped.append({"file_path": fp, "reason": "File not found"})
            continue
        cp = str(target.relative_to(filigree_dir.resolve().parent))
        if cp in seen_canonical:
            skipped.append({"file_path": fp, "reason": "duplicate"})
            continue
        seen_canonical.add(cp)
        try:
            file_record = tracker.register_file(cp)
        except (RegistryResolutionError, RegistryUnavailableError) as exc:
            return _registry_error_text(exc, action="triggering batch scan")
        canonical_paths.append(cp)
        file_ids.append(file_record.id)

    if not canonical_paths:
        return _text(
            ErrorResponse(
                error="No files eligible for scanning",
                code=ErrorCode.VALIDATION,
                details={"skipped": skipped},
            )
        )

    project_root = filigree_dir.parent
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    # batch_id is a caller-facing correlation string; each file also gets its
    # own scan_run_id so per-file lifecycles (PID, log, completion) don't
    # collide — previously a single shared scan_run_id caused the fastest
    # child's completion POST to finalize the run before the others finished.
    batch_id = f"{scanner_name}-batch-{ts}-{secrets.token_hex(3)}"

    # Reserve a per-file scan_run BEFORE spawning. Any cooldown conflicts
    # surface here atomically. Reserved rows carry status='pending' until
    # the spawn succeeds and backfills pid/log_path.
    reserved: list[dict[str, Any]] = []
    for i, (cp, fid) in enumerate(zip(canonical_paths, file_ids, strict=True)):
        child_run_id = f"{batch_id}-{i}"
        try:
            created, blocking = tracker.reserve_scan_run(
                scan_run_id=child_run_id,
                scanner_name=scanner_name,
                scan_source=scanner_name,
                file_path=cp,
                file_id=fid,
                api_url=api_url,
            )
        except (sqlite3.Error, ValueError) as exc:
            _logger.warning("reserve_scan_run failed for %s: %s", cp, exc)
            skipped.append({"file_path": cp, "reason": f"reservation_failed: {exc}"})
            continue
        if blocking is not None:
            skipped.append({"file_path": cp, "reason": "rate_limited", "blocking_run_id": blocking["id"]})
            continue
        assert created is not None  # noqa: S101
        reserved.append(
            {
                "scan_run_id": child_run_id,
                "canonical_path": cp,
                "file_id": fid,
                "index": i,
            }
        )

    if not reserved:
        if skipped and all(item["reason"] == "rate_limited" for item in skipped):
            blocking_run_ids = [item["blocking_run_id"] for item in skipped if "blocking_run_id" in item]
            return _text(
                ErrorResponse(
                    error="All files are blocked by recent scanner runs. Retry after the blocking run(s) complete.",
                    code=ErrorCode.CONFLICT,
                    details={
                        "skipped": skipped,
                        "blocking_run_ids": blocking_run_ids,
                    },
                )
            )
        return _text(
            ErrorResponse(
                error="No files eligible for scanning",
                code=ErrorCode.VALIDATION,
                details={"skipped": skipped},
            )
        )

    # Spawn one scanner process per reserved run.  On failure, transition
    # that run to 'failed' and record a spawn_error; the others proceed.
    spawned: list[dict[str, Any]] = []
    spawn_errors: list[dict[str, str]] = []
    for entry in reserved:
        cp = entry["canonical_path"]
        child_run_id = entry["scan_run_id"]
        try:
            spawn_result = _spawn_scan(
                cfg=cfg,
                canonical_path=cp,
                api_url=api_url,
                project_root=project_root,
                scan_run_id=child_run_id,
                filigree_dir=filigree_dir,
                prompt=prompt,
                log_suffix=f"-{entry['index']}",
            )
        except ScannerSpawnError as exc:
            reason = str(exc)
            error_item = {"file_path": cp, "reason": reason}
            status_update_error = _mark_scan_run_failed(
                tracker,
                child_run_id,
                error_message=f"Scanner process failed to spawn: {reason}",
                context="batch spawn failure",
            )
            if status_update_error:
                error_item["status_update_error"] = status_update_error
            spawn_errors.append(error_item)
            continue
        entry["spawn_result"] = spawn_result
        spawned.append(entry)

    if not spawned:
        return _text(
            ErrorResponse(
                error="All scanner processes failed to spawn",
                code=ErrorCode.IO,
                details={
                    "spawn_errors": spawn_errors,
                    "skipped": skipped,
                    "batch_id": batch_id,
                },
            )
        )

    # Backfill pid/log_path onto each reservation and transition to running.
    # If backfill fails for any run, kill that one process and mark it
    # failed; the rest of the batch stays alive.
    finalized: list[dict[str, Any]] = []
    for entry in spawned:
        spawn_result = entry["spawn_result"]
        proc = spawn_result["proc"]
        scan_log_path = spawn_result["scan_log_path"]
        log_rel = str(scan_log_path.relative_to(filigree_dir.parent))
        try:
            tracker.set_scan_run_spawn_info(entry["scan_run_id"], pid=proc.pid, log_path=log_rel)
            tracker.update_scan_run_status(entry["scan_run_id"], "running")
        except (sqlite3.Error, KeyError, ValueError) as exc:
            with contextlib.suppress(OSError):
                proc.kill()
            status_update_error = _mark_scan_run_failed(
                tracker,
                entry["scan_run_id"],
                error_message=f"Scanner process terminated after DB tracking failed: {exc}",
                context="batch DB tracking failure",
            )
            _logger.error(
                "Failed to finalize scan run %s (pid %d killed): %s",
                entry["scan_run_id"],
                proc.pid,
                exc,
            )
            error_item = {"file_path": entry["canonical_path"], "reason": f"db_tracking_failed: {exc}"}
            if status_update_error:
                error_item["status_update_error"] = status_update_error
            spawn_errors.append(error_item)
            continue
        entry["log_rel"] = log_rel
        entry["pid"] = proc.pid
        finalized.append(entry)

    if not finalized:
        return _text(
            ErrorResponse(
                error="All scanner processes spawned but DB tracking failed",
                code=ErrorCode.IO,
                details={
                    "spawn_errors": spawn_errors,
                    "skipped": skipped,
                    "batch_id": batch_id,
                },
            )
        )

    # Quick check: did any process exit immediately with error?
    await asyncio.sleep(0.2)
    immediate_failures = 0
    status_update_errors: list[dict[str, str]] = []
    for entry in finalized:
        proc = entry["spawn_result"]["proc"]
        ec = proc.poll()
        if ec is not None and ec != 0:
            immediate_failures += 1
            status_update_error = _mark_scan_run_failed(
                tracker,
                entry["scan_run_id"],
                exit_code=ec,
                error_message="Scanner exited immediately",
                context="batch immediate exit",
            )
            if status_update_error:
                status_update_errors.append(
                    {
                        "scan_run_id": entry["scan_run_id"],
                        "file_path": entry["canonical_path"],
                        "error": status_update_error,
                    }
                )

    scan_run_ids = [entry["scan_run_id"] for entry in finalized]
    per_file = [
        {
            "scan_run_id": entry["scan_run_id"],
            "file_path": entry["canonical_path"],
            "file_id": entry["file_id"],
            "pid": entry["pid"],
            "log_path": entry["log_rel"],
        }
        for entry in finalized
    ]

    if immediate_failures == len(finalized):
        return _text(
            ErrorResponse(
                error=f"All {len(finalized)} scanner processes exited immediately.",
                code=ErrorCode.IO,
                details={
                    "batch_id": batch_id,
                    "scan_run_ids": scan_run_ids,
                    "per_file": per_file,
                    **({"spawn_errors": spawn_errors} if spawn_errors else {}),
                    **({"skipped": skipped} if skipped else {}),
                    **({"status_update_errors": status_update_errors} if status_update_errors else {}),
                },
            )
        )

    result: dict[str, Any] = {
        "status": "triggered",
        "scanner": scanner_name,
        "file_count": len(finalized),
        "processes_spawned": len(finalized),
        "batch_id": batch_id,
        "scan_run_ids": scan_run_ids,
        "per_file": per_file,
        "api_url": api_url,
        "api_url_source": api_resolution.source,
        "sandbox_summary": cfg.sandbox_summary(),
        "sandbox_class": cfg.sandbox_class(),
        **cfg.risk_metadata(),
    }
    if spawn_errors:
        result["spawn_errors"] = spawn_errors
    if skipped:
        result["skipped"] = skipped
    if immediate_failures:
        result["immediate_failures"] = immediate_failures
    if status_update_errors:
        result["status_update_errors"] = status_update_errors
    log_warnings = [entry["spawn_result"]["log_warning"] for entry in finalized if entry["spawn_result"].get("log_warning")]
    if log_warnings:
        result["warnings"] = log_warnings
    return _text(result)


async def _handle_get_scan_status(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetScanStatusArgs)
    scan_run_id = args.get("scan_run_id", "")
    if not isinstance(scan_run_id, str) or not scan_run_id.strip():
        return _text(ErrorResponse(error="scan_run_id is required", code=ErrorCode.VALIDATION))
    log_lines = args.get("log_lines", 50)

    err_resp = _validate_int_range(log_lines, "log_lines", min_val=1, max_val=500)
    if err_resp is not None:
        return err_resp

    tracker = _get_db()
    try:
        status = tracker.get_scan_status(scan_run_id, log_lines=log_lines)
    except KeyError:
        return _text(ErrorResponse(error=f"Scan run not found: {scan_run_id}", code=ErrorCode.NOT_FOUND))
    except sqlite3.Error as exc:
        _logger.error("Database error getting scan status for %s: %s", scan_run_id, exc)
        return _text(ErrorResponse(error=f"Database error: {exc}", code=ErrorCode.IO))
    return _text(status)


async def _handle_preview_scan(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_filigree_dir, _safe_path

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))

    args = _parse_args(arguments, PreviewScanArgs)
    scanner_name = args["scanner"]
    file_path = args["file_path"]
    prompt = args.get("prompt", "bug-hunt")
    prompt_err = _validate_prompt_pack(prompt)
    if prompt_err is not None:
        return _text(prompt_err)

    try:
        target = _safe_path(file_path)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))

    cfg, err = _load_scanner_or_error(filigree_dir, scanner_name)
    if err is not None:
        return _text(err)
    assert cfg is not None  # noqa: S101  -- narrowing after error-check
    prompt_support_err = _validate_scanner_accepts_prompt(cfg, prompt)
    if prompt_support_err is not None:
        return _text(prompt_support_err)

    canonical_path = str(target.relative_to(filigree_dir.resolve().parent))
    project_root = filigree_dir.parent
    api_resolution, api_resolution_err = _resolve_scanner_api_url_or_error(filigree_dir)
    if api_resolution_err is not None:
        return _text(api_resolution_err)
    assert api_resolution is not None  # noqa: S101
    try:
        cmd = cfg.build_command(
            file_path=canonical_path,
            api_url=api_resolution.url,
            project_root=str(project_root),
            scan_run_id="preview-dry-run",
            prompt=prompt,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))

    cmd_err = validate_scanner_command(cmd, project_root=project_root)

    return _text(
        {
            "scanner": scanner_name,
            "file_path": file_path,
            "command": cmd,
            "command_string": shlex.join(cmd),
            "api_url": api_resolution.url,
            "api_url_source": api_resolution.source,
            "valid": cmd_err is None,
            "validation_error": cmd_err,
            "sandbox_summary": cfg.sandbox_summary(),
            "sandbox_class": cfg.sandbox_class(),
            **cfg.risk_metadata(),
        }
    )
