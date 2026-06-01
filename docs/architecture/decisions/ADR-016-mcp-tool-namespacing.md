# ADR-016: MCP Tool-Name Namespacing Convention

**Status**: Accepted
**Date**: 2026-06-02
**Deciders**: John (project lead) — ratified **D2 = flat** and **D5 = drop the
`filigree_` prefix** on 2026-06-02; a 3-agent poll resolved D1/D3/D4/D6/D7/D8/D9
(§6).

> ⚠ **Convention update vs the issue text.** `filigree-7771610917` framed the
> target as `filigree_finding_list`, `filigree_file_get` (with prefix). That is
> superseded: clients already surface every tool as `mcp__filigree__<name>`, so
> a `filigree_` prefix yields `mcp__filigree__filigree_finding_list` — the
> server token twice. The ratified convention therefore **drops `filigree_`**:
> the tool name is `<entity>_<verb>` (e.g. `finding_list`, `issue_get`), and the
> client wrapper supplies server identity. The **§7 final map is authoritative**;
> the §5 tables retain the prefixed draft as the reasoning record.
**Context**: `filigree-7771610917` (rename the ~114 flat MCP tools to a
subsystem-namespaced convention) under epic `filigree-18bd3b8c98`. Companion to
the rollout plan `docs/plans/2026-06-02-mcp-tool-namespacing-rename-plan.md`.
This ADR fixes the **naming convention** the curated `RENAME_MAP` is built
against; the plan covers phasing, aliasing seam, telemetry, and consumer
coordination.

## Summary

Adopt **`filigree_<entity>_<verb>`** as the MCP tool-name convention, with a
small set of explicit sub-rules for relationship reads, batch ops, sub-entities,
and the nounless maintenance/diagnostic verbs that have no domain entity. The
draft + reasoning for all 114 tools is in §5; the **authoritative final map is
§7**. All 9 open questions are resolved: a 3-agent poll on 2026-06-02 (§6)
settled 7 (D2 split 2–1), and the project lead ratified **D2 = flat** and
**D5 = drop the `filigree_` prefix**. The catalogue stays at **114 tools** —
aliasing renames, it does not add (§7.1).

## Decision

### Convention rules

- **R1 — Primary entity ops:** `filigree_<entity>_<verb>`, entity **singular**.
  `get_issue` → `filigree_issue_get`; `update_finding` → `filigree_finding_update`.
- **R2 — List collapse:** a plural-returning verb becomes `_list`.
  `list_issues` → `filigree_issue_list`; `get_comments` → `filigree_comment_list`.
- **R3 — Relationship/scoped reads:** `filigree_<anchor>_<related>_list`, where
  the *anchor* entity is the namespace. `get_issue_files` →
  `filigree_issue_file_list`; `get_file_annotations` → `filigree_file_annotation_list`.
  This is what keeps a scoped read (`files of an issue`) distinct from the
  global list (`list_files` → `filigree_file_list`).
- **R4 — Batch ops:** `filigree_<entity>_batch_<verb>`.
  `batch_close` → `filigree_issue_batch_close`.
- **R5 — Sub-entities** get a second noun segment before the verb.
  `add_plan_step` → `filigree_plan_step_add`.
- **R6 — Pseudo-namespaces for non-domain ops:** maintenance/diagnostic tools
  with no domain entity group under a stable pseudo-namespace —
  `admin_` (mutating maintenance), `project_` (read-only project aggregates),
  `mcp_` / `session_` (server/agent diagnostics).
- **R7 — Nounless lifecycle verbs** (claim/lease + ready/blocked queue) group
  under a single lifecycle namespace — proposed `work_` (see **D1**).

### Hard invariants (CI-enforced — see plan §5.5)

1. **Injective:** no two old names map to one new name.
2. **No shadow:** no new name equals any existing old name.
3. **Total:** every key in the live `_all_handlers` set has exactly one row.

The map is a **curated artifact reviewed row-by-row**, never derived from
`_tool_subsystem` (the owning *module* is not the target namespace — e.g.
`list_findings` lives in `files.py` but its namespace is `finding`).

### The `filigree_` prefix

Kept per the issue, though clients already surface tools as
`mcp__filigree__<name>` (so the literal prefix is partly redundant for
cross-server collision). The load-bearing win is **intra-server disambiguation**
(`finding_` vs `file_` vs `issue_`). Recorded here because a reviewer will ask;
dropping the prefix is **D5**.

## 5. Draft RENAME_MAP (all 114 tools)

Legend: **✓** settled by rules R1–R7 · **D#** blocked on the matching §6 decision.

### `issue` (lifecycle + CRUD)
| Old | Proposed new | |
|-----|--------------|--|
| `get_issue` | `filigree_issue_get` | ✓ |
| `list_issues` | `filigree_issue_list` | ✓ |
| `search_issues` | `filigree_issue_search` | ✓ |
| `create_issue` | `filigree_issue_create` | ✓ |
| `update_issue` | `filigree_issue_update` | ✓ |
| `close_issue` | `filigree_issue_close` | ✓ |
| `reopen_issue` | `filigree_issue_reopen` | ✓ |
| `delete_issue` | `filigree_issue_delete` | ✓ |
| `validate_issue` | `filigree_issue_validate` | ✓ |
| `batch_close` | `filigree_issue_batch_close` | ✓ (R4) |
| `batch_update` | `filigree_issue_batch_update` | ✓ (R4) |
| `get_issue_files` | `filigree_issue_file_list` | ✓ (R3) |
| `get_issue_events` | `filigree_issue_event_list` | ✓ (R3) |
| `get_issue_annotations` | `filigree_issue_annotation_list` | ✓ (R3) |

### `work` (claim/lease lifecycle + ready/blocked queue) — ✓ D1 resolved (poll 3–0)
| Old | Proposed new | |
|-----|--------------|--|
| `get_ready` | `filigree_work_ready` | **D1** |
| `get_blocked` | `filigree_work_blocked` | **D1** |
| `start_work` | `filigree_work_start` | **D1** |
| `start_next_work` | `filigree_work_start_next` | **D1** |
| `claim_issue` | `filigree_work_claim` | **D1** |
| `claim_next` | `filigree_work_claim_next` | **D1** |
| `reclaim_issue` | `filigree_work_reclaim` | **D1** |
| `release_claim` | `filigree_work_release` | **D1** |
| `release_my_claims` | `filigree_work_release_mine` | **D1** |
| `heartbeat_work` | `filigree_work_heartbeat` | **D1** |
| `get_stale_claims` | `filigree_work_stale_list` | **D1** |

### `dependency`
| Old | Proposed new | |
|-----|--------------|--|
| `add_dependency` | `filigree_dependency_add` | ✓ |
| `remove_dependency` | `filigree_dependency_remove` | ✓ |
| `get_critical_path` | `filigree_dependency_critical_path` | **D7** (vs `issue_critical_path`) |

### `plan`
| Old | Proposed new | |
|-----|--------------|--|
| `create_plan` | `filigree_plan_create` | ✓ |
| `create_plan_from_file` | `filigree_plan_create_from_file` | ✓ |
| `get_plan` | `filigree_plan_get` | ✓ |
| `add_plan_step` | `filigree_plan_step_add` | ✓ (R5) |
| `move_plan_step` | `filigree_plan_step_move` | ✓ (R5) |
| `label_plan_tree` | `filigree_plan_label_tree` | ✓ |
| `label_subtree` | `filigree_issue_subtree_label` | ✓ D7b→issue (poll 3–0; labels an issue subtree, not a plan) |
| `retarget_plan_dependency` | `filigree_plan_dependency_retarget` | ✓ (R5) |

### `label`
| Old | Proposed new | |
|-----|--------------|--|
| `add_label` | `filigree_label_add` | ✓ |
| `remove_label` | `filigree_label_remove` | ✓ |
| `list_labels` | `filigree_label_list` | ✓ |
| `get_label_taxonomy` | `filigree_label_taxonomy_get` | ✓ |
| `batch_add_label` | `filigree_label_batch_add` | ✓ (R4) |
| `batch_remove_label` | `filigree_label_batch_remove` | ✓ (R4) |

### `comment`
| Old | Proposed new | |
|-----|--------------|--|
| `add_comment` | `filigree_comment_add` | ✓ |
| `get_comments` | `filigree_comment_list` | ✓ (R2) |
| `batch_add_comment` | `filigree_comment_batch_add` | ✓ (R4) |

### `file`
| Old | Proposed new | |
|-----|--------------|--|
| `list_files` | `filigree_file_list` | ✓ |
| `get_file` | `filigree_file_get` | ✓ |
| `register_file` | `filigree_file_register` | ✓ |
| `add_file_association` | `filigree_file_association_add` | **D4** (vs `file_associate`) |
| `delete_file_record` | `filigree_file_delete` | ✓ |
| `get_file_timeline` | `filigree_file_timeline_get` | ✓ |

### `finding`
| Old | Proposed new | |
|-----|--------------|--|
| `list_findings` | `filigree_finding_list` | ✓ (canonical example) |
| `get_finding` | `filigree_finding_get` | ✓ |
| `dismiss_finding` | `filigree_finding_dismiss` | ✓ |
| `promote_finding` | `filigree_finding_promote` | ✓ |
| `update_finding` | `filigree_finding_update` | ✓ |
| `batch_update_findings` | `filigree_finding_batch_update` | ✓ (R4) |
| `report_finding` | `filigree_finding_report` | ✓ |

### `annotation`
| Old | Proposed new | |
|-----|--------------|--|
| `annotate_file` | `filigree_annotation_create` | **D6** (vs `file_annotate`) |
| `carry_forward_annotation` | `filigree_annotation_carry_forward` | ✓ |
| `get_annotation` | `filigree_annotation_get` | ✓ |
| `get_file_annotations` | `filigree_file_annotation_list` | ✓ (R3) |
| `get_issue_annotations` | `filigree_issue_annotation_list` | ✓ (R3, dup of issue row above — same target, single map entry) |
| `link_annotation` | `filigree_annotation_link` | ✓ |
| `unlink_annotation` | `filigree_annotation_unlink` | ✓ |
| `list_annotations` | `filigree_annotation_list` | ✓ |
| `list_attention_annotations` | `filigree_annotation_attention_list` | ✓ |
| `promote_annotation` | `filigree_annotation_promote` | ✓ |
| `resolve_annotation` | `filigree_annotation_resolve` | ✓ |
| `supersede_annotation` | `filigree_annotation_supersede` | ✓ |
| `update_annotation` | `filigree_annotation_update` | ✓ |

### `observation`
| Old | Proposed new | |
|-----|--------------|--|
| `observe` | `filigree_observation_create` | **D3** (vs keep iconic `observe`) |
| `list_observations` | `filigree_observation_list` | ✓ |
| `dismiss_observation` | `filigree_observation_dismiss` | ✓ |
| `promote_observation` | `filigree_observation_promote` | ✓ |
| `promote_observations_to_issue` | `filigree_observation_promote_to_issue` | ✓ |
| `link_observation` | `filigree_observation_link` | ✓ |
| `batch_dismiss_observations` | `filigree_observation_batch_dismiss` | ✓ (R4) |
| `batch_link_observations` | `filigree_observation_batch_link` | ✓ (R4) |
| `batch_promote_observations` | `filigree_observation_batch_promote` | ✓ (R4) |

### `entity` (cross-product associations, ADR-029)
| Old | Proposed new | |
|-----|--------------|--|
| `add_entity_association` | `filigree_entity_association_add` | **D4** |
| `remove_entity_association` | `filigree_entity_association_remove` | **D4** |
| `list_entity_associations` | `filigree_entity_association_list` | **D4** |
| `list_associations_by_entity` | `filigree_entity_association_list_by_entity` | **D4** |

### `scanner` (definitions/registry) and `scan` (runs)
| Old | Proposed new | |
|-----|--------------|--|
| `list_scanners` | `filigree_scanner_list` | ✓ |
| `list_available_scanners` | `filigree_scanner_available_list` | ✓ |
| `enable_scanner` | `filigree_scanner_enable` | ✓ |
| `disable_scanner` | `filigree_scanner_disable` | ✓ |
| `list_prompt_packs` | `filigree_prompt_pack_list` | ✓ D8→top-level (poll 3–0) |
| `get_scan_status` | `filigree_scan_status_get` | ✓ |
| `preview_scan` | `filigree_scan_preview` | ✓ |
| `trigger_scan` | `filigree_scan_trigger` | ✓ |
| `trigger_scan_batch` | `filigree_scan_trigger_batch` | ✓ |

### `event` (history)
| Old | Proposed new | |
|-----|--------------|--|
| `get_changes` | `filigree_change_list` | **D7** (federation `/changes` feed — keep `change` vs fold into `event`) |

> Note: `get_issue_events` is namespaced under `issue` (R3) above. There is no
> standalone global event-list tool, so no `event` namespace is created here.

### Introspection: `template` / `type` / `pack` / `schema` / `status` / `transition` / `workflow`
| Old | Proposed new | |
|-----|--------------|--|
| `get_template` | `filigree_template_get` | ✓ |
| `get_type_info` | `filigree_type_get` | ✓ |
| `list_types` | `filigree_type_list` | ✓ |
| `list_packs` | `filigree_pack_list` | ✓ |
| `get_schema` | `filigree_schema_get` | ✓ |
| `get_workflow_statuses` | `filigree_workflow_status_list` | ✓ D9→nested (poll 3–0) |
| `get_valid_transitions` | `filigree_workflow_transition_list` | ✓ D9→nested |
| `explain_status` | `filigree_workflow_status_explain` | ✓ D9→nested (avoids `status` colliding w/ `scan_status`/`mcp_status`) |
| `get_workflow_guide` | `filigree_workflow_guide_get` | ✓ |

### Diagnostics / project aggregates (R6 pseudo-namespaces)
| Old | Proposed new | |
|-----|--------------|--|
| `get_stats` | `filigree_stats_get` | ✓ D2→flatter (poll 2–1) |
| `get_summary` | `filigree_summary_get` | ✓ D2→flatter |
| `get_metrics` | `filigree_metrics_get` | ✓ D2→flatter |
| `get_mcp_status` | `filigree_mcp_status_get` | ✓ |
| `session_context` | `filigree_session_context_get` | ✓ |

### `admin` (mutating maintenance, R6)
| Old | Proposed new | |
|-----|--------------|--|
| `archive_closed` | `filigree_admin_archive_closed` | ✓ |
| `compact_events` | `filigree_admin_compact_events` | ✓ |
| `export_jsonl` | `filigree_admin_export_jsonl` | ✓ |
| `import_jsonl` | `filigree_admin_import_jsonl` | ✓ |
| `undo_last` | `filigree_admin_undo_last` | ✓ |
| `restart_dashboard` | `filigree_admin_restart_dashboard` | ✓ |
| `reload_templates` | `filigree_admin_reload_templates` | ✓ |

**Coverage check:** 14 + 11 + 3 + 8 + 6 + 3 + 6 + 7 + 13 + 9 + 4 + 9 + 1 + 9 + 5 + 7
= **114** = `len(_all_handlers)`. Injectivity holds across the proposed targets
(verify in CI once D-rows resolve).

## 6. Decision resolution — agent poll (2026-06-02)

The 9 questions were put to **3 independent agents** (identical neutral prompt,
no "recommended" labels), each deciding from the standpoint of an AI agent that
selects tools by fuzzy-matching name+description. Outcome below. **8 of 9 were
unanimous; D2 split 2–1.** The §5 tables above carry the resolved forms. This
section is advisory — **final ratification rests with the project lead**; the
two soft spots (D2, D5) are flagged for explicit sign-off.

| # | Resolution | Vote | Note |
|---|------------|------|------|
| **D1** | `work_` namespace; `get_ready`/`get_blocked` under `work_` | 3–0 | "work/queue" is the session-start mental model; claim is a subset. |
| **D2** | **flatter** — `filigree_stats_get` / `_summary_get` / `_metrics_get` (drop `project_`) | 2–1 | No `project` entity exists, so the prefix adds a segment without discrimination. *Overturns the original `project_` recommendation.* **Soft — confirm.** |
| **D3** | `filigree_observation_create` | 3–0 | Groups with the `observation_*` cluster; lone `observe` would orphan it. |
| **D4** | `_association_add/_remove/_list` (noun+verb), `file` + `entity` | 3–0 | `associate`/`dissociate` splits the family and is a spelling trap. |
| **D5** | **DROP** `filigree_` → names are `<entity>_<verb>` | poll 3–0 *keep, but overridden by project lead* | Client already prefixes `mcp__filigree__`, so `filigree_` duplicates the server token (`mcp__filigree__filigree_…`). Intra-server disambiguation lives in the entity token (`finding_`/`file_`/`issue_`); cross-server identity is the client wrapper's job. **Ratified: drop.** |
| **D6** | `filigree_annotation_create` | 3–0 | Annotation is an 11-tool cluster; `file` is just a param. |
| **D7a** | `get_critical_path` → **`dependency`** (`filigree_dependency_critical_path`) | 3–0 | Computation over the dependency DAG; joins add/remove_dependency. |
| **D7b** | `label_subtree` → **`issue`** (`filigree_issue_subtree_label`) | 3–0 | Labels an *issue* subtree; `label_plan_tree` keeps `plan`. *Overturns the original `plan` draft.* |
| **D7c** | `get_changes` → **own `change`** namespace (`filigree_change_list`) | 3–0 | Federation feed ≠ per-issue `event` audit log; own prefix routes federation consumers. |
| **D8** | **top-level** `filigree_prompt_pack_list` (drop `scanner_`) | 3–0 | An agent thinking "prompt pack" won't prefix-guess "scanner". *Overturns the original `scanner_` draft.* |
| **D9** | **nested** `filigree_workflow_status_list` / `_transition_list` / `_status_explain` | 3–0 | Clusters with `workflow_guide`; bare `status_` collides with `scan_status`/`mcp_status`. *Overturns the original bare-namespace draft.* |

**Post-resolution validation:** the consensus map was re-checked against the
three hard invariants — 114/114 total coverage, injective, no-shadow — **all
PASS** (re-run after applying the D2/D7b/D8/D9 changes).

**Residual micro-decision (non-blocking):** D7a name form — `filigree_dependency_critical_path`
(majority) vs `…_critical_path_get` (one agent, for verb-suffix uniformity).
Recommend the majority form; the trailing noun reads as a read already.

D2 + D5 are ratified (header). The §7 map is frozen as the code-level
`RENAME_MAP` SSOT input; Phase-1 aliasing (plan §5) can start.

## 7. Final RENAME_MAP (authoritative — prefix dropped, all 114)

`<entity>_<verb>`, no `filigree_` prefix (D5). Validated: 114/114 total coverage
of live `_all_handlers`, injective, no-shadow (no new name equals any old name).
This block is the literal SSOT input for `src/filigree/mcp_tools/rename.py`.

```python
RENAME_MAP = {
    # issue (15)
    "get_issue": "issue_get", "list_issues": "issue_list", "search_issues": "issue_search",
    "create_issue": "issue_create", "update_issue": "issue_update", "close_issue": "issue_close",
    "reopen_issue": "issue_reopen", "delete_issue": "issue_delete", "validate_issue": "issue_validate",
    "batch_close": "issue_batch_close", "batch_update": "issue_batch_update",
    "get_issue_files": "issue_file_list", "get_issue_events": "issue_event_list",
    "get_issue_annotations": "issue_annotation_list", "label_subtree": "issue_subtree_label",
    # work — claim/lease + queue (11)
    "get_ready": "work_ready", "get_blocked": "work_blocked", "start_work": "work_start",
    "start_next_work": "work_start_next", "claim_issue": "work_claim", "claim_next": "work_claim_next",
    "reclaim_issue": "work_reclaim", "release_claim": "work_release", "release_my_claims": "work_release_mine",
    "heartbeat_work": "work_heartbeat", "get_stale_claims": "work_stale_list",
    # dependency (3)
    "add_dependency": "dependency_add", "remove_dependency": "dependency_remove",
    "get_critical_path": "dependency_critical_path",
    # plan (7)
    "create_plan": "plan_create", "create_plan_from_file": "plan_create_from_file", "get_plan": "plan_get",
    "add_plan_step": "plan_step_add", "move_plan_step": "plan_step_move", "label_plan_tree": "plan_label_tree",
    "retarget_plan_dependency": "plan_dependency_retarget",
    # label (6)
    "add_label": "label_add", "remove_label": "label_remove", "list_labels": "label_list",
    "get_label_taxonomy": "label_taxonomy_get", "batch_add_label": "label_batch_add",
    "batch_remove_label": "label_batch_remove",
    # comment (3)
    "add_comment": "comment_add", "get_comments": "comment_list", "batch_add_comment": "comment_batch_add",
    # file (7)
    "list_files": "file_list", "get_file": "file_get", "register_file": "file_register",
    "add_file_association": "file_association_add", "delete_file_record": "file_delete",
    "get_file_timeline": "file_timeline_get", "get_file_annotations": "file_annotation_list",
    # finding (7)
    "list_findings": "finding_list", "get_finding": "finding_get", "dismiss_finding": "finding_dismiss",
    "promote_finding": "finding_promote", "update_finding": "finding_update",
    "batch_update_findings": "finding_batch_update", "report_finding": "finding_report",
    # annotation (11)
    "annotate_file": "annotation_create", "carry_forward_annotation": "annotation_carry_forward",
    "get_annotation": "annotation_get", "link_annotation": "annotation_link",
    "unlink_annotation": "annotation_unlink", "list_annotations": "annotation_list",
    "list_attention_annotations": "annotation_attention_list", "promote_annotation": "annotation_promote",
    "resolve_annotation": "annotation_resolve", "supersede_annotation": "annotation_supersede",
    "update_annotation": "annotation_update",
    # observation (9)
    "observe": "observation_create", "list_observations": "observation_list",
    "dismiss_observation": "observation_dismiss", "promote_observation": "observation_promote",
    "promote_observations_to_issue": "observation_promote_to_issue", "link_observation": "observation_link",
    "batch_dismiss_observations": "observation_batch_dismiss", "batch_link_observations": "observation_batch_link",
    "batch_promote_observations": "observation_batch_promote",
    # entity (4)
    "add_entity_association": "entity_association_add", "remove_entity_association": "entity_association_remove",
    "list_entity_associations": "entity_association_list",
    "list_associations_by_entity": "entity_association_list_by_entity",
    # scanner (4) + scan (4)
    "list_scanners": "scanner_list", "list_available_scanners": "scanner_available_list",
    "enable_scanner": "scanner_enable", "disable_scanner": "scanner_disable",
    "get_scan_status": "scan_status_get", "preview_scan": "scan_preview",
    "trigger_scan": "scan_trigger", "trigger_scan_batch": "scan_trigger_batch",
    # prompt pack (1) + change (1)
    "list_prompt_packs": "prompt_pack_list", "get_changes": "change_list",
    # introspection: template/type/pack/schema/workflow (9)
    "get_template": "template_get", "get_type_info": "type_get", "list_types": "type_list",
    "list_packs": "pack_list", "get_schema": "schema_get",
    "get_workflow_statuses": "workflow_status_list", "get_valid_transitions": "workflow_transition_list",
    "explain_status": "workflow_status_explain", "get_workflow_guide": "workflow_guide_get",
    # diagnostics / project aggregates (5)
    "get_stats": "stats_get", "get_summary": "summary_get", "get_metrics": "metrics_get",
    "get_mcp_status": "mcp_status_get", "session_context": "session_context_get",
    # admin (7)
    "archive_closed": "admin_archive_closed", "compact_events": "admin_compact_events",
    "export_jsonl": "admin_export_jsonl", "import_jsonl": "admin_import_jsonl",
    "undo_last": "admin_undo_last", "restart_dashboard": "admin_restart_dashboard",
    "reload_templates": "admin_reload_templates",
}
```

### 7.1 Tool-count guarantee — the catalogue does NOT grow

Aliasing renames; it does **not** add tools. `list_tools` advertises **exactly
114** (new names only, plan §5.2). Old names resolve in `call_tool` for the
transition window but are **never in the served catalogue** — so the agent-
visible registry stays at 114, not 228. There is no "300-tool" surface at any
point. Phase 2 deletes the old-name resolution; the served count is unchanged
because old names were never served.

## Consequences

- A ratified map unblocks the curated `RENAME_MAP` SSOT (plan §4) and the
  additive Phase-1 aliasing (plan §5).
- Every D-row left open is a row the implementer cannot code; resolving them is
  the gating step, not the aliasing mechanics.
- The map becomes the input to: alias registration, `_tool_argument_names`,
  `TIER_MAP` re-key, doc generation, and the completeness/injectivity tests.
