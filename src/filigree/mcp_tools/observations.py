"""MCP tools for observation CRUD — agent scratchpad."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from mcp.types import TextContent, Tool

from filigree.issue_payloads import issue_to_public
from filigree.mcp_tools.common import (
    _MAX_LIST_RESULTS,
    _apply_has_more,
    _list_response,
    _parse_args,
    _registry_error_text,
    _resolve_pagination,
    _slim_issue,
    _text,
    _validate_actor,
    _validate_int_range,
    _validate_str,
)
from filigree.mcp_tools.payloads import observation_link_to_mcp, observation_to_mcp
from filigree.registry import RegistryResolutionError, RegistryUnavailableError
from filigree.types.api import BatchFailure, BatchResponse, ErrorCode, ErrorResponse, parse_response_detail
from filigree.types.inputs import (
    BatchDismissObservationsArgs,
    BatchLinkObservationsArgs,
    BatchPromoteObservationsArgs,
    DismissObservationArgs,
    LinkObservationArgs,
    ListObservationsArgs,
    ObserveArgs,
    PromoteObservationArgs,
    PromoteObservationsToIssueArgs,
)

logger = logging.getLogger(__name__)


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for observation tools."""
    tools = [
        Tool(
            name="observe",
            description="Record an observation — something you noticed in passing. Fire-and-forget: observations are not issues. They expire after 14 days unless promoted or dismissed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Short description of the observation"},
                    "detail": {"type": "string", "description": "Longer explanation or context"},
                    "file_path": {"type": "string", "description": "File path (relative to project root)"},
                    "line": {"type": "integer", "description": "Line number in file (1-indexed, 0 accepted for unknown)"},
                    "source_issue_id": {"type": "string", "description": "Issue ID that prompted this observation"},
                    "priority": {
                        "type": "integer",
                        "description": "Priority 0-4 (0=critical)",
                        "default": 3,
                        "minimum": 0,
                        "maximum": 4,
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["summary"],
            },
        ),
        Tool(
            name="list_observations",
            description=(
                "List pending observations with optional filtering by file, actor, priority, "
                "or age. Automatically sweeps expired observations. Useful for session-end "
                "triage: pass actor=<your-name> to see only your own observations, or "
                "older_than_hours=48 to surface stale items."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": _MAX_LIST_RESULTS,
                        "minimum": 1,
                        "description": f"Max results (default {_MAX_LIST_RESULTS}, capped at {_MAX_LIST_RESULTS} unless no_limit=true)",
                    },
                    "offset": {"type": "integer", "default": 0, "description": "Skip first N results", "minimum": 0},
                    "no_limit": {
                        "type": "boolean",
                        "default": False,
                        "description": f"Bypass the default result cap of {_MAX_LIST_RESULTS}. Use with caution on large projects.",
                    },
                    "file_path": {"type": "string", "description": "Filter by substring in file path"},
                    "file_id": {"type": "string", "description": "Filter by exact file ID"},
                    "actor": {"type": "string", "description": "Filter by exact actor (e.g. your agent name)"},
                    "source_issue_id": {
                        "type": "string",
                        "description": "Filter by the issue this observation was noticed while working on",
                    },
                    "priority_min": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "description": "Only observations with priority >= this value (0=critical, 4=backlog)",
                    },
                    "priority_max": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "description": "Only observations with priority <= this value (0=critical, 4=backlog)",
                    },
                    "older_than_hours": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Only observations created more than N hours ago (use with sort_by=created_at to surface stale items)",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["priority", "created_at", "expires_at"],
                        "default": "priority",
                        "description": "Sort field. Default 'priority' (ascending = critical first); 'created_at' to triage by age.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "asc",
                        "description": "Sort direction. Default 'asc' (lowest priority number first, oldest first).",
                    },
                },
            },
        ),
        Tool(
            name="dismiss_observation",
            description="Dismiss a single observation (logs to audit trail).",
            inputSchema={
                "type": "object",
                "properties": {
                    "observation_id": {"type": "string", "description": "Observation ID"},
                    "reason": {"type": "string", "description": "Reason for dismissal"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["observation_id"],
            },
        ),
        Tool(
            name="batch_dismiss_observations",
            description=(
                "Dismiss multiple observations in one call. Returns "
                "BatchResponse[str] (succeeded observation IDs) by default, or "
                "BatchResponse[ObservationDict] when response_detail='full' "
                "(succeeded[] then carries the pre-dismissal observation records). "
                "failed[] is always present (empty if none)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "observation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Observation IDs to dismiss",
                    },
                    "reason": {"type": "string", "default": "", "description": "Reason for dismissal"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns observation ID strings in succeeded[]; 'full' returns the pre-dismissal ObservationDict records.",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["observation_ids"],
            },
        ),
        Tool(
            name="promote_observation",
            description=(
                "Promote an observation to a real issue. Deletes the observation, creates an "
                "issue with the from-observation label and any labels passed via labels=[]. "
                "Use type='bug' for defects, type='task' for improvements/cleanup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "observation_id": {"type": "string", "description": "Observation ID"},
                    "type": {
                        "type": "string",
                        "default": "task",
                        "description": (
                            "Issue type: 'bug' for defects, 'task' for improvements/cleanup, "
                            "'feature' for new capability, 'requirement' for formal requirements "
                            "when the requirements pack is enabled"
                        ),
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Override priority (default: observation priority)",
                        "minimum": 0,
                        "maximum": 4,
                    },
                    "title": {"type": "string", "description": "Override title (default: observation summary)"},
                    "description": {"type": "string", "description": "Extra description to prepend"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional labels to attach to the promoted issue. Use this to "
                            "carry session-cluster context (e.g. ['cluster:mcp-review-e']) "
                            "onto promoted observations."
                        ),
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["observation_id"],
            },
        ),
        Tool(
            name="link_observation",
            description=(
                "Link one observation to an existing issue as durable triage evidence, then remove it from the pending observation queue."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "observation_id": {"type": "string", "description": "Observation ID"},
                    "issue_id": {"type": "string", "description": "Existing issue ID to link the observation to"},
                    "disposition": {
                        "type": "string",
                        "enum": ["evidence", "duplicate", "superseded", "related"],
                        "default": "evidence",
                        "description": "Triage disposition for the link",
                    },
                    "reason": {"type": "string", "default": "", "description": "Reason or operator note for the link"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["observation_id", "issue_id"],
            },
        ),
        Tool(
            name="batch_link_observations",
            description=(
                "Link multiple observations to one existing issue with the same disposition. Returns BatchResponse[ObservationLink]."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "observation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Observation IDs to link",
                    },
                    "issue_id": {"type": "string", "description": "Existing issue ID to link the observations to"},
                    "disposition": {
                        "type": "string",
                        "enum": ["evidence", "duplicate", "superseded", "related"],
                        "default": "evidence",
                        "description": "Triage disposition for every link",
                    },
                    "reason": {"type": "string", "default": "", "description": "Reason or operator note for every link"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["observation_ids", "issue_id"],
            },
        ),
        Tool(
            name="promote_observations_to_issue",
            description=(
                "Promote multiple observations into one issue. The created issue preserves "
                "all source observation IDs in fields.source_observation_ids."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "observation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Observation IDs to merge into one issue",
                    },
                    "type": {
                        "type": "string",
                        "default": "task",
                        "description": "Issue type for the created issue",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Override priority (default: highest priority among observations)",
                        "minimum": 0,
                        "maximum": 4,
                    },
                    "title": {"type": "string", "description": "Override title (default: first observation summary)"},
                    "description": {"type": "string", "description": "Extra description to prepend"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional labels to attach to the promoted issue",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["observation_ids"],
            },
        ),
        Tool(
            name="batch_promote_observations",
            description=(
                "Promote multiple observations in one call. Returns "
                "BatchResponse[SlimIssue] by default, or BatchResponse[PublicIssue] "
                "when response_detail='full'. failed[] is always present (empty if none)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "observation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Observation IDs to promote",
                    },
                    "type": {
                        "type": "string",
                        "default": "task",
                        "description": (
                            "Issue type: 'bug' for defects, 'task' for improvements/cleanup, "
                            "'feature' for new capability, 'requirement' for formal requirements "
                            "when the requirements pack is enabled"
                        ),
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Override priority for all created issues (default: each observation priority)",
                        "minimum": 0,
                        "maximum": 4,
                    },
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns SlimIssue items in succeeded[]; 'full' returns full PublicIssue records.",
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["observation_ids"],
            },
        ),
    ]

    handlers: dict[str, Callable[..., Any]] = {
        "observe": _handle_observe,
        "list_observations": _handle_list_observations,
        "dismiss_observation": _handle_dismiss_observation,
        "batch_dismiss_observations": _handle_batch_dismiss_observations,
        "promote_observation": _handle_promote_observation,
        "link_observation": _handle_link_observation,
        "batch_link_observations": _handle_batch_link_observations,
        "promote_observations_to_issue": _handle_promote_observations_to_issue,
        "batch_promote_observations": _handle_batch_promote_observations,
    }

    return tools, handlers


async def _handle_observe(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, ObserveArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    # Validate raw argument types up front — otherwise a bool priority would
    # silently coerce to int (True==1), a dict detail would hit SQLite binding
    # errors, and a non-string file_path would crash in _normalize_scan_path.
    for err in (
        _validate_str(args.get("summary"), "summary"),
        _validate_str(args.get("detail"), "detail"),
        _validate_str(args.get("file_path"), "file_path"),
        _validate_str(args.get("source_issue_id"), "source_issue_id"),
        _validate_int_range(args.get("line"), "line", min_val=0),
        _validate_int_range(args.get("priority"), "priority", min_val=0, max_val=4),
    ):
        if err is not None:
            return err

    tracker = _get_db()
    try:
        obs = tracker.create_observation(
            args.get("summary", ""),
            detail=args.get("detail", ""),
            file_path=args.get("file_path", ""),
            line=args.get("line"),
            source_issue_id=args.get("source_issue_id", ""),
            priority=args.get("priority", 3),
            actor=actor,
        )
    except (RegistryResolutionError, RegistryUnavailableError) as e:
        return _registry_error_text(e, action="recording observation")
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()
    return _text(observation_to_mcp(obs))


async def _handle_list_observations(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, ListObservationsArgs)
    effective_limit, offset, pag_err = _resolve_pagination(arguments)
    if pag_err is not None:
        return pag_err
    tracker = _get_db()
    try:
        observations = tracker.list_observations(
            limit=effective_limit + 1,
            offset=offset,
            file_path=args.get("file_path", ""),
            file_id=args.get("file_id", ""),
            actor=args.get("actor", ""),
            source_issue_id=args.get("source_issue_id", ""),
            priority_min=args.get("priority_min"),
            priority_max=args.get("priority_max"),
            older_than_hours=args.get("older_than_hours"),
            sort_by=args.get("sort_by", "priority"),
            direction=args.get("direction", "asc"),
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    observations, has_more = _apply_has_more(observations, effective_limit)
    next_offset = offset + len(observations) if has_more else None
    # Drops the legacy ``stats`` sibling per the loom precedent (Phase C4 dropped
    # it on the HTTP side); consumers needing observation stats use
    # ``tracker.observation_stats()`` via a dedicated tool.
    return _text(_list_response([observation_to_mcp(obs) for obs in observations], has_more=has_more, next_offset=next_offset))


async def _handle_dismiss_observation(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, DismissObservationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    tracker = _get_db()
    try:
        tracker.dismiss_observation(
            args["observation_id"],
            actor=actor,
            reason=args.get("reason", ""),
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.NOT_FOUND))
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()
    return _text({"status": "dismissed", "observation_id": args["observation_id"]})


async def _handle_batch_dismiss_observations(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, BatchDismissObservationsArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    # Validate observation_ids is a list of strings up front — a bare string
    # would otherwise be iterated char-by-char and produce bogus per-character
    # not_found results (see filigree-45580755aa).
    raw_ids = args.get("observation_ids", [])
    if not isinstance(raw_ids, list):
        return _text(ErrorResponse(error="'observation_ids' must be an array of strings", code=ErrorCode.VALIDATION))
    if not all(isinstance(x, str) for x in raw_ids):
        return _text(ErrorResponse(error="'observation_ids' must contain only string values", code=ErrorCode.VALIDATION))
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)

    tracker = _get_db()
    # Snapshot pre-dismissal records for full mode — the rows are deleted by
    # batch_dismiss_observations so the fetch must happen first.
    full_records: list[dict[str, Any]] = []
    if detail == "full":
        full_records = [observation_to_mcp(obs) for obs in tracker.get_observations_by_ids(raw_ids)]
    try:
        result = tracker.batch_dismiss_observations(
            raw_ids,
            actor=actor,
            reason=args.get("reason", ""),
        )
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()
    not_found_set = set(result["not_found"])
    # Preserve input order, deduped, for the succeeded list — db returns a
    # row-count plus the not-found ids, so the dismissed-id list is computed
    # here as: unique inputs minus not-found.
    succeeded_ids = [oid for oid in dict.fromkeys(raw_ids) if oid not in not_found_set]
    failed: list[BatchFailure] = [
        BatchFailure(id=oid, error=f"Observation not found: {oid}", code=ErrorCode.NOT_FOUND) for oid in result["not_found"]
    ]
    if detail == "full":
        full_resp: BatchResponse[dict[str, Any]] = BatchResponse(succeeded=full_records, failed=failed)
        return _text(full_resp)
    resp: BatchResponse[str] = BatchResponse(succeeded=succeeded_ids, failed=failed)
    return _text(resp)


async def _handle_batch_promote_observations(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, BatchPromoteObservationsArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    raw_ids = args.get("observation_ids", [])
    if not isinstance(raw_ids, list):
        return _text(ErrorResponse(error="'observation_ids' must be an array of strings", code=ErrorCode.VALIDATION))
    if not all(isinstance(x, str) for x in raw_ids):
        return _text(ErrorResponse(error="'observation_ids' must contain only string values", code=ErrorCode.VALIDATION))

    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)

    issue_type = args.get("type", "task")
    type_err = _validate_str(issue_type, "type")
    if type_err is not None:
        return type_err

    priority = args.get("priority")
    if priority is not None:
        priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
        if priority_err:
            return priority_err

    tracker = _get_db()
    try:
        promoted, failed = tracker.batch_promote_observations(
            raw_ids,
            issue_type=issue_type,
            priority=priority,
            actor=actor,
        )
    except sqlite3.Error as e:
        logger.error("batch_promote_observations database error", exc_info=True)
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()

    issues = [tracker.get_issue(result["issue"].id) for result in promoted]
    if detail == "full":
        full_resp: BatchResponse[dict[str, Any]] = BatchResponse(
            succeeded=[dict(issue_to_public(issue)) for issue in issues],
            failed=failed,
        )
        return _text(full_resp)
    slim_resp = BatchResponse(
        succeeded=[_slim_issue(issue) for issue in issues],
        failed=failed,
    )
    return _text(slim_resp)


async def _handle_link_observation(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, LinkObservationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    tracker = _get_db()
    try:
        link = tracker.link_observation_to_issue(
            args["observation_id"],
            args["issue_id"],
            disposition=args.get("disposition", "evidence"),
            reason=args.get("reason", ""),
            actor=actor,
        )
    except KeyError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        msg = str(e)
        err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
        return _text(ErrorResponse(error=msg, code=err_code))
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()
    return _text(observation_link_to_mcp(link))


async def _handle_batch_link_observations(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, BatchLinkObservationsArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    raw_ids = args.get("observation_ids", [])
    if not isinstance(raw_ids, list):
        return _text(ErrorResponse(error="'observation_ids' must be an array of strings", code=ErrorCode.VALIDATION))
    if not all(isinstance(x, str) for x in raw_ids):
        return _text(ErrorResponse(error="'observation_ids' must contain only string values", code=ErrorCode.VALIDATION))

    tracker = _get_db()
    try:
        linked, failed = tracker.batch_link_observations_to_issue(
            raw_ids,
            args["issue_id"],
            disposition=args.get("disposition", "evidence"),
            reason=args.get("reason", ""),
            actor=actor,
        )
    except KeyError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()
    resp: BatchResponse[dict[str, Any]] = BatchResponse(
        succeeded=[observation_link_to_mcp(item) for item in linked],
        failed=failed,
    )
    return _text(resp)


async def _handle_promote_observations_to_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, PromoteObservationsToIssueArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    raw_ids = args.get("observation_ids", [])
    if not isinstance(raw_ids, list):
        return _text(ErrorResponse(error="'observation_ids' must be an array of strings", code=ErrorCode.VALIDATION))
    if not all(isinstance(x, str) for x in raw_ids):
        return _text(ErrorResponse(error="'observation_ids' must contain only string values", code=ErrorCode.VALIDATION))

    priority = args.get("priority")
    if priority is not None:
        priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
        if priority_err:
            return priority_err
    labels = args.get("labels")
    if labels is not None and (not isinstance(labels, list) or not all(isinstance(lbl, str) for lbl in labels)):
        return _text(ErrorResponse(error="labels must be a list of strings", code=ErrorCode.VALIDATION))

    tracker = _get_db()
    try:
        result = tracker.promote_observations_to_issue(
            raw_ids,
            issue_type=args.get("type", "task"),
            priority=priority,
            title=args.get("title"),
            extra_description=args.get("description", ""),
            actor=actor,
            labels=labels,
        )
    except TypeError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except ValueError as e:
        msg = str(e)
        err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
        return _text(ErrorResponse(error=msg, code=err_code))
    except sqlite3.Error as e:
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()
    issue = tracker.get_issue(result["issue"].id)
    resp: dict[str, object] = dict(issue_to_public(issue))
    if result.get("warnings"):
        resp["warnings"] = result["warnings"]
    return _text(resp)


async def _handle_promote_observation(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, PromoteObservationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    priority = args.get("priority")
    if priority is not None:
        priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
        if priority_err:
            return priority_err

    labels = args.get("labels")
    if labels is not None and (not isinstance(labels, list) or not all(isinstance(lbl, str) for lbl in labels)):
        return _text(ErrorResponse(error="labels must be a list of strings", code=ErrorCode.VALIDATION))

    tracker = _get_db()
    try:
        result = tracker.promote_observation(
            args["observation_id"],
            issue_type=args.get("type", "task"),
            priority=priority,
            title=args.get("title"),
            extra_description=args.get("description", ""),
            actor=actor,
            labels=labels,
        )
    except ValueError as e:
        msg = str(e)
        err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
        return _text(ErrorResponse(error=msg, code=err_code))
    except sqlite3.Error as e:
        logger.error("promote_observation database error", exc_info=True)
        return _text(ErrorResponse(error=f"Database error: {e}", code=ErrorCode.IO))
    _refresh_summary()
    issue = tracker.get_issue(result["issue"].id)
    resp: dict[str, object] = dict(issue_to_public(issue))
    if result.get("warnings"):
        resp["warnings"] = result["warnings"]
    return _text(resp)
