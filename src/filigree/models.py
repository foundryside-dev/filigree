"""Pure data models — Issue, FileRecord, ScanFinding.

These dataclasses represent database rows as typed Python objects. They depend
only on ``filigree.types.core`` (TypedDicts and Literal types), so any module
in the package can import them without circular-dependency risk.

Extracted from ``core.py`` to break the cycle.  Import flow:
    types/core.py  -->  models.py  -->  db_base.py / core.py / db_*.py mixins
(arrows show the key dependency chain that motivated the extraction)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, get_args

from filigree.types.core import (
    FileRecordDict,
    FindingStatus,
    ISOTimestamp,
    IssueDict,
    RegistryBackend,
    ScanFindingDict,
    Severity,
    StatusCategory,
)

_EMPTY_TS: ISOTimestamp = ISOTimestamp("")

# Derive valid sets from Literal types (avoids importing from db_files)
_VALID_STATUS_CATEGORIES: frozenset[str] = frozenset(get_args(StatusCategory))
_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))
_VALID_FINDING_STATUSES: frozenset[str] = frozenset(get_args(FindingStatus))
_VALID_REGISTRY_BACKENDS: frozenset[str] = frozenset(get_args(RegistryBackend))


@dataclass
class Issue:
    id: str
    title: str
    # status and type remain str (not Literal) because valid values are
    # defined by workflow templates at runtime and cannot be statically enumerated.
    status: str = "open"
    priority: int = 2
    type: str = "task"
    parent_id: str | None = None
    assignee: str = ""
    claimed_at: ISOTimestamp | None = None
    last_heartbeat_at: ISOTimestamp | None = None
    claim_expires_at: ISOTimestamp | None = None
    created_at: ISOTimestamp = _EMPTY_TS
    updated_at: ISOTimestamp = _EMPTY_TS
    closed_at: ISOTimestamp | None = None
    # Opaque ``branch@sha`` commit anchors (warpline seam, contract B). Set at
    # claim / close from a caller-supplied value, stored verbatim, never parsed.
    claim_commit: str | None = None
    close_commit: str | None = None
    description: str = ""
    notes: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    # Computed (not stored directly)
    labels: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    is_ready: bool = False
    children: list[str] = field(default_factory=list)
    status_category: StatusCategory = "open"
    data_warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status_category not in _VALID_STATUS_CATEGORIES:
            raise ValueError(f"Invalid status_category {self.status_category!r}, expected one of {sorted(_VALID_STATUS_CATEGORIES)}")
        # bool is an int subclass, so a bare isinstance check would let
        # True/False through and they would serialize as priority:true/false
        # into agent-facing JSON. Exclude bool explicitly.
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not (0 <= self.priority <= 4):
            raise ValueError(f"Invalid priority {self.priority!r}, expected int 0-4")

    def to_dict(self) -> IssueDict:
        # filigree-7ea6b80f3b: corruption is an out-of-band attribute on the
        # _ParsedJson dict subclass returned by _safe_json_loads, duck-typed
        # here to avoid a db_base→models import cycle. A user-supplied field
        # literally named ``_fields_error`` no longer triggers a false strip.
        fields = self.fields
        warnings = list(self.data_warnings)
        if getattr(fields, "_filigree_corrupt", False):
            fields = {}
            warnings.append("fields data was corrupt and could not be parsed")
        return IssueDict(
            id=self.id,
            title=self.title,
            status=self.status,
            status_category=self.status_category,
            priority=self.priority,
            type=self.type,
            parent_id=self.parent_id,
            assignee=self.assignee,
            claimed_at=self.claimed_at,
            last_heartbeat_at=self.last_heartbeat_at,
            claim_expires_at=self.claim_expires_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
            closed_at=self.closed_at,
            claim_commit=self.claim_commit,
            close_commit=self.close_commit,
            description=self.description,
            notes=self.notes,
            fields=fields,
            labels=self.labels,
            blocks=self.blocks,
            blocked_by=self.blocked_by,
            is_ready=self.is_ready,
            children=self.children,
            data_warnings=warnings,
        )

    def format_claim_next_reason(self) -> str:
        """Build the ``selection_reason`` string for a successful claim_next.

        Shared between the CLI command, MCP handler, and any HTTP surface so the
        wire shape is identical across surfaces (Phase E §9 envelope parity).
        """
        parts = [f"P{self.priority}"]
        if self.type != "task":
            parts.append(f"type={self.type}")
        parts.append("ready issue (no blockers)")
        return f"Highest-priority {', '.join(parts)}"


@dataclass
class FileRecord:
    """Stored file identity and registry metadata.

    ``content_hash == ''`` is the intentional sentinel for
    ``registry_backend == 'local'`` because the local backend cannot compute a
    drift hash. Loomweave-backed records must carry a non-empty hash. This
    correlated invariant is enforced at construction by ``__post_init__`` — the
    two illegal cross combinations raise ``ValueError`` — so it holds on every
    hydration path, not only where the Loomweave registry client rejects blank
    hashes before rows are written.
    """

    id: str
    path: str
    language: str = ""
    file_type: str = ""
    content_hash: str = ""
    registry_backend: RegistryBackend = "local"
    created_by: str = ""
    updated_by: str = ""
    first_seen: ISOTimestamp = _EMPTY_TS
    updated_at: ISOTimestamp = _EMPTY_TS
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The (registry_backend, content_hash) pair is a correlated invariant,
        # not two independent fields: ``local`` files carry the empty-hash
        # sentinel (the local backend cannot compute a drift hash) and
        # ``loomweave`` files must carry a non-empty hash. Reject the two illegal
        # cross combinations at construction — mirrors ScanFinding's enum guard
        # and closes the type-level hole the flat dataclass otherwise allows.
        if self.registry_backend not in _VALID_REGISTRY_BACKENDS:
            raise ValueError(f"Invalid registry_backend: {self.registry_backend!r}")
        is_local = self.registry_backend == "local"
        has_empty_hash = self.content_hash == ""
        if is_local != has_empty_hash:
            raise ValueError(
                f"Invalid file identity: registry_backend={self.registry_backend!r} "
                f"requires {'an empty' if is_local else 'a non-empty'} content_hash, "
                f"got content_hash={self.content_hash!r}"
            )

    def to_dict(self) -> FileRecordDict:
        # filigree-7ea6b80f3b: out-of-band corruption flag (see Issue.to_dict).
        metadata = self.metadata
        warnings: list[str] = []
        if getattr(metadata, "_filigree_corrupt", False):
            metadata = {}
            warnings.append("metadata was corrupt and could not be parsed")
        return FileRecordDict(
            id=self.id,
            path=self.path,
            language=self.language,
            file_type=self.file_type,
            content_hash=self.content_hash,
            registry_backend=self.registry_backend,
            created_by=self.created_by,
            updated_by=self.updated_by,
            first_seen=self.first_seen,
            updated_at=self.updated_at,
            metadata=metadata,
            data_warnings=warnings,
        )


@dataclass
class ScanFinding:
    id: str
    file_id: str
    severity: Severity = "info"
    status: FindingStatus = "open"
    scan_source: str = ""
    rule_id: str = ""
    message: str = ""
    suggestion: str = ""
    scan_run_id: str = ""
    line_start: int | None = None
    line_end: int | None = None
    fingerprint: str = ""
    issue_id: str | None = None
    seen_count: int = 1
    created_by: str = ""
    updated_by: str = ""
    first_seen: ISOTimestamp = _EMPTY_TS
    updated_at: ISOTimestamp = _EMPTY_TS
    last_seen_at: ISOTimestamp | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # N6 (weft-c815d5e77d): the linked issue's status and resolution, surfaced
    # via a LEFT JOIN in the finding read paths so a finding pointing at a
    # dismissed (``not_a_bug``) issue reads as triaged, not open work. Both are
    # ``None`` when the finding is unlinked, when the linked issue row is missing
    # (LEFT JOIN miss), or when the read path did not join (bare ``SELECT *``).
    issue_status: str | None = None
    issue_resolution: str | None = None
    # Wardline's suppression verdict, lifted out of
    # ``metadata.wardline.suppression_state`` (``"baselined"`` | ``"waived"`` |
    # ``"judged"`` | …) onto the read surface so an agent triaging via
    # ``finding_list`` / the weft findings list can tell an accepted/suppressed
    # defect from open work without parsing nested metadata. ``None`` when the
    # finding carries no wardline suppression. Mirrors the N6 issue_status lift;
    # populated from metadata in ``FileDBMixin._build_scan_finding``.
    suppression_state: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(f"Invalid severity {self.severity!r}, expected one of {sorted(_VALID_SEVERITIES)}")
        if self.status not in _VALID_FINDING_STATUSES:
            raise ValueError(f"Invalid finding status {self.status!r}, expected one of {sorted(_VALID_FINDING_STATUSES)}")

    def to_dict(self) -> ScanFindingDict:
        # filigree-7ea6b80f3b: out-of-band corruption flag (see Issue.to_dict).
        metadata = self.metadata
        warnings: list[str] = []
        if getattr(metadata, "_filigree_corrupt", False):
            metadata = {}
            warnings.append("metadata was corrupt and could not be parsed")
        return ScanFindingDict(
            id=self.id,
            file_id=self.file_id,
            severity=self.severity,
            status=self.status,
            scan_source=self.scan_source,
            rule_id=self.rule_id,
            message=self.message,
            suggestion=self.suggestion,
            scan_run_id=self.scan_run_id,
            line_start=self.line_start,
            line_end=self.line_end,
            fingerprint=self.fingerprint,
            issue_id=self.issue_id,
            seen_count=self.seen_count,
            created_by=self.created_by,
            updated_by=self.updated_by,
            first_seen=self.first_seen,
            updated_at=self.updated_at,
            last_seen_at=self.last_seen_at,
            issue_status=self.issue_status,
            issue_resolution=self.issue_resolution,
            suppression_state=self.suppression_state,
            metadata=metadata,
            data_warnings=warnings,
        )
