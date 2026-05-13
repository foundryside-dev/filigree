"""MCP tools for file tracking, associations, and finding triage."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from mcp.types import TextContent, Tool

from filigree.core import VALID_ASSOC_TYPES, VALID_FINDING_STATUSES, VALID_SEVERITIES
from filigree.issue_payloads import issue_to_public
from filigree.mcp_tools.common import (
    _list_response,
    _parse_args,
    _text,
    _validate_actor,
    _validate_int_range,
    _validate_str,
)
from filigree.mcp_tools.payloads import (
    file_assoc_to_mcp,
    file_detail_to_mcp,
    file_record_to_mcp,
    finding_to_mcp,
    timeline_entry_to_mcp,
)
from filigree.types.api import BatchFailure, BatchResponse, ErrorCode, ErrorResponse, parse_response_detail
from filigree.types.core import FindingStatus
from filigree.types.inputs import (
    AddFileAssociationArgs,
    BatchUpdateFindingsArgs,
    DeleteFileRecordArgs,
    DismissFindingArgs,
    GetFileArgs,
    GetFileTimelineArgs,
    GetFindingArgs,
    GetIssueFilesArgs,
    ListFilesArgs,
    ListFindingsArgs,
    PromoteFindingArgs,
    RegisterFileArgs,
    UpdateFindingArgs,
)

_logger = logging.getLogger(__name__)


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for file-domain tools."""
    tools = [
        Tool(
            name="list_files",
            description="List tracked files with filtering, sorting, and pagination.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 10000},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "language": {"type": "string", "description": "Filter by language"},
                    "path_prefix": {"type": "string", "description": "Filter by substring in file path"},
                    "min_findings": {"type": "integer", "minimum": 0, "description": "Minimum open findings count"},
                    "has_severity": {
                        "type": "string",
                        "enum": sorted(VALID_SEVERITIES),
                        "description": "Require at least one open finding at this severity",
                    },
                    "scan_source": {"type": "string", "description": "Filter files by finding source"},
                    "sort": {
                        "type": "string",
                        "enum": ["updated_at", "first_seen", "path", "language"],
                        "default": "updated_at",
                    },
                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                },
            },
        ),
        Tool(
            name="get_file",
            description="Get file details, linked issues, recent findings, and summary by file ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="get_file_timeline",
            description="Get merged timeline events for a file (finding, association, metadata updates).",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID"},
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 10000},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "event_type": {
                        "type": "string",
                        "enum": ["finding", "association", "file_metadata_update", "issue_event"],
                        "description": "Optional event type filter",
                    },
                    "include_issue_events": {
                        "type": "boolean",
                        "default": False,
                        "description": "Merge events from issues currently associated with the file",
                    },
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="delete_file_record",
            description=(
                "Delete a file record. Refuses by default when associations or open findings exist; "
                "force=true cascades file associations and findings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID"},
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Cascade associations and open findings",
                    },
                    "actor": {"type": "string", "description": "Actor identity for audit attribution"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="get_issue_files",
            description="List files associated with an issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="add_file_association",
            description="Create a file<->issue association. Idempotent for duplicate tuples.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID"},
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "assoc_type": {
                        "type": "string",
                        "enum": sorted(VALID_ASSOC_TYPES),
                        "description": "Association type",
                    },
                    "actor": {"type": "string", "description": "Actor identity for audit attribution"},
                },
                "required": ["file_id", "issue_id", "assoc_type"],
            },
        ),
        Tool(
            name="register_file",
            description="Register or fetch a file record by project-relative path without running a scanner.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to project root)"},
                    "language": {"type": "string", "description": "Optional language hint"},
                    "file_type": {"type": "string", "description": "Optional file type tag"},
                    "metadata": {"type": "object", "description": "Optional metadata map"},
                    "actor": {"type": "string", "description": "Actor identity for audit attribution"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="get_finding",
            description="Get a single scan finding by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string", "description": "Finding ID"},
                },
                "required": ["finding_id"],
            },
        ),
        Tool(
            name="list_findings",
            description="List scan findings across all files with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": sorted(VALID_SEVERITIES), "description": "Filter by severity"},
                    "status": {"type": "string", "enum": sorted(VALID_FINDING_STATUSES), "description": "Filter by finding status"},
                    "scan_source": {"type": "string", "description": "Filter by scan source"},
                    "scan_run_id": {"type": "string", "description": "Filter by scan run ID"},
                    "file_id": {"type": "string", "description": "Filter by file ID"},
                    "issue_id": {"type": "string", "description": "Filter by linked issue ID"},
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 10000},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                },
            },
        ),
        Tool(
            name="update_finding",
            description="Update a finding's status or linked issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string", "description": "Finding ID"},
                    "status": {"type": "string", "enum": sorted(VALID_FINDING_STATUSES), "description": "New finding status"},
                    "issue_id": {"type": "string", "description": "Issue ID to link"},
                    "actor": {"type": "string", "description": "Actor identity for audit attribution"},
                },
                "required": ["finding_id"],
            },
        ),
        Tool(
            name="batch_update_findings",
            description=(
                "Update the status of multiple findings at once. Returns "
                "BatchResponse[str] (succeeded finding IDs) by default, or "
                "BatchResponse[ScanFindingDict] when response_detail='full'. "
                "failed[] is always present (empty if none)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "finding_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of finding IDs to update",
                    },
                    "status": {"type": "string", "enum": sorted(VALID_FINDING_STATUSES), "description": "New status for all findings"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns finding ID strings in succeeded[]; 'full' returns full ScanFindingDict records.",
                    },
                    "actor": {"type": "string", "description": "Actor identity for audit attribution"},
                },
                "required": ["finding_ids", "status"],
            },
        ),
        Tool(
            name="promote_finding",
            description="Promote a scan finding directly to a tracked issue and link the finding to it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string", "description": "Finding ID"},
                    "priority": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "description": "Override priority (default: inferred from severity)",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional labels to attach to the promoted issue. Use this to "
                            "carry session-cluster context (e.g. ['cluster:mcp-review-e']) "
                            "onto promoted findings."
                        ),
                    },
                    "actor": {"type": "string", "description": "Actor identity"},
                },
                "required": ["finding_id"],
            },
        ),
        Tool(
            name="dismiss_finding",
            description=(
                "Dismiss a finding by transitioning it to a non-open status. Default status is "
                "'false_positive'; pass status= to land in an alternate dismissal status "
                "('fixed', 'unseen_in_latest', 'acknowledged'). (filigree-cb980eee0d, P3.13.)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string", "description": "Finding ID"},
                    "reason": {"type": "string", "description": "Optional reason for dismissal"},
                    "status": {
                        "type": "string",
                        "enum": ["false_positive", "fixed", "unseen_in_latest", "acknowledged"],
                        "default": "false_positive",
                        "description": (
                            "Dismissal status. Defaults to 'false_positive' to match the legacy "
                            "shape; pass an alternate status to record 'won't fix here' (acknowledged) "
                            "or 'no longer present' (unseen_in_latest)."
                        ),
                    },
                    "actor": {"type": "string", "description": "Actor identity for audit attribution"},
                },
                "required": ["finding_id"],
            },
        ),
    ]

    handlers: dict[str, Callable[..., Any]] = {
        "list_files": _handle_list_files,
        "get_file": _handle_get_file,
        "delete_file_record": _handle_delete_file_record,
        "get_file_timeline": _handle_get_file_timeline,
        "get_issue_files": _handle_get_issue_files,
        "add_file_association": _handle_add_file_association,
        "register_file": _handle_register_file,
        "get_finding": _handle_get_finding,
        "list_findings": _handle_list_findings,
        "update_finding": _handle_update_finding,
        "batch_update_findings": _handle_batch_update_findings,
        "promote_finding": _handle_promote_finding,
        "dismiss_finding": _handle_dismiss_finding,
    }

    return tools, handlers


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_list_files(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, ListFilesArgs)
    tracker = _get_db()
    limit = args.get("limit", 100)
    offset = args.get("offset", 0)
    min_findings = args.get("min_findings")
    has_severity = args.get("has_severity")
    language = args.get("language")
    path_prefix = args.get("path_prefix")
    scan_source = args.get("scan_source")
    sort = args.get("sort", "updated_at")
    direction = args.get("direction")
    valid_sorts = {"updated_at", "first_seen", "path", "language"}

    for err in (
        _validate_int_range(limit, "limit", min_val=1, max_val=10000),
        _validate_int_range(offset, "offset", min_val=0),
        _validate_int_range(min_findings, "min_findings", min_val=0),
        _validate_str(language, "language"),
        _validate_str(path_prefix, "path_prefix"),
        _validate_str(scan_source, "scan_source"),
    ):
        if err is not None:
            return err
    if has_severity is not None and (not isinstance(has_severity, str) or has_severity not in VALID_SEVERITIES):
        return _text(ErrorResponse(error=f"has_severity must be one of {sorted(VALID_SEVERITIES)}", code=ErrorCode.VALIDATION))
    if not isinstance(sort, str) or sort not in valid_sorts:
        return _text(ErrorResponse(error=f"sort must be one of {sorted(valid_sorts)}", code=ErrorCode.VALIDATION))
    if direction is not None and (not isinstance(direction, str) or direction.upper() not in {"ASC", "DESC"}):
        return _text(ErrorResponse(error="direction must be 'asc' or 'desc'", code=ErrorCode.VALIDATION))

    files_result = tracker.list_files_paginated(
        limit=limit,
        offset=offset,
        language=language,
        path_prefix=path_prefix,
        min_findings=min_findings,
        has_severity=has_severity,
        scan_source=scan_source,
        sort=sort,
        direction=direction,
    )
    items = [file_record_to_mcp(item) for item in files_result["results"]]
    has_more = bool(files_result["has_more"])
    next_offset = offset + len(items) if has_more else None
    return _text(_list_response(items, has_more=has_more, next_offset=next_offset))


async def _handle_get_file(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetFileArgs)
    tracker = _get_db()
    file_id = args.get("file_id", "")
    if not isinstance(file_id, str) or not file_id.strip():
        return _text(ErrorResponse(error="file_id is required", code=ErrorCode.VALIDATION))
    try:
        data = tracker.get_file_detail(file_id)
    except KeyError:
        return _text(ErrorResponse(error=f"File not found: {file_id}", code=ErrorCode.NOT_FOUND))
    return _text(file_detail_to_mcp(data))


async def _handle_delete_file_record(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, DeleteFileRecordArgs)
    tracker = _get_db()
    file_id = args.get("file_id", "")
    force = args.get("force", False)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    if not isinstance(file_id, str) or not file_id.strip():
        return _text(ErrorResponse(error="file_id is required", code=ErrorCode.VALIDATION))
    if not isinstance(force, bool):
        return _text(ErrorResponse(error="force must be a boolean", code=ErrorCode.VALIDATION))
    try:
        result = tracker.delete_file_record(file_id, force=force, actor=actor)
    except KeyError:
        return _text(ErrorResponse(error=f"File not found: {file_id}", code=ErrorCode.NOT_FOUND))
    except ValueError as exc:
        code = ErrorCode.CONFLICT if "Cannot delete file record" in str(exc) else ErrorCode.VALIDATION
        return _text(ErrorResponse(error=str(exc), code=code))
    except sqlite3.Error as exc:
        return _text(ErrorResponse(error=f"Database error: {exc}", code=ErrorCode.IO))
    return _text(result)


async def _handle_get_file_timeline(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetFileTimelineArgs)
    tracker = _get_db()
    file_id = args.get("file_id", "")
    limit = args.get("limit", 50)
    offset = args.get("offset", 0)
    event_type = args.get("event_type")
    include_issue_events = args.get("include_issue_events", False)
    valid_event_types = {"finding", "association", "file_metadata_update", "issue_event"}

    if not isinstance(file_id, str) or not file_id.strip():
        return _text(ErrorResponse(error="file_id is required", code=ErrorCode.VALIDATION))
    if not isinstance(limit, int) or limit < 1 or limit > 10000:
        return _text(ErrorResponse(error="limit must be an integer in [1, 10000]", code=ErrorCode.VALIDATION))
    if not isinstance(offset, int) or offset < 0:
        return _text(ErrorResponse(error="offset must be a non-negative integer", code=ErrorCode.VALIDATION))
    if event_type is not None and (not isinstance(event_type, str) or event_type not in valid_event_types):
        return _text(
            ErrorResponse(
                error=f"event_type must be one of {sorted(valid_event_types)}",
                code=ErrorCode.VALIDATION,
            )
        )
    if not isinstance(include_issue_events, bool):
        return _text(ErrorResponse(error="include_issue_events must be a boolean", code=ErrorCode.VALIDATION))

    try:
        timeline_result = tracker.get_file_timeline(
            file_id,
            limit=limit,
            offset=offset,
            event_type=event_type,
            include_issue_events=include_issue_events,
        )
    except KeyError:
        return _text(ErrorResponse(error=f"File not found: {file_id}", code=ErrorCode.NOT_FOUND))
    items = [timeline_entry_to_mcp(item) for item in timeline_result["results"]]
    has_more = timeline_result["has_more"]
    next_offset = (offset + len(items)) if has_more else None
    return _text(_list_response(items, has_more=has_more, next_offset=next_offset))


async def _handle_get_issue_files(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetIssueFilesArgs)
    tracker = _get_db()
    issue_id = args.get("issue_id", "")
    if not isinstance(issue_id, str) or not issue_id.strip():
        return _text(ErrorResponse(error="issue_id is required", code=ErrorCode.VALIDATION))
    try:
        tracker.get_issue(issue_id)
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {issue_id}", code=ErrorCode.NOT_FOUND))
    items = [file_assoc_to_mcp(item) for item in tracker.get_issue_files(issue_id)]
    return _text(_list_response(items, has_more=False))


async def _handle_add_file_association(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, AddFileAssociationArgs)
    tracker = _get_db()
    file_id = args.get("file_id", "")
    issue_id = args.get("issue_id", "")
    assoc_type = args.get("assoc_type", "")
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    if not isinstance(file_id, str) or not file_id.strip():
        return _text(ErrorResponse(error="file_id is required", code=ErrorCode.VALIDATION))
    if not isinstance(issue_id, str) or not issue_id.strip():
        return _text(ErrorResponse(error="issue_id is required", code=ErrorCode.VALIDATION))
    if not isinstance(assoc_type, str) or not assoc_type.strip():
        return _text(ErrorResponse(error="assoc_type is required", code=ErrorCode.VALIDATION))

    try:
        tracker.get_file(file_id)
    except KeyError:
        return _text(ErrorResponse(error=f"File not found: {file_id}", code=ErrorCode.NOT_FOUND))

    try:
        tracker.get_issue(issue_id)
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {issue_id}", code=ErrorCode.NOT_FOUND))

    try:
        tracker.add_file_association(file_id, issue_id, assoc_type, actor=actor)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    return _text({"status": "created"})


async def _handle_register_file(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _get_filigree_dir, _safe_path

    args = _parse_args(arguments, RegisterFileArgs)
    tracker = _get_db()
    raw_path = args.get("path", "")
    language = args.get("language", "")
    file_type = args.get("file_type", "")
    metadata = args.get("metadata")
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    if not isinstance(raw_path, str) or not raw_path.strip():
        return _text(ErrorResponse(error="path is required", code=ErrorCode.VALIDATION))
    if language is not None and not isinstance(language, str):
        return _text(ErrorResponse(error="language must be a string", code=ErrorCode.VALIDATION))
    if file_type is not None and not isinstance(file_type, str):
        return _text(ErrorResponse(error="file_type must be a string", code=ErrorCode.VALIDATION))
    if metadata is not None and not isinstance(metadata, dict):
        return _text(ErrorResponse(error="metadata must be an object", code=ErrorCode.VALIDATION))

    try:
        target = _safe_path(raw_path)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))

    filigree_dir = _get_filigree_dir()
    if filigree_dir is None:
        return _text(ErrorResponse(error="Project directory not initialized", code=ErrorCode.NOT_INITIALIZED))

    canonical_path = str(target.relative_to(filigree_dir.resolve().parent))
    try:
        file_record = tracker.register_file(
            canonical_path,
            language=language or "",
            file_type=file_type or "",
            metadata=metadata,
            actor=actor,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    return _text(file_record_to_mcp(file_record.to_dict()))


# ---------------------------------------------------------------------------
# Finding triage handlers
# ---------------------------------------------------------------------------


async def _handle_get_finding(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetFindingArgs)
    finding_id = args.get("finding_id", "")
    if not isinstance(finding_id, str) or not finding_id.strip():
        return _text(ErrorResponse(error="finding_id is required", code=ErrorCode.VALIDATION))
    tracker = _get_db()
    try:
        finding = tracker.get_finding(finding_id)
    except KeyError:
        return _text(ErrorResponse(error=f"Finding not found: {finding_id}", code=ErrorCode.NOT_FOUND))
    return _text(finding_to_mcp(finding))


async def _handle_list_findings(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, ListFindingsArgs)
    tracker = _get_db()
    limit = args.get("limit", 100)
    offset = args.get("offset", 0)

    for err in (
        _validate_int_range(limit, "limit", min_val=1, max_val=10000),
        _validate_int_range(offset, "offset", min_val=0),
    ):
        if err is not None:
            return err

    filters: dict[str, Any] = {}
    for key in ("severity", "status", "scan_source", "scan_run_id", "file_id", "issue_id"):
        val = args.get(key)
        if val is not None:
            filters[key] = val

    # Validate string-type filters from MCP input
    for key in ("scan_source", "scan_run_id", "file_id", "issue_id"):
        val = filters.get(key)
        if val is not None and not isinstance(val, str):
            return _text(ErrorResponse(error=f"{key} must be a string", code=ErrorCode.VALIDATION))

    try:
        result = tracker.list_findings_global(limit=limit, offset=offset, **filters)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    findings = [finding_to_mcp(item) for item in result["findings"]]
    total = int(result["total"])
    has_more = (offset + len(findings)) < total
    next_offset = offset + len(findings) if has_more else None
    return _text(_list_response(findings, has_more=has_more, next_offset=next_offset))


async def _handle_update_finding(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, UpdateFindingArgs)
    finding_id = args.get("finding_id", "")
    if not isinstance(finding_id, str) or not finding_id.strip():
        return _text(ErrorResponse(error="finding_id is required", code=ErrorCode.VALIDATION))
    status = args.get("status")
    issue_id = args.get("issue_id")
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    if status is None and issue_id is None:
        return _text(ErrorResponse(error="At least one of status or issue_id must be provided", code=ErrorCode.VALIDATION))

    tracker = _get_db()
    try:
        updated = tracker.update_finding(finding_id, status=status, issue_id=issue_id, actor=actor)
    except KeyError:
        return _text(ErrorResponse(error=f"Finding not found: {finding_id}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    return _text(finding_to_mcp(updated))


async def _handle_batch_update_findings(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, BatchUpdateFindingsArgs)
    finding_ids = args.get("finding_ids", [])
    status = args.get("status", "")
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    if not isinstance(finding_ids, list) or not finding_ids:
        return _text(ErrorResponse(error="finding_ids must be a non-empty list", code=ErrorCode.VALIDATION))
    if not isinstance(status, str) or not status.strip():
        return _text(ErrorResponse(error="status is required", code=ErrorCode.VALIDATION))
    if status not in VALID_FINDING_STATUSES:
        return _text(
            ErrorResponse(
                error=f"Invalid finding status: {status!r}. Valid: {', '.join(sorted(VALID_FINDING_STATUSES))}",
                code=ErrorCode.VALIDATION,
            )
        )
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)

    tracker = _get_db()
    updated_ids: list[str] = []
    updated_records: list[dict[str, Any]] = []
    errors: list[BatchFailure] = []
    for fid in finding_ids:
        try:
            record = tracker.update_finding(fid, status=status, actor=actor)
            updated_ids.append(fid)
            if detail == "full":
                updated_records.append(finding_to_mcp(record))
        except KeyError as e:
            _logger.warning("batch_update_findings: failed for %s: %s", fid, e)
            errors.append(BatchFailure(id=fid, error=str(e), code=ErrorCode.NOT_FOUND))
        except ValueError as e:
            _logger.warning("batch_update_findings: failed for %s: %s", fid, e)
            errors.append(BatchFailure(id=fid, error=str(e), code=ErrorCode.VALIDATION))
    if not updated_ids and errors:
        return _text(
            ErrorResponse(
                error=f"All {len(errors)} finding update(s) failed",
                code=ErrorCode.VALIDATION,
            )
        )
    if detail == "full":
        full_result: BatchResponse[dict[str, Any]] = BatchResponse(succeeded=updated_records, failed=errors)
        return _text(full_result)
    result: BatchResponse[str] = BatchResponse(succeeded=updated_ids, failed=errors)
    return _text(result)


async def _handle_promote_finding(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, PromoteFindingArgs)
    finding_id = args.get("finding_id", "")
    if not isinstance(finding_id, str) or not finding_id.strip():
        return _text(ErrorResponse(error="finding_id is required", code=ErrorCode.VALIDATION))
    priority = args.get("priority")
    priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
    if priority_err:
        return priority_err
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    labels = args.get("labels")
    if labels is not None and (not isinstance(labels, list) or not all(isinstance(lbl, str) for lbl in labels)):
        return _text(ErrorResponse(error="labels must be a list of strings", code=ErrorCode.VALIDATION))

    tracker = _get_db()
    try:
        result = tracker.promote_finding_to_issue(finding_id, priority=priority, actor=actor, labels=labels)
    except KeyError:
        return _text(ErrorResponse(error=f"Finding not found: {finding_id}", code=ErrorCode.NOT_FOUND))
    except ValueError as exc:
        _logger.warning("Failed to promote finding %s: %s", finding_id, exc)
        return _text(ErrorResponse(error=f"Failed to promote finding: {exc}", code=ErrorCode.VALIDATION))
    except sqlite3.Error as exc:
        _logger.exception("Database error promoting finding %s", finding_id)
        return _text(ErrorResponse(error=f"Database error promoting finding: {exc}", code=ErrorCode.IO))
    _refresh_summary()
    response: dict[str, object] = dict(issue_to_public(result["issue"]))
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return _text(response)


async def _handle_dismiss_finding(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, DismissFindingArgs)
    finding_id = args.get("finding_id", "")
    if not isinstance(finding_id, str) or not finding_id.strip():
        return _text(ErrorResponse(error="finding_id is required", code=ErrorCode.VALIDATION))

    reason = args.get("reason")
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    # P3.13: allow callers to pick an alternate dismissal status. The default
    # preserves the legacy shape (false_positive). Validate against the
    # subset that makes sense for "dismiss" — promotion to 'open' would
    # contradict the verb.
    status_arg = args.get("status", "false_positive")
    valid_dismiss_statuses: dict[str, FindingStatus] = {
        "false_positive": "false_positive",
        "fixed": "fixed",
        "unseen_in_latest": "unseen_in_latest",
        "acknowledged": "acknowledged",
    }
    if status_arg not in valid_dismiss_statuses:
        return _text(
            ErrorResponse(
                error=f"Invalid dismiss status: {status_arg!r}. Valid: {', '.join(sorted(valid_dismiss_statuses))}",
                code=ErrorCode.VALIDATION,
            )
        )
    status: FindingStatus = valid_dismiss_statuses[status_arg]

    tracker = _get_db()
    try:
        updated = tracker.update_finding(finding_id, status=status, dismiss_reason=reason or None, actor=actor)
    except KeyError:
        return _text(ErrorResponse(error=f"Finding not found: {finding_id}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except sqlite3.Error as e:
        _logger.exception("Database error dismissing finding %s", finding_id)
        return _text(ErrorResponse(error=f"Database error dismissing finding: {e}", code=ErrorCode.IO))
    return _text(finding_to_mcp(updated))
