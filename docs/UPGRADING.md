# Upgrading Filigree

This guide covers version-to-version Filigree upgrades. For Beads import, see
[MIGRATION.md](MIGRATION.md).

## Upgrading to 3.0.0 (store consolidation)

Filigree 3.0.0 moves the machine-owned store from the legacy `.filigree/`
directory to the federation convention `.weft/filigree/` (the `.filigree.conf`
anchor stays). The move happens **only** on an explicit `filigree init` against a
legacy install — never on passive discovery — and is crash-convergent: it copies
the database forward, rewrites the conf, then removes the legacy database. A
re-run resumes a half-finished move.

### Stop ALL writers before upgrading — this is mandatory, not advisory

Because the migration **deletes the legacy database** once it has been copied
forward, any process still holding the legacy database open can lose writes: a
write it commits after the copy lands on a file that is about to be unlinked, and
is never carried into the new store. Before running the `filigree init` that
performs the migration, stop **every** writer for the project:

- the web dashboard (ephemeral session dashboards and `--server-mode` daemons —
  `filigree server stop`),
- any MCP server holding the project open,
- other CLI/agent sessions.

Filigree defends this automatically where it can: `migrate` **refuses** (with a
`StoreMigrationBusyError` naming the port) when it detects a registered
server-mode daemon or a bound ephemeral dashboard for the project, and it holds
the legacy database's write lock across the copy so an actively-writing process
is blocked rather than silently dropped. **Known limitation:** an MCP/stdio
connection opened *in your own session* (for example, the agent session running
the upgrade) is not registered anywhere and cannot be detected — you must stop it
yourself. When in doubt, quiesce everything and re-run; the migration is
idempotent and safe to repeat.

After the migration completes, restart the dashboard / server / MCP processes so
they reopen against `.weft/filigree/`. A daemon left running from before the move
keeps writing to its now-stale connection until it is restarted.

## Upgrading to 3.0.0 (MCP tool-name namespacing)

3.0.0 completes the MCP tool-name namespacing started in 2.3.0 (ADR-016). The
~115 flat tool names (`get_issue`, `list_findings`, `start_work`, …) were
renamed to a subsystem-namespaced `<entity>_<verb>` convention (`issue_get`,
`finding_list`, `work_start`, …) so an agent — and the tool-search ranker — can
disambiguate the catalogue by entity prefix.

**2.3.0 served the new names while still accepting the old ones** (a transition
window: `list_tools` advertised only the 116 namespaced names, but `call_tool`
resolved a legacy name to the same handler). **3.0.0 removes that fallback.** A
call to a legacy flat name now returns the standard `NOT_FOUND` (`Unknown tool`)
envelope, exactly as a typo would. There is no `filigree_` prefix on the new
names: MCP clients already surface every tool as `mcp__filigree__<name>`, so the
server token is supplied by the client wrapper.

### What you must do

- **MCP consumers (federation siblings, agents, scripts) that bind tool names by
  string must switch to the new names.** Any caller that reads `list_tools`
  dynamically already sees only the new names and needs no change — only
  hardcoded old names break. The full mapping is below.
- The **CLI is unaffected** — CLI verbs (`start-next-work`, `close`, …) are a
  separate surface and were never renamed.
- The deprecation-telemetry signal that 2.3.0 surfaced in `get_mcp_status`
  (`deprecated_tool_name_calls`) is **removed** — there is no longer a deprecated
  call to count.

### Full old → new tool-name mapping

| Old name (removed) | New name |
| --- | --- |
| `batch_close` | `issue_batch_close` |
| `batch_update` | `issue_batch_update` |
| `close_issue` | `issue_close` |
| `create_issue` | `issue_create` |
| `delete_issue` | `issue_delete` |
| `get_issue` | `issue_get` |
| `get_issue_annotations` | `issue_annotation_list` |
| `get_issue_events` | `issue_event_list` |
| `get_issue_files` | `issue_file_list` |
| `label_subtree` | `issue_subtree_label` |
| `list_issues` | `issue_list` |
| `reopen_issue` | `issue_reopen` |
| `search_issues` | `issue_search` |
| `update_issue` | `issue_update` |
| `validate_issue` | `issue_validate` |
| `claim_issue` | `work_claim` |
| `claim_next` | `work_claim_next` |
| `get_blocked` | `work_blocked` |
| `get_ready` | `work_ready` |
| `get_stale_claims` | `work_stale_list` |
| `heartbeat_work` | `work_heartbeat` |
| `reclaim_issue` | `work_reclaim` |
| `release_claim` | `work_release` |
| `release_my_claims` | `work_release_mine` |
| `start_next_work` | `work_start_next` |
| `start_work` | `work_start` |
| `add_dependency` | `dependency_add` |
| `get_critical_path` | `dependency_critical_path` |
| `remove_dependency` | `dependency_remove` |
| `add_plan_step` | `plan_step_add` |
| `create_plan` | `plan_create` |
| `create_plan_from_file` | `plan_create_from_file` |
| `get_plan` | `plan_get` |
| `label_plan_tree` | `plan_label_tree` |
| `move_plan_step` | `plan_step_move` |
| `retarget_plan_dependency` | `plan_dependency_retarget` |
| `add_label` | `label_add` |
| `batch_add_label` | `label_batch_add` |
| `batch_remove_label` | `label_batch_remove` |
| `get_label_taxonomy` | `label_taxonomy_get` |
| `list_labels` | `label_list` |
| `remove_label` | `label_remove` |
| `add_comment` | `comment_add` |
| `batch_add_comment` | `comment_batch_add` |
| `get_comments` | `comment_list` |
| `list_reconciliation_debt` | `reconciliation_debt_list` |
| `add_file_association` | `file_association_add` |
| `delete_file_record` | `file_delete` |
| `get_file` | `file_get` |
| `get_file_annotations` | `file_annotation_list` |
| `get_file_timeline` | `file_timeline_get` |
| `list_files` | `file_list` |
| `register_file` | `file_register` |
| `batch_update_findings` | `finding_batch_update` |
| `dismiss_finding` | `finding_dismiss` |
| `get_finding` | `finding_get` |
| `list_findings` | `finding_list` |
| `promote_finding` | `finding_promote` |
| `promote_finding_and_attach_entity` | `finding_promote_and_attach_entity` |
| `report_finding` | `finding_report` |
| `update_finding` | `finding_update` |
| `annotate_file` | `annotation_create` |
| `carry_forward_annotation` | `annotation_carry_forward` |
| `get_annotation` | `annotation_get` |
| `link_annotation` | `annotation_link` |
| `list_annotations` | `annotation_list` |
| `list_attention_annotations` | `annotation_attention_list` |
| `promote_annotation` | `annotation_promote` |
| `resolve_annotation` | `annotation_resolve` |
| `supersede_annotation` | `annotation_supersede` |
| `unlink_annotation` | `annotation_unlink` |
| `update_annotation` | `annotation_update` |
| `batch_dismiss_observations` | `observation_batch_dismiss` |
| `batch_link_observations` | `observation_batch_link` |
| `batch_promote_observations` | `observation_batch_promote` |
| `dismiss_observation` | `observation_dismiss` |
| `link_observation` | `observation_link` |
| `list_observations` | `observation_list` |
| `observe` | `observation_create` |
| `promote_observation` | `observation_promote` |
| `promote_observations_to_issue` | `observation_promote_to_issue` |
| `add_entity_association` | `entity_association_add` |
| `list_associations_by_entity` | `entity_association_list_by_entity` |
| `list_entity_associations` | `entity_association_list` |
| `remove_entity_association` | `entity_association_remove` |
| `disable_scanner` | `scanner_disable` |
| `enable_scanner` | `scanner_enable` |
| `list_available_scanners` | `scanner_available_list` |
| `list_scanners` | `scanner_list` |
| `get_scan_status` | `scan_status_get` |
| `preview_scan` | `scan_preview` |
| `trigger_scan` | `scan_trigger` |
| `trigger_scan_batch` | `scan_trigger_batch` |
| `list_prompt_packs` | `prompt_pack_list` |
| `get_changes` | `change_list` |
| `get_template` | `template_get` |
| `get_type_info` | `type_get` |
| `list_types` | `type_list` |
| `list_packs` | `pack_list` |
| `get_schema` | `schema_get` |
| `explain_status` | `workflow_status_explain` |
| `get_valid_transitions` | `workflow_transition_list` |
| `get_workflow_guide` | `workflow_guide_get` |
| `get_workflow_statuses` | `workflow_status_list` |
| `get_stats` | `stats_get` |
| `get_summary` | `summary_get` |
| `get_metrics` | `metrics_get` |
| `get_mcp_status` | `mcp_status_get` |
| `session_context` | `session_context_get` |
| `archive_closed` | `admin_archive_closed` |
| `compact_events` | `admin_compact_events` |
| `export_jsonl` | `admin_export_jsonl` |
| `import_jsonl` | `admin_import_jsonl` |
| `reload_templates` | `admin_reload_templates` |
| `restart_dashboard` | `admin_restart_dashboard` |
| `undo_last` | `admin_undo_last` |

## Upgrading from 2.1.0 to 2.1.1

Filigree 2.1.1 ships database schema `user_version` 21 (2.1.0 ships 20). The
first 2.1.1 open applies a single in-place migration:

| Step | Schema | What changes |
|------|--------|--------------|
| 20 to 21 | v21 | Adds `deleted_issues.entity_ids`, surfaced as `affected_entities` on the `issue_deleted` changes-feed record |

The migration is an additive `ALTER TABLE ... ADD COLUMN` (`NOT NULL DEFAULT
'[]'`) that backfills existing tombstones; `FiligreeDB.initialize()` applies it
automatically on first normal database open after the binary is upgraded. Use
`filigree doctor` before and after the upgrade to validate local configuration;
`doctor --fix` is limited to local binding and dashboard-pointer repair. No application-level action is required. A
federation consumer of `/api/loom/changes` should begin honouring the new
`affected_entities` field on `issue_deleted` records — purge the listed entity
bindings on reconcile; see `docs/federation/contracts.md` §F5.

## Upgrading from 2.0.x to 2.1.0

Filigree 2.1.0 ships database schema `user_version` 20. Databases from the
2.0.x line ship schema 14, so the first 2.1.0 open applies migrations 14 to
20 in place:

| Step | Schema | What changes |
|------|--------|--------------|
| 14 to 15 | v15 | Adds `entity_associations` for issue-to-entity bindings |
| 15 to 16 | v16 | Adds `events.event_seq` and rebuilds the audit-event unique index |
| 16 to 17 | v17 | Adds `file_records.content_hash` and `file_records.registry_backend` |
| 17 to 18 | v18 | Stamps `application_id` on pre-app-id-aware databases (metadata only) |
| 18 to 19 | v19 | Adds `scan_findings.fingerprint` and partitions the dedup index |
| 19 to 20 | v20 | Adds the `deleted_issues` tombstone behind the `issue_deleted` changes-feed signal |

`FiligreeDB.initialize()` applies pending migrations automatically on the first
normal database open after the binary is upgraded. `filigree doctor` validates
the configured database path and reports schema state; `doctor --fix` does not
apply schema migrations.

### Before You Upgrade

1. Stop long-running writers: dashboard processes, server-mode daemons, and MCP
   clients that keep a Filigree connection open.
2. Back up the project database. For the default layout, copy
   `.filigree/filigree.db` plus any `-wal` and `-shm` sidecars after writers
   are stopped. For projects with `.filigree.conf`, back up the configured
   `db` path instead.
3. Upgrade the Filigree executable. Use the command that matches how you
   installed it:

```bash
uv tool upgrade filigree
# or
pip install --upgrade "filigree[all]"
```

When running from a source checkout, sync the checkout and run project commands
through `uv run`.

### In-Place Upgrade Procedure

Run these commands from each project root:

```bash
filigree doctor
filigree stats
filigree doctor
filigree stats
filigree session-context
```

For source checkouts, prefix the same commands with `uv run`:

```bash
uv run filigree doctor
uv run filigree stats
uv run filigree doctor
uv run filigree stats
uv run filigree session-context
```

The first normal DB command after the binary upgrade opens the existing
database and applies pending schema migrations through the standard
`FiligreeDB.initialize()` path. Do not edit `PRAGMA user_version` by hand or
delete and recreate `.filigree/` to upgrade an existing project.

Automation can use `filigree doctor --fix --json` for the shared doctor summary
contract when it wants local binding/dashboard-pointer repair:

```json
{"ok": true, "checks": [{"id": "mcp.registration", "status": "fixed", "fixed": true}], "next_actions": []}
```

`--fix` repairs only local agent bindings and stale dashboard pointers. It does
not mutate issue rows, scan findings, scanner results, entity associations, or
database schema.

An automation wrapper should do only the safe orchestration around this built-in
path:

```bash
# Pseudocode for deployment automation
stop_filigree_writers
backup_configured_database
upgrade_filigree_binary_to_2_1_0
filigree stats
filigree doctor
restart_mcp_or_dashboard_processes
```

If `doctor` reports `SCHEMA_MISMATCH`, the database is newer than the installed
Filigree binary. Upgrade the binary and restart the MCP server or dashboard that
reported the mismatch; do not downgrade the database.

### Breaking API and Workflow Changes

#### Custom Workflow Packs

Custom workflow packs that rely on reopen, release-revert, or forced close must
declare `reverse_transitions`. Missing reverse edges now raise
`InvalidTransitionError`.

```json
{
  "reverse_transitions": [
    {"from": "closed", "to": "open", "enforcement": "soft"}
  ]
}
```

Normal transition suggestions remain forward-only; reverse transitions are for
controlled cleanup paths.

#### HTTP Force Close

HTTP batch-close rejects `force=true` unless the dashboard was started with:

```bash
filigree dashboard --allow-http-force-close
```

Prefer the CLI or MCP force-close path for trusted operator workflows. Only
enable the HTTP flag for deployments that intentionally expose forced bulk close
over the local dashboard API.

#### Corrupt Custom Fields

`update_issue(fields=...)` no longer merges over corrupt `issues.fields` JSON.
If you need to replace a corrupt field bag deliberately, pass
`force_overwrite_corrupt=True` from the Python API. The overwrite emits a
`corrupt_fields_overwritten` event.

#### Audit Event Duplicates

`_record_event` now preserves same-second bursts with `event_seq` and raises
`sqlite3.IntegrityError` for true duplicate rows. Embedders should treat that as
a transaction failure instead of relying on silent deduplication.

#### Internal Transaction Keyword

The internal `_commit=` keyword was removed from `claim_issue` and
`_claim_next_with_prior`. Prefer `start_work` and `start_next_work` for composed
claim-and-transition flows. Low-level embedders that already own the transaction
boundary must use the internal `_skip_begin=True` path.

### After You Upgrade

Restart MCP servers, dashboards, and long-running agent sessions so they load
the 2.1.0 package and schema support. A stale MCP process pinned to schema 16
will keep returning `SCHEMA_MISMATCH` against a schema-17 project database until
it is restarted.
