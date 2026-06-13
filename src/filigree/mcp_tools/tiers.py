"""Central tier classification for the MCP tool surface (single source of truth).

There are ~114 flat MCP tools, all equal-weight. ToolSearch ranks deferred
tools by keyword match against tool *name* + *description*, so an agent
struggles to surface the handful it actually needs from the full catalogue.

This module assigns every tool a discoverability **tier** without renaming
anything (renames are a separate, deferred breaking change). The tier is:

- ``core``   — the small set an agent reaches for constantly (find/claim/
               read/mutate/close work plus the primary discovery entrypoints).
- ``common`` — regular but not every-session (comments, labels, plans,
               files, metrics, schema/template introspection).
- ``niche``  — rare / admin / batch / federation / scanner / annotation
               internals.

The tier is applied centrally in ``mcp_server`` by appending a compact,
uniform marker (``[tier: <tier>]``) to each tool's description, and surfaced
as a curated catalogue by ``get_workflow_guide``.

This is a **leaf** module: it must not import ``mcp_server`` or any tool
module (that would create an import cycle). It only holds data + a lookup.
"""

from __future__ import annotations

from typing import Literal

Tier = Literal["core", "common", "niche"]

#: The handful of tools an agent uses constantly. Kept deliberately small.
_CORE: frozenset[str] = frozenset(
    {
        "get_ready",
        "get_blocked",
        "start_work",
        "start_next_work",
        "create_issue",
        "update_issue",
        "close_issue",
        "get_issue",
        "list_issues",
        "search_issues",
        "add_comment",
        "session_context",
    }
)

#: Regular-but-not-every-session tools.
_COMMON: frozenset[str] = frozenset(
    {
        # discovery / introspection entrypoints
        "get_workflow_guide",
        "get_mcp_status",
        "get_valid_transitions",
        "explain_status",
        "get_stats",
        "get_summary",
        "get_metrics",
        # issue lifecycle (regular but secondary)
        "reopen_issue",
        "delete_issue",
        "validate_issue",
        "get_comments",
        # labels
        "add_label",
        "remove_label",
        "list_labels",
        "get_label_taxonomy",
        # dependencies & planning
        "add_dependency",
        "remove_dependency",
        "get_critical_path",
        "create_plan",
        "get_plan",
        "add_plan_step",
        # files
        "list_files",
        "get_file",
        "register_file",
        "get_issue_files",
        "list_findings",
        "get_finding",
        # events / history
        "get_changes",
        "get_issue_events",
        # templates / types / packs
        "get_template",
        "get_type_info",
        "list_types",
        "list_packs",
        "get_schema",
        "get_workflow_statuses",
    }
)


# Every other registered tool falls into ``niche`` via ``_DEFAULT_TIER``. We
# enumerate the niche set explicitly anyway so the completeness test pins the
# full taxonomy and a reviewer can see the intended classification at a glance.
_NICHE: frozenset[str] = frozenset(
    {
        # claim primitives (the atomic start_* verbs are core; these are the
        # niche claim-only / lease-management path)
        "claim_issue",
        "claim_next",
        "reclaim_issue",
        "release_claim",
        "release_my_claims",
        "heartbeat_work",
        "get_stale_claims",
        # batch operations
        "batch_close",
        "batch_update",
        "batch_add_comment",
        "batch_add_label",
        "batch_remove_label",
        "batch_update_findings",
        "batch_dismiss_observations",
        "batch_link_observations",
        "batch_promote_observations",
        # admin / maintenance
        "archive_closed",
        "compact_events",
        "checkpoint_db",
        "export_jsonl",
        "import_jsonl",
        "undo_last",
        "restart_dashboard",
        "reload_templates",
        "list_reconciliation_debt",
        # planning internals
        "create_plan_from_file",
        "move_plan_step",
        "label_plan_tree",
        "label_subtree",
        "retarget_plan_dependency",
        # files internals
        "add_file_association",
        "delete_file_record",
        "get_file_timeline",
        "dismiss_finding",
        "promote_finding",
        "promote_finding_and_attach_entity",
        "update_finding",
        # annotations (whole subsystem is niche)
        "annotate_file",
        "carry_forward_annotation",
        "get_annotation",
        "get_file_annotations",
        "get_issue_annotations",
        "link_annotation",
        "list_annotations",
        "list_attention_annotations",
        "promote_annotation",
        "resolve_annotation",
        "supersede_annotation",
        "unlink_annotation",
        "update_annotation",
        # observations
        "observe",
        "list_observations",
        "dismiss_observation",
        "promote_observation",
        "promote_observations_to_issue",
        "link_observation",
        # entity associations (cross-product federation)
        "add_entity_association",
        "remove_entity_association",
        "list_entity_associations",
        "list_associations_by_entity",
        # federation consumer bindings
        "ingest_warpline_worklist",
        # scanners
        "list_scanners",
        "list_available_scanners",
        "list_prompt_packs",
        "enable_scanner",
        "disable_scanner",
        "get_scan_status",
        "preview_scan",
        "trigger_scan",
        "trigger_scan_batch",
        "report_finding",
    }
)

#: Fallback for any tool not explicitly classified. The *tag* code uses this
#: defensively so a newly-added, un-classified tool never crashes the server
#: at import. The completeness test, by contrast, asserts strict membership in
#: ``TIER_MAP`` so CI still fails loudly when a new tool is added without a
#: deliberate tier.
_DEFAULT_TIER: Tier = "niche"

#: Single source of truth: tool name -> tier.
TIER_MAP: dict[str, Tier] = {}
for _name in _CORE:
    TIER_MAP[_name] = "core"
for _name in _COMMON:
    TIER_MAP[_name] = "common"
for _name in _NICHE:
    TIER_MAP[_name] = "niche"


def tier_for(tool_name: str) -> Tier:
    """Return the discoverability tier for ``tool_name``.

    Unknown tools fall back to :data:`_DEFAULT_TIER` (``niche``) so the
    server never fails to start on an untiered tool; CI guards against
    silent misses via the completeness test.
    """

    return TIER_MAP.get(tool_name, _DEFAULT_TIER)
