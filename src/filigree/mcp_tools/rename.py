"""Canonical MCP tool-name rename map (ADR-016 §7).

Maps each **current** wire name to its **namespaced** successor. The successor
form is ``<entity>_<verb>`` with **no** ``filigree_`` prefix: MCP clients already
surface every tool as ``mcp__filigree__<name>``, so a ``filigree_`` prefix would
duplicate the server token (``mcp__filigree__filigree_finding_list``). Server
identity is the client wrapper's job; the entity token (``finding_`` / ``file_``
/ ``issue_``) does the intra-server disambiguation this rename exists for.

This module is **data + a derived inverse only**, and it **is** consumed by
``mcp_server`` (Phase-1 aliasing — see
``docs/plans/2026-06-02-mcp-tool-namespacing-rename-plan.md`` §5):

* the canonicalize-at-top resolve step in ``call_tool`` maps an inbound new
  name to its canonical (old) name via ``NEW_TO_OLD``, keeping every downstream
  guard/dispatch valid;
* ``list_tools`` renames the served ``Tool.name`` via ``RENAME_MAP``;
* a deprecation-telemetry counter records inbound calls that arrive under an
  old name so the cutover can be tracked.

It remains the frozen, CI-validated source of truth so the agreed names cannot
drift. ``RENAME_MAP`` is exposed as a read-only ``MappingProxyType`` so the
table cannot be mutated at runtime, and **injectivity** is enforced by an
import-time assert below (a structural guard, not test-only). The remaining
invariants — **total coverage** of the live handler set and **no-shadow** (no
new name equals any current name) — need the server's handler set and are
asserted in ``tests/mcp/test_rename_map.py`` against ``mcp_server._all_handlers``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType

#: current wire name -> namespaced successor (``<entity>_<verb>``, no prefix).
_RENAME_MAP_DATA: dict[str, str] = {
    # issue (15)
    "get_issue": "issue_get",
    "list_issues": "issue_list",
    "search_issues": "issue_search",
    "create_issue": "issue_create",
    "update_issue": "issue_update",
    "close_issue": "issue_close",
    "reopen_issue": "issue_reopen",
    "delete_issue": "issue_delete",
    "validate_issue": "issue_validate",
    "batch_close": "issue_batch_close",
    "batch_update": "issue_batch_update",
    "get_issue_files": "issue_file_list",
    "get_issue_events": "issue_event_list",
    "get_issue_annotations": "issue_annotation_list",
    "label_subtree": "issue_subtree_label",
    # work — claim/lease lifecycle + ready/blocked queue (11)
    "get_ready": "work_ready",
    "get_blocked": "work_blocked",
    "start_work": "work_start",
    "start_next_work": "work_start_next",
    "claim_issue": "work_claim",
    "claim_next": "work_claim_next",
    "reclaim_issue": "work_reclaim",
    "release_claim": "work_release",
    "release_my_claims": "work_release_mine",
    "heartbeat_work": "work_heartbeat",
    "get_stale_claims": "work_stale_list",
    # dependency (3)
    "add_dependency": "dependency_add",
    "remove_dependency": "dependency_remove",
    "get_critical_path": "dependency_critical_path",
    # plan (7)
    "create_plan": "plan_create",
    "create_plan_from_file": "plan_create_from_file",
    "get_plan": "plan_get",
    "add_plan_step": "plan_step_add",
    "move_plan_step": "plan_step_move",
    "label_plan_tree": "plan_label_tree",
    "retarget_plan_dependency": "plan_dependency_retarget",
    # label (6)
    "add_label": "label_add",
    "remove_label": "label_remove",
    "list_labels": "label_list",
    "get_label_taxonomy": "label_taxonomy_get",
    "batch_add_label": "label_batch_add",
    "batch_remove_label": "label_batch_remove",
    # comment (3)
    "add_comment": "comment_add",
    "get_comments": "comment_list",
    "batch_add_comment": "comment_batch_add",
    # file (7)
    "list_files": "file_list",
    "get_file": "file_get",
    "register_file": "file_register",
    "add_file_association": "file_association_add",
    "delete_file_record": "file_delete",
    "get_file_timeline": "file_timeline_get",
    "get_file_annotations": "file_annotation_list",
    # finding (8)
    "list_findings": "finding_list",
    "get_finding": "finding_get",
    "dismiss_finding": "finding_dismiss",
    "promote_finding": "finding_promote",
    "promote_finding_and_attach_entity": "finding_promote_and_attach_entity",
    "update_finding": "finding_update",
    "batch_update_findings": "finding_batch_update",
    "report_finding": "finding_report",
    # annotation (11)
    "annotate_file": "annotation_create",
    "carry_forward_annotation": "annotation_carry_forward",
    "get_annotation": "annotation_get",
    "link_annotation": "annotation_link",
    "unlink_annotation": "annotation_unlink",
    "list_annotations": "annotation_list",
    "list_attention_annotations": "annotation_attention_list",
    "promote_annotation": "annotation_promote",
    "resolve_annotation": "annotation_resolve",
    "supersede_annotation": "annotation_supersede",
    "update_annotation": "annotation_update",
    # observation (9)
    "observe": "observation_create",
    "list_observations": "observation_list",
    "dismiss_observation": "observation_dismiss",
    "promote_observation": "observation_promote",
    "promote_observations_to_issue": "observation_promote_to_issue",
    "link_observation": "observation_link",
    "batch_dismiss_observations": "observation_batch_dismiss",
    "batch_link_observations": "observation_batch_link",
    "batch_promote_observations": "observation_batch_promote",
    # entity association — cross-product federation (4)
    "add_entity_association": "entity_association_add",
    "remove_entity_association": "entity_association_remove",
    "list_entity_associations": "entity_association_list",
    "list_associations_by_entity": "entity_association_list_by_entity",
    # scanner (4) + scan (4)
    "list_scanners": "scanner_list",
    "list_available_scanners": "scanner_available_list",
    "enable_scanner": "scanner_enable",
    "disable_scanner": "scanner_disable",
    "get_scan_status": "scan_status_get",
    "preview_scan": "scan_preview",
    "trigger_scan": "scan_trigger",
    "trigger_scan_batch": "scan_trigger_batch",
    # prompt pack (1) + change feed (1)
    "list_prompt_packs": "prompt_pack_list",
    "get_changes": "change_list",
    # introspection: template / type / pack / schema / workflow (9)
    "get_template": "template_get",
    "get_type_info": "type_get",
    "list_types": "type_list",
    "list_packs": "pack_list",
    "get_schema": "schema_get",
    "get_workflow_statuses": "workflow_status_list",
    "get_valid_transitions": "workflow_transition_list",
    "explain_status": "workflow_status_explain",
    "get_workflow_guide": "workflow_guide_get",
    # diagnostics / project aggregates (5)
    "get_stats": "stats_get",
    "get_summary": "summary_get",
    "get_metrics": "metrics_get",
    "get_mcp_status": "mcp_status_get",
    "session_context": "session_context_get",
    # admin — mutating maintenance (7)
    "archive_closed": "admin_archive_closed",
    "compact_events": "admin_compact_events",
    "export_jsonl": "admin_export_jsonl",
    "import_jsonl": "admin_import_jsonl",
    "undo_last": "admin_undo_last",
    "restart_dashboard": "admin_restart_dashboard",
    "reload_templates": "admin_reload_templates",
}

# Injectivity is load-bearing: NEW_TO_OLD inverts the map by comprehension, so a
# repeated successor would silently drop a row and mis-resolve an inbound new
# name. Enforce it at import so the invariant is structural, not test-only — a
# bare ``assert`` would be stripped under ``python -O``, so raise explicitly.
if len(set(_RENAME_MAP_DATA.values())) != len(_RENAME_MAP_DATA):
    _dupes = sorted(name for name, count in Counter(_RENAME_MAP_DATA.values()).items() if count > 1)
    _msg = f"RENAME_MAP successors must be injective (no two current names may share a namespaced successor); collisions: {_dupes}"
    raise RuntimeError(_msg)

#: Read-only view of the rename table. Exposed as a ``MappingProxyType`` so the
#: frozen-source-of-truth contract is enforced structurally — attempts to mutate
#: it raise ``TypeError`` rather than silently drifting the agreed names.
RENAME_MAP: Mapping[str, str] = MappingProxyType(_RENAME_MAP_DATA)

#: Derived inverse: namespaced name -> current canonical name. Consumed by the
#: canonicalize-at-top resolve step in ``call_tool`` (plan §5.1). Built from
#: ``RENAME_MAP``; the injectivity guard above guarantees no value collision
#: silently drops a row, making this a true bijection.
NEW_TO_OLD: dict[str, str] = {new: old for old, new in RENAME_MAP.items()}
