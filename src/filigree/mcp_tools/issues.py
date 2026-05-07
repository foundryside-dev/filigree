"""MCP tools for issue CRUD, search, claim, and batch operations."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from mcp.types import TextContent, Tool

from filigree.issue_payloads import issue_to_public
from filigree.mcp_tools.common import (
    _MAX_LIST_RESULTS,
    _apply_has_more,
    _build_transition_error,
    _list_response,
    _parse_args,
    _resolve_pagination,
    _slim_issue,
    _text,
    _validate_actor,
    _validate_int_range,
)
from filigree.mcp_tools.payloads import file_assoc_to_mcp
from filigree.types.api import (
    AmbiguousTransitionError,
    BatchResponse,
    ClaimNextEmptyResponse,
    ClaimNextResponse,
    ErrorCode,
    ErrorResponse,
    InvalidTransitionError,
    IssueWithChangedFields,
    IssueWithTransitions,
    PublicIssue,
    SlimIssue,
    TransitionDetail,
    classify_value_error,
    parse_response_detail,
)
from filigree.types.inputs import (
    BatchCloseArgs,
    BatchUpdateArgs,
    ClaimIssueArgs,
    ClaimNextArgs,
    CloseIssueArgs,
    CreateIssueArgs,
    GetIssueArgs,
    ListIssuesArgs,
    ReleaseClaimArgs,
    ReopenIssueArgs,
    SearchIssuesArgs,
    StartNextWorkArgs,
    StartWorkArgs,
    UpdateIssueArgs,
)

logger = logging.getLogger(__name__)

_UPDATE_TRACKED_FIELDS = ("status", "priority", "title", "assignee", "description", "notes", "parent_id", "fields")
_LIST_ISSUES_SORT_FIELDS = {"created_at", "updated_at", "priority"}


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for issue-domain tools."""
    tools = [
        Tool(
            name="get_issue",
            description="Get full details of an issue including deps, labels, children, ready status. Set include_transitions=true for valid next states.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "include_transitions": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include valid_transitions in response (saves a separate call)",
                    },
                    "include_files": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Include file associations in response (default false; pass true to "
                            "include the files list). Federation consumers typically want a clean "
                            "issue projection — this aligns with /api/loom/issues/{issue_id} which "
                            "has defaulted include_files to false since Phase C3."
                        ),
                    },
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="list_issues",
            description="List issues with optional filters. Use status_category for template-aware filtering.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by exact status name (use get_valid_transitions for allowed values)",
                    },
                    "status_category": {
                        "type": "string",
                        "enum": ["open", "wip", "done"],
                        "description": "Filter by status category (expands to all matching states)",
                    },
                    "type": {
                        "type": "string",
                        "description": "Filter by type (use list_types for available types)",
                    },
                    "priority": {"type": "integer", "minimum": 0, "maximum": 4, "description": "Filter by priority"},
                    "parent_issue_id": {"type": "string", "description": "Filter by parent issue ID"},
                    "assignee": {"type": "string", "description": "Filter by assignee"},
                    "label": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Filter by label(s). Multiple labels use AND logic. Supports virtual labels (age:fresh, has:findings).",
                    },
                    "label_prefix": {
                        "type": "string",
                        "description": "Filter by label namespace prefix (must include trailing colon, e.g. 'cluster:')",
                    },
                    "not_label": {
                        "type": "string",
                        "description": "Exclude issues with this label. Supports exact match, prefix (trailing colon), and virtual labels.",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["created_at", "updated_at", "priority"],
                        "default": "priority",
                        "description": "Sort issues by created_at, updated_at, or priority.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "asc",
                        "description": "Sort direction.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _MAX_LIST_RESULTS,
                        "minimum": 1,
                        "description": f"Max results (default {_MAX_LIST_RESULTS}, capped at {_MAX_LIST_RESULTS} unless no_limit=true)",
                    },
                    "offset": {"type": "integer", "default": 0, "minimum": 0, "description": "Skip first N results"},
                    "no_limit": {
                        "type": "boolean",
                        "default": False,
                        "description": f"Bypass the default result cap of {_MAX_LIST_RESULTS}. Use with caution on large projects.",
                    },
                },
            },
        ),
        Tool(
            name="create_issue",
            description=(
                "Create a new issue. You can set labels at creation time via labels=[...]. "
                "Use get_template first to see available fields for the type."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "type": {"type": "string", "default": "task", "description": "Issue type"},
                    "priority": {
                        "type": "integer",
                        "default": 2,
                        "minimum": 0,
                        "maximum": 4,
                        "description": "Priority 0-4 (0=critical)",
                    },
                    "parent_issue_id": {"type": "string", "description": "Parent issue ID (for hierarchy)"},
                    "description": {"type": "string", "description": "Issue description"},
                    "notes": {"type": "string", "description": "Additional notes"},
                    "fields": {"type": "object", "description": "Custom fields (from template schema)"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to attach during creation (avoids a follow-up add_label call)",
                    },
                    "deps": {"type": "array", "items": {"type": "string"}, "description": "Issue IDs this depends on"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="update_issue",
            description=(
                "Update an issue's status, priority, title, or custom fields. "
                "Use get_valid_transitions to see allowed status changes. "
                "Soft transition warnings are returned in data_warnings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "status": {
                        "type": "string",
                        "description": "New status (use get_valid_transitions for allowed values)",
                    },
                    "priority": {"type": "integer", "minimum": 0, "maximum": 4, "description": "New priority"},
                    "title": {"type": "string", "description": "New title"},
                    "assignee": {"type": "string", "description": "New assignee"},
                    "description": {"type": "string", "description": "New description"},
                    "notes": {"type": "string", "description": "New notes"},
                    "parent_issue_id": {
                        "type": "string",
                        "description": "New parent issue ID (empty string to clear)",
                    },
                    "fields": {"type": "object", "description": "Fields to merge into existing fields"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="close_issue",
            description="Close an issue with optional reason",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "reason": {"type": "string", "description": "Close reason"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "fields": {"type": "object", "description": "Custom fields to set (e.g. root_cause for incidents)"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="reopen_issue",
            description="Reopen a closed issue, returning it to its type's initial state. Clears closed_at.",
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
            name="search_issues",
            description="Search issues by title and description",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "default": _MAX_LIST_RESULTS,
                        "minimum": 1,
                        "description": f"Max results (default {_MAX_LIST_RESULTS}, capped at {_MAX_LIST_RESULTS} unless no_limit=true)",
                    },
                    "offset": {"type": "integer", "default": 0, "minimum": 0, "description": "Skip first N results"},
                    "no_limit": {
                        "type": "boolean",
                        "default": False,
                        "description": f"Bypass the default result cap of {_MAX_LIST_RESULTS}. Use with caution on large projects.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="claim_issue",
            description=(
                "Atomically claim an open-category issue, or an unassigned wip-category issue released for handoff, "
                "by setting assignee (optimistic locking). "
                "Does NOT change status — use update_issue to advance through workflow after claiming."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to claim"},
                    "assignee": {"type": "string", "minLength": 1, "description": "Who is claiming (agent name)"},
                    "actor": {
                        "type": "string",
                        "description": "Agent/user identity for audit trail (defaults to assignee)",
                    },
                },
                "required": ["issue_id", "assignee"],
            },
        ),
        Tool(
            name="release_claim",
            description="Release a claimed issue by clearing its assignee. Does NOT change status. Only succeeds if the issue has an assignee.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to release"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="claim_next",
            description="Claim the highest-priority open-category ready issue by setting assignee. Does NOT change status — use update_issue to advance through workflow after claiming.",
            inputSchema={
                "type": "object",
                "properties": {
                    "assignee": {"type": "string", "minLength": 1, "description": "Who is claiming (agent name)"},
                    "type": {"type": "string", "description": "Filter by issue type"},
                    "priority_min": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "description": "Minimum priority (0=critical)",
                    },
                    "priority_max": {"type": "integer", "minimum": 0, "maximum": 4, "description": "Maximum priority"},
                    "actor": {
                        "type": "string",
                        "description": "Agent/user identity for audit trail (defaults to assignee)",
                    },
                },
                "required": ["assignee"],
            },
        ),
        Tool(
            name="start_work",
            description=(
                "Atomically claim an issue and transition it to a working status. "
                "target_status defaults to the unique wip-category status reachable from the current status; "
                "AmbiguousTransitionError surfaces if the current status has multiple reachable wip targets "
                "(specify target_status explicitly). On transition failure the claim is rolled back."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to claim and transition"},
                    "assignee": {"type": "string", "minLength": 1, "description": "Who is starting work (agent name)"},
                    "target_status": {
                        "type": "string",
                        "description": (
                            "Optional target status. Defaults to the unique wip-category status reachable "
                            "from the current status; required when multiple wip targets are reachable."
                        ),
                    },
                    "actor": {
                        "type": "string",
                        "description": "Agent/user identity for audit trail (defaults to assignee)",
                    },
                },
                "required": ["issue_id", "assignee"],
            },
        ),
        Tool(
            name="start_next_work",
            description=(
                "Claim the highest-priority open-category ready issue and atomically transition it to a working status. "
                "Tie-break ordering: priority asc, created_at asc, issue_id asc (same as claim_next). "
                "Returns the transitioned issue, or {status: 'empty'} when no ready issue matches."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "assignee": {"type": "string", "minLength": 1, "description": "Who is starting work (agent name)"},
                    "type": {"type": "string", "description": "Filter by issue type"},
                    "priority_min": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "description": "Minimum priority (0=critical)",
                    },
                    "priority_max": {"type": "integer", "minimum": 0, "maximum": 4, "description": "Maximum priority"},
                    "target_status": {
                        "type": "string",
                        "description": (
                            "Optional target status. Defaults to the unique wip-category status reachable "
                            "from the selected issue's current status; required when multiple wip targets are reachable."
                        ),
                    },
                    "actor": {
                        "type": "string",
                        "description": "Agent/user identity for audit trail (defaults to assignee)",
                    },
                },
                "required": ["assignee"],
            },
        ),
        Tool(
            name="batch_close",
            description=(
                "Close multiple issues in one call. Returns BatchResponse[SlimIssue] "
                "(default) or BatchResponse[PublicIssue] when response_detail='full'. "
                "failed[] is always present (empty if none); newly_unblocked is "
                "included only when the close unblocks dependent issues; "
                "valid_transitions appears on per-item failures with code=INVALID_TRANSITION."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue IDs to close",
                    },
                    "reason": {"type": "string", "default": "", "description": "Close reason"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns SlimIssue items in succeeded[]; 'full' returns full PublicIssue records.",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["issue_ids"],
            },
        ),
        Tool(
            name="batch_update",
            description=(
                "Update multiple issues with the same changes in one call. Returns "
                "BatchResponse[SlimIssue] (default) or BatchResponse[PublicIssue] "
                "when response_detail='full'. failed[] is always present (empty if "
                "none); valid_transitions appears on per-item failures with "
                "code=INVALID_TRANSITION."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue IDs to update",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status (use get_valid_transitions for allowed values)",
                    },
                    "priority": {"type": "integer", "minimum": 0, "maximum": 4, "description": "New priority"},
                    "assignee": {"type": "string", "description": "New assignee"},
                    "fields": {"type": "object", "description": "Fields to merge"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns SlimIssue items in succeeded[]; 'full' returns full PublicIssue records.",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["issue_ids"],
            },
        ),
    ]

    handlers: dict[str, Callable[..., Any]] = {
        "get_issue": _handle_get_issue,
        "list_issues": _handle_list_issues,
        "create_issue": _handle_create_issue,
        "update_issue": _handle_update_issue,
        "close_issue": _handle_close_issue,
        "reopen_issue": _handle_reopen_issue,
        "search_issues": _handle_search_issues,
        "claim_issue": _handle_claim_issue,
        "release_claim": _handle_release_claim,
        "claim_next": _handle_claim_next,
        "batch_close": _handle_batch_close,
        "batch_update": _handle_batch_update,
        "start_work": _handle_start_work,
        "start_next_work": _handle_start_next_work,
    }

    return tools, handlers


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_get_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetIssueArgs)
    tracker = _get_db()
    include_files = bool(args.get("include_files", False))
    try:
        issue = tracker.get_issue(args["issue_id"])
        issue_payload = issue_to_public(issue)

        # Fail-fast to match dashboard and get_issue_files MCP tool; see
        # filigree-c6c7842661 for why swallowing sqlite3.Error is wrong.
        file_assocs: list[Any] = []
        if include_files:
            file_assocs = [file_assoc_to_mcp(item) for item in tracker.get_issue_files(args["issue_id"])]

        if args.get("include_transitions"):
            transitions = tracker.get_valid_transitions(args["issue_id"])
            result = IssueWithTransitions(
                **issue_payload,
                valid_transitions=[
                    TransitionDetail(
                        to=t.to,
                        category=t.category,
                        enforcement=t.enforcement or "",
                        requires_fields=list(t.requires_fields),
                        missing_fields=list(t.missing_fields),
                        ready=t.ready,
                    )
                    for t in transitions
                ],
            )
            out: dict[str, Any] = dict(result)
            if include_files:
                out["files"] = file_assocs
            return _text(out)
        out = dict(issue_payload)
        if include_files:
            out["files"] = file_assocs
        return _text(out)
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))


async def _handle_list_issues(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, ListIssuesArgs)
    priority = args.get("priority")
    priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
    if priority_err:
        return priority_err
    tracker = _get_db()
    status_filter = args.get("status")
    status_category = args.get("status_category")
    if status_category and not status_filter:
        # Pass the category name directly — list_issues() handles category
        # expansion internally (open/wip/done + aliases like in_progress/closed).
        status_filter = status_category

    effective_limit, offset, pag_err = _resolve_pagination(arguments)
    if pag_err is not None:
        return pag_err

    sort_by = args.get("sort_by", "priority")
    direction = args.get("direction", "asc")
    if not isinstance(sort_by, str) or sort_by not in _LIST_ISSUES_SORT_FIELDS:
        return _text(ErrorResponse(error=f"sort_by must be one of {sorted(_LIST_ISSUES_SORT_FIELDS)}", code=ErrorCode.VALIDATION))
    if not isinstance(direction, str) or direction.lower() not in {"asc", "desc"}:
        return _text(ErrorResponse(error="direction must be 'asc' or 'desc'", code=ErrorCode.VALIDATION))

    try:
        issues = tracker.list_issues(
            status=status_filter,
            type=args.get("type"),
            priority=priority,
            parent_id=args.get("parent_issue_id"),
            assignee=args.get("assignee"),
            label=args.get("label"),
            label_prefix=args.get("label_prefix"),
            not_label=args.get("not_label"),
            sort_by=sort_by,
            direction=direction,
            limit=effective_limit + 1,
            offset=offset,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    issues, has_more = _apply_has_more(issues, effective_limit)
    items = [issue_to_public(i) for i in issues]
    next_offset = offset + len(items) if has_more else None
    return _text(_list_response(items, has_more=has_more, next_offset=next_offset))


async def _handle_create_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, CreateIssueArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    priority = args.get("priority", 2)
    priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
    if priority_err:
        return priority_err
    tracker = _get_db()
    try:
        issue = tracker.create_issue(
            args["title"],
            type=args.get("type", "task"),
            priority=priority,
            parent_id=args.get("parent_issue_id"),
            description=args.get("description", ""),
            notes=args.get("notes", ""),
            fields=args.get("fields"),
            labels=args.get("labels"),
            deps=args.get("deps"),
            actor=actor,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    _refresh_summary()
    return _text(issue_to_public(issue))


async def _handle_update_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, UpdateIssueArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    priority = args.get("priority")
    priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
    if priority_err:
        return priority_err
    tracker = _get_db()
    try:
        before = tracker.get_issue(args["issue_id"])
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    try:
        issue = tracker.update_issue(
            args["issue_id"],
            status=args.get("status"),
            priority=priority,
            title=args.get("title"),
            assignee=args.get("assignee"),
            description=args.get("description"),
            notes=args.get("notes"),
            parent_id=args.get("parent_issue_id"),
            fields=args.get("fields"),
            actor=actor,
        )
        _refresh_summary()
        changed = [attr for attr in _UPDATE_TRACKED_FIELDS if getattr(issue, attr) != getattr(before, attr)]
        result = IssueWithChangedFields(**issue_to_public(issue), changed_fields=changed)
        return _text(result)
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        msg = str(e)
        if classify_value_error(msg) == ErrorCode.INVALID_TRANSITION:
            return _text(_build_transition_error(tracker, args["issue_id"], msg))
        return _text(ErrorResponse(error=msg, code=ErrorCode.VALIDATION))


async def _handle_close_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, CloseIssueArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        ready_before = {i.id for i in tracker.get_ready()}
        annotation_warnings = tracker.get_annotation_closeout_warnings(args["issue_id"])
        issue = tracker.close_issue(
            args["issue_id"],
            reason=args.get("reason", ""),
            actor=actor,
            fields=args.get("fields"),
        )
        _refresh_summary()
        ready_after = tracker.get_ready()
        newly_unblocked = [i for i in ready_after if i.id not in ready_before]
        result: dict[str, Any] = dict(issue_to_public(issue))
        if annotation_warnings:
            result["annotation_warnings"] = annotation_warnings
        if newly_unblocked:
            result["newly_unblocked"] = [_slim_issue(i) for i in newly_unblocked]
        return _text(result)
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(_build_transition_error(tracker, args["issue_id"], str(e)))


async def _handle_reopen_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, ReopenIssueArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        issue = tracker.reopen_issue(
            args["issue_id"],
            actor=actor,
        )
        _refresh_summary()
        return _text(issue_to_public(issue))
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.INVALID_TRANSITION))


async def _handle_search_issues(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, SearchIssuesArgs)
    tracker = _get_db()
    effective_limit, offset, pag_err = _resolve_pagination(arguments)
    if pag_err is not None:
        return pag_err

    issues = tracker.search_issues(
        args["query"],
        limit=effective_limit + 1,
        offset=offset,
    )
    issues, has_more = _apply_has_more(issues, effective_limit)
    items = [_slim_issue(i) for i in issues]
    next_offset = offset + len(items) if has_more else None
    return _text(_list_response(items, has_more=has_more, next_offset=next_offset))


async def _handle_claim_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, ClaimIssueArgs)
    assignee = args.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return _text(ErrorResponse(error="assignee must be a non-empty string", code=ErrorCode.VALIDATION))
    actor, actor_err = _validate_actor(args.get("actor", assignee))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        issue = tracker.claim_issue(
            args["issue_id"],
            assignee=assignee,
            actor=actor,
        )
        _refresh_summary()
        return _text(issue_to_public(issue))
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        msg = str(e)
        if classify_value_error(msg) == ErrorCode.INVALID_TRANSITION:
            return _text(_build_transition_error(tracker, args["issue_id"], msg))
        return _text(ErrorResponse(error=msg, code=ErrorCode.CONFLICT))


async def _handle_release_claim(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, ReleaseClaimArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        issue = tracker.release_claim(args["issue_id"], actor=actor)
        _refresh_summary()
        return _text(issue_to_public(issue))
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.CONFLICT))


async def _handle_claim_next(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, ClaimNextArgs)
    assignee = args.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return _text(ErrorResponse(error="assignee must be a non-empty string", code=ErrorCode.VALIDATION))
    actor, actor_err = _validate_actor(args.get("actor", assignee))
    if actor_err:
        return actor_err
    priority_min = args.get("priority_min")
    pmin_err = _validate_int_range(priority_min, "priority_min", min_val=0, max_val=4)
    if pmin_err:
        return pmin_err
    priority_max = args.get("priority_max")
    pmax_err = _validate_int_range(priority_max, "priority_max", min_val=0, max_val=4)
    if pmax_err:
        return pmax_err
    tracker = _get_db()
    try:
        claimed = tracker.claim_next(
            assignee,
            type_filter=args.get("type"),
            priority_min=priority_min,
            priority_max=priority_max,
            actor=actor,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    if claimed is None:
        return _text(ClaimNextEmptyResponse(status="empty", reason="No ready issues matching filters"))
    _refresh_summary()
    result = ClaimNextResponse(
        **issue_to_public(claimed),
        selection_reason=claimed.format_claim_next_reason(),
    )
    return _text(result)


async def _handle_batch_close(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, BatchCloseArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = _get_db()
    issue_ids = args["issue_ids"]
    if not all(isinstance(i, str) for i in issue_ids):
        return _text(ErrorResponse(error="All issue IDs must be strings", code=ErrorCode.VALIDATION))
    ready_before = {i.id for i in tracker.get_ready()}
    closed, failed = tracker.batch_close(
        issue_ids,
        reason=args.get("reason", ""),
        actor=actor,
    )
    _refresh_summary()
    ready_after = tracker.get_ready()
    newly_unblocked = [i for i in ready_after if i.id not in ready_before]
    if detail == "full":
        full_result: BatchResponse[PublicIssue] = BatchResponse(
            succeeded=[issue_to_public(i) for i in closed],
            failed=failed,
        )
        if newly_unblocked:
            full_result["newly_unblocked"] = [_slim_issue(i) for i in newly_unblocked]
        return _text(full_result)
    slim_result: BatchResponse[SlimIssue] = BatchResponse(
        succeeded=[_slim_issue(i) for i in closed],
        failed=failed,
    )
    if newly_unblocked:
        slim_result["newly_unblocked"] = [_slim_issue(i) for i in newly_unblocked]
    return _text(slim_result)


async def _handle_batch_update(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, BatchUpdateArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    priority = args.get("priority")
    priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
    if priority_err:
        return priority_err
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = _get_db()
    issue_ids = args["issue_ids"]
    if not all(isinstance(i, str) for i in issue_ids):
        return _text(ErrorResponse(error="All issue IDs must be strings", code=ErrorCode.VALIDATION))
    u_fields = args.get("fields")
    if u_fields is not None and not isinstance(u_fields, dict):
        return _text(ErrorResponse(error="fields must be a JSON object", code=ErrorCode.VALIDATION))
    updated, update_failed = tracker.batch_update(
        issue_ids,
        status=args.get("status"),
        priority=priority,
        assignee=args.get("assignee"),
        fields=u_fields,
        actor=actor,
    )
    _refresh_summary()
    if detail == "full":
        full_result: BatchResponse[PublicIssue] = BatchResponse(
            succeeded=[issue_to_public(i) for i in updated],
            failed=update_failed,
        )
        return _text(full_result)
    result: BatchResponse[SlimIssue] = BatchResponse(
        succeeded=[_slim_issue(i) for i in updated],
        failed=update_failed,
    )
    return _text(result)


async def _handle_start_work(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, StartWorkArgs)
    assignee = args.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return _text(ErrorResponse(error="assignee must be a non-empty string", code=ErrorCode.VALIDATION))
    actor, actor_err = _validate_actor(args.get("actor", assignee))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        issue = tracker.start_work(
            args["issue_id"],
            assignee=assignee,
            target_status=args.get("target_status"),
            actor=actor,
        )
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except (AmbiguousTransitionError, InvalidTransitionError) as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.INVALID_TRANSITION))
    except ValueError as e:
        msg = str(e)
        code = classify_value_error(msg)
        if code == ErrorCode.INVALID_TRANSITION:
            return _text(_build_transition_error(tracker, args["issue_id"], msg))
        return _text(ErrorResponse(error=msg, code=ErrorCode.CONFLICT))
    _refresh_summary()
    return _text(issue_to_public(issue))


async def _handle_start_next_work(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, StartNextWorkArgs)
    assignee = args.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return _text(ErrorResponse(error="assignee must be a non-empty string", code=ErrorCode.VALIDATION))
    actor, actor_err = _validate_actor(args.get("actor", assignee))
    if actor_err:
        return actor_err
    priority_min = args.get("priority_min")
    pmin_err = _validate_int_range(priority_min, "priority_min", min_val=0, max_val=4)
    if pmin_err:
        return pmin_err
    priority_max = args.get("priority_max")
    pmax_err = _validate_int_range(priority_max, "priority_max", min_val=0, max_val=4)
    if pmax_err:
        return pmax_err
    tracker = _get_db()
    try:
        claimed = tracker.start_next_work(
            assignee=assignee,
            type_filter=args.get("type"),
            priority_min=priority_min,
            priority_max=priority_max,
            target_status=args.get("target_status"),
            actor=actor,
        )
    except (AmbiguousTransitionError, InvalidTransitionError) as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.INVALID_TRANSITION))
    except ValueError as e:
        msg = str(e)
        return _text(ErrorResponse(error=msg, code=classify_value_error(msg)))
    if claimed is None:
        return _text(ClaimNextEmptyResponse(status="empty", reason="No ready issues matching filters"))
    _refresh_summary()
    return _text(issue_to_public(claimed))
