"""Loom-generation response types.

Per ADR-002 §6, loom wraps internal results in the unified envelopes
defined in ``filigree.types.api``. Endpoint-specific response types that
extend those envelopes (e.g. scan-results, which adds a counts sibling
and top-level warnings on top of the batch wrapper) are declared here.

Kept intentionally small: each TypedDict maps 1:1 to a fixture in
``tests/fixtures/contracts/loom/`` and is the single source of truth for
that endpoint's loom wire shape. If a type has no fixture companion, it
does not belong here.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from filigree.types.api import BatchFailure
from filigree.types.core import (
    AssocType,
    FindingStatus,
    ISOTimestamp,
    Severity,
    StatusCategory,
)
from filigree.types.events import EventType
from filigree.types.files import FindingsSummary


class SlimIssueLoom(TypedDict):
    """Slim issue projection for the loom generation.

    Key difference from ``filigree.types.api.SlimIssue``: ``issue_id``
    replaces ``id`` per the Phase D vocabulary shift. Populated by the
    loom issue adapters in Phase C3 and later.
    """

    issue_id: str
    title: str
    status: str
    priority: int
    type: str


class ScanStats(TypedDict):
    """Per-ingest counts surfaced alongside succeeded / failed.

    Mirrors the integer counters in ``filigree.types.files.ScanIngestResult``
    except ``new_finding_ids`` (moved up to ``ScanIngestResponseLoom.succeeded``)
    and ``warnings`` (moved up to ``ScanIngestResponseLoom.warnings``).
    """

    files_created: int
    files_updated: int
    findings_created: int
    findings_updated: int
    observations_created: int
    observations_failed: int


class BatchCloseResponseLoom(TypedDict):
    """Response shape for ``POST /api/loom/batch/close``.

    ``succeeded`` carries ``SlimIssueLoom`` by default and ``IssueLoom``
    when the request sets ``response_detail=full`` — the union covers
    both projections the handler may emit (see C5 in
    ``docs/federation/contracts.md``). ``newly_unblocked`` stays
    ``SlimIssueLoom`` regardless of ``response_detail`` per the locked
    C5 rule, and uses the loom vocabulary (``issue_id``) like every
    other loom-shaped issue.

    Pinned by ``tests/fixtures/contracts/loom/batch-close.json`` and
    the contract test in ``tests/api/test_envelope_types.py``.
    """

    succeeded: list[SlimIssueLoom | IssueLoom]
    failed: list[BatchFailure]
    newly_unblocked: NotRequired[list[SlimIssueLoom]]


class IssueLoom(TypedDict):
    """Loom-vocab issue projection.

    Same field shape as ``IssueDict`` (``filigree.types.core``) except
    the issue's own primary key is named ``issue_id`` rather than
    ``id``. Reference fields that hold *other* issues' ids
    (``parent_id``, ``blocks``, ``blocked_by``, ``children``) keep
    their existing names — only the entity's own primary key is
    renamed per the loom vocabulary.

    ``data_warnings`` contains non-fatal response warnings, including
    soft-transition advisories returned by mutation endpoints.

    Used as the canonical response shape for every single-issue loom
    endpoint in Phase C3 (GET, PATCH, close, reopen, claim, release,
    claim-next, create). Endpoints with optional enrichment use the
    ``WithFiles`` / ``WithUnblocked`` subtypes below; otherwise
    consumers see exactly this 20-field projection.

    Pinned by ``tests/fixtures/contracts/loom/issues-*.json``.
    """

    issue_id: str
    title: str
    status: str
    status_category: StatusCategory
    priority: int
    type: str
    parent_id: str | None
    assignee: str
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


class IssueLoomWithFiles(IssueLoom):
    """``IssueLoom`` + ``files``: the response shape returned when
    ``GET /api/loom/issues/{issue_id}?include_files=true``.

    The ``files`` payload mirrors what ``db.get_issue_files()`` returns —
    a list of file-association rows. Loom does not yet declare a
    dedicated ``FileAssocLoom`` TypedDict; phase D may tighten this
    when the file surface is loom-ified.
    """

    files: list[dict[str, Any]]


class IssueLoomWithUnblocked(IssueLoom):
    """``IssueLoom`` + ``newly_unblocked``: returned by close-issue
    when at least one issue became ready as a result. Mirrors MCP's
    ``IssueWithUnblocked`` semantics but uses ``SlimIssueLoom``
    rather than the classic ``SlimIssue``.
    """

    newly_unblocked: list[SlimIssueLoom]


class CommentRecordLoom(TypedDict):
    """Comment row in the loom vocabulary.

    Classic ``CommentRecord`` uses ``id`` for the comment's own primary
    key; loom renames it to ``comment_id`` for the same reason
    ``SlimIssue.id`` becomes ``SlimIssueLoom.issue_id`` — entity
    primary keys are typed by entity name.
    """

    comment_id: int
    author: str
    text: str
    created_at: ISOTimestamp


class BlockedIssueLoom(SlimIssueLoom):
    """SlimIssueLoom + ``blocked_by`` for ``GET /api/loom/blocked``.

    Mirrors classic ``BlockedIssue`` (which extends ``SlimIssue``) but uses
    the loom ``issue_id`` vocabulary for the entity primary key. Reference
    ids in ``blocked_by`` keep their existing names per the loom-vocabulary
    scope (only the entity's own primary key is renamed).

    Pinned by ``tests/fixtures/contracts/loom/blocked.json``.
    """

    blocked_by: list[str]


class FileRecordLoom(TypedDict):
    """File record with summary counts — items in ``GET /api/loom/files``.

    Mirrors classic ``EnrichedFileItem`` (FileRecordDict + summary +
    associations_count + observation_count) except the file's own primary
    key is renamed ``id`` → ``file_id``. Defined independently rather than
    extending ``FileRecordDict`` because TypedDict inheritance cannot drop
    the inherited ``id`` key — the loom wire shape must contain
    ``file_id`` exclusively.

    Pinned by ``tests/fixtures/contracts/loom/files.json``.
    """

    file_id: str
    path: str
    language: str
    file_type: str
    first_seen: ISOTimestamp
    updated_at: ISOTimestamp
    metadata: dict[str, Any]
    data_warnings: list[str]
    summary: FindingsSummary
    associations_count: int
    observation_count: int


class FileAssocLoom(TypedDict):
    """Issue-to-file association row — items in ``GET /api/loom/issues/{issue_id}/files``.

    Mirrors classic ``IssueFileAssociation`` except the association row's
    own primary key is renamed ``id`` → ``assoc_id``. The cross-entity
    references (``file_id``, ``issue_id``) keep their existing names per
    the loom-vocabulary scope.

    Pinned by ``tests/fixtures/contracts/loom/issue-files.json``.
    """

    assoc_id: int
    file_id: str
    issue_id: str
    assoc_type: AssocType
    created_at: ISOTimestamp
    file_path: str
    file_language: str | None


class ScanFindingLoom(TypedDict):
    """Scan finding row — items in ``GET /api/loom/findings`` and the
    embedded findings list inside file/finding details.

    Mirrors classic ``ScanFindingDict`` except the finding's own primary
    key is renamed ``id`` → ``finding_id``. Cross-entity refs (``file_id``,
    ``issue_id``) keep their existing names.

    Pinned by ``tests/fixtures/contracts/loom/findings.json``.
    """

    finding_id: str
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
    issue_id: str | None
    seen_count: int
    first_seen: ISOTimestamp
    updated_at: ISOTimestamp
    last_seen_at: ISOTimestamp | None
    metadata: dict[str, Any]
    data_warnings: list[str]


class ObservationLoom(TypedDict):
    """Observation row — items in ``GET /api/loom/observations``.

    Mirrors classic ``ObservationDict`` except the observation's own
    primary key is renamed ``id`` → ``observation_id``. Cross-entity refs
    (``file_id``, ``source_issue_id``) keep their existing names.

    Pinned by ``tests/fixtures/contracts/loom/observations.json``.
    """

    observation_id: str
    summary: str
    detail: str
    file_id: str | None
    file_path: str
    line: int | None
    source_issue_id: str
    priority: int
    actor: str
    created_at: ISOTimestamp
    expires_at: ISOTimestamp


class ScannerLoom(TypedDict):
    """Scanner registration entry — items in ``GET /api/loom/scanners``.

    Mirrors the dict produced by ``ScannerConfig.to_dict()``. ``name`` is
    the scanner's primary key but is already a string-name (not a uuid),
    so no rename is needed for the loom vocabulary.

    Pinned by ``tests/fixtures/contracts/loom/scanners.json``.
    """

    name: str
    description: str
    file_types: list[str]


class PackLoom(TypedDict):
    """Workflow pack entry — items in ``GET /api/loom/packs``.

    Mirrors MCP's ``PackListItem``. ``pack`` is the entity's logical
    primary key; renaming it would harm readability (federation consumers
    branching on pack name still want to see ``pack``), so it is kept.
    Defined here rather than reused from ``filigree.types.api`` so the
    loom surface owns its wire shape independently.

    Pinned by ``tests/fixtures/contracts/loom/packs.json``.
    """

    pack: str
    version: str
    display_name: str
    description: str
    types: list[str]
    requires_packs: list[str]


class TypeSummaryLoom(TypedDict):
    """Issue-type summary entry — items in ``GET /api/loom/types``.

    Mirrors the classic ``/api/types`` shape. ``type`` is the entity's
    logical primary key (a string name like ``task``); no rename per the
    loom scope.

    Pinned by ``tests/fixtures/contracts/loom/types.json``.
    """

    type: str
    display_name: str
    pack: str
    initial_state: str


class IssueEventLoom(TypedDict):
    """Event row for ``GET /api/loom/issues/{issue_id}/events``.

    Mirrors classic ``EventRecord`` except the event's own primary key
    is renamed ``id`` → ``event_id``. ``issue_id`` is a cross-entity
    reference and keeps its name.

    Pinned by ``tests/fixtures/contracts/loom/issue-events.json``.
    """

    event_id: int
    issue_id: str
    event_type: EventType
    actor: str
    old_value: str | None
    new_value: str | None
    comment: str
    created_at: ISOTimestamp


class ChangeRecordLoom(IssueEventLoom):
    """Cross-issue event row for ``GET /api/loom/changes``.

    Extends ``IssueEventLoom`` with the joined ``issue_title`` column —
    matches the structural difference between ``EventRecord`` and
    ``EventRecordWithTitle`` in ``filigree.types.events``.

    Pinned by ``tests/fixtures/contracts/loom/changes.json``.
    """

    issue_title: str


class ScanIngestResponseLoom(TypedDict):
    """Response shape for ``POST /api/loom/scan-results``.

    ``succeeded`` contains server-generated finding ids for newly-created
    findings (classic called this ``new_finding_ids``). ``failed`` is
    always present as an empty list in 2.0; populated once per-finding
    ingest failure tracking lands (non-breaking addition). ``stats`` and
    ``warnings`` are loom-specific additions on top of the batch
    envelope.

    Declared as a concrete ``TypedDict`` rather than subclassing
    ``BatchResponse[str]``: at runtime, TypedDict + ``Generic`` does not
    preserve the ``str`` substitution (``succeeded`` would resolve to
    ``list[~_T]``), and parent ``NotRequired`` markers are stripped on a
    ``total=True`` subclass, which would falsely make ``newly_unblocked``
    a required key. Scan ingestion never unblocks issues, so
    ``newly_unblocked`` is omitted entirely.

    Pinned by ``tests/fixtures/contracts/loom/scan-results.json`` and the
    contract test in ``tests/api/test_envelope_types.py``.
    """

    succeeded: list[str]
    failed: list[BatchFailure]
    stats: ScanStats
    warnings: list[str]
