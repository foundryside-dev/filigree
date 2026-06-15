"""Weft-generation shape adapters.

Adapters take internal domain results (from the ``filigree.db_*`` core
layer) and produce the weft-generation wire shape. Per ADR-002 §6,
adapters are **thin, data-only transformations**: they rename keys, move
fields between nesting levels, and wrap results in the unified
envelopes. They do not contain business logic.

If a weft handler would need to branch on generation state (``if
generation == "weft": ...``), the branch belongs in the handler layer,
not here.
"""

from __future__ import annotations

from typing import Any

from filigree.generations.weft.types import (
    BlockedIssueWeft,
    ChangeRecordWeft,
    CommentRecordWeft,
    FileAssocWeft,
    FileRecordWeft,
    IssueEventWeft,
    IssueWeft,
    ObservationWeft,
    PackWeft,
    ScanFindingWeft,
    ScanIngestResponseWeft,
    ScannerWeft,
    ScanStats,
    SlimIssueWeft,
    TypeSummaryWeft,
    WeftReasonWeft,
)
from filigree.models import Issue
from filigree.scanners import ScannerConfig
from filigree.templates import TypeTemplate, WorkflowPack
from filigree.types.core import ISOTimestamp, ObservationDict, ScanFindingDict
from filigree.types.events import EventRecord, EventRecordWithTitle
from filigree.types.files import EnrichedFileItem, IssueFileAssociation, ScanIngestResult
from filigree.types.planning import CommentRecord


def slim_issue_to_weft(issue: Issue) -> SlimIssueWeft:
    """Project an ``Issue`` into the weft slim shape.

    Renames ``id`` to ``issue_id`` per the Phase D vocabulary shift and
    keeps the same five-field projection as classic ``SlimIssue`` (title,
    status, priority, type). Used by every weft batch handler whose
    ``succeeded`` and ``newly_unblocked`` lists return slim issues.
    """
    return SlimIssueWeft(
        issue_id=issue.id,
        title=issue.title,
        status=issue.status,
        priority=issue.priority,
        type=issue.type,
    )


def issue_to_weft(issue: Issue) -> IssueWeft:
    """Project an ``Issue`` into the full weft-vocab issue shape.

    Mirrors ``Issue.to_dict()`` (returning ``IssueDict``) except the
    issue's own primary key is renamed ``id`` → ``issue_id``. Reference
    fields holding other issues' ids (``parent_id``, ``blocks``,
    ``blocked_by``, ``children``) keep their existing names per the
    weft-vocabulary scope (only the entity's own primary key is renamed).

    Used by every single-issue weft endpoint that returns a full issue
    projection (GET, PATCH, close, reopen, claim, release, claim-next,
    create).
    """
    classic = issue.to_dict()
    return IssueWeft(
        issue_id=classic["id"],
        title=classic["title"],
        status=classic["status"],
        status_category=classic["status_category"],
        priority=classic["priority"],
        type=classic["type"],
        parent_id=classic["parent_id"],
        assignee=classic["assignee"],
        claimed_at=classic["claimed_at"],
        last_heartbeat_at=classic["last_heartbeat_at"],
        claim_expires_at=classic["claim_expires_at"],
        created_at=classic["created_at"],
        updated_at=classic["updated_at"],
        closed_at=classic["closed_at"],
        description=classic["description"],
        notes=classic["notes"],
        fields=classic["fields"],
        labels=classic["labels"],
        blocks=classic["blocks"],
        blocked_by=classic["blocked_by"],
        is_ready=classic["is_ready"],
        children=classic["children"],
        data_warnings=classic["data_warnings"],
    )


def comment_record_to_weft(record: CommentRecord, *, created_at: ISOTimestamp | None = None) -> CommentRecordWeft:
    """Project a classic ``CommentRecord`` (``id``) into the weft shape
    (``comment_id``).

    The ``created_at`` override exists because the dashboard's
    ``add_comment`` handler fetches the timestamp separately after
    insert (rather than reading it from a CommentRecord); callers can
    pass the freshly-fetched timestamp through this argument.
    """
    return CommentRecordWeft(
        comment_id=record["id"],
        author=record["author"],
        text=record["text"],
        created_at=created_at if created_at is not None else record["created_at"],
    )


def blocked_issue_to_weft(issue: Issue) -> BlockedIssueWeft:
    """Project a blocked ``Issue`` into the weft shape with ``blocked_by``.

    Used by ``GET /api/weft/blocked``. Mirrors classic ``BlockedIssue``
    (which extends ``SlimIssue``) but renames ``id`` → ``issue_id``.
    """
    return BlockedIssueWeft(
        issue_id=issue.id,
        title=issue.title,
        status=issue.status,
        priority=issue.priority,
        type=issue.type,
        blocked_by=list(issue.blocked_by),
    )


def file_record_to_weft(record: EnrichedFileItem) -> FileRecordWeft:
    """Project an ``EnrichedFileItem`` into the weft file shape.

    Renames the file's primary key ``id`` → ``file_id`` and preserves the
    enriched fields (summary, associations_count, observation_count).
    Used by ``GET /api/weft/files``.
    """
    return FileRecordWeft(
        file_id=record["id"],
        path=record["path"],
        language=record["language"],
        file_type=record["file_type"],
        first_seen=record["first_seen"],
        updated_at=record["updated_at"],
        metadata=dict(record["metadata"]),
        data_warnings=list(record["data_warnings"]),
        summary=record["summary"],
        associations_count=record["associations_count"],
        observation_count=record["observation_count"],
    )


def file_assoc_to_weft(record: IssueFileAssociation) -> FileAssocWeft:
    """Project an ``IssueFileAssociation`` into the weft assoc shape.

    Renames the association row's primary key ``id`` → ``assoc_id``.
    Cross-entity refs ``file_id`` and ``issue_id`` keep their names.
    Used by ``GET /api/weft/issues/{issue_id}/files``.
    """
    return FileAssocWeft(
        assoc_id=record["id"],
        file_id=record["file_id"],
        issue_id=record["issue_id"],
        assoc_type=record["assoc_type"],
        created_at=record["created_at"],
        file_path=record["file_path"],
        file_language=record["file_language"],
    )


def scan_finding_to_weft(record: ScanFindingDict) -> ScanFindingWeft:
    """Project a ``ScanFindingDict`` into the weft finding shape.

    Renames the finding's primary key ``id`` → ``finding_id``. Used by
    ``GET /api/weft/findings``.
    """
    return ScanFindingWeft(
        finding_id=record["id"],
        file_id=record["file_id"],
        severity=record["severity"],
        status=record["status"],
        scan_source=record["scan_source"],
        rule_id=record["rule_id"],
        message=record["message"],
        suggestion=record["suggestion"],
        scan_run_id=record["scan_run_id"],
        line_start=record["line_start"],
        line_end=record["line_end"],
        fingerprint=record["fingerprint"],
        issue_id=record["issue_id"],
        # N6 (weft-c815d5e77d): linked issue status + close_reason resolution.
        issue_status=record["issue_status"],
        issue_resolution=record["issue_resolution"],
        # Wardline suppression verdict (lifted from metadata) rides the same surface.
        suppression_state=record["suppression_state"],
        seen_count=record["seen_count"],
        first_seen=record["first_seen"],
        updated_at=record["updated_at"],
        last_seen_at=record["last_seen_at"],
        metadata=dict(record["metadata"]),
        data_warnings=list(record["data_warnings"]),
    )


def observation_to_weft(record: ObservationDict) -> ObservationWeft:
    """Project an ``ObservationDict`` into the weft observation shape.

    Renames the observation's primary key ``id`` → ``observation_id``.
    Used by ``GET /api/weft/observations``.
    """
    return ObservationWeft(
        observation_id=record["id"],
        summary=record["summary"],
        detail=record["detail"],
        file_id=record["file_id"],
        file_path=record["file_path"],
        line=record["line"],
        source_issue_id=record["source_issue_id"],
        priority=record["priority"],
        actor=record["actor"],
        created_at=record["created_at"],
        expires_at=record["expires_at"],
    )


def scanner_config_to_weft(config: ScannerConfig) -> ScannerWeft:
    """Project a ``ScannerConfig`` into the weft scanner shape.

    Mirrors ``ScannerConfig.to_dict()`` exactly. ``name`` is the
    scanner's primary key but is already string-named, so no rename
    applies. Used by ``GET /api/weft/scanners``.
    """
    return ScannerWeft(
        name=config.name,
        description=config.description,
        file_types=list(config.file_types),
    )


def pack_to_weft(pack: WorkflowPack) -> PackWeft:
    """Project a ``WorkflowPack`` into the weft packs-list shape.

    Mirrors MCP's ``PackListItem``. ``pack`` is the entity's logical
    primary key; not renamed. Used by ``GET /api/weft/packs``.
    """
    return PackWeft(
        pack=pack.pack,
        version=pack.version,
        display_name=pack.display_name,
        description=pack.description,
        types=sorted(pack.types.keys()),
        requires_packs=list(pack.requires_packs),
    )


def type_template_to_weft(template: TypeTemplate) -> TypeSummaryWeft:
    """Project a ``TypeTemplate`` into the weft types-list shape.

    Matches the classic ``/api/types`` projection (4 keys). Used by
    ``GET /api/weft/types``.
    """
    return TypeSummaryWeft(
        type=template.type,
        display_name=template.display_name,
        pack=template.pack,
        initial_state=template.initial_state,
    )


def issue_event_to_weft(record: EventRecord) -> IssueEventWeft:
    """Project an ``EventRecord`` into the weft event shape.

    Renames the event row's primary key ``id`` → ``event_id``. Used by
    ``GET /api/weft/issues/{issue_id}/events``. The ``issue_id`` field
    is a cross-entity ref and is kept as-is.
    """
    return IssueEventWeft(
        event_id=record["id"],
        issue_id=record["issue_id"],
        event_type=record["event_type"],
        actor=record["actor"],
        old_value=record["old_value"],
        new_value=record["new_value"],
        comment=record["comment"],
        created_at=record["created_at"],
    )


def change_record_to_weft(record: EventRecordWithTitle) -> ChangeRecordWeft:
    """Project an ``EventRecordWithTitle`` into the weft change shape.

    Same as ``issue_event_to_weft`` but includes the joined
    ``issue_title``. Used by ``GET /api/weft/changes``.
    """
    return ChangeRecordWeft(
        event_id=record["id"],
        issue_id=record["issue_id"],
        event_type=record["event_type"],
        actor=record["actor"],
        old_value=record["old_value"],
        new_value=record["new_value"],
        comment=record["comment"],
        created_at=record["created_at"],
        issue_title=record["issue_title"],
        # Uniform on the wire: only synthetic issue_deleted records carry the
        # cascaded entity bindings; real event rows normalize to []. (schema v21)
        affected_entities=record.get("affected_entities", []),
    )


def list_response(items: list[Any], *, limit: int, offset: int, total: int | None = None, has_more: bool | None = None) -> dict[str, Any]:
    """Build a unified ``ListResponse[T]`` envelope.

    Two paging modes:

    1. **Total known** — pass ``total``. ``has_more`` is computed as
       ``offset + len(items) < total``; ``next_offset`` is the next page
       boundary. This is the common case for SQL-COUNT-backed paginators
       (``list_files_paginated``, ``count_search_results``).
    2. **Total unknown** — pass ``has_more`` directly (e.g. when callers
       overfetch by 1 and trim to detect more). ``total`` is ignored.

    ``next_offset`` is omitted entirely (NotRequired) when
    ``has_more`` is False, matching the documented ``ListResponse``
    contract: present only when there is more to fetch.
    """
    if has_more is None:
        if total is None:
            msg = "list_response requires either total= or has_more="
            raise ValueError(msg)
        has_more = offset + len(items) < total
    body: dict[str, Any] = {"items": items, "has_more": has_more}
    if has_more:
        body["next_offset"] = offset + len(items)
    return body


def scan_ingest_result_to_weft(result: ScanIngestResult) -> ScanIngestResponseWeft:
    """Transform an internal ``ScanIngestResult`` into the weft response shape.

    - ``new_finding_ids`` → ``succeeded`` (the generic batch wrapper's
      success list; weft's succeeded type is ``list[str]``).
    - ``files_created`` / ``files_updated`` / ``findings_created`` /
      ``findings_updated`` / ``observations_created`` /
      ``observations_failed`` → ``stats`` sibling.
    - ``warnings`` → top-level (kept at top level so consumers that only
      care about operator warnings do not have to reach into ``stats``).
    - ``failed`` is ``[]`` until per-finding ingest failure tracking
      lands (non-breaking addition per ADR-002 §3).
    """
    response = ScanIngestResponseWeft(
        succeeded=list(result["new_finding_ids"]),
        failed=[],
        stats=ScanStats(
            files_created=result["files_created"],
            files_updated=result["files_updated"],
            findings_created=result["findings_created"],
            findings_updated=result["findings_updated"],
            observations_created=result["observations_created"],
            observations_failed=result["observations_failed"],
        ),
        warnings=list(result["warnings"]),
    )
    # weft-reason carriers (PDR-0023): only surfaced when non-empty, so a clean
    # same-scheme ingest yields the byte-identical 4-key envelope (NotRequired
    # field omitted). Today the only carrier is the G4 scheme_mismatch case.
    weft_reasons = result.get("weft_reasons") or []
    if weft_reasons:
        response["weft_reasons"] = [
            WeftReasonWeft(reason_class=r["reason_class"], cause=r["cause"], fix=r["fix"]) for r in weft_reasons
        ]
    return response
