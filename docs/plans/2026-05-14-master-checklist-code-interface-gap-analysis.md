# Master Checklist Code and Interface Gap Analysis

Date: 2026-05-14

Scope: `docs/plans/2026-05-13-mcp-senior-user-review-master-checklist.md`
checked against the current repository source, tests, and CLI/MCP surfaces.

Method: inspected the MCP tool registry and handlers, DB mixins, CLI command
help, workflow templates, and focused tests. This is a current implementation
analysis, not a new product decision pass.

## Executive Summary

Several checklist items are implemented but still unchecked in the master list.
The remaining high-risk gaps are concentrated in four places:

1. Strict unknown MCP parameter rejection is not implemented.
2. ADR-008 claim-aware write defaults are not implemented; claim checks remain
   opt-in through `expected_assignee`.
3. `report_finding` still auto-creates paired observations by default, contrary
   to ADR-007.
4. MCP self-discovery/schema metadata is still hand-maintained rather than
   generated from the live tool registry.

The strongest completed areas are schema-mismatch fail-closed behavior,
stale-claim discovery and `release_my_claims`, archived-status hydration,
file-timeline issue events, `get_summary(format="json")`, observation list
filters, and stats field normalization.

## Status Key

- Done: the checklist criterion is met by current code and interface evidence.
- Partial: meaningful work landed, but one or more checklist criteria remain.
- Not done: current behavior still contradicts the locked checklist outcome.
- Interface drift: the MCP and CLI surfaces differ materially.

## P1 Findings

| Checklist item | Current status | Evidence | Remaining work |
|---|---:|---|---|
| Strict unknown MCP parameter rejection | Done | `call_tool()` now derives accepted parameter names from each registered tool's `inputSchema.properties` and rejects unknown keys before handler dispatch (`src/filigree/mcp_server.py`). Regression coverage in `tests/mcp/test_boundary_validation.py` verifies single and multiple unknown keys return `ErrorResponse(code=VALIDATION)` naming the tool and bad parameter(s), while unknown tools still return `NOT_FOUND`. | None. |
| Schema-mismatch hard stop and binary diagnostics | Partial, mostly done | MCP degraded mode short-circuits all normal calls except `get_mcp_status` to `SCHEMA_MISMATCH` (`src/filigree/mcp_server.py:453`). Runtime drift is rechecked on every call (`src/filigree/mcp_server.py:475`). Tests cover warm degraded mode and structured errors (`tests/test_schema_mismatch.py:111`). `get_mcp_status_payload()` includes schema versions and `filigree_dir` (`src/filigree/mcp_server.py:159`). | Add binary/path/source diagnostics for the actual executing binary or venv/tool install pair. Current diagnostic does not identify the executable that produced the warning. |
| Observation triage and session cleanup filters | Done for 2.0 scope | MCP/CLI list filters now cover actor, file, source issue, priority, age, sort, and direction (`src/filigree/mcp_tools/observations.py`; `src/filigree/cli_commands/observations.py`). Schema v13 adds durable `observation_links` snapshots. Core/MCP/CLI expose `link_observation`, `batch_link_observations`, and `promote_observations_to_issue`, with regression coverage in `tests/core/test_observations.py`, `tests/mcp/test_observations.py`, and `tests/cli/test_observations_commands.py`. | Future first-class session/run IDs remain deferred by ADR-011; filter-by-session should be revisited if that model lands. |
| `report_finding` side effects explicit, traceable, slim | Partial, ADR mismatch remains | The surface documents the side effect, has `actor`, `response_detail`, and `observation_id` (`src/filigree/mcp_tools/scanners.py:68`). But `create_observation` defaults to `True` in schema and handler (`src/filigree/mcp_tools/scanners.py:105`; `src/filigree/mcp_tools/scanners.py:355`), and CLI help says a paired observation is auto-created by default. Linked-observation cleanup exists on finding terminal status or promotion (`src/filigree/db_files.py:1059`). | Flip paired observation creation to explicit opt-in, or revise ADR/checklist. Preserve slim response and actor attribution. |
| End-of-session cleanup story for mixed scratch work | Partial | `release_my_claims` exists with actor, label, label-prefix, dry-run, revert-status, and reason (`src/filigree/db_issues.py:1225`; `uv run filigree release-my-claims --help`). `archive` exists with age and label scoping (`uv run filigree archive --help`) and has a tight-window guard in MCP (`src/filigree/mcp_tools/meta.py:867`). | Cleanup is fragmented across release claims, archive, batch observation operations, clean-stale-findings, and delete-file-record. There is no single documented session cleanup recipe spanning claims, observations, findings, files, and scratch issues. |
| Stale-claim and handoff discovery | Done, with one P3 extension left | `get_stale_claims` selects assigned non-done issues and respects claim expiry (`src/filigree/db_issues.py:1371`). `release_my_claims` discovers live claims by actor and skips done-category issues (`src/filigree/db_issues.py:1235`). CLI exposes both `get-stale-claims` and `release-my-claims`. | Only the proactive near-expiry extension remains (`expires_within_hours`), tracked as P3 polish. |

## P2 Findings

| Checklist item | Current status | Evidence | Remaining work |
|---|---:|---|---|
| Claim-aware writes hard to misuse | Not done | `_check_expected_assignee()` explicitly skips the check when `expected_assignee is None` (`src/filigree/db_issues.py:228`). MCP docs say omitted `expected_assignee` preserves write-anywhere behavior (`src/filigree/mcp_tools/issues.py:198`). Tests assert a different actor can update a held issue when the precondition is omitted (`tests/core/test_workflow_behavior.py:984`). | Implement ADR-008: when `actor` is present and issue is held, default expected holder to `actor`; require explicit override for cross-claim writes. |
| Live-work search and catch-up filters | Partial and interface-drifted | Search supports bracket/hyphen literal fallback and `status_category` (`src/filigree/db_issues.py:2015`; `uv run filigree search --help`). MCP `get_changes` supports actor, issue, label, type, after-event-id, and heartbeat exclusion (`src/filigree/mcp_tools/meta.py:252`). CLI `get-changes` only exposes `since`, `limit`, and `after-event-id` (`src/filigree/cli_commands/planning.py:472`). | Add multi-value filters if still desired, and bring CLI catch-up filters to parity with MCP. |
| MCP self-discovery and docs generated from live registry | Not done | `get_schema` hand-codes `accepted_by_tools` lists in `src/filigree/mcp_tools/workflow.py:195`. Tests check only presence of selected values, not registry-derived completeness (`tests/mcp/test_tools.py:1367`). | Generate `accepted_by_tools`, docs counts, and schema metadata from the live registry, then pin drift tests. |
| ID and relationship naming consistency | Partial | Public issue payload emits both `parent_id` and `parent_issue_id` for compatibility (`src/filigree/issue_payloads.py:22`). Dependency tools use `from_issue_id`/`to_issue_id` (`src/filigree/mcp_tools/planning.py:72`). | Complete deprecation story for legacy `parent_id` and any remaining mixed naming in CLI/API docs. |
| Common response envelopes and slim paths | Partial | List envelopes are common (`src/filigree/mcp_tools/common.py:101`), batch envelopes are common, stats include canonical and compatibility keys (`src/filigree/db_meta.py:404`). `get_valid_transitions` still returns a bare array (`src/filigree/mcp_tools/workflow.py:335`), and `get_plan` has no `response_detail` or slim mode (`src/filigree/mcp_tools/planning.py:104`). | Normalize remaining bare arrays and verbose full-record defaults where ADR-009 calls for slim/default plus `response_detail`. |
| Workflow-template semantics and soft enforcement | Partial | Transition validation, warnings, close reason, reopen cleanup, and start-work behavior have substantial implementation. Example: `release_claim` reverts WIP to an open predecessor (`src/filigree/db_issues.py:1122`), and transition warnings flow through `data_warnings`. | Still needs a single documented semantic contract for template defaults, warning channels, reopen targets, and transition field meanings. |
| Archive and done-status model | Done | Archived status resolves as done when not declared by active templates (`src/filigree/db_workflow.py:250`). `archive_closed` selects done-category records only and writes `archived` (`src/filigree/db_events.py:407`). `stats --json` counts `archived` under `status_category_counts.done`. Stale claims exclude done-category rows (`src/filigree/db_issues.py:1379`). | The tracker/checklist can be marked complete unless a docs-only follow-up is desired. |
| Plan editing and plan-read ergonomics | Partial | High-level plan operations exist: `create_plan_from_file`, `add_plan_step`, `retarget_plan_dependency`, `move_plan_step`, `label_plan_tree`, and `label_subtree` (`src/filigree/mcp_tools/planning.py:53`). Plan dependency reference validation rejects ambiguous JSON values (`src/filigree/mcp_tools/planning.py:371`). | `get_plan` still has no slim/full mode; move operations do not appear to warn about surprising dependency carry-forward; no dry-run compaction preview was found. |
| Close, dismiss, and reason semantics | Partial | Issue close reasons are folded into `fields.close_reason`; finding dismissal accepts status and reason metadata (`src/filigree/mcp_tools/files.py:718`). `dismiss_finding` defaults to `false_positive` (`src/filigree/mcp_tools/files.py:245`). Stale finding cleanup exists (`clean-stale-findings`). | Decide/document finding dismissal defaults and archival/expiry semantics; unify reason presentation for history consumers. |
| Hydrate blockers/context for one-call triage | Not done | `get_blocked` schema has no `include_blockers` option and returns slim issue plus blocker ID list (`src/filigree/mcp_tools/planning.py:98`). Summary also prints blocker IDs only (`src/filigree/summary.py:221`). | Add `include_blockers=true` or equivalent slim blocker title/status/priority context. |
| Requirement-type documentation mismatch | Not done for default config | Built-in requirements pack exists, but the live project `list-types --json` does not include `requirement`. MCP observation promotion docs still advertise `type='requirement'` without saying the pack is optional (`src/filigree/mcp_tools/observations.py:166`). | Either enable requirements by default or update public docs/tool descriptions to say it requires the requirements pack. |
| Actor identity through file/finding write events | Partial | Manual `report_finding` actor is passed to paired observation attribution (`src/filigree/mcp_tools/scanners.py:353`; `src/filigree/db_files.py:705`). `promote_finding` records actor on created issue (`src/filigree/db_files.py:1375`). Finding records themselves have no actor field/event stream; `dismiss_finding` has no actor parameter. | Define and implement durable actor attribution for file/finding write operations, especially dismiss/update/register/delete paths. |
| Annotation handoff validation | Not done for the checklist criterion | `carry_forward_annotation` validates that both issue targets exist, then inserts a link to the new target and records an acknowledgement (`src/filigree/db_annotations.py:1071`). It does not check that the annotation currently has an active link to `from_target_id` before acknowledging it. Existing tests cover the happy path (`tests/core/test_annotations.py:179`; `tests/mcp/test_annotations.py:92`). | Add the explicit from-target active-link precondition and a regression that a nonexistent source link returns `VALIDATION`. |

## P3 Findings

| Checklist item | Current status | Evidence | Remaining work |
|---|---:|---|---|
| Actor/session filters for `archive_closed` | Partial | MCP/CLI archive supports `label`; MCP also accepts `actor` for history (`src/filigree/mcp_tools/meta.py:357`; `uv run filigree archive --help`). | No actor/session filter; document session-unique labels or add filter. |
| `get_summary` JSON or human-only | Done for MCP, not CLI | MCP `get_summary(format="json")` returns `{markdown, stats}` (`src/filigree/mcp_tools/meta.py:287`, `src/filigree/mcp_tools/meta.py:780`). There is no CLI `get-summary` command. | Mark MCP criterion complete; decide whether CLI needs equivalent. |
| Preview stale observations in summary | Done | `generate_summary()` appends stale observation count and oldest age when observation stats are available (`src/filigree/summary.py:300`). Session context also reports stale observations (`src/filigree/hooks.py:156`). | Mark complete if count/oldest-age preview is sufficient; otherwise add sample IDs. |
| Canonical `get_stats` fields and aliases | Done | `get_stats()` returns `status_name_counts`, `status_category_counts`, plus compatibility `by_status` and `by_category` (`src/filigree/db_meta.py:404`). CLI JSON confirms all four. | Documentation/deprecation wording may still need a pass. |
| `add_comment` echo text or structured comment | Partial | MCP `add_comment` returns full `PublicIssue` plus `comment_id` (`src/filigree/mcp_tools/meta.py:492`); no comment text or structured comment is included. CLI JSON add-comment should be checked separately if this becomes a ship item. | Either add structured comment echo or decide full issue plus ID is acceptable. |
| `release_claim(if_held=true)` held-by-other behavior | Decided in implementation: conflict | `release_claim` raises if held by another actor in `if_held` mode (`src/filigree/db_issues.py:1190`). API tests assert the conflict behavior. | Update checklist as decided, or create ADR note if product wants no-op. |
| Near-expiry stale claims | Not done | `get_stale_claims` only accepts `stale_after_hours` in MCP and CLI (`src/filigree/mcp_tools/issues.py:484`; `uv run filigree get-stale-claims --help`). | Add `expires_within_hours` if proactive heartbeat discovery is wanted. |
| File timelines include issue events | Done | `get_file_timeline` supports `include_issue_events` and `event_type='issue_event'` (`src/filigree/db_files.py:1677`; `uv run filigree get-file-timeline --help`). Tests cover CLI and core behavior (`tests/core/test_files.py:2352`; `tests/cli/test_files_commands.py:382`). | Mark complete. |
| Closed scratch/file immutable history | Done by ADR | Master checklist already marks this complete under ADR-003/ADR-005. | None. |

## Interface Drift Notes

- MCP `get_changes` is richer than CLI `get-changes`. The CLI lacks actor,
  issue, label, type, and heartbeat controls, even though MCP exposes them.
- MCP exposes `get_summary`; CLI does not expose `get-summary`. This is fine if
  summary JSON is MCP-only by design, but the checklist should say so.
- MCP schemas are all hand-written and permissive. Without unknown-parameter
  rejection, interface docs and runtime behavior can silently diverge.
- The live project does not enable the requirements pack by default, but public
  tool descriptions still mention `requirement` as if it were generally valid.

## Recommended Checklist Updates

Mark as Done after maintainer review:

- P1 stale-claim and handoff discovery, except the P3 near-expiry extension.
- P2 archive/done-status model.
- P3 `get_summary` JSON for MCP.
- P3 stale observation preview in summary.
- P3 `get_stats` canonical fields and compatibility aliases.
- P3 file timeline issue events.

Keep as active gaps:

- Strict unknown MCP parameter rejection. **Resolved 2026-05-14:** dispatcher-level registry-derived validation now rejects unknown keys before handler dispatch.
- Schema-mismatch binary diagnostics.
- Observation duplicate/link/merge dispositions. **Resolved 2026-05-14:** `observation_links`, `link_observation`, `batch_link_observations`, and `promote_observations_to_issue` landed with CLI/MCP/docs/tests.
- ADR-007 `report_finding` default side effect change.
- Mixed scratch cleanup documentation/orchestration.
- ADR-008 actor-as-default claim-aware writes.
- CLI parity for `get_changes` filters.
- Registry-generated MCP schema/docs.
- Remaining response-envelope normalization.
- Plan read slim/full mode and move/dependency warnings.
- Blocker hydration for `get_blocked`.
- Requirement pack/doc mismatch.
- File/finding actor attribution beyond observation/issue promotion.
- Annotation carry-forward source-link validation.
- Archive session/actor filter or explicit session-label doctrine.
- `add_comment` structured comment echo if the response becomes slim.
- Near-expiry claim discovery.
