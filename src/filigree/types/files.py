"""TypedDicts for db_files.py return types."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from filigree.types.core import (
    AssocType,
    FileRecordDict,
    ISOTimestamp,
    ScanFindingDict,
    ScanRunStatus,
)


class FileAssociation(TypedDict):
    """Shape returned by ``get_file_associations()`` — file-to-issue direction."""

    id: int
    file_id: str
    issue_id: str
    assoc_type: AssocType
    actor: str
    created_at: ISOTimestamp
    issue_title: str | None
    issue_status: str | None


class IssueFileAssociation(TypedDict):
    """Shape returned by ``get_issue_files()`` — issue-to-file direction."""

    id: int
    file_id: str
    issue_id: str
    assoc_type: AssocType
    actor: str
    created_at: ISOTimestamp
    file_path: str
    file_language: str | None


class SeverityBreakdown(TypedDict):
    """Severity-bucketed finding counts (reused by several return shapes)."""

    critical: int
    high: int
    medium: int
    low: int
    info: int


class FindingsSummary(SeverityBreakdown):
    """Shape returned by ``get_file_findings_summary()``."""

    total_findings: int
    open_findings: int


class GlobalFindingsStats(FindingsSummary):
    """Shape returned by ``get_global_findings_stats()``."""

    files_with_findings: int


class HotspotFileRef(TypedDict):
    """Minimal file reference embedded in ``FileHotspot``."""

    id: str
    path: str
    language: str


class FileHotspot(TypedDict):
    """Shape returned by ``get_file_hotspots()``."""

    file: HotspotFileRef
    score: int
    findings_breakdown: SeverityBreakdown


class FileDetail(TypedDict):
    """Shape returned by ``get_file_detail()``."""

    file: FileRecordDict
    associations: list[FileAssociation]
    recent_findings: list[ScanFindingDict]
    summary: FindingsSummary
    observation_count: int


class DeleteFileRecordResult(TypedDict):
    """Shape returned by ``delete_file_record()``."""

    status: str
    file_id: str
    deleted_findings: int
    deleted_associations: int
    deleted_file_events: int
    deleted_annotation_links: int
    unlinked_observations: int
    actor: str


class DeleteIssueResult(TypedDict):
    """Shape returned by ``delete_issue()`` (hard-delete + tombstone).

    Counts the child rows removed in FK-safe order plus the rows the final
    issue delete auto-orphaned via ``ON DELETE SET NULL`` (children's
    ``parent_id`` and ``scan_findings.issue_id``). ``status`` is always
    ``"deleted"``. The delete is irreversible — events are destroyed, so
    ``undo_last`` cannot reverse it.
    """

    status: Literal["deleted"]
    issue_id: str
    deleted_events: int
    deleted_comments: int
    deleted_labels: int
    deleted_dependencies_out: int
    deleted_dependencies_in: int
    deleted_file_associations: int
    deleted_observation_links: int
    deleted_attachments: int
    deleted_annotation_links: int
    deleted_annotation_closeout_acks: int
    deleted_entity_associations: int
    orphaned_children: int
    orphaned_findings: int
    actor: str


class ScanRunRecord(TypedDict):
    """Shape returned by ``get_scan_runs()``.

    Note: ``completed_at`` is ``MAX(updated_at)`` via SQL aggregate. The query
    uses ``GROUP BY scan_run_id`` so each group has at least one row, guaranteeing
    a non-NULL result. If the query changes to allow empty groups, this field
    should be widened to ``ISOTimestamp | None``.
    """

    scan_run_id: str
    scan_source: str
    started_at: ISOTimestamp
    completed_at: ISOTimestamp
    total_findings: int
    files_scanned: int


class ScanRunDict(TypedDict):
    """Shape for scan_runs table rows returned by ScansMixin.

    Not to be confused with ScanRunRecord (above), which is a GROUP BY
    projection from scan_findings for legacy scan history queries.
    """

    id: str
    scanner_name: str
    scan_source: str
    status: ScanRunStatus
    file_paths: list[str]
    file_ids: list[str]
    pid: int | None
    api_url: str  # empty string means not set
    log_path: str  # empty string means not set
    started_at: ISOTimestamp
    updated_at: ISOTimestamp
    completed_at: ISOTimestamp | None
    exit_code: int | None
    findings_count: int
    error_message: str  # empty string means no error
    data_warnings: list[str]


class ScanRunStatusDict(ScanRunDict):
    """Extended shape for get_scan_status with live process info and log tail."""

    process_alive: bool
    log_tail: list[str]


class ScanIngestResult(TypedDict):
    """Shape returned by ``process_scan_results()``."""

    files_created: int
    files_updated: int
    findings_created: int
    findings_updated: int
    new_finding_ids: list[str]
    observations_created: int
    observations_failed: int
    warnings: list[str]


class EnrichedFileItem(FileRecordDict):
    """Shape of each item in ``list_files_paginated()`` results.

    Extends FileRecordDict with inline summary and counts.
    """

    summary: FindingsSummary
    associations_count: int
    observation_count: int


class TimelineEntry(TypedDict):
    """Shape of each item in ``get_file_timeline()`` results."""

    id: str
    type: str
    timestamp: ISOTimestamp
    source_id: str
    data: dict[str, Any]


class CleanStaleResult(TypedDict):
    """Shape returned by ``clean_stale_findings()``."""

    findings_fixed: int
    closed_issue_ids: list[str]
