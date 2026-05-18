"""TypedDicts for MCP tool handler and dashboard route API responses."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, Literal, NotRequired, TypedDict, TypeVar, assert_never

from filigree.types.core import ISOTimestamp, IssueDict, StatusCategory
from filigree.types.events import EventType
from filigree.types.planning import CommentRecord, PlanTree, StatsResult

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


class TransitionDetail(TypedDict):
    """Full transition info returned by get_issue and get_valid_transitions.

    ``enforcement`` is always populated in practice because TransitionDetail
    is built from TransitionOption, which sources it from TransitionDefinition.
    The underlying type allows None for unregistered-type fallback paths.
    """

    to: str
    category: str
    enforcement: str
    requires_fields: list[str]
    missing_fields: list[str]
    ready: bool


class TransitionHint(TypedDict):
    """Abbreviated transition hint included in error responses."""

    to: str
    category: str
    ready: NotRequired[bool]


# InboundTransitionInfo uses "from" as a key at runtime (a Python keyword).
# TypedDict cannot express this with class syntax; we use functional form.
InboundTransitionInfo = TypedDict("InboundTransitionInfo", {"from": str, "enforcement": str})


class OutboundTransitionInfo(TypedDict):
    """Outbound transition info returned by explain_status."""

    to: str
    enforcement: str
    requires_fields: list[str]


class SlimIssue(TypedDict):
    """Reduced 5-key issue shape for search results and unblocked lists.

    Per Phase D3 of the 2.0 federation work package, the entity's own
    primary key is named ``issue_id`` (matching the loom vocabulary used
    by ``SlimIssueLoom`` since Phase C). Cross-entity references inside
    response payloads (``parent_id``, ``blocks``, ``blocked_by``,
    ``children``) keep their existing names — only the entity's own
    primary key is renamed.
    """

    issue_id: str
    title: str
    status: str
    priority: int
    type: str


class ReadyIssue(SlimIssue):
    """Ready-queue issue projection.

    By default ready surfaces return the inherited slim keys only. When callers
    request context, parent fields are added so agents can display the owning
    epic/plan without an immediate follow-up ``get_issue`` call.
    """

    parent_issue_id: NotRequired[str | None]
    parent_title: NotRequired[str | None]


class PublicIssue(TypedDict):
    """Full issue shape for MCP and CLI JSON responses.

    Internal/classic code may still use IssueDict with ``id``; agent-facing
    2.0 surfaces expose the entity primary key as ``issue_id``.

    Both ``parent_id`` and ``parent_issue_id`` carry the same value — the
    name was inconsistent across get_issue (parent_id) vs get_ready /
    list_issues (parent_issue_id). To close the gap without breaking
    existing consumers, both names are emitted; agents should prefer
    ``parent_issue_id`` for consistency with the slim/ready shapes
    (filigree-cb980eee0d, P2.9). ``parent_id`` is retained for backward
    compatibility and may be removed in a future major.
    """

    issue_id: str
    title: str
    status: str
    status_category: StatusCategory
    priority: int
    type: str
    parent_id: str | None
    parent_issue_id: str | None
    assignee: str
    claimed_at: ISOTimestamp | None
    last_heartbeat_at: ISOTimestamp | None
    claim_expires_at: ISOTimestamp | None
    created_at: ISOTimestamp
    updated_at: ISOTimestamp
    closed_at: ISOTimestamp | None
    description: str
    notes: str
    fields: dict[str, Any]
    labels: list[str]
    blocks: list[str]
    blocked_by: list[str]
    is_ready: bool
    children: list[str]
    data_warnings: list[str]


class BlockedIssue(SlimIssue):
    """Slim issue with blocked_by list for get_blocked responses."""

    blocked_by: list[str]


class ErrorResponse(TypedDict):
    """Unified error envelope for MCP tools, CLI, and dashboard routes.

    Flat shape — `details` is optional. Dashboard previously used nested
    `{"error": {"message", "code"}}`; 2.0 consolidates to this one shape.
    """

    error: str
    code: ErrorCode
    details: NotRequired[dict[str, Any]]


class TransitionError(TypedDict):
    """Extended error for invalid status transitions.

    Includes valid_transitions hint to guide the caller toward correct states.
    ``code`` matches the 2.0 uppercase ErrorCode.INVALID_TRANSITION so this
    envelope is indistinguishable from the regular ErrorResponse for case-
    sensitive consumers.
    """

    error: str
    code: Literal[ErrorCode.INVALID_TRANSITION]
    valid_transitions: NotRequired[list[TransitionHint]]
    hint: NotRequired[str]
    reopen_available: NotRequired[bool]


# ---------------------------------------------------------------------------
# Flat inheritance — IssueDict + extra keys (preserves wire format)
#
# IMPORTANT: Never use `class Foo(IssueDict, total=False)` — this makes ALL
# inherited IssueDict keys optional to mypy, defeating type safety. Instead
# use `NotRequired` on individual optional fields.
#
# NOTE on **spread: `Foo(**issue.to_dict(), extra=val)` silently passes through
# any extra keys that to_dict() might add in the future. The shape contract
# tests in test_type_contracts.py catch drift by asserting exact key-set equality.
#
# RESERVED EXTENSION KEYS — these names must never be added to IssueDict:
# valid_transitions, changed_fields, newly_unblocked, selection_reason,
# dep_details, events, comments
# ---------------------------------------------------------------------------


class IssueWithTransitions(PublicIssue):
    """Issue detail with optional valid_transitions (MCP get_issue)."""

    valid_transitions: NotRequired[list[TransitionDetail]]


class IssueWithChangedFields(PublicIssue):
    """Issue update response with list of changed field names."""

    changed_fields: list[str]


class IssueWithUnblocked(PublicIssue):
    """Issue close response with optional newly-unblocked issues."""

    newly_unblocked: NotRequired[list[SlimIssue]]


class ClaimNextResponse(PublicIssue):
    """Claimed issue with human-readable selection reason."""

    selection_reason: str


# ---------------------------------------------------------------------------
# Flat inheritance — StatsResult + prefix
# ---------------------------------------------------------------------------


class StatsWithPrefix(StatsResult):
    """Project stats with project prefix for dashboard display."""

    prefix: str


# ---------------------------------------------------------------------------
# Dashboard detail — IssueDict + dep/event/comment data
# ---------------------------------------------------------------------------


class DepDetail(TypedDict):
    """Minimal dependency info for dep_details lookup in issue detail."""

    title: str
    status: str
    status_category: StatusCategory
    priority: int


class IssueDetailEvent(TypedDict):
    """Slim 5-column projection of EventRecord — only the columns selected by
    api_issue_detail's SQL query. Do NOT extend to full EventRecord; that is
    a separate type in types/events.py."""

    event_type: EventType
    actor: str
    old_value: str | None
    new_value: str | None
    created_at: ISOTimestamp


class EnrichedIssueDetail(IssueDict):
    """Full issue detail with dependency info, events, and comments."""

    dep_details: dict[str, DepDetail]
    events: list[IssueDetailEvent]
    comments: list[CommentRecord]


# ---------------------------------------------------------------------------
# True envelopes — list / search / batch wrappers
# ---------------------------------------------------------------------------


class PlanResponse(PlanTree):
    """Plan tree with computed progress percentage (MCP get_plan)."""

    progress_pct: float


# ---------------------------------------------------------------------------
# MCP planning / meta response shapes
# ---------------------------------------------------------------------------


class DependencyMutationDetail(TypedDict):
    """Dependency edge metadata included on issue mutation responses."""

    from_issue_id: str
    to_issue_id: str


class IssueMutationResponse(PublicIssue):
    """Flat post-mutation issue payload with optional action metadata.

    Single-target MCP mutations that act on an issue return the current
    PublicIssue fields at top level, then add narrowly named metadata for the
    operation. The issue's own ``status`` remains the issue workflow status,
    avoiding action-specific overloads like ``status='ok'``.
    """

    changed_fields: NotRequired[list[str]]
    comment_id: NotRequired[int]
    comment: NotRequired[dict[str, Any]]
    label: NotRequired[str]
    label_result: NotRequired[str]
    dependency: NotRequired[DependencyMutationDetail]
    dependency_result: NotRequired[str]
    undone: NotRequired[bool]
    event_type: NotRequired[str]
    event_id: NotRequired[int]
    newly_unblocked: NotRequired[list[SlimIssue]]
    warnings: NotRequired[list[str]]


class DependencyActionResponse(IssueMutationResponse):
    """Response for add_dependency / remove_dependency MCP tools."""


class AddCommentResult(IssueMutationResponse):
    """Response for add_comment MCP tool."""


class LabelActionResponse(IssueMutationResponse):
    """Response for add_label / remove_label MCP tools."""


class CriticalPathMcpNode(TypedDict):
    """Issue node in get_critical_path MCP response."""

    issue_id: str
    title: str
    priority: int
    type: str


class CriticalPathResponse(TypedDict):
    """Response for get_critical_path MCP tool."""

    path: list[CriticalPathMcpNode]
    length: int
    note: NotRequired[str]


class JsonlTransferResponse(TypedDict):
    """Response for export_jsonl / import_jsonl MCP tools."""

    status: str
    records: int
    path: str
    skipped_types: NotRequired[dict[str, int]]


class ArchiveClosedResponse(TypedDict):
    """Response for archive_closed MCP tool."""

    status: str
    archived_count: int
    archived_ids: list[str]


class CompactEventsResponse(TypedDict):
    """Response for compact_events MCP tool."""

    status: str
    events_deleted: int


class ClaimNextEmptyResponse(TypedDict):
    """Response when claim_next finds no matching issues."""

    status: str
    reason: str


# ---------------------------------------------------------------------------
# MCP workflow handler responses
# ---------------------------------------------------------------------------


class WorkflowStatusesResponse(TypedDict):
    """Response for get_workflow_statuses MCP tool (D5: state→status rename)."""

    statuses: dict[str, list[str]]


class EntityIdSchema(TypedDict):
    """One entity ID prefix entry returned by get_schema."""

    entity: str
    prefix: str
    primary_key: str
    example: str
    accepted_by_tools: list[str]


class SchemaResponse(TypedDict):
    """Agent-facing MCP schema/discovery metadata."""

    project_prefix: str
    entity_id_prefixes: dict[str, EntityIdSchema]


class PackListItem(TypedDict):
    """Single pack entry returned by list_packs MCP tool."""

    pack: str
    version: str
    display_name: str
    description: str
    types: list[str]
    requires_packs: list[str]


class ValidationResult(TypedDict):
    """Response for validate_issue MCP tool."""

    valid: bool
    warnings: list[str]
    errors: list[str]


class WorkflowGuideResponse(TypedDict):
    """Response for get_workflow_guide MCP tool.

    ``pack`` and ``guide`` are always present.  ``message`` appears when
    no guide is available; ``note`` appears when the pack was resolved
    from a type name.
    """

    pack: str
    guide: dict[str, Any] | None
    message: NotRequired[str]
    note: NotRequired[str]


class StatusExplanation(TypedDict):
    """Response for explain_status MCP tool (D5: state→status rename).

    The ``status`` key replaces the legacy ``state`` key on this response
    payload — only the surface-level vocabulary changed; the underlying
    workflow data is unchanged.
    """

    status: str
    category: str
    type: str
    inbound_transitions: list[InboundTransitionInfo]
    outbound_transitions: list[OutboundTransitionInfo]
    required_fields: list[str]


# ---------------------------------------------------------------------------
# 2.0 response envelopes
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


class ErrorCode(StrEnum):
    """Closed set of error codes for MCP + dashboard + CLI.

    Callers branch on these values; retry / UX policies depend on them.
    The full legacy-code → ErrorCode mapping lives in
    ``LEGACY_CODE_TO_ERRORCODE`` below — that dict is the single source of
    truth so inline comments here don't drift when new codes are collapsed.
    """

    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    PERMISSION = "PERMISSION"
    NOT_INITIALIZED = "NOT_INITIALIZED"
    IO = "IO"
    INVALID_API_URL = "INVALID_API_URL"
    STOP_FAILED = "STOP_FAILED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    INTERNAL = "INTERNAL"


class BatchFailure(TypedDict):
    """One failed item inside a BatchResponse.failed list.

    ``id`` holds whichever id shape the specific batch tool operates on
    (issue-id, finding-id, observation-id); the consumer disambiguates
    by context. The field was originally named ``item_id`` when this
    type was introduced in Stage 1, but had no live wire consumers at
    that time; it was renamed to ``id`` during Stage 2B task 2b.0 to
    match the wire shape of the retired ``BatchFailureDetail`` and
    avoid a gratuitous wire-contract break.

    ``valid_transitions`` is populated by
    ``db._batch_with_transition_errors`` when the per-item failure is
    an invalid transition; callers can use it to guide the retry.
    """

    id: str
    error: str
    code: ErrorCode
    valid_transitions: NotRequired[list[TransitionHint]]


class BatchResponse(TypedDict, Generic[_T]):
    """Unified response for batch mutation operations.

    Rules:
    - ``succeeded`` is a list of T, where T is the per-op result type
      (e.g. SlimIssue for issue batch ops, FindingRecord for finding
      batch ops).
    - ``failed`` is always present (empty list if no failures).
    - ``newly_unblocked`` is OMITTED entirely when the op cannot unblock
      (not present as ``[]``). Present only for close/transition ops.
    """

    succeeded: list[_T]
    failed: list[BatchFailure]
    newly_unblocked: NotRequired[list[SlimIssue]]


class ListResponse(TypedDict, Generic[_T]):
    """Unified response for list/query operations.

    - ``items`` is always present (empty list for no results).
    - ``has_more`` is always present (never omitted); callers can
      reliably distinguish "no more" from "field absent."
    - ``next_offset`` is present only when ``has_more=True``.
    """

    items: list[_T]
    has_more: bool
    next_offset: NotRequired[int]


# ---------------------------------------------------------------------------
# Response detail (slim/full) opt-in for batch ops
# ---------------------------------------------------------------------------

ResponseDetail = Literal["slim", "full"]


def parse_response_detail(raw: str | None) -> ResponseDetail | ErrorResponse:
    """Parse a ``response_detail`` value to the closed ``ResponseDetail`` literal.

    Returns ``"slim"`` (default when ``raw`` is None or ``"slim"``) or
    ``"full"``. Returns an ``ErrorResponse`` with ``code=VALIDATION`` for
    any other value. Shared by MCP handlers, CLI commands, and the
    dashboard query-param parser so the slim/full vocabulary is enforced
    in one place.
    """
    if raw is None or raw == "slim":
        return "slim"
    if raw == "full":
        return "full"
    return ErrorResponse(
        error=f"Invalid value for response_detail: {raw!r}. Must be 'slim' or 'full'.",
        code=ErrorCode.VALIDATION,
    )


# ---------------------------------------------------------------------------
# 2.0 typed exceptions
# ---------------------------------------------------------------------------


class SchemaVersionMismatchError(ValueError):
    """Raised by ``FiligreeDB.initialize`` when the on-disk schema version is
    newer than the installed filigree's ``CURRENT_SCHEMA_VERSION`` — the binary
    cannot safely downgrade the database.

    Structured carrier for installed/database version numbers — callers can
    read ``.installed`` and ``.database`` directly rather than parsing the
    message string. Inherits from ``ValueError`` so pre-Stage-1 ``except
    ValueError`` catches (including upstream tests) continue to work.
    Stage 5 wires the MCP + dashboard startup boundaries to catch this
    type specifically and surface it as ``ErrorCode.SCHEMA_MISMATCH``.
    """

    def __init__(self, *, installed: int, database: int) -> None:
        self.installed = installed
        self.database = database
        super().__init__(
            f"Database schema v{database} is newer than this version of filigree (expects v{installed}). Downgrade is not supported."
        )


class ClaimConflictError(ValueError):
    """Raised when an optimistic-lock CAS check on a claim-aware write fails.

    Carries the failing issue's id and the expected/observed assignee pair so
    callers can render a structured conflict envelope. Subclasses
    ``ValueError`` so pre-typed-exception callers (``except ValueError``)
    continue to work; the dashboard / MCP / CLI surfaces route this class
    to ``ErrorCode.CONFLICT`` via ``isinstance`` rather than message-text
    matching (2.1.0 §0.3).
    """

    def __init__(self, issue_id: str, *, observed: str, expected: str, message: str | None = None) -> None:
        self.issue_id = issue_id
        self.observed = observed
        self.expected = expected
        super().__init__(message or f"Cannot operate on {issue_id}: assigned to '{observed}' (expected '{expected}')")


class AmbiguousTransitionError(Exception):
    """Raised when start-work cannot choose between multiple wip-category targets.

    Carries the ambiguous type_name plus the full list of candidate
    target states so callers can render a disambiguation prompt. Stage 3
    will wire this into the ``start_work`` path; the type is defined now
    so the mapping is stable when the raise sites land.
    """

    def __init__(self, type_name: str, candidates: list[str], current_status: str | None = None) -> None:
        self.type_name = type_name
        self.candidates = candidates
        self.current_status = current_status
        scope = f" from {current_status!r}" if current_status is not None else ""
        super().__init__(
            f"start_work ambiguous for type {type_name!r}{scope}: "
            f"multiple wip-category targets available ({candidates}). "
            f"Specify target_status explicitly."
        )


class InvalidTransitionError(ValueError):
    """Raised by WorkflowPack.canonical_working_status when no wip-category
    target is reachable from the current status.

    Carries type_name + current_status so callers can show *why* a workflow
    transition is unavailable. Subclasses ValueError so existing state-machine
    error handlers continue to classify it as INVALID_TRANSITION.
    """

    def __init__(
        self,
        type_name: str,
        current_status: str,
        *,
        to_state: str | None = None,
        backward: bool = False,
        valid_transitions: list[TransitionHint] | None = None,
        message: str | None = None,
    ) -> None:
        self.type_name = type_name
        self.current_status = current_status
        self.to_state = to_state
        self.backward = backward
        self.valid_transitions = valid_transitions
        if message is not None:
            super().__init__(message)
            return
        if to_state is None:
            message = f"No wip-category transition from {current_status!r} for type {type_name!r}."
        elif backward:
            message = f"Reverse transition {current_status!r} -> {to_state!r} is not declared for type {type_name!r}."
        else:
            message = f"Transition {current_status!r} -> {to_state!r} is not declared for type {type_name!r}."
        super().__init__(message)


def errorcode_to_http_status(code: ErrorCode) -> int:
    """Map an ErrorCode to the HTTP status the dashboard should return.

    Exhaustive match: adding an ErrorCode member without extending this
    function fails mypy via ``assert_never``. Dashboard routes are free
    to choose a more specific status (e.g. 409 for a particular conflict)
    but should never invent a status that disagrees with this default.
    """
    match code:
        case ErrorCode.VALIDATION | ErrorCode.INVALID_API_URL:
            return 400
        case ErrorCode.PERMISSION:
            return 403
        case ErrorCode.NOT_FOUND:
            return 404
        case ErrorCode.CONFLICT | ErrorCode.INVALID_TRANSITION:
            return 409
        case ErrorCode.NOT_INITIALIZED | ErrorCode.SCHEMA_MISMATCH:
            # Service exists but is not in a state where it can answer —
            # 503 lets clients retry once the project is initialized or
            # the schema is migrated.
            return 503
        case ErrorCode.IO | ErrorCode.STOP_FAILED | ErrorCode.INTERNAL:
            return 500
        case _:
            assert_never(code)


def classify_value_error(message: str) -> ErrorCode:
    """Classify a ``ValueError`` message as INVALID_TRANSITION or VALIDATION.

    Substring heuristic on the exception message: presence of ``status``,
    ``transition``, or ``state`` means the core rejected a status-machine
    transition (409 on the wire); anything else is generic input validation
    (400). Used identically by the MCP, dashboard, and CLI surfaces so the
    same input produces the same code everywhere.

    This heuristic is a bridge until Stage 3 introduces typed raise sites
    (``InvalidTransitionError``, ``AmbiguousTransitionError``) in the data
    layer; once those land, ValueError handlers will catch the typed
    subclass explicitly and this function retires.

    Boundary rule (documented in 2B rebaseline §Task 2b.4):

    - **State-machine sites MUST use this helper.** Currently:
      ``db._batch_with_transition_errors`` (db_issues.py:894),
      ``_handle_update_issue`` / ``_handle_close_issue`` /
      ``_handle_reopen_issue`` (mcp_tools/issues.py), CLI ``update`` /
      ``close`` / ``reopen`` ``--json`` paths (cli_commands/issues.py),
      dashboard PATCH / close / reopen routes
      (dashboard_routes/issues.py). These wrap a ``db.*`` method that
      raises ``ValueError`` for multiple classes of issue: transition
      failure, already-closed, dependency conflict, or validation. The
      helper routes correctly across these.

    - **Input-validation sites MUST hardcode VALIDATION.** Currently:
      ``_safe_path`` / URL parsing (dashboard_routes/common.py), schema
      validation in ``mcp_tools/meta.py``, ``mcp_tools/files.py``,
      ``mcp_tools/scanners.py``, ``mcp_tools/observations.py``. These
      catch ``ValueError`` for one class only (bad input from the
      caller); using the helper would mis-classify future error-message
      additions containing the keyword "status" or "state" (e.g. a
      scanner complaining about a bad "status filter" arg would land
      as INVALID_TRANSITION, which is wrong).

    The enforcement test at ``tests/util/test_classify_value_error_boundary.py``
    greps the input-validation modules listed above for
    ``classify_value_error`` imports and fails if any appear — so
    breaking this rule in a future change trips CI.
    """
    lowered = message.lower()
    if "status" in lowered or "transition" in lowered or "state" in lowered:
        return ErrorCode.INVALID_TRANSITION
    return ErrorCode.VALIDATION


# Mapping used only during Stage 2a rollout for developer reference.
# After Stage 2a lands, this dict stays as documentation.
LEGACY_CODE_TO_ERRORCODE: dict[str, ErrorCode] = {
    "invalid": ErrorCode.VALIDATION,
    "validation_error": ErrorCode.VALIDATION,
    "invalid_path": ErrorCode.VALIDATION,
    "invalid_command": ErrorCode.VALIDATION,
    "not_found": ErrorCode.NOT_FOUND,
    "scanner_not_found": ErrorCode.NOT_FOUND,
    "unknown_tool": ErrorCode.NOT_FOUND,
    "command_not_found": ErrorCode.NOT_FOUND,
    "conflict": ErrorCode.CONFLICT,
    "invalid_transition": ErrorCode.INVALID_TRANSITION,
    "permission_error": ErrorCode.PERMISSION,
    "not_initialized": ErrorCode.NOT_INITIALIZED,
    "io_error": ErrorCode.IO,
    "db_error": ErrorCode.IO,
    "database_error": ErrorCode.IO,
    "import_error": ErrorCode.IO,
    "batch_all_failed": ErrorCode.VALIDATION,  # rare; used in scanner batch path
    "invalid_api_url": ErrorCode.INVALID_API_URL,
    "stop_failed": ErrorCode.STOP_FAILED,
    # --- Added during Stage 2a sweep — legacy codes discovered beyond the
    # --- original 19-code mapping. Mapping decisions documented in
    # --- docs/plans/2026-04-18-2.0-unified-surface-plan.md (Stage 2a fix-up).
    "ingestion_error": ErrorCode.IO,
    "file_not_found": ErrorCode.NOT_FOUND,
    "promotion_error": ErrorCode.VALIDATION,
    "spawn_failed": ErrorCode.IO,
    # rate_limited is a cooldown conflict, not an IO failure — clients
    # should retry when the blocking run completes (check details.blocking_run_id).
    "rate_limited": ErrorCode.CONFLICT,
    "no_eligible_files": ErrorCode.VALIDATION,
    "project_not_found": ErrorCode.NOT_FOUND,
    "project_unavailable": ErrorCode.NOT_INITIALIZED,
}
