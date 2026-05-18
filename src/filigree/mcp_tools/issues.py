"""MCP tools for issue CRUD, search, claim, and batch operations."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from filigree.types.core import StatusCategory

from mcp.types import TextContent, Tool

from filigree.core import WrongProjectError
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
    ClaimConflictError,
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
    GetStaleClaimsArgs,
    HeartbeatWorkArgs,
    ListIssuesArgs,
    ReclaimIssueArgs,
    ReleaseClaimArgs,
    ReleaseMyClaimsArgs,
    ReopenIssueArgs,
    SearchIssuesArgs,
    StartNextWorkArgs,
    StartWorkArgs,
    UpdateIssueArgs,
)

logger = logging.getLogger(__name__)

_UPDATE_TRACKED_FIELDS = ("status", "priority", "title", "assignee", "description", "notes", "parent_id", "fields")
_LIST_ISSUES_SORT_FIELDS = {"created_at", "updated_at", "priority"}


def _wrong_project_response(exc: WrongProjectError) -> list[TextContent]:
    # 2.1.0 §1.2: MCP is an untrusted-surface boundary (agents can be
    # arbitrarily scoped); surface the generic ``safe_message`` so a
    # foreign prefix is not leaked back to a caller who guessed an ID.
    return _text(ErrorResponse(error=exc.safe_message, code=ErrorCode.VALIDATION))


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
                    "type": {
                        "type": "string",
                        "default": "task",
                        "description": (
                            "Issue type. Core examples: 'bug', 'task', 'feature'. "
                            "'requirement' is available when the requirements pack is enabled."
                        ),
                    },
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
                "Soft transition warnings are returned in data_warnings. "
                "When actor is present and the issue is held, actor is the default expected holder. "
                "Pass expected_assignee for coordinator overrides; mismatches return CONFLICT."
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
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition. When omitted and actor is present on a "
                            "held issue, actor is the default expected holder. Set explicitly for "
                            "coordinator compare-and-swap overrides."
                        ),
                    },
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="close_issue",
            description=(
                "Close an issue. Routes through the same transition validator as update_issue, so the "
                "current status must have a defined transition to the target done state. When status is "
                "omitted, defaults to the first done-category state for the type (e.g. 'closed'); pass "
                "status explicitly to land in an alternate done state (e.g. 'wont_fix', 'not_a_bug', "
                "'cancelled'). Returns INVALID_TRANSITION with valid_transitions when the path isn't "
                "defined — walk the workflow with update_issue, pass a reachable status, or pass "
                "force=true to use the declared reverse/escape edge for cleanup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                    "reason": {"type": "string", "description": "Close reason"},
                    "status": {
                        "type": "string",
                        "description": (
                            "Target done-category status. Optional; defaults to first done state for the "
                            "type. Use get_valid_transitions(issue_id) to discover reachable done states."
                        ),
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "fields": {"type": "object", "description": "Custom fields to set (e.g. root_cause for incidents)"},
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition. When omitted and actor is present on a "
                            "held issue, actor is the default expected holder. Set explicitly for "
                            "coordinator compare-and-swap overrides."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Use the template reverse/escape transition and close from any state. "
                            "Use only for cleanup flows that intentionally leave the normal workflow; "
                            "transition_forced is recorded before status_changed."
                        ),
                    },
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="reopen_issue",
            description=(
                "Reopen a closed issue to the last non-done status before closure. "
                "Clears closed_at and stale close-only fields such as close_reason."
            ),
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
            description=(
                "Search issues by title and description. Pure word-token queries use FTS5 "
                "with prefix matching for ranked relevance. Queries containing punctuation "
                "(hyphens, brackets, etc.) — e.g. 'mcp-review-e' or '[cluster-foo]' — fall "
                "back to a LIKE substring search on the raw query so agents can find their "
                "own self-tagged work without splitting it manually. Pass status_category "
                "to scope results to live work (open/wip) and exclude archived/closed rows."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Search query. Pure word tokens use FTS5; queries with hyphens, "
                            "brackets, or other punctuation use LIKE substring fallback so "
                            "self-tagged work prefixed like '[cluster-foo]' is found verbatim."
                        ),
                    },
                    "status_category": {
                        "type": "string",
                        "enum": ["open", "wip", "done"],
                        "description": (
                            "Optional category filter. Default returns all categories; "
                            "pass 'open' or 'wip' to exclude archived/closed results."
                        ),
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
            description=(
                "Release a claimed issue by clearing its assignee, and (by default) revert wip-category "
                "issues back to an open-category status so they rejoin get_ready discovery rather than "
                "being orphaned in wip with no assignee. The reverse target is the template-defined open "
                "predecessor of the current wip status (e.g. in_progress→open for task, fixing→confirmed "
                "for bug); types with no open predecessor fall back to initial_state. Pass "
                "revert_status=false to keep the legacy behaviour and leave the status unchanged. "
                "By default this is strict and only succeeds if the issue has an assignee. "
                "Pass if_held=true for release-if-held cleanup: unassigned issues are a no-op, "
                "and assigned issues are only released when held by expected_assignee or, if omitted, actor."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to release"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "if_held": {
                        "type": "boolean",
                        "default": False,
                        "description": "Idempotent release-if-held mode; unassigned issues are returned unchanged.",
                    },
                    "expected_assignee": {
                        "type": "string",
                        "description": "Only release when the current assignee matches this value; defaults to actor in if_held mode.",
                    },
                    "reason": {"type": "string", "description": "Audit reason for releasing the claim."},
                    "revert_status": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "When true (default), wip-category issues transition back to an open-category "
                            "predecessor on release so they rejoin discovery. Set to false to skip the "
                            "status revert (legacy behaviour)."
                        ),
                    },
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="release_my_claims",
            description=(
                "Bulk-release every live claim held by ``actor`` in one call — designed for "
                "end-of-session cleanup. Discovers all issues whose assignee == actor "
                "(optionally narrowed by ``label`` and/or ``label_prefix``), then releases "
                "each via release_claim(if_held=True). Done-category issues are skipped "
                "(their assignee is audit trail, not a live claim). Returns "
                "BatchResponse[SlimIssue] with succeeded[] (released) and failed[] "
                "(per-issue errors). Pair this with the ``cluster:*`` label convention: "
                "tag your scratch with ``cluster:my-session`` at create time, then call "
                "release_my_claims(actor='my-session', label_prefix='cluster:') at session end."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "actor": {
                        "type": "string",
                        "description": "Agent identity whose claims should be released. Required.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Restrict to issues carrying this exact label.",
                    },
                    "label_prefix": {
                        "type": "string",
                        "description": "Restrict to issues with a label starting with this prefix (must include trailing colon, e.g. 'cluster:').",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, list the issues that would be released without making changes.",
                    },
                    "revert_status": {
                        "type": "boolean",
                        "default": True,
                        "description": "Forwarded to release_claim per-item; true reverts wip→open so released issues rejoin discovery.",
                    },
                    "reason": {
                        "type": "string",
                        "default": "",
                        "description": "Audit reason recorded on each release event.",
                    },
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' (default) returns SlimIssue items in succeeded[]; 'full' returns full PublicIssue records.",
                    },
                },
                "required": ["actor"],
            },
        ),
        Tool(
            name="heartbeat_work",
            description=(
                "Refresh claim liveness metadata for a claimed issue. "
                "By default actor is treated as the expected current holder; pass expected_assignee for coordinator flows."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to heartbeat"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail and holder check"},
                    "expected_assignee": {
                        "type": "string",
                        "description": "Only heartbeat when the current assignee matches this value.",
                    },
                    "lease_hours": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 48,
                        "description": "Lease duration from this heartbeat, in hours.",
                    },
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="get_stale_claims",
            description="List assigned, non-done issues whose claim lease has expired or whose legacy assignment is older than the stale threshold.",
            inputSchema={
                "type": "object",
                "properties": {
                    "stale_after_hours": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 48,
                        "description": "Age threshold for legacy assignments without explicit claim expiry.",
                    },
                    "expires_within_hours": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8760,
                        "description": "Also include active explicit leases expiring within this many hours.",
                    },
                },
            },
        ),
        Tool(
            name="reclaim_issue",
            description=(
                "Safely transfer a claimed issue to a new assignee when the current assignee matches expected_assignee. "
                "Records the reason on the reclaim event."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to reclaim"},
                    "assignee": {"type": "string", "minLength": 1, "description": "New assignee"},
                    "expected_assignee": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Current assignee expected by the caller",
                    },
                    "reason": {"type": "string", "minLength": 1, "description": "Why the claim is being reclaimed"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                    "lease_hours": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 48,
                        "description": "Lease duration for the new assignee, in hours.",
                    },
                },
                "required": ["issue_id", "assignee", "expected_assignee", "reason"],
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
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition applied per-item. When omitted and actor "
                            "is present on a held issue, actor is the default expected holder. "
                            "Mismatches land in failed[] with code=CONFLICT."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Use the template reverse/escape transition on every item. "
                            "Use only for cleanup flows that intentionally leave the normal workflow."
                        ),
                    },
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
                    "expected_assignee": {
                        "type": "string",
                        "description": (
                            "Claim-aware precondition applied per-item. When omitted and actor "
                            "is present on a held issue, actor is the default expected holder. "
                            "Mismatches land in failed[] with code=CONFLICT."
                        ),
                    },
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
        "release_my_claims": _handle_release_my_claims,
        "heartbeat_work": _handle_heartbeat_work,
        "get_stale_claims": _handle_get_stale_claims,
        "reclaim_issue": _handle_reclaim_issue,
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
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
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
            expected_assignee=expected_assignee,
        )
        _refresh_summary()
        changed = [attr for attr in _UPDATE_TRACKED_FIELDS if getattr(issue, attr) != getattr(before, attr)]
        result = IssueWithChangedFields(**issue_to_public(issue), changed_fields=changed)
        return _text(result)
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except WrongProjectError as e:
        return _wrong_project_response(e)
    except ValueError as e:
        msg = str(e)
        if isinstance(e, ClaimConflictError):
            return _text(ErrorResponse(error=msg, code=ErrorCode.CONFLICT))
        if classify_value_error(msg) == ErrorCode.INVALID_TRANSITION:
            transitions = e.valid_transitions if isinstance(e, InvalidTransitionError) else None
            return _text(_build_transition_error(tracker, args["issue_id"], msg, valid_transitions=transitions))
        return _text(ErrorResponse(error=msg, code=ErrorCode.VALIDATION))


async def _handle_close_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, CloseIssueArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    force = args.get("force", False)
    if not isinstance(force, bool):
        return _text(ErrorResponse(error="force must be a boolean", code=ErrorCode.VALIDATION))
    tracker = _get_db()
    try:
        ready_before = {i.id for i in tracker.get_ready()}
        annotation_warnings = tracker.get_annotation_closeout_warnings(args["issue_id"])
        issue = tracker.close_issue(
            args["issue_id"],
            reason=args.get("reason", ""),
            status=args.get("status"),
            actor=actor,
            fields=args.get("fields"),
            expected_assignee=expected_assignee,
            force=force,
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
    except WrongProjectError as e:
        return _wrong_project_response(e)
    except ValueError as e:
        msg = str(e)
        if isinstance(e, ClaimConflictError):
            return _text(ErrorResponse(error=msg, code=ErrorCode.CONFLICT))
        transitions = e.valid_transitions if isinstance(e, InvalidTransitionError) else None
        return _text(_build_transition_error(tracker, args["issue_id"], msg, valid_transitions=transitions))


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
    except WrongProjectError as e:
        return _wrong_project_response(e)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.INVALID_TRANSITION))


async def _handle_search_issues(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, SearchIssuesArgs)
    tracker = _get_db()
    effective_limit, offset, pag_err = _resolve_pagination(arguments)
    if pag_err is not None:
        return pag_err

    status_category_raw = args.get("status_category")
    if status_category_raw is not None and status_category_raw not in ("open", "wip", "done"):
        return _text(
            ErrorResponse(
                error=f"Invalid status_category: {status_category_raw!r}. Valid: open, wip, done.",
                code=ErrorCode.VALIDATION,
            )
        )
    status_category = cast("StatusCategory | None", status_category_raw)
    try:
        issues = tracker.search_issues(
            args["query"],
            limit=effective_limit + 1,
            offset=offset,
            status_category=status_category,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
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
    except WrongProjectError as e:
        return _wrong_project_response(e)
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
    if_held = args.get("if_held", False)
    if not isinstance(if_held, bool):
        return _text(ErrorResponse(error="if_held must be a boolean", code=ErrorCode.VALIDATION))
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    reason = args.get("reason", "")
    if not isinstance(reason, str):
        return _text(ErrorResponse(error="reason must be a string", code=ErrorCode.VALIDATION))
    revert_status = args.get("revert_status", True)
    if not isinstance(revert_status, bool):
        return _text(ErrorResponse(error="revert_status must be a boolean", code=ErrorCode.VALIDATION))
    tracker = _get_db()
    try:
        issue = tracker.release_claim(
            args["issue_id"],
            actor=actor,
            if_held=if_held,
            expected_assignee=expected_assignee,
            reason=reason,
            revert_status=revert_status,
        )
        _refresh_summary()
        return _text(issue_to_public(issue))
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except WrongProjectError as e:
        return _wrong_project_response(e)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.CONFLICT))


async def _handle_release_my_claims(arguments: dict[str, Any]) -> list[TextContent]:
    """Bulk-release every live claim held by ``actor`` in one call.

    Designed for end-of-session cleanup: tag scratch with ``cluster:my-session``
    at create time, then release_my_claims(actor='my-session',
    label_prefix='cluster:') at session end. (F4 — review-h.)
    """
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, ReleaseMyClaimsArgs)
    raw_actor = args.get("actor", "")
    if not isinstance(raw_actor, str) or not raw_actor.strip():
        return _text(ErrorResponse(error="actor is required and must be a non-empty string", code=ErrorCode.VALIDATION))
    actor = raw_actor.strip()
    label = args.get("label")
    if label is not None and not isinstance(label, str):
        return _text(ErrorResponse(error="label must be a string", code=ErrorCode.VALIDATION))
    label_prefix = args.get("label_prefix")
    if label_prefix is not None and not isinstance(label_prefix, str):
        return _text(ErrorResponse(error="label_prefix must be a string", code=ErrorCode.VALIDATION))
    dry_run = args.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return _text(ErrorResponse(error="dry_run must be a boolean", code=ErrorCode.VALIDATION))
    revert_status = args.get("revert_status", True)
    if not isinstance(revert_status, bool):
        return _text(ErrorResponse(error="revert_status must be a boolean", code=ErrorCode.VALIDATION))
    reason = args.get("reason", "")
    if not isinstance(reason, str):
        return _text(ErrorResponse(error="reason must be a string", code=ErrorCode.VALIDATION))
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)

    tracker = _get_db()
    try:
        released, failed = tracker.release_my_claims(
            actor=actor,
            label=label,
            label_prefix=label_prefix,
            dry_run=dry_run,
            revert_status=revert_status,
            reason=reason,
        )
    except ValueError as exc:
        return _text(ErrorResponse(error=str(exc), code=ErrorCode.VALIDATION))
    if not dry_run:
        _refresh_summary()
    if detail == "full":
        full_payload: dict[str, Any] = {
            "succeeded": [issue_to_public(i) for i in released],
            "failed": failed,
        }
        if dry_run:
            full_payload["dry_run"] = True
        return _text(full_payload)
    slim_payload: dict[str, Any] = {
        "succeeded": [_slim_issue(i) for i in released],
        "failed": failed,
    }
    if dry_run:
        slim_payload["dry_run"] = True
    return _text(slim_payload)


async def _handle_heartbeat_work(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, HeartbeatWorkArgs)
    # When the caller omits actor, default the audit identity to the issue's
    # current assignee rather than the literal string 'mcp'. The previous
    # default ('mcp') failed the implicit holder check whenever the actual
    # holder forgot to pass actor: the rightful holder's heartbeat would
    # CONFLICT with "expected 'mcp'", silently letting the lease expire.
    # (filigree-cb980eee0d, P2.5 senior-user MCP review.)
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    raw_actor = args.get("actor")
    if raw_actor is None:
        # No explicit actor — read the current holder so the audit row is
        # attributed correctly and the holder check is a no-op (caller
        # didn't ask for one).
        tracker = _get_db()
        try:
            issue = tracker.get_issue(args["issue_id"])
        except KeyError:
            return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
        actor = issue.assignee or "mcp"
    else:
        actor, actor_err = _validate_actor(raw_actor)
        if actor_err:
            return actor_err
    lease_hours = args.get("lease_hours", 48)
    lease_err = _validate_int_range(lease_hours, "lease_hours", min_val=1)
    if lease_err:
        return lease_err
    tracker = _get_db()
    try:
        issue = tracker.heartbeat_work(
            args["issue_id"],
            actor=actor,
            expected_assignee=expected_assignee,
            lease_hours=lease_hours,
        )
        _refresh_summary()
        return _text(issue_to_public(issue))
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except WrongProjectError as e:
        return _wrong_project_response(e)
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.CONFLICT))


async def _handle_get_stale_claims(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetStaleClaimsArgs)
    stale_after_hours = args.get("stale_after_hours", 48)
    expires_within_hours = args.get("expires_within_hours")
    stale_err = _validate_int_range(stale_after_hours, "stale_after_hours", min_val=1)
    if stale_err:
        return stale_err
    expiry_err = _validate_int_range(expires_within_hours, "expires_within_hours", min_val=1, max_val=8760)
    if expiry_err:
        return expiry_err
    tracker = _get_db()
    try:
        issues = tracker.get_stale_claims(
            stale_after_hours=stale_after_hours,
            expires_within_hours=expires_within_hours,
        )
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    return _text(_list_response([issue_to_public(issue) for issue in issues], has_more=False))


async def _handle_reclaim_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, ReclaimIssueArgs)
    assignee = args.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return _text(ErrorResponse(error="assignee must be a non-empty string", code=ErrorCode.VALIDATION))
    expected_assignee = args.get("expected_assignee")
    if not isinstance(expected_assignee, str) or not expected_assignee.strip():
        return _text(ErrorResponse(error="expected_assignee must be a non-empty string", code=ErrorCode.VALIDATION))
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return _text(ErrorResponse(error="reason must be a non-empty string", code=ErrorCode.VALIDATION))
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    lease_hours = args.get("lease_hours", 48)
    lease_err = _validate_int_range(lease_hours, "lease_hours", min_val=1)
    if lease_err:
        return lease_err
    tracker = _get_db()
    try:
        issue = tracker.reclaim_issue(
            args["issue_id"],
            assignee=assignee,
            expected_assignee=expected_assignee,
            reason=reason,
            actor=actor,
            lease_hours=lease_hours,
        )
        _refresh_summary()
        return _text(issue_to_public(issue))
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))
    except WrongProjectError as e:
        return _wrong_project_response(e)
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
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
    force = args.get("force", False)
    if not isinstance(force, bool):
        return _text(ErrorResponse(error="force must be a boolean", code=ErrorCode.VALIDATION))
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = _get_db()
    issue_ids = args["issue_ids"]
    if not all(isinstance(i, str) for i in issue_ids):
        return _text(ErrorResponse(error="All issue IDs must be strings", code=ErrorCode.VALIDATION))
    ready_before = {i.id for i in tracker.get_ready()}
    try:
        closed, failed = tracker.batch_close(
            issue_ids,
            reason=args.get("reason", ""),
            actor=actor,
            expected_assignee=expected_assignee,
            force=force,
        )
    except WrongProjectError as e:
        # 2.1.0 §0.4: envelope-level abort on foreign-prefix.
        return _wrong_project_response(e)
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
    expected_assignee = args.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _text(ErrorResponse(error="expected_assignee must be a string", code=ErrorCode.VALIDATION))
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
    try:
        updated, update_failed = tracker.batch_update(
            issue_ids,
            status=args.get("status"),
            priority=priority,
            assignee=args.get("assignee"),
            fields=u_fields,
            actor=actor,
            expected_assignee=expected_assignee,
        )
    except WrongProjectError as e:
        # 2.1.0 §0.4: envelope-level abort on foreign-prefix.
        return _wrong_project_response(e)
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
    except WrongProjectError as e:
        return _wrong_project_response(e)
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
