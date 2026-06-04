"""Shared scanner finding reporting orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from filigree.db_files import INGESTED_FILE_ID_KEY, _normalize_scan_path
from filigree.types.files import ScanIngestResult

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScannerReportOutcome:
    """Structured result for a single reported scanner finding."""

    result: ScanIngestResult
    finding_record: dict[str, Any]
    observation_ids: list[str]
    normalized_finding: dict[str, Any]

    @property
    def status(self) -> str:
        return "created" if self.result["findings_created"] else "updated"


def report_finding_observation_ids(
    tracker: Any,
    *,
    file_id: str,
    finding_id: str,
) -> list[str]:
    """Find observation IDs paired with a given finding."""
    observations = tracker.list_observations(file_id=file_id, limit=10000)
    return [observation["id"] for observation in observations if observation.get("source_finding_id") == finding_id]


def reported_finding_record(
    tracker: Any,
    result: ScanIngestResult,
    *,
    file_id: str | None,
    rule_id: str,
    line_start: int | None,
    message: str,
    severity: str,
) -> dict[str, Any] | None:
    for finding_id in result.get("new_finding_ids", []):
        try:
            return cast(dict[str, Any], tracker.get_finding(finding_id))
        except KeyError:
            continue

    matching_findings = cast(
        list[dict[str, Any]],
        tracker.list_findings_global(scan_source="agent", file_id=file_id, limit=10000)["findings"],
    )
    return next(
        (
            item
            for item in matching_findings
            if item["rule_id"] == rule_id
            and item.get("line_start") == line_start
            and item.get("message") == message
            and item.get("severity") == severity
        ),
        None,
    )


def normalize_report_finding_line_attribution(tracker: Any, finding: dict[str, Any]) -> list[str]:
    """Clear impossible line attribution before strict scan-result ingest validation."""
    project_root = getattr(tracker, "project_root", None)
    if project_root is None:
        return []

    path = finding.get("path")
    if not isinstance(path, str):
        return []

    try:
        root = Path(project_root).resolve()
        target = (root / _normalize_scan_path(path)).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        return []

    if not target.is_file():
        return []

    try:
        with target.open("rb") as handle:
            line_count = sum(1 for _ in handle)
    except OSError as exc:
        _logger.debug("Could not count lines for scanner report target %s: %s", target, exc, exc_info=True)
        return []

    warnings: list[str] = []
    line_label = "line" if line_count == 1 else "lines"
    rule_id = finding.get("rule_id", "")

    line_start = finding.get("line_start")
    if isinstance(line_start, int) and not isinstance(line_start, bool) and line_start > line_count:
        finding.pop("line_start", None)
        finding.pop("line_end", None)
        warnings.append(
            f"Finding {rule_id!r} at {path}: line_start {line_start} exceeds file length "
            f"({path} has {line_count} {line_label}); line attribution cleared"
        )
        return warnings

    line_end = finding.get("line_end")
    if isinstance(line_end, int) and not isinstance(line_end, bool) and line_end > line_count:
        finding.pop("line_end", None)
        warnings.append(
            f"Finding {rule_id!r} at {path}: line_end {line_end} exceeds file length "
            f"({path} has {line_count} {line_label}); line_end cleared"
        )
    return warnings


def report_scanner_finding(
    tracker: Any,
    finding: dict[str, Any],
    *,
    create_observation: bool,
    observation_actor: str = "",
    refresh_summary: Callable[[Any], None] | None = None,
) -> ScannerReportOutcome:
    """Ingest a single scanner finding and return its normalized DB record."""
    normalized_finding = dict(finding)
    line_warnings = normalize_report_finding_line_attribution(tracker, normalized_finding)
    result = tracker.process_scan_results(
        scan_source="agent",
        findings=[normalized_finding],
        scan_run_id="",
        create_observations=create_observation,
        observation_actor=observation_actor,
    )
    if refresh_summary is not None:
        refresh_summary(tracker)
    if line_warnings:
        result["warnings"].extend(line_warnings)

    reported_file_id = normalized_finding.get(INGESTED_FILE_ID_KEY)
    reported_line_start = normalized_finding.get("line_start")
    lookup_line_start = reported_line_start if isinstance(reported_line_start, int) and not isinstance(reported_line_start, bool) else None
    finding_record = reported_finding_record(
        tracker,
        result,
        file_id=reported_file_id if isinstance(reported_file_id, str) else None,
        rule_id=str(normalized_finding["rule_id"]),
        line_start=lookup_line_start,
        message=str(normalized_finding["message"]),
        severity=str(normalized_finding.get("severity", "info")),
    )
    if finding_record is None:
        msg = "Reported finding was not found after ingestion"
        raise LookupError(msg)

    observation_ids = (
        report_finding_observation_ids(tracker, file_id=finding_record["file_id"], finding_id=finding_record["id"])
        if create_observation
        else []
    )
    return ScannerReportOutcome(
        result=result,
        finding_record=finding_record,
        observation_ids=observation_ids,
        normalized_finding=normalized_finding,
    )
