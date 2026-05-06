# IMPORT CONSTRAINT: types/ modules must only import from typing, stdlib, and each other.
# NEVER import from core.py, db_base.py, or any mixin — this prevents circular imports.
"""TypedDict contracts for MCP tool handler input arguments.

Each TypedDict mirrors the JSON Schema ``inputSchema`` on the corresponding
``mcp.types.Tool`` definition.  The ``TOOL_ARGS_MAP`` registry maps tool names
to their TypedDict class so the sync test can verify structural agreement.

Safety note on cast():
    The MCP SDK validates argument presence/types against JSON Schema before
    handler invocation.  Core validates authoritatively.  The TypedDicts here
    are a *static-analysis* tool — ``cast()`` provides type narrowing only,
    not runtime validation.  Direct handler calls that bypass MCP SDK
    validation are unsafe — callers must pre-validate arguments.
"""

# NOTE: Do NOT add ``from __future__ import annotations`` to this module.
# It breaks TypedDict.__required_keys__ / __optional_keys__ introspection
# on Python <3.14, which the sync test in test_input_type_contracts.py
# depends on for verifying required/optional agreement with JSON Schema.

from typing import Any, Literal, NotRequired, TypedDict

from filigree.types.core import AssocType, FindingStatus, ISOTimestamp, Severity, StatusCategory

# ---------------------------------------------------------------------------
# issues.py handlers
# ---------------------------------------------------------------------------


class GetIssueArgs(TypedDict):
    issue_id: str
    include_transitions: NotRequired[bool]
    include_files: NotRequired[bool]


class ListIssuesArgs(TypedDict):
    # status is str (not Literal) because valid values are template-defined and
    # cannot be statically enumerated, unlike status_category which is fixed.
    status: NotRequired[str]
    status_category: NotRequired[StatusCategory]
    type: NotRequired[str]
    priority: NotRequired[int]
    parent_issue_id: NotRequired[str]
    assignee: NotRequired[str]
    label: NotRequired[str | list[str]]
    label_prefix: NotRequired[str]
    not_label: NotRequired[str]
    sort_by: NotRequired[Literal["created_at", "updated_at", "priority"]]
    direction: NotRequired[Literal["asc", "desc"]]
    limit: NotRequired[int]
    offset: NotRequired[int]
    no_limit: NotRequired[bool]


class CreateIssueArgs(TypedDict):
    title: str
    type: NotRequired[str]
    priority: NotRequired[int]
    parent_issue_id: NotRequired[str]
    description: NotRequired[str]
    notes: NotRequired[str]
    fields: NotRequired[dict[str, Any]]
    labels: NotRequired[list[str]]
    deps: NotRequired[list[str]]
    actor: NotRequired[str]


class UpdateIssueArgs(TypedDict):
    issue_id: str
    status: NotRequired[str]
    priority: NotRequired[int]
    title: NotRequired[str]
    assignee: NotRequired[str]
    description: NotRequired[str]
    notes: NotRequired[str]
    parent_issue_id: NotRequired[str]
    fields: NotRequired[dict[str, Any]]
    actor: NotRequired[str]


class CloseIssueArgs(TypedDict):
    issue_id: str
    reason: NotRequired[str]
    actor: NotRequired[str]
    fields: NotRequired[dict[str, Any]]


class ReopenIssueArgs(TypedDict):
    issue_id: str
    actor: NotRequired[str]


class SearchIssuesArgs(TypedDict):
    query: str
    limit: NotRequired[int]
    offset: NotRequired[int]
    no_limit: NotRequired[bool]


class ClaimIssueArgs(TypedDict):
    issue_id: str
    assignee: str
    actor: NotRequired[str]


class ReleaseClaimArgs(TypedDict):
    issue_id: str
    actor: NotRequired[str]


class ClaimNextArgs(TypedDict):
    assignee: str
    type: NotRequired[str]
    priority_min: NotRequired[int]
    priority_max: NotRequired[int]
    actor: NotRequired[str]


class StartWorkArgs(TypedDict):
    issue_id: str
    assignee: str
    target_status: NotRequired[str]
    actor: NotRequired[str]


class StartNextWorkArgs(TypedDict):
    assignee: str
    type: NotRequired[str]
    priority_min: NotRequired[int]
    priority_max: NotRequired[int]
    target_status: NotRequired[str]
    actor: NotRequired[str]


class BatchCloseArgs(TypedDict):
    issue_ids: list[str]
    reason: NotRequired[str]
    response_detail: NotRequired[str]
    actor: NotRequired[str]


class BatchUpdateArgs(TypedDict):
    issue_ids: list[str]
    status: NotRequired[str]
    priority: NotRequired[int]
    assignee: NotRequired[str]
    fields: NotRequired[dict[str, Any]]
    response_detail: NotRequired[str]
    actor: NotRequired[str]


# ---------------------------------------------------------------------------
# meta.py handlers
# ---------------------------------------------------------------------------


class AddCommentArgs(TypedDict):
    issue_id: str
    text: str
    actor: NotRequired[str]


class GetCommentsArgs(TypedDict):
    issue_id: str


class AddLabelArgs(TypedDict):
    issue_id: str
    label: str


class RemoveLabelArgs(TypedDict):
    issue_id: str
    label: str


class ListLabelsArgs(TypedDict):
    namespace: NotRequired[str]
    top: NotRequired[int]


class BatchAddLabelArgs(TypedDict):
    issue_ids: list[str]
    label: str
    response_detail: NotRequired[str]
    actor: NotRequired[str]


class BatchRemoveLabelArgs(TypedDict):
    issue_ids: list[str]
    label: str
    response_detail: NotRequired[str]
    actor: NotRequired[str]


class BatchAddCommentArgs(TypedDict):
    issue_ids: list[str]
    text: str
    response_detail: NotRequired[str]
    actor: NotRequired[str]


class GetChangesArgs(TypedDict):
    since: ISOTimestamp
    limit: NotRequired[int]
    actor: NotRequired[str]
    issue_id: NotRequired[str]
    label: NotRequired[str]
    type: NotRequired[str]


class GetMetricsArgs(TypedDict):
    days: NotRequired[int]


class ExportJsonlArgs(TypedDict):
    output_path: str


class ImportJsonlArgs(TypedDict):
    input_path: str
    merge: NotRequired[bool]


class ArchiveClosedArgs(TypedDict):
    days_old: NotRequired[int]
    actor: NotRequired[str]


class CompactEventsArgs(TypedDict):
    keep_recent: NotRequired[int]


class UndoLastArgs(TypedDict):
    issue_id: str
    actor: NotRequired[str]


class GetIssueEventsArgs(TypedDict):
    issue_id: str
    limit: NotRequired[int]


# ---------------------------------------------------------------------------
# planning.py handlers
# ---------------------------------------------------------------------------


class AddDependencyArgs(TypedDict):
    from_issue_id: str
    to_issue_id: str
    actor: NotRequired[str]


class RemoveDependencyArgs(TypedDict):
    from_issue_id: str
    to_issue_id: str
    actor: NotRequired[str]


class GetPlanArgs(TypedDict):
    milestone_id: str


class StepInput(TypedDict):
    title: str
    priority: NotRequired[int]
    description: NotRequired[str]
    labels: NotRequired[list[str]]
    deps: NotRequired[list[int | str]]


class PhaseInput(TypedDict):
    title: str
    priority: NotRequired[int]
    description: NotRequired[str]
    labels: NotRequired[list[str]]
    steps: NotRequired[list[StepInput]]


class MilestoneInput(TypedDict):
    title: str
    priority: NotRequired[int]
    description: NotRequired[str]
    labels: NotRequired[list[str]]


class CreatePlanArgs(TypedDict):
    milestone: MilestoneInput
    phases: list[PhaseInput]
    actor: NotRequired[str]


class CreatePlanFromFileArgs(TypedDict):
    file_path: str
    actor: NotRequired[str]


class LabelSubtreeArgs(TypedDict):
    parent_id: str
    label: str
    response_detail: NotRequired[str]


# ---------------------------------------------------------------------------
# workflow.py handlers
# ---------------------------------------------------------------------------


class GetTemplateArgs(TypedDict):
    type: str


class GetTypeInfoArgs(TypedDict):
    type: str


class GetValidTransitionsArgs(TypedDict):
    issue_id: str


class ValidateIssueArgs(TypedDict):
    issue_id: str


class GetWorkflowGuideArgs(TypedDict):
    pack: str


class ExplainStatusArgs(TypedDict):
    type: str
    status: str


# ---------------------------------------------------------------------------
# files.py handlers
# ---------------------------------------------------------------------------


class ListFilesArgs(TypedDict):
    limit: NotRequired[int]
    offset: NotRequired[int]
    language: NotRequired[str]
    path_prefix: NotRequired[str]
    min_findings: NotRequired[int]
    has_severity: NotRequired[Severity]
    scan_source: NotRequired[str]
    sort: NotRequired[Literal["updated_at", "first_seen", "path", "language"]]
    direction: NotRequired[Literal["asc", "desc"]]


class GetFileArgs(TypedDict):
    file_id: str


class GetFileTimelineArgs(TypedDict):
    file_id: str
    limit: NotRequired[int]
    offset: NotRequired[int]
    event_type: NotRequired[str]


class GetIssueFilesArgs(TypedDict):
    issue_id: str


class AddFileAssociationArgs(TypedDict):
    file_id: str
    issue_id: str
    assoc_type: AssocType


class RegisterFileArgs(TypedDict):
    path: str
    language: NotRequired[str]
    file_type: NotRequired[str]
    metadata: NotRequired[dict[str, Any]]


class TriggerScanArgs(TypedDict):
    scanner: str
    file_path: str
    api_url: NotRequired[str]


class GetFindingArgs(TypedDict):
    finding_id: str


class ListFindingsArgs(TypedDict):
    severity: NotRequired[Severity]
    status: NotRequired[FindingStatus]
    scan_source: NotRequired[str]
    scan_run_id: NotRequired[str]
    file_id: NotRequired[str]
    issue_id: NotRequired[str]
    limit: NotRequired[int]
    offset: NotRequired[int]


class UpdateFindingArgs(TypedDict):
    finding_id: str
    status: NotRequired[FindingStatus]
    issue_id: NotRequired[str]


class BatchUpdateFindingsArgs(TypedDict):
    finding_ids: list[str]
    status: FindingStatus
    response_detail: NotRequired[str]


class PromoteFindingArgs(TypedDict):
    finding_id: str
    priority: NotRequired[int]
    actor: NotRequired[str]


class DismissFindingArgs(TypedDict):
    finding_id: str
    reason: NotRequired[str]


# ---------------------------------------------------------------------------
# scanners.py handlers
# ---------------------------------------------------------------------------


class ReportFindingArgs(TypedDict):
    file_path: str
    rule_id: str
    message: str
    severity: NotRequired[str]
    line_start: NotRequired[int]
    line_end: NotRequired[int]
    category: NotRequired[str]


class TriggerScanBatchArgs(TypedDict):
    scanner: str
    file_paths: list[str]
    api_url: NotRequired[str]


class GetScanStatusArgs(TypedDict):
    scan_run_id: str
    log_lines: NotRequired[int]  # 1..500


class PreviewScanArgs(TypedDict):
    scanner: str
    file_path: str


# ---------------------------------------------------------------------------
# observations.py handlers
# ---------------------------------------------------------------------------


class ObserveArgs(TypedDict):
    summary: str
    detail: NotRequired[str]
    file_path: NotRequired[str]
    line: NotRequired[int]
    source_issue_id: NotRequired[str]
    priority: NotRequired[int]
    actor: NotRequired[str]


class ListObservationsArgs(TypedDict):
    limit: NotRequired[int]
    offset: NotRequired[int]
    no_limit: NotRequired[bool]
    file_path: NotRequired[str]
    file_id: NotRequired[str]


class DismissObservationArgs(TypedDict):
    observation_id: str
    reason: NotRequired[str]
    actor: NotRequired[str]


class BatchDismissObservationsArgs(TypedDict):
    observation_ids: list[str]
    reason: NotRequired[str]
    response_detail: NotRequired[str]
    actor: NotRequired[str]


class PromoteObservationArgs(TypedDict):
    observation_id: str
    type: NotRequired[str]
    priority: NotRequired[int]
    title: NotRequired[str]
    description: NotRequired[str]
    actor: NotRequired[str]


# Registry: tool_name -> TypedDict class.
# Populated as TypedDicts are defined below.
# No-argument tools (empty inputSchema properties) are intentionally excluded.
TOOL_ARGS_MAP: dict[str, type] = {
    # issues.py
    "get_issue": GetIssueArgs,
    "list_issues": ListIssuesArgs,
    "create_issue": CreateIssueArgs,
    "update_issue": UpdateIssueArgs,
    "close_issue": CloseIssueArgs,
    "reopen_issue": ReopenIssueArgs,
    "search_issues": SearchIssuesArgs,
    "claim_issue": ClaimIssueArgs,
    "release_claim": ReleaseClaimArgs,
    "claim_next": ClaimNextArgs,
    "batch_close": BatchCloseArgs,
    "batch_update": BatchUpdateArgs,
    "start_work": StartWorkArgs,
    "start_next_work": StartNextWorkArgs,
    # meta.py
    "add_comment": AddCommentArgs,
    "get_comments": GetCommentsArgs,
    "add_label": AddLabelArgs,
    "remove_label": RemoveLabelArgs,
    "list_labels": ListLabelsArgs,
    "batch_add_label": BatchAddLabelArgs,
    "batch_remove_label": BatchRemoveLabelArgs,
    "batch_add_comment": BatchAddCommentArgs,
    "get_changes": GetChangesArgs,
    "get_metrics": GetMetricsArgs,
    "export_jsonl": ExportJsonlArgs,
    "import_jsonl": ImportJsonlArgs,
    "archive_closed": ArchiveClosedArgs,
    "compact_events": CompactEventsArgs,
    "undo_last": UndoLastArgs,
    "get_issue_events": GetIssueEventsArgs,
    # planning.py
    "add_dependency": AddDependencyArgs,
    "remove_dependency": RemoveDependencyArgs,
    "get_plan": GetPlanArgs,
    "create_plan": CreatePlanArgs,
    "create_plan_from_file": CreatePlanFromFileArgs,
    "label_subtree": LabelSubtreeArgs,
    # workflow.py
    "get_template": GetTemplateArgs,
    "get_type_info": GetTypeInfoArgs,
    "get_valid_transitions": GetValidTransitionsArgs,
    "validate_issue": ValidateIssueArgs,
    "get_workflow_guide": GetWorkflowGuideArgs,
    "explain_status": ExplainStatusArgs,
    # files.py
    "list_files": ListFilesArgs,
    "get_file": GetFileArgs,
    "get_file_timeline": GetFileTimelineArgs,
    "get_issue_files": GetIssueFilesArgs,
    "add_file_association": AddFileAssociationArgs,
    "register_file": RegisterFileArgs,
    "get_finding": GetFindingArgs,
    "list_findings": ListFindingsArgs,
    "update_finding": UpdateFindingArgs,
    "batch_update_findings": BatchUpdateFindingsArgs,
    "promote_finding": PromoteFindingArgs,
    "dismiss_finding": DismissFindingArgs,
    # scanners.py (list_scanners has no args — excluded)
    "trigger_scan": TriggerScanArgs,
    "report_finding": ReportFindingArgs,
    "trigger_scan_batch": TriggerScanBatchArgs,
    "get_scan_status": GetScanStatusArgs,
    "preview_scan": PreviewScanArgs,
    # observations.py
    "observe": ObserveArgs,
    "list_observations": ListObservationsArgs,
    "dismiss_observation": DismissObservationArgs,
    "batch_dismiss_observations": BatchDismissObservationsArgs,
    "promote_observation": PromoteObservationArgs,
}
