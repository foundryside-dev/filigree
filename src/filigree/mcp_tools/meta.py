"""MCP tools for comments, labels, changes, stats, export/import, and maintenance."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any, cast, get_args

from mcp.types import TextContent, Tool

from filigree.core import WrongProjectError
from filigree.issue_payloads import issue_to_public
from filigree.label_payloads import label_namespace_from_public, label_namespace_item_to_public
from filigree.mcp_tools.common import (
    _list_response,
    _parse_args,
    _text,
    _validate_actor,
    _validate_int_range,
    _validate_str,
    get_db,
    refresh_summary,
    resolve_request_filigree_dir,
    safe_path,
)
from filigree.mcp_tools.payloads import comment_to_mcp, event_to_mcp, undo_result_to_mcp
from filigree.types.api import (
    AddCommentResult,
    ArchiveClosedResponse,
    BatchResponse,
    ClaimConflictError,
    CompactEventsResponse,
    ErrorCode,
    ErrorResponse,
    JsonlTransferResponse,
    LabelActionResponse,
    PublicIssue,
    claim_conflict_envelope,
    parse_response_detail,
)
from filigree.types.events import EventType
from filigree.types.inputs import (
    AddCommentArgs,
    AddLabelArgs,
    ArchiveClosedArgs,
    BatchAddCommentArgs,
    BatchAddLabelArgs,
    BatchRemoveLabelArgs,
    CompactEventsArgs,
    ExportJsonlArgs,
    GetChangesArgs,
    GetCommentsArgs,
    GetIssueEventsArgs,
    GetMetricsArgs,
    ImportJsonlArgs,
    ListLabelsArgs,
    RemoveLabelArgs,
    UndoLastArgs,
)

logger = logging.getLogger(__name__)


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for meta-domain tools."""
    tools = [
        Tool(
            name="add_comment",
            description=(
                "Add a comment to an issue. Returns the flat updated PublicIssue plus comment_id. "
                "When actor is present and the issue is held, actor is the default expected holder. "
                "Pass expected_assignee for coordinator overrides; mismatches return CONFLICT."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "text": {"type": "string", "description": "Comment text"},
                    "actor": {"type": "string", "description": "Agent/user identity (used as comment author)"},
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition. When omitted and actor is present on a "
                            "held issue, actor is the default expected holder. Set explicitly for "
                            "coordinator compare-and-swap overrides."
                        ),
                    },
                },
                "required": ["issue_id", "text"],
            },
        ),
        Tool(
            name="get_comments",
            description="Get all comments on an issue (for agent-to-agent context handoff)",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="add_label",
            description=(
                "Add a label to an issue. Returns the flat updated PublicIssue plus label and label_result. "
                "When actor is present and the issue is held, actor is the default expected holder. "
                "Pass expected_assignee for coordinator overrides; mismatches return CONFLICT."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "label": {"type": "string", "description": "Label to add"},
                    "actor": {"type": "string", "description": "Agent/user identity for claim-aware write safety"},
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition. When omitted and actor is present on a "
                            "held issue, actor is the default expected holder. Set explicitly for "
                            "coordinator compare-and-swap overrides."
                        ),
                    },
                },
                "required": ["issue_id", "label"],
            },
        ),
        Tool(
            name="remove_label",
            description=(
                "Remove a label from an issue. Returns the flat updated PublicIssue plus label and label_result. "
                "When actor is present and the issue is held, actor is the default expected holder. "
                "Pass expected_assignee for coordinator overrides; mismatches return CONFLICT."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "label": {"type": "string", "description": "Label to remove"},
                    "actor": {"type": "string", "description": "Agent/user identity for claim-aware write safety"},
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition. When omitted and actor is present on a "
                            "held issue, actor is the default expected holder. Set explicitly for "
                            "coordinator compare-and-swap overrides."
                        ),
                    },
                },
                "required": ["issue_id", "label"],
            },
        ),
        Tool(
            name="batch_add_label",
            description=(
                "Add the same label to multiple issues in one call. Returns "
                "BatchResponse[str] (succeeded issue IDs) by default, or "
                "BatchResponse[PublicIssue] when response_detail='full'. "
                "failed[] is always present (empty if none)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue IDs to update",
                    },
                    "label": {"type": "string", "description": "Label to add"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns issue ID strings in succeeded[]; 'full' returns full PublicIssue records.",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition applied per-item. When omitted and actor "
                            "is present on a held issue, actor is the default expected holder. "
                            "Mismatches land in failed[] with code=CONFLICT."
                        ),
                    },
                },
                "required": ["issue_ids", "label"],
            },
        ),
        Tool(
            name="batch_remove_label",
            description=(
                "Remove the same label from multiple issues in one call. Returns "
                "BatchResponse[str] (succeeded issue IDs) by default, or "
                "BatchResponse[PublicIssue] when response_detail='full'. "
                "failed[] is always present (empty if none)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue IDs to update",
                    },
                    "label": {"type": "string", "description": "Label to remove"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns issue ID strings in succeeded[]; 'full' returns full PublicIssue records.",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition applied per-item. When omitted and actor "
                            "is present on a held issue, actor is the default expected holder. "
                            "Mismatches land in failed[] with code=CONFLICT."
                        ),
                    },
                },
                "required": ["issue_ids", "label"],
            },
        ),
        Tool(
            name="batch_add_comment",
            description=(
                "Add the same comment to multiple issues in one call. Returns "
                "BatchResponse[str] (succeeded issue IDs) by default, or "
                "BatchResponse[PublicIssue] when response_detail='full' "
                "(succeeded[] then carries the full commented-on issues). "
                "failed[] is always present (empty if none)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue IDs to update",
                    },
                    "text": {"type": "string", "description": "Comment text"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns issue ID strings in succeeded[]; 'full' returns full PublicIssue records of the commented-on issues.",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition applied per-item. When omitted and actor "
                            "is present on a held issue, actor is the default expected holder. "
                            "Mismatches land in failed[] with code=CONFLICT."
                        ),
                    },
                },
                "required": ["issue_ids", "text"],
            },
        ),
        Tool(
            name="get_changes",
            description=(
                "Get events since a timestamp (for session resumption). Returns chronological event list "
                "with optional catch-up filters. Heartbeat events are excluded by default so the catch-up "
                "feed isn't dominated by liveness pings; pass include_heartbeats=true (or type='heartbeat' "
                "explicitly) to see them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "ISO timestamp to get events after"},
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "minimum": 1,
                        "description": "Max events (default 100)",
                    },
                    "actor": {"type": "string", "description": "Only include events written by this actor"},
                    "issue_id": {"type": "string", "description": "Only include events for this issue"},
                    "label": {"type": "string", "description": "Only include events for issues currently carrying this label"},
                    "after_event_id": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Resume after this event id when since matches multiple events",
                    },
                    "type": {
                        "type": "string",
                        "enum": list(get_args(EventType)),
                        "description": "Only include events of this event type",
                    },
                    "include_heartbeats": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Default false — heartbeat events are excluded so the catch-up feed isn't "
                            "dominated by liveness pings. Set true (or pass type='heartbeat') to include them."
                        ),
                    },
                },
                "required": ["since"],
            },
        ),
        Tool(
            name="get_summary",
            description=(
                "Get the pre-computed project summary (same as context.md). Default returns markdown "
                "for human display; pass format='json' to receive a structured envelope "
                "{markdown: str, stats: <stats_get output>} so callers doing programmatic orientation "
                "don't need a follow-up stats_get call. (filigree-cb980eee0d, P3.12.)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "json"],
                        "default": "markdown",
                        "description": "Output format: markdown (default) or json envelope.",
                    },
                },
            },
        ),
        Tool(
            name="session_context",
            description="Get the same startup project snapshot produced by `filigree session-context`.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_stats",
            description=(
                "Get project statistics: by_status counts literal workflow statuses, "
                "by_category counts template categories (open/wip/done), plus by_type and ready/blocked counts."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_metrics",
            description="Flow metrics: cycle time, lead time, throughput. Useful for retrospectives and velocity tracking.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "minimum": 1, "description": "Lookback window in days"},
                },
            },
        ),
        Tool(
            name="export_jsonl",
            description="Export all project data to a JSONL file for backup or migration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "description": "File path to write JSONL output"},
                },
                "required": ["output_path"],
            },
        ),
        Tool(
            name="import_jsonl",
            description="Import project data from a JSONL file. Use merge=true to skip existing records.",
            inputSchema={
                "type": "object",
                "properties": {
                    "input_path": {"type": "string", "description": "File path to read JSONL from"},
                    "merge": {
                        "type": "boolean",
                        "default": False,
                        "description": "Skip existing records instead of failing",
                    },
                },
                "required": ["input_path"],
            },
        ),
        Tool(
            name="archive_closed",
            description=(
                "Archive old closed issues (>N days). Reduces active issue count for better performance. "
                "Requires a non-empty label filter when days_old<7 to prevent accidentally sweeping up "
                "issues closed minutes ago across the whole project. (filigree-cb980eee0d, P3.17.)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days_old": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 0,
                        "description": (
                            "Archive issues closed more than N days ago (must be >= 0). "
                            "Values <7 require a label filter to scope the archival."
                        ),
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "label": {
                        "type": "string",
                        "description": "Only archive closed issues currently carrying this label (required when days_old<7)",
                    },
                },
            },
        ),
        Tool(
            name="compact_events",
            description="Remove old events for archived issues. Run after admin_archive_closed to reclaim space.",
            inputSchema={
                "type": "object",
                "properties": {
                    "keep_recent": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 0,
                        "description": "Keep N most recent events per archived issue (must be >= 0)",
                    },
                },
            },
        ),
        Tool(
            name="undo_last",
            description="Undo the most recent reversible action on an issue. Covers status, title, priority, assignee, description, notes, claims, and dependency changes. Success returns the flat updated PublicIssue plus undo metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="get_issue_events",
            description="Get events for a specific issue, newest first. Useful for reviewing history before undo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "description": "Max events (default 50)"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="list_labels",
            description=(
                "List all distinct labels grouped by namespace with counts. "
                "Use label_taxonomy_get to see reserved namespaces and suggested vocabulary."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Filter to a specific namespace (e.g. 'cluster')"},
                    "top": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 0,
                        "description": "Max labels per namespace (default 10, 0 for unlimited)",
                    },
                },
            },
        ),
        Tool(
            name="get_label_taxonomy",
            description=(
                "Get the full label vocabulary: reserved namespaces, auto-tags, virtual labels, "
                "and suggested manual labels. Use before adding labels to see what's available."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="restart_dashboard",
            description="Stop and restart the ephemeral dashboard. Returns the new URL.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_reconciliation_debt",
            description=(
                "List issues carrying reconciliation debt — governed finding→issue auto-closes that the "
                "Legis closure gate deferred (blocked, or could not confirm). Each row is one issue with "
                "its debt_count and latest debt timestamp. Use to find and action deferred cascade closes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "description": "Max results (default 50)"},
                    "offset": {"type": "integer", "default": 0, "minimum": 0, "description": "Skip first N results"},
                },
            },
        ),
    ]

    handlers: dict[str, Callable[..., Any]] = {
        "add_comment": _handle_add_comment,
        "get_comments": _handle_get_comments,
        "add_label": _handle_add_label,
        "remove_label": _handle_remove_label,
        "batch_add_label": _handle_batch_add_label,
        "batch_remove_label": _handle_batch_remove_label,
        "batch_add_comment": _handle_batch_add_comment,
        "get_changes": _handle_get_changes,
        "get_summary": _handle_get_summary,
        "session_context": _handle_session_context,
        "get_stats": _handle_get_stats,
        "get_metrics": _handle_get_metrics,
        "export_jsonl": _handle_export_jsonl,
        "import_jsonl": _handle_import_jsonl,
        "archive_closed": _handle_archive_closed,
        "compact_events": _handle_compact_events,
        "undo_last": _handle_undo_last,
        "get_issue_events": _handle_get_issue_events,
        "list_labels": _handle_list_labels,
        "get_label_taxonomy": _handle_get_label_taxonomy,
        "restart_dashboard": _handle_restart_dashboard,
        "list_reconciliation_debt": _handle_list_reconciliation_debt,
    }

    return tools, handlers


async def _handle_list_reconciliation_debt(arguments: dict[str, Any]) -> list[TextContent]:
    limit = arguments.get("limit", 50)
    offset = arguments.get("offset", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _text(ErrorResponse(error="limit must be a positive integer", code=ErrorCode.VALIDATION))
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _text(ErrorResponse(error="offset must be a non-negative integer", code=ErrorCode.VALIDATION))
    tracker = get_db()
    try:
        rows = tracker.list_reconciliation_debt(limit=limit + 1, offset=offset)
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_offset = offset + len(rows) if has_more else None
    return _text(_list_response(rows, has_more=has_more, next_offset=next_offset))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_add_comment(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, AddCommentArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    tracker = get_db()
    try:
        tracker.get_issue(args["issue_id"])
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    try:
        comment_id = tracker.add_comment(
            args["issue_id"],
            args["text"],
            author=actor,
            expected_assignee=expected_assignee,
        )
    except ValueError as e:
        msg = str(e)
        if isinstance(e, ClaimConflictError):
            # Untrusted MCP surface: generic safe_message string, details retained.
            return _text(claim_conflict_envelope(e, safe=True))
        return _text(ErrorResponse(error=msg, code=ErrorCode.VALIDATION))
    refresh_summary()
    issue = tracker.get_issue(args["issue_id"])
    response: dict[str, Any] = dict(issue_to_public(issue))
    response["comment_id"] = comment_id
    response["comment"] = comment_to_mcp(tracker.get_comment(comment_id))
    return _text(cast(AddCommentResult, response))


async def _handle_get_comments(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, GetCommentsArgs)
    tracker = get_db()
    try:
        tracker.get_issue(args["issue_id"])
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    comments = [comment_to_mcp(comment) for comment in tracker.get_comments(args["issue_id"])]
    return _text(_list_response(comments, has_more=False))


async def _handle_add_label(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, AddLabelArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    tracker = get_db()
    try:
        tracker.get_issue(args["issue_id"])
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    try:
        added, canonical, replaced = tracker.add_label(
            args["issue_id"],
            args["label"],
            actor=actor,
            expected_assignee=expected_assignee,
        )
    except ValueError as e:
        msg = str(e)
        if isinstance(e, ClaimConflictError):
            # Untrusted MCP surface: generic safe_message string, details retained.
            return _text(claim_conflict_envelope(e, safe=True))
        return _text(ErrorResponse(error=msg, code=ErrorCode.VALIDATION))
    refresh_summary()
    # Mutual-exclusivity displacement was previously silent — surface it as
    # label_result='replaced' with a replaced_labels list, plus a
    # data_warnings entry so callers iterating warnings see it too
    # (filigree-cb980eee0d, P2.7).
    status = "replaced" if replaced else ("added" if added else "already_exists")
    issue = tracker.get_issue(args["issue_id"])
    response: dict[str, Any] = dict(issue_to_public(issue))
    response["label"] = canonical
    response["label_result"] = status
    if replaced:
        response["replaced_labels"] = replaced
        warnings = list(response.get("data_warnings") or [])
        warnings.append(f"Adding '{canonical}' displaced mutually-exclusive label(s): {', '.join(replaced)}")
        response["data_warnings"] = warnings
    return _text(cast(LabelActionResponse, response))


async def _handle_remove_label(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, RemoveLabelArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    tracker = get_db()
    try:
        tracker.get_issue(args["issue_id"])
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    try:
        removed, canonical = tracker.remove_label(
            args["issue_id"],
            args["label"],
            actor=actor,
            expected_assignee=expected_assignee,
        )
    except ValueError as e:
        msg = str(e)
        if isinstance(e, ClaimConflictError):
            # Untrusted MCP surface: generic safe_message string, details retained.
            return _text(claim_conflict_envelope(e, safe=True))
        return _text(ErrorResponse(error=msg, code=ErrorCode.VALIDATION))
    refresh_summary()
    status = "removed" if removed else "not_found"
    issue = tracker.get_issue(args["issue_id"])
    response: dict[str, Any] = dict(issue_to_public(issue))
    response["label"] = canonical
    response["label_result"] = status
    return _text(cast(LabelActionResponse, response))


async def _handle_batch_add_label(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, BatchAddLabelArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = get_db()
    issue_ids = args["issue_ids"]
    if not all(isinstance(i, str) for i in issue_ids):
        return _text(ErrorResponse(error="All issue IDs must be strings", code=ErrorCode.VALIDATION))
    if not isinstance(args["label"], str):
        return _text(ErrorResponse(error="label must be a string", code=ErrorCode.VALIDATION))
    try:
        label_succeeded, label_failed = tracker.batch_add_label(
            issue_ids,
            label=args["label"],
            actor=actor,
            expected_assignee=expected_assignee,
        )
    except WrongProjectError as e:
        # 2.1.0 §0.4: envelope-level abort on foreign-prefix.
        # 2.1.0 §1.2: untrusted-surface serialisation uses safe_message.
        return _text(ErrorResponse(error=e.safe_message, code=ErrorCode.VALIDATION))
    refresh_summary()
    if detail == "full":
        full_result: BatchResponse[PublicIssue] = BatchResponse(
            succeeded=[issue_to_public(tracker.get_issue(row["id"])) for row in label_succeeded],
            failed=label_failed,
        )
        return _text(full_result)
    result: BatchResponse[str] = BatchResponse(
        succeeded=[row["id"] for row in label_succeeded],
        failed=label_failed,
    )
    return _text(result)


async def _handle_batch_remove_label(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, BatchRemoveLabelArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = get_db()
    issue_ids = args["issue_ids"]
    if not all(isinstance(i, str) for i in issue_ids):
        return _text(ErrorResponse(error="All issue IDs must be strings", code=ErrorCode.VALIDATION))
    if not isinstance(args["label"], str):
        return _text(ErrorResponse(error="label must be a string", code=ErrorCode.VALIDATION))
    try:
        label_succeeded, label_failed = tracker.batch_remove_label(
            issue_ids,
            label=args["label"],
            actor=actor,
            expected_assignee=expected_assignee,
        )
    except WrongProjectError as e:
        # 2.1.0 §0.4: envelope-level abort on foreign-prefix.
        # 2.1.0 §1.2: untrusted-surface serialisation uses safe_message.
        return _text(ErrorResponse(error=e.safe_message, code=ErrorCode.VALIDATION))
    refresh_summary()
    if detail == "full":
        full_result: BatchResponse[PublicIssue] = BatchResponse(
            succeeded=[issue_to_public(tracker.get_issue(row["id"])) for row in label_succeeded],
            failed=label_failed,
        )
        return _text(full_result)
    result: BatchResponse[str] = BatchResponse(
        succeeded=[row["id"] for row in label_succeeded],
        failed=label_failed,
    )
    return _text(result)


async def _handle_batch_add_comment(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, BatchAddCommentArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = get_db()
    issue_ids = args["issue_ids"]
    if not all(isinstance(i, str) for i in issue_ids):
        return _text(ErrorResponse(error="All issue IDs must be strings", code=ErrorCode.VALIDATION))
    if not isinstance(args["text"], str):
        return _text(ErrorResponse(error="text must be a string", code=ErrorCode.VALIDATION))
    try:
        comment_succeeded, comment_failed = tracker.batch_add_comment(
            issue_ids,
            text=args["text"],
            author=actor,
            expected_assignee=expected_assignee,
        )
    except WrongProjectError as e:
        # 2.1.0 §0.4: envelope-level abort on foreign-prefix.
        # 2.1.0 §1.2: untrusted-surface serialisation uses safe_message.
        return _text(ErrorResponse(error=e.safe_message, code=ErrorCode.VALIDATION))
    refresh_summary()
    if detail == "full":
        full_result: BatchResponse[PublicIssue] = BatchResponse(
            succeeded=[issue_to_public(tracker.get_issue(str(row["id"]))) for row in comment_succeeded],
            failed=comment_failed,
        )
        return _text(full_result)
    result: BatchResponse[str] = BatchResponse(
        succeeded=[str(row["id"]) for row in comment_succeeded],
        failed=comment_failed,
    )
    return _text(result)


async def _handle_get_changes(arguments: dict[str, Any]) -> list[TextContent]:
    from datetime import datetime

    args = _parse_args(arguments, GetChangesArgs)
    since = args["since"]
    since_normalized = since.replace("Z", "+00:00") if since.endswith("Z") else since
    try:
        datetime.fromisoformat(since_normalized)
    except (ValueError, AttributeError):
        return _text(
            ErrorResponse(error=f"Invalid ISO timestamp: {since!r}. Expected format: 2026-01-15T10:30:00", code=ErrorCode.VALIDATION)
        )
    tracker = get_db()
    limit = args.get("limit", 100)
    limit_err = _validate_int_range(limit, "limit", min_val=1)
    if limit_err:
        return limit_err
    after_event_id = args.get("after_event_id")
    event_id_err = _validate_int_range(after_event_id, "after_event_id", min_val=0) if after_event_id is not None else None
    if event_id_err:
        return event_id_err
    for field in ("actor", "issue_id", "label", "type"):
        str_err = _validate_str(args.get(field), field)
        if str_err:
            return str_err
    label = args.get("label")
    if label is not None:
        label = label.strip()
        if not label:
            return _text(ErrorResponse(error="label cannot be empty", code=ErrorCode.VALIDATION))
        if any(ord(c) < 32 or c == "\x7f" for c in label):
            return _text(ErrorResponse(error="label contains control characters", code=ErrorCode.VALIDATION))
    event_type = args.get("type")
    if event_type is not None and event_type not in get_args(EventType):
        return _text(ErrorResponse(error=f"Invalid event type: {event_type}", code=ErrorCode.VALIDATION))
    # Default-exclude heartbeat events so the catch-up firehose isn't
    # dominated by liveness pings. Callers can opt back in by passing
    # type='heartbeat' explicitly or include_heartbeats=true to see all
    # events. (filigree-cb980eee0d, P2.11.)
    include_heartbeats = args.get("include_heartbeats", False)
    if not isinstance(include_heartbeats, bool):
        return _text(ErrorResponse(error="include_heartbeats must be a boolean", code=ErrorCode.VALIDATION))
    exclude_types: list[str] = []
    if not include_heartbeats and event_type != "heartbeat":
        exclude_types.append("heartbeat")
    # Overfetch by 1 to detect has_more, matching list_issues / search_issues.
    events = tracker.get_events_since(
        since_normalized,
        after_event_id=after_event_id,
        limit=limit + 1,
        actor=args.get("actor"),
        issue_id=args.get("issue_id"),
        label=label,
        event_type=event_type,
        exclude_types=exclude_types or None,
    )
    has_more = len(events) > limit
    if has_more:
        events = events[:limit]
    items = [event_to_mcp(event) for event in events]
    response: dict[str, Any] = dict(_list_response(items, has_more=has_more))
    response["next_since"] = items[-1]["created_at"] if items else since_normalized
    response["next_event_id"] = items[-1]["event_id"] if items else after_event_id
    return _text(response)


async def _handle_get_summary(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.summary import generate_summary

    fmt = arguments.get("format", "markdown")
    if fmt not in {"markdown", "json"}:
        return _text(ErrorResponse(error=f"Invalid format: {fmt!r}. Use 'markdown' or 'json'.", code=ErrorCode.VALIDATION))
    tracker = get_db()
    summary = generate_summary(tracker)
    if fmt == "json":
        # Bundle markdown + structured stats so a single call powers
        # programmatic orientation. (filigree-cb980eee0d, P3.12.)
        return _text({"markdown": summary, "stats": tracker.get_stats()})
    return _text(summary)


async def _handle_session_context(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.hooks import _build_context

    tracker = get_db()
    return _text(_build_context(tracker, resolve_request_filigree_dir(tracker)))


async def _handle_get_stats(arguments: dict[str, Any]) -> list[TextContent]:

    tracker = get_db()
    return _text(tracker.get_stats())


async def _handle_get_metrics(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.analytics import get_flow_metrics

    args = _parse_args(arguments, GetMetricsArgs)
    days_err = _validate_int_range(args.get("days", 30), "days", min_val=1)
    if days_err is not None:
        return days_err
    tracker = get_db()
    return _text(get_flow_metrics(tracker, days=args.get("days", 30)))


async def _handle_export_jsonl(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, ExportJsonlArgs)
    tracker = get_db()
    try:
        safe = safe_path(args["output_path"])
        count = tracker.export_jsonl(safe)
        return _text(JsonlTransferResponse(status="ok", records=count, path=str(safe)))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except (OSError, sqlite3.Error) as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.IO))


async def _handle_import_jsonl(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, ImportJsonlArgs)
    tracker = get_db()
    try:
        safe = safe_path(args["input_path"])
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    try:
        result = tracker.import_jsonl(safe, merge=args.get("merge", False))
        refresh_summary()
        resp = JsonlTransferResponse(status="ok", records=result["count"], path=str(safe))
        if result["skipped_types"]:
            resp["skipped_types"] = result["skipped_types"]
        return _text(resp)
    except WrongProjectError as e:
        # 2.1.0 §1.2: untrusted-surface serialisation uses safe_message.
        logging.getLogger(__name__).warning("import_jsonl failed: %s", e, exc_info=True)
        return _text(ErrorResponse(error=e.safe_message, code=ErrorCode.VALIDATION))
    except ValueError as e:
        # Malformed records / unknown type / invalid status are user-correctable
        # data errors, not transient IO — mirror _handle_export_jsonl's split so
        # callers switching on `code` (per CLAUDE.md) don't retry a permanent error.
        logging.getLogger(__name__).warning("import_jsonl failed: %s", e, exc_info=True)
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except (OSError, sqlite3.Error) as e:
        logging.getLogger(__name__).warning("import_jsonl failed: %s", e, exc_info=True)
        return _text(ErrorResponse(error=str(e), code=ErrorCode.IO))


async def _handle_archive_closed(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, ArchiveClosedArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    days_old = args.get("days_old", 30)
    days_err = _validate_int_range(days_old, "days_old", min_val=0)
    if days_err:
        return days_err
    label = args.get("label")
    label_err = _validate_str(label, "label")
    if label_err:
        return label_err
    # Footgun guard: days_old < 7 without a label filter could sweep up
    # issues closed minutes ago across the whole project. Require an
    # explicit label scope when running with a tight age window.
    # (filigree-cb980eee0d, P3.17.)
    if isinstance(days_old, int) and days_old < 7 and not (label and isinstance(label, str) and label.strip()):
        return _text(
            ErrorResponse(
                error=(
                    f"days_old={days_old} requires a non-empty label filter. "
                    "Tight age windows without a label scope risk archiving "
                    "issues closed seconds ago across the whole project. Pass "
                    "label='cluster:<name>' or 'mcp-review-scratch' to scope, "
                    "or use days_old>=7."
                ),
                code=ErrorCode.VALIDATION,
            )
        )
    tracker = get_db()
    try:
        archived = tracker.archive_closed(
            days_old=days_old,
            actor=actor,
            label=label,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    refresh_summary()
    return _text(ArchiveClosedResponse(status="ok", archived_count=len(archived), archived_ids=archived))


async def _handle_compact_events(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, CompactEventsArgs)
    keep_recent = args.get("keep_recent", 50)
    keep_err = _validate_int_range(keep_recent, "keep_recent", min_val=0)
    if keep_err:
        return keep_err
    tracker = get_db()
    deleted = tracker.compact_events(keep_recent=keep_recent)
    return _text(CompactEventsResponse(status="ok", events_deleted=deleted))


async def _handle_undo_last(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, UndoLastArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = get_db()
    try:
        result = tracker.undo_last(args["issue_id"], actor=actor)
        if result["undone"]:
            refresh_summary()
        return _text(undo_result_to_mcp(result))
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))


async def _handle_get_issue_events(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, GetIssueEventsArgs)
    limit = args.get("limit", 50)
    limit_err = _validate_int_range(limit, "limit", min_val=1)
    if limit_err is not None:
        return limit_err
    tracker = get_db()
    try:
        events = tracker.get_issue_events(args["issue_id"], limit=limit + 1)
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    has_more = len(events) > limit
    if has_more:
        events = events[:limit]
    return _text(_list_response([event_to_mcp(event) for event in events], has_more=has_more))


async def _handle_list_labels(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, ListLabelsArgs)
    top = args.get("top", 10)
    top_err = _validate_int_range(top, "top", min_val=0)
    if top_err is not None:
        return top_err
    tracker = get_db()
    try:
        result = tracker.list_labels(
            namespace=label_namespace_from_public(args.get("namespace")),
            top=top,
        )
    except (sqlite3.Error, ValueError) as exc:
        return _text(ErrorResponse(error=f"Failed to list labels: {exc}", code=ErrorCode.IO))
    # ``tracker.list_labels`` returns ``{namespaces: {ns: {type, writable,
    # labels}}, total_in_result}``. Flatten the namespaces map to a list of
    # entries so list_labels matches the ListResponse[T] envelope used by
    # every other MCP list tool. The bounded total is recoverable by the
    # caller as ``sum(len(item['labels']) for item in items)``.
    items: list[dict[str, Any]] = [label_namespace_item_to_public(ns, ns_data) for ns, ns_data in result["namespaces"].items()]
    return _text(_list_response(items, has_more=False))


async def _handle_get_label_taxonomy(arguments: dict[str, Any]) -> list[TextContent]:

    tracker = get_db()
    try:
        result = tracker.get_label_taxonomy()
    except (sqlite3.Error, ValueError) as exc:
        return _text(ErrorResponse(error=f"Failed to get label taxonomy: {exc}", code=ErrorCode.IO))
    return _text(result)


async def _handle_restart_dashboard(arguments: dict[str, Any]) -> list[TextContent]:
    """Stop the ephemeral dashboard and restart it."""
    import os
    import signal
    import time

    from filigree.core import find_filigree_root
    from filigree.ephemeral import is_pid_alive, read_pid_file, verify_pid_ownership

    try:
        filigree_dir = find_filigree_root()
    except FileNotFoundError:
        return _text(ErrorResponse(error="No .filigree/ directory found", code=ErrorCode.NOT_INITIALIZED))

    pid_file = filigree_dir / "ephemeral.pid"
    info = read_pid_file(pid_file)

    # Stop existing dashboard if running. Only mark ``stopped`` once the PID is
    # actually gone (filigree-2298877675) — previously this was set
    # unconditionally after SIGTERM+grace, so a wedged dashboard produced
    # ``status: "restarted"`` even though ``ensure_dashboard_running`` simply
    # reused the same still-alive process.
    stopped = False
    if info is not None and verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)):
        pid = info["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for graceful shutdown
            for _ in range(20):  # up to 2 seconds
                time.sleep(0.1)
                if not is_pid_alive(pid):
                    break
            if is_pid_alive(pid):
                # Escalate: SIGKILL the unresponsive dashboard.
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    return _text(
                        ErrorResponse(
                            error=f"Cannot stop dashboard (PID {pid}): permission denied",
                            code=ErrorCode.PERMISSION,
                        )
                    )
                for _ in range(10):  # up to 1 more second
                    time.sleep(0.1)
                    if not is_pid_alive(pid):
                        break
            if is_pid_alive(pid):
                return _text(
                    ErrorResponse(
                        error=(
                            f"Old dashboard (PID {pid}) did not exit after SIGTERM+SIGKILL; "
                            "aborting restart to avoid reporting a spurious success"
                        ),
                        code=ErrorCode.STOP_FAILED,
                    )
                )
            stopped = True
        except ProcessLookupError:
            stopped = True  # Already dead
        except PermissionError:
            return _text(
                ErrorResponse(
                    error=f"Cannot stop dashboard (PID {pid}): permission denied",
                    code=ErrorCode.PERMISSION,
                )
            )

    # Restart via ensure_dashboard_running
    from filigree.hooks import ensure_dashboard_running

    try:
        url = ensure_dashboard_running()
    except Exception as exc:
        logger.exception("restart_dashboard: ensure_dashboard_running raised")
        return _text(
            ErrorResponse(
                error=f"Dashboard restart failed: {exc}",
                code=ErrorCode.INTERNAL,
            )
        )

    result: dict[str, Any] = {"status": "restarted" if stopped else "started"}
    if url:
        result["url"] = url
        return _text(result)
    logger.warning("restart_dashboard: ensure_dashboard_running returned no URL")
    return _text(
        ErrorResponse(
            error="Dashboard did not start",
            code=ErrorCode.IO,
        )
    )
