"""TypedDicts for db_planning.py, db_meta.py, and analytics.py return types."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from filigree.types.core import ISOTimestamp, IssueDict, StatusCategory

# ---------------------------------------------------------------------------
# db_planning.py types
# ---------------------------------------------------------------------------


class CriticalPathNode(TypedDict):
    """Single node in the critical-path chain from ``get_critical_path()``."""

    id: str
    title: str
    priority: int
    type: str


# DependencyRecord uses "from" as a key at runtime (a Python keyword).
# TypedDict cannot express this with class syntax; we use functional form.
DependencyRecord = TypedDict("DependencyRecord", {"from": str, "to": str, "type": str})


class PlanPhase(TypedDict):
    """A single phase entry inside a PlanTree."""

    phase: IssueDict
    steps: list[IssueDict]
    total: int
    completed: int
    ready: int


class PlanTree(TypedDict):
    """Plan tree returned by ``get_plan()`` and ``create_plan()``."""

    milestone: IssueDict
    phases: list[PlanPhase]
    total_steps: int
    completed_steps: int


class ProgressDict(TypedDict):
    """Progress summary for a release or plan subtree."""

    total: int
    completed: int
    in_progress: int
    open: int
    pct: int


class ChildSummary(TypedDict):
    """Type breakdown of direct children in a release tree."""

    epics: int
    milestones: int
    tasks: int
    bugs: int
    other: int
    total: int


class IssueRef(TypedDict):
    """Minimal issue reference used in release summaries."""

    id: str
    title: str
    type: str
    dangling: NotRequired[bool]


class TreeNode(TypedDict):
    """A node in the recursive release/plan tree."""

    issue: IssueDict
    progress: ProgressDict | None
    children: list[TreeNode]
    truncated: NotRequired[bool]


class ReleaseSummaryItem(TypedDict):
    """Shape of each item returned by ``get_releases_summary()``.

    Cannot use ``class ReleaseSummaryItem(IssueDict)`` because ``blocks``
    and ``blocked_by`` are ``list[IssueRef]`` here (enriched references with
    title/type) vs ``list[str]`` (bare IDs) in IssueDict. TypedDict
    inheritance does not allow narrowing inherited field types.
    """

    # Manually duplicates IssueDict keys — kept in sync by contract test
    # TestReleaseSummaryItemShape in test_type_contracts.py
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
    claim_commit: str | None
    close_commit: str | None
    description: str
    notes: str
    fields: dict[str, Any]
    labels: list[str]
    blocks: list[IssueRef]
    blocked_by: list[IssueRef]
    is_ready: bool
    children: list[str]
    data_warnings: list[str]
    version: str | None
    target_date: str | None
    progress: ProgressDict
    child_summary: ChildSummary


class ReleaseTree(TypedDict):
    """Shape returned by ``get_release_tree()``."""

    release: IssueDict
    children: list[TreeNode]
    data_warnings: NotRequired[list[str]]


# ---------------------------------------------------------------------------
# db_meta.py types
# ---------------------------------------------------------------------------


class CommentRecord(TypedDict):
    """Row from the comments table returned by ``get_comments()``."""

    id: int
    author: str
    verified_author: str | None
    text: str
    created_at: ISOTimestamp


class StatsResult(TypedDict):
    """Aggregate stats returned by ``get_stats()``."""

    by_status: dict[str, int]
    by_category: dict[str, int]
    by_type: dict[str, int]
    ready_count: int
    blocked_count: int
    total_dependencies: int


# ---------------------------------------------------------------------------
# analytics.py types
# ---------------------------------------------------------------------------


class TypeMetrics(TypedDict):
    """Per-type cycle time metrics nested inside ``FlowMetrics``."""

    avg_cycle_time_hours: float | None
    count: int


class FlowMetrics(TypedDict):
    """Aggregate flow metrics returned by ``get_flow_metrics()``."""

    period_days: int
    throughput: int
    avg_cycle_time_hours: float | None
    avg_lead_time_hours: float | None
    by_type: dict[str, TypeMetrics]
