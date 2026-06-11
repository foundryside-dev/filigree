# Consumer Migration Guide: 2.x → 3.0.0

Filigree 3.0.0 is a **major release**. It opens a SemVer-major boundary to land
the breaking wire-surface changes that could not ship mid-2.x without breaking
federation consumers. This guide is the single old→new reference for everyone
who consumes a Filigree surface — federation siblings (Loomweave, Wardline,
Legis), agents and scripts that bind MCP tool names, and out-of-suite consumers
pinned to the public HTTP endpoints.

> **Operators** upgrading an installed Filigree (stop-writers, schema migration,
> store move) should read [UPGRADING.md](UPGRADING.md). This document is the
> *consumer*-facing contract reference. **Beads → Filigree** import is
> [MIGRATION.md](MIGRATION.md).

## The five breaking surfaces

| # | Surface | Who is affected |
|---|---------|-----------------|
| 1 | [MCP tool-name namespacing](#1-mcp-tool-name-namespacing) | Any caller that hardcodes MCP tool names |
| 2 | [`get_stats` alias keys removed](#2-get_stats-alias-keys-removed) | Anything reading project-stats JSON |
| 3 | [Loomweave / Weft rebrand](#3-loomweave-weft-rebrand) | Federation consumers of the HTTP / token / entity-binding surfaces |
| 4 | [`TransitionMode` enum](#4-transitionmode-enum-internal-python-api) | Embedders of the in-process Python API |
| 5 | [`safe_message` parity for claim/transition errors](#5-safe_message-parity-for-claimtransition-errors) | Consumers that string-match error *prose* over HTTP/MCP |

Two schema migrations (v26 rebrand, v27 entity-association signing column) apply
**automatically and in place** on the first database open after the binary is
upgraded — see [UPGRADING.md](UPGRADING.md) and
[SCHEMA_MIGRATIONS.md](SCHEMA_MIGRATIONS.md). No consumer action is needed for
the migration itself; the items below are about the *wire* and *API* contracts
the migration exposes.

---

## 1. MCP tool-name namespacing

3.0.0 completes the namespacing started in 2.3.0
([ADR-016](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-016-mcp-tool-namespacing.md)).
The ~115 flat tool names (`get_issue`, `list_findings`, `start_work`, …) were
renamed to a subsystem-namespaced `<entity>_<verb>` convention (`issue_get`,
`finding_list`, `work_start`, …).

- **2.3.0** served the new names while still *resolving* the old ones (a
  transition window).
- **3.0.0 removes the fallback.** `call_tool` now rejects a legacy flat name with
  the standard `NOT_FOUND` (`Unknown tool`) envelope — exactly as a typo would.

There is **no `filigree_` prefix** on the new names: MCP clients already surface
every tool as `mcp__filigree__<name>`, so the server token is the client
wrapper's job.

### What you must do

- **Callers that hardcode tool names by string must switch to the new names** —
  see the full table below.
- **Callers that read `list_tools` dynamically need no change** — it has served
  only the namespaced names since 2.3.0.
- **The CLI is unaffected** — CLI verbs (`start-next-work`, `close`, …) are a
  separate surface and were never renamed.
- The 2.3.0 deprecation-telemetry field (`deprecated_tool_name_calls` in
  `mcp_status_get` / `mcp-status`) is **removed** — there is no longer a
  deprecated call to count.

### Full old → new tool-name map

> Source of truth: `src/filigree/mcp_tools/rename.py` (`_RENAME_MAP_DATA`, a
> frozen, CI-validated map with import-time injectivity and test-enforced total
> coverage). Regenerate this table from that module if it ever changes.

**`issue`**

| Old (removed) | New |
|---|---|
| `get_issue` | `issue_get` |
| `list_issues` | `issue_list` |
| `search_issues` | `issue_search` |
| `create_issue` | `issue_create` |
| `update_issue` | `issue_update` |
| `close_issue` | `issue_close` |
| `reopen_issue` | `issue_reopen` |
| `delete_issue` | `issue_delete` |
| `validate_issue` | `issue_validate` |
| `batch_close` | `issue_batch_close` |
| `batch_update` | `issue_batch_update` |
| `get_issue_files` | `issue_file_list` |
| `get_issue_events` | `issue_event_list` |
| `get_issue_annotations` | `issue_annotation_list` |
| `label_subtree` | `issue_subtree_label` |

**`work`** (claim / lease lifecycle + ready/blocked queue)

| Old (removed) | New |
|---|---|
| `get_ready` | `work_ready` |
| `get_blocked` | `work_blocked` |
| `get_stale_claims` | `work_stale_list` |
| `claim_issue` | `work_claim` |
| `claim_next` | `work_claim_next` |
| `reclaim_issue` | `work_reclaim` |
| `release_claim` | `work_release` |
| `release_my_claims` | `work_release_mine` |
| `heartbeat_work` | `work_heartbeat` |
| `start_work` | `work_start` |
| `start_next_work` | `work_start_next` |

**`dependency`**

| Old (removed) | New |
|---|---|
| `add_dependency` | `dependency_add` |
| `remove_dependency` | `dependency_remove` |
| `get_critical_path` | `dependency_critical_path` |

**`plan`**

| Old (removed) | New |
|---|---|
| `create_plan` | `plan_create` |
| `create_plan_from_file` | `plan_create_from_file` |
| `get_plan` | `plan_get` |
| `add_plan_step` | `plan_step_add` |
| `move_plan_step` | `plan_step_move` |
| `label_plan_tree` | `plan_label_tree` |
| `retarget_plan_dependency` | `plan_dependency_retarget` |

**`label`**

| Old (removed) | New |
|---|---|
| `add_label` | `label_add` |
| `remove_label` | `label_remove` |
| `batch_add_label` | `label_batch_add` |
| `batch_remove_label` | `label_batch_remove` |
| `list_labels` | `label_list` |
| `get_label_taxonomy` | `label_taxonomy_get` |

**`comment`**

| Old (removed) | New |
|---|---|
| `add_comment` | `comment_add` |
| `batch_add_comment` | `comment_batch_add` |
| `get_comments` | `comment_list` |

**`file`**

| Old (removed) | New |
|---|---|
| `register_file` | `file_register` |
| `get_file` | `file_get` |
| `list_files` | `file_list` |
| `delete_file_record` | `file_delete` |
| `add_file_association` | `file_association_add` |
| `get_file_annotations` | `file_annotation_list` |
| `get_file_timeline` | `file_timeline_get` |

**`finding`**

| Old (removed) | New |
|---|---|
| `report_finding` | `finding_report` |
| `list_findings` | `finding_list` |
| `get_finding` | `finding_get` |
| `update_finding` | `finding_update` |
| `batch_update_findings` | `finding_batch_update` |
| `dismiss_finding` | `finding_dismiss` |
| `promote_finding` | `finding_promote` |
| `promote_finding_and_attach_entity` | `finding_promote_and_attach_entity` |

**`annotation`**

| Old (removed) | New |
|---|---|
| `annotate_file` | `annotation_create` |
| `get_annotation` | `annotation_get` |
| `list_annotations` | `annotation_list` |
| `list_attention_annotations` | `annotation_attention_list` |
| `update_annotation` | `annotation_update` |
| `resolve_annotation` | `annotation_resolve` |
| `link_annotation` | `annotation_link` |
| `unlink_annotation` | `annotation_unlink` |
| `carry_forward_annotation` | `annotation_carry_forward` |
| `supersede_annotation` | `annotation_supersede` |
| `promote_annotation` | `annotation_promote` |

**`observation`**

| Old (removed) | New |
|---|---|
| `observe` | `observation_create` |
| `list_observations` | `observation_list` |
| `dismiss_observation` | `observation_dismiss` |
| `link_observation` | `observation_link` |
| `promote_observation` | `observation_promote` |
| `promote_observations_to_issue` | `observation_promote_to_issue` |
| `batch_dismiss_observations` | `observation_batch_dismiss` |
| `batch_link_observations` | `observation_batch_link` |
| `batch_promote_observations` | `observation_batch_promote` |

**`entity`** (cross-product associations — see
[ADR-029](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-029-entity-association-opacity.md))

| Old (removed) | New |
|---|---|
| `add_entity_association` | `entity_association_add` |
| `remove_entity_association` | `entity_association_remove` |
| `list_entity_associations` | `entity_association_list` |
| `list_associations_by_entity` | `entity_association_list_by_entity` |

**`scanner`** / **`scan`**

| Old (removed) | New |
|---|---|
| `list_scanners` | `scanner_list` |
| `list_available_scanners` | `scanner_available_list` |
| `enable_scanner` | `scanner_enable` |
| `disable_scanner` | `scanner_disable` |
| `trigger_scan` | `scan_trigger` |
| `trigger_scan_batch` | `scan_trigger_batch` |
| `get_scan_status` | `scan_status_get` |
| `preview_scan` | `scan_preview` |

**`template` / `type` / `pack` / `schema` / `change` / `prompt_pack`**

| Old (removed) | New |
|---|---|
| `get_template` | `template_get` |
| `get_type_info` | `type_get` |
| `list_types` | `type_list` |
| `list_packs` | `pack_list` |
| `get_schema` | `schema_get` |
| `get_changes` | `change_list` |
| `list_prompt_packs` | `prompt_pack_list` |
| `list_reconciliation_debt` | `reconciliation_debt_list` |

**`workflow`**

| Old (removed) | New |
|---|---|
| `get_workflow_statuses` | `workflow_status_list` |
| `get_valid_transitions` | `workflow_transition_list` |
| `explain_status` | `workflow_status_explain` |
| `get_workflow_guide` | `workflow_guide_get` |

**Analytics / status**

| Old (removed) | New |
|---|---|
| `get_stats` | `stats_get` |
| `get_summary` | `summary_get` |
| `get_metrics` | `metrics_get` |
| `get_mcp_status` | `mcp_status_get` |
| `session_context` | `session_context_get` |

**`admin`**

| Old (removed) | New |
|---|---|
| `archive_closed` | `admin_archive_closed` |
| `compact_events` | `admin_compact_events` |
| `export_jsonl` | `admin_export_jsonl` |
| `import_jsonl` | `admin_import_jsonl` |
| `reload_templates` | `admin_reload_templates` |
| `restart_dashboard` | `admin_restart_dashboard` |
| `undo_last` | `admin_undo_last` |

---

## 2. `get_stats` alias keys removed

The deprecated `status_name_counts` / `status_category_counts` keys are gone.
They were always exact duplicates of `by_status` / `by_category` — deprecated in
2.1.0 and removed at this major boundary. The drop loses no data.

They are removed from **every** surface that carries `get_stats` output:

- the MCP `stats_get` tool,
- the MCP `summary_get` JSON envelope (under the nested `stats` object),
- the HTTP `GET /api/stats` projection,
- the `filigree stats --json` CLI output.

### What you must do

| Removed key | Read instead |
|---|---|
| `status_name_counts` | `by_status` — counts keyed by literal workflow status name (`open`, `in_progress`, …) |
| `status_category_counts` | `by_category` — template categories `open` / `wip` / `done` |

The values are identical to what the removed keys carried, so this is a key-name
change only. No in-suite sibling read the removed keys (confirmed by full
call-site enumeration); the affected audience is any **out-of-suite** consumer
pinned to the public `GET /api/stats` endpoint.

---

## 3. Loomweave / Weft rebrand

3.0.0 lands the **Clarion → Loomweave** (sibling / registry / SEI authority) and
**Loom → Weft** (federation + named API generation) renames as a hard
wire-break, with **no compatibility aliases**. The v26 data migration rewrites
every stored identifier prefix in place.

### 3a. HTTP endpoint prefix: `/api/loom/*` → `/api/weft/*`

The federation HTTP generation moved from the `loom` prefix to `weft`. Every
federation endpoint changed prefix:

| Old | New |
|---|---|
| `GET /api/loom/changes` | `GET /api/weft/changes` |
| `GET /api/loom/issues` | `GET /api/weft/issues` |
| `GET /api/loom/blocked` | `GET /api/weft/blocked` |
| `GET /api/loom/findings` | `GET /api/weft/findings` |
| `GET /api/loom/files/{file_id}/findings` | `GET /api/weft/files/{file_id}/findings` |
| `POST /api/loom/batch/close` | `POST /api/weft/batch/close` |
| …(every `/api/loom/*` route) | …(same path under `/api/weft/*`) |

Federation consumers must repoint their base path. The `/api/scan-results` and
`/api/observations` ingest aliases are unchanged.

### 3b. Entity-association identifier: `clarion_entity_id` → `loomweave_entity_id`

The entity-association binding (see
[ADR-029](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-029-entity-association-opacity.md))
carried the sibling's pre-rebrand name. What changed for consumers:

- **Response field.** The row returned by `entity_association_list` /
  `entity_association_list_by_entity` (and the HTTP
  `GET /api/issue/{id}/entity-associations` and
  `GET /api/entity-associations` responses) now carries the key
  **`loomweave_entity_id`** where it was `clarion_entity_id`.
- **The request parameter is unchanged.** Both transports still take an opaque
  **`entity_id`** on `add` / `remove` / reverse-lookup — that name did **not**
  change. Filigree never parses it (ADR-029, Decision 1).

### 3c. SEI prefix: `clarion:eid:` → `loomweave:eid:`

Stored Stable Entity Identifiers were rewritten in place by the v26 migration —
across the entity-association column, the `deleted_issues` tombstone
`entity_ids` array, and the entity-association audit events. A consumer that
**persisted** SEIs handed to/from Filigree must expect the `loomweave:eid:`
prefix on read. A federation consumer already on `loomweave:eid:` reconciles
cleanly.

### 3d. Finding rule-id prefix: `CLA-` → `LMWV-`

Finding rule-ids minted under the old prefix were rewritten `CLA-` → `LMWV-` by
the same migration.

### 3e. Token env var (outbound registry token): `CLARION_LOOM_TOKEN` → `WEFT_TOKEN`

The outbound registry/federation bearer token env var is now **`WEFT_TOKEN`**.
`CLARION_LOOM_TOKEN` is **no longer read**. Deployments that talk to the registry
must export `WEFT_TOKEN`.

> Do not confuse this with the **inbound** federation bearer that gates
> Filigree's own `/api/weft/*` + `/mcp` surface — that is `WEFT_FEDERATION_TOKEN`
> (a distinct token; see [UPGRADING.md](UPGRADING.md) and ADR-018). The
> deprecated `FILIGREE_*_API_TOKEN` aliases for the inbound token still resolve
> with a migration nudge; the **outbound** `CLARION_LOOM_TOKEN` does not.

### 3f. `registry_backend` config value: `clarion` → `loomweave`

A deployed config naming the registry backend `clarion` is migrated to
`loomweave` on load via a one-shot rename-on-load shim — no manual edit is
required, but new config should write `loomweave`.

### 3g. Registry error codes: `CLARION_*` → `LOOMWEAVE_*`

The registry error codes `CLARION_REGISTRY_VERSION_MISMATCH` and
`CLARION_OUT_OF_SYNC` are emitted as **`LOOMWEAVE_REGISTRY_VERSION_MISMATCH`**
and **`LOOMWEAVE_OUT_OF_SYNC`**. Consumers switching on error `code` must use
the new names.

### 3h. URI scheme: `loom://` is gone; `weft://` is reserved, not live

There is **no live federation URI scheme** in 3.0.0. The `loom://` scheme that
2.x planning documents referenced was never implemented on the wire, and the
federation hub formally closed the URI-scheme apparatus in favour of **SEI**
(opaque stable entity identity) plus per-product association surfaces. The
name **`weft://`** is reserved for future federation-level resources; do not
parse or construct `loom://`/`weft://` URIs against this release. All internal
`loom` naming (handler names, types, test fixtures under
`tests/fixtures/contracts/weft/`) now reads `weft`.

> A note on stored Legis signatures: rewriting the `loomweave:eid:` prefix
> invalidates any Legis signature computed over old `entity_ids`. Nothing
> pre-3.0.0 has shipped, so there is no released signature corpus to preserve —
> signatures are cut fresh over the new IDs; no re-sign pass exists or is
> needed. Filigree never verifies them, so **reads do not break** either way.

---

## 4. `TransitionMode` enum (internal Python API)

The internal transition-direction flag changed from a bare `backward: bool` to a
`TransitionMode{FORWARD, BACKWARD}` enum
([ADR-019](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-019-transition-mode-enum.md)).
This flag has **no MCP / CLI / HTTP / wire exposure** — only code that embeds the
in-process `FiligreeDB` Python API is affected.

```python
from filigree.types.api import TransitionMode

# before (2.x):
db.update_issue(issue_id, status="open", backward=True)
# after (3.0.0):
db.update_issue(issue_id, status="open", mode=TransitionMode.BACKWARD)
```

`InvalidTransitionError.backward` is now `InvalidTransitionError.mode`. There is
no `backward=` alias — it is a clean cut. Agents, CLI users, and federation
consumers see no difference.

---

## 5. `safe_message` parity for claim/transition errors

`ClaimConflictError` and `InvalidTransitionError` now follow the
`WrongProjectError` pattern on **untrusted surfaces**: over HTTP and MCP, the
error *string* is a fixed, generic `safe_message` instead of reflecting arbitrary
call-site text (which could carry issue IDs or actor names).

- Claim conflict → `"Issue is claimed by a different assignee"`
- Invalid transition → `"Requested status transition is not allowed"`

**The structured recovery data is retained**, so agents still self-correct:

- claim conflicts keep `details.observed` / `details.expected` (the assignees);
- transition errors keep `current_status` / `type_name` / `to_state` (and
  `valid_transitions` when computed) in the HTTP `details` payload and the MCP
  `TransitionError` payload.

The **CLI keeps the full rich `str(exc)`** operator message — it is the local
diagnostic surface and is unchanged.

### What you must do

**Almost certainly nothing.** This is not a breaking change unless you
**string-matched the prose** of these two error messages over HTTP/MCP. The
correct pattern — which the 2.0 envelope contract already directs — is to switch
on the structured `code` and read `details`, not to parse the human string. If
you did parse the prose, switch to `code` + `details`.

---

## See also

- [UPGRADING.md](UPGRADING.md) — operator upgrade steps (stop writers, schema migration, store move).
- [SCHEMA_MIGRATIONS.md](SCHEMA_MIGRATIONS.md) — the v26 / v27 migration records.
- [MCP Server reference](mcp.md) · [Python API reference](api-reference.md) · [Federation contracts](federation/contracts.md).
- ADRs (GitHub):
  [ADR-016 MCP tool namespacing](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-016-mcp-tool-namespacing.md) ·
  [ADR-019 TransitionMode](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-019-transition-mode-enum.md) ·
  [ADR-020 transport-bound actor identity](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-020-transport-bound-actor-identity.md) ·
  [ADR-029 entity-association opacity](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-029-entity-association-opacity.md).
