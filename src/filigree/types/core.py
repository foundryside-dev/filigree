"""Foundational TypedDicts and Literal types for dataclass to_dict() returns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Literal, NewType, NotRequired, TypedDict, TypeVar

if TYPE_CHECKING:
    from filigree.models import Issue

ISOTimestamp = NewType("ISOTimestamp", str)
IssueId = NewType("IssueId", str)
FileId = NewType("FileId", str)
EntityId = NewType("EntityId", str)
ClarionEntityId = EntityId
ContentHash = NewType("ContentHash", str)

_MAX_CONTENT_HASH_LEN = 512


def make_issue_id(value: str) -> IssueId:
    """Validate and brand an issue id crossing an untyped boundary."""
    if not isinstance(value, str) or not value.strip():
        msg = "issue_id must not be blank"
        raise ValueError(msg)
    return IssueId(value)


def make_file_id(value: str) -> FileId:
    """Validate and brand a Filigree-local file id crossing an untyped boundary."""
    if not isinstance(value, str) or not value.strip():
        msg = "file_id must not be blank"
        raise ValueError(msg)
    return FileId(value)


def make_entity_id(value: str) -> EntityId:
    """Validate and brand an opaque federated entity id.

    Filigree deliberately does not parse Clarion's entity-id grammar; this only
    rejects empty values at the local boundary.
    """
    if not isinstance(value, str) or not value.strip():
        msg = "entity_id must not be blank"
        raise ValueError(msg)
    return EntityId(value)


def make_clarion_entity_id(value: str) -> ClarionEntityId:
    """Backward-compatible alias for Clarion-specific entity branding."""
    return make_entity_id(value)


def make_content_hash(value: str) -> ContentHash:
    """Validate and brand a content-hash token.

    The algorithm and length remain the producer's contract. Filigree only
    rejects values that are unusable as stable comparison tokens: blank,
    padded, whitespace/control-bearing, or implausibly large strings.
    """
    if not isinstance(value, str) or not value.strip():
        msg = "content_hash must not be blank"
        raise ValueError(msg)
    if value != value.strip():
        msg = "content_hash must not contain leading or trailing whitespace"
        raise ValueError(msg)
    if len(value) > _MAX_CONTENT_HASH_LEN:
        msg = f"content_hash must be at most {_MAX_CONTENT_HASH_LEN} characters"
        raise ValueError(msg)
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        msg = "content_hash must not contain whitespace or control characters"
        raise ValueError(msg)
    return ContentHash(value)


# Constrained-string Literal types — canonical definitions.
# core.py re-exports these; db_files.py derives frozensets via get_args().
Severity = Literal["critical", "high", "medium", "low", "info"]
FindingStatus = Literal["open", "acknowledged", "fixed", "false_positive", "unseen_in_latest"]
AssocType = Literal["bug_in", "task_for", "scan_finding", "mentioned_in"]
StatusCategory = Literal["open", "wip", "done"]
ScanRunStatus = Literal["pending", "running", "completed", "failed", "timeout"]
AnnotationIntent = Literal["explanation", "warning", "breadcrumb", "hypothesis", "decision", "handoff", "gotcha"]
AnnotationStatus = Literal["active", "resolved", "superseded", "promoted"]
AnnotationTargetType = Literal["issue", "file", "finding", "observation"]
AnnotationRelationship = Literal["relevant_to", "must_consider", "evidence_for", "explains", "created_from", "promoted_to"]
AnnotationAnchorState = Literal["current", "line_drifted", "content_changed_anchor_found", "stale", "file_missing"]
AnnotationProvenanceTrustLevel = Literal["complete", "partial", "minimal"]
RegistryBackend = Literal["local", "clarion"]


class _ProjectConfigRequired(TypedDict):
    """Required keys for ProjectConfig (needed for ID generation and migrations)."""

    prefix: str
    version: int


class ClarionConfig(TypedDict, total=False):
    """ADR-014 Clarion registry backend configuration.

    ``token_env`` names the environment variable that carries the Bearer
    token the Clarion read API expects (Authorization header). Defaults to
    ``"CLARION_LOOM_TOKEN"``. Per the Clarion 1.0 cross-product contract,
    Clarion accepts unauthenticated calls on loopback bind and rejects them
    on non-loopback; if the env var is unset, Filigree sends no header.
    """

    base_url: str
    timeout_seconds: int | float
    allow_local_fallback: bool
    token_env: str


class ProjectConfig(_ProjectConfigRequired, total=False):
    """Shape of .filigree/config.json.

    ``prefix`` (issue ID generation) and ``version`` (schema migrations) are
    always required.  Other keys are optional.
    """

    name: str
    enabled_packs: list[str]
    mode: str
    registry_backend: RegistryBackend
    clarion: ClarionConfig


_T = TypeVar("_T")


class PaginatedResult(TypedDict, Generic[_T]):
    """Envelope returned by paginated query methods.

    Generic over the item type: ``PaginatedResult[FileRecordDict]`` etc.
    Un-parameterised ``PaginatedResult`` is equivalent to ``PaginatedResult[dict[str, Any]]``
    for backward compatibility.
    """

    results: list[_T]
    total: int
    limit: int
    offset: int
    has_more: bool


class IssueDict(TypedDict):
    """Shape of Issue.to_dict() return value.

    ``data_warnings`` contains non-fatal response warnings such as transient
    soft-transition advisories and parse/corruption warnings.
    """

    id: str
    title: str
    status: str
    status_category: StatusCategory
    priority: int
    type: str
    parent_id: str | None
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


class FileRecordDict(TypedDict):
    """Shape of FileRecord.to_dict() return value."""

    id: str
    path: str
    language: str
    file_type: str
    content_hash: str
    registry_backend: RegistryBackend
    created_by: str
    updated_by: str
    first_seen: ISOTimestamp
    updated_at: ISOTimestamp
    metadata: dict[str, Any]
    data_warnings: list[str]


class ScanFindingDict(TypedDict):
    """Shape of ScanFinding.to_dict() return value."""

    id: str
    file_id: str
    severity: Severity
    status: FindingStatus
    scan_source: str
    rule_id: str
    message: str
    suggestion: str
    scan_run_id: str
    line_start: int | None
    line_end: int | None
    fingerprint: str
    issue_id: str | None
    seen_count: int
    created_by: str
    updated_by: str
    first_seen: ISOTimestamp
    updated_at: ISOTimestamp
    last_seen_at: ISOTimestamp | None
    metadata: dict[str, Any]
    data_warnings: list[str]


class ObservationDict(TypedDict):
    """Shape contract for observation dict representations."""

    id: str
    summary: str
    detail: str
    file_id: str | None
    file_path: str
    line: int | None
    source_issue_id: str
    source_finding_id: str
    priority: int
    actor: str
    created_at: ISOTimestamp
    expires_at: ISOTimestamp
    # v23->v24 (ADR-012): nullable transport-bound actor identity. Always present
    # — the SELECT * read paths surface it via dict(row) and the manual
    # create-return literal stamps self._verified_actor (None when unverified).
    verified_actor: str | None


class ObservationLinkDict(TypedDict):
    """Durable snapshot for an observation linked into issue triage."""

    id: int
    obs_id: str
    observation_id: NotRequired[str]
    issue_id: str
    disposition: str
    summary: str
    detail: str
    file_id: str | None
    file_path: str
    line: int | None
    source_issue_id: str
    source_finding_id: str
    priority: int
    observation_actor: str
    actor: str
    reason: str
    linked_at: ISOTimestamp


class BatchDismissResult(TypedDict):
    """Shape contract for batch_dismiss_observations() return value."""

    dismissed: int
    not_found: list[str]


class PromoteObservationResult(TypedDict):
    """Shape contract for promote_observation() return value.

    Note: ``issue`` is an Issue dataclass (not IssueDict) because this is an
    internal return type. The MCP layer calls ``issue.to_dict()`` before
    serializing to the wire format.
    """

    issue: Issue
    warnings: NotRequired[list[str]]


class ObservationStatsDict(TypedDict):
    """Shape contract for observation_stats() return value."""

    count: int
    stale_count: int
    oldest_hours: float | None
    expiring_soon_count: int
    sweep_consecutive_failures: int
    last_successful_sweep_at: ISOTimestamp | None
