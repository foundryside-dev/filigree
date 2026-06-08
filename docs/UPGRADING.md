# Upgrading Filigree

This guide covers version-to-version Filigree upgrades. For Beads import, see
[MIGRATION.md](MIGRATION.md). For the consumer-facing old→new contract reference
(MCP tool names, stats keys, the Loomweave/Weft rebrand surfaces), see the
[3.0.0 consumer migration guide](MIGRATION-3.0.md).

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

The complete ~115-row old→new table (grouped by subsystem) is the
[3.0.0 consumer migration guide §1](MIGRATION-3.0.md#1-mcp-tool-name-namespacing).
A few of the most-bound renames, to orient:

| Old name (removed) | New name |
| --- | --- |
| `get_issue` | `issue_get` |
| `list_issues` | `issue_list` |
| `start_work` | `work_start` |
| `start_next_work` | `work_start_next` |
| `get_ready` | `work_ready` |
| `list_findings` | `finding_list` |
| `report_finding` | `finding_report` |
| `get_stats` | `stats_get` |
| `session_context` | `session_context_get` |

The pattern is `<verb>_<entity>` → `<entity>_<verb>` (`get_issue` → `issue_get`),
with batch/list/get verbs trailing (`batch_close` → `issue_batch_close`). See the
guide for every row.

## Upgrading to 3.0.0 (Loomweave / Weft rebrand)

3.0.0 lands the **Clarion → Loomweave** and **Loom → Weft** renames as a hard
wire-break (schema v26), **with no compatibility aliases**. The v26 data
migration rewrites every stored identifier prefix in place — it runs
automatically on the first database open after the binary is upgraded, alongside
the v27 entity-association signing-column add.

The consumer-visible contract changes are enumerated in the
[3.0.0 consumer migration guide §3](MIGRATION-3.0.md#3-loomweave-weft-rebrand).
In brief:

- HTTP federation prefix `/api/loom/*` → `/api/weft/*`.
- Entity-association response field `clarion_entity_id` → `loomweave_entity_id`
  (the opaque request parameter `entity_id` is unchanged).
- Stored SEI prefix `clarion:eid:` → `loomweave:eid:`; finding rule-ids
  `CLA-` → `LMWV-`.
- Outbound registry token env var `CLARION_LOOM_TOKEN` → `WEFT_TOKEN` (distinct
  from the inbound `WEFT_FEDERATION_TOKEN` that gates this server's
  `/api/weft/*` + `/mcp` surface).
- `registry_backend` config value `clarion` → `loomweave` (migrated on load).

**Not renamed in 3.0.0** (intentionally — do not migrate these): the registry
error codes `CLARION_REGISTRY_VERSION_MISMATCH` / `CLARION_OUT_OF_SYNC` and the
`loom://` URI scheme.

### What you must do

- Repoint federation consumers from `/api/loom/*` to `/api/weft/*`.
- Export `WEFT_TOKEN` where a deployment previously set `CLARION_LOOM_TOKEN`.
- No manual database or config edit is required — the v26 migration and the
  config rename-on-load shim handle the stored data.

## Upgrading to 3.0.0 (TransitionMode enum — internal Python API)

The internal transition-direction flag `backward: bool` is replaced by a
`TransitionMode{FORWARD, BACKWARD}` enum
([ADR-019](https://github.com/foundryside-dev/filigree/blob/main/docs/architecture/decisions/ADR-019-transition-mode-enum.md)).
This flag has **no MCP / CLI / HTTP / wire exposure** — only code that embeds the
in-process `FiligreeDB` Python API is affected. Migrate
`update_issue(..., backward=True)` to `mode=TransitionMode.BACKWARD` (imported
from `filigree.types.api`); `InvalidTransitionError.backward` is now `.mode`.
There is no `backward=` alias. See the
[consumer migration guide §4](MIGRATION-3.0.md#4-transitionmode-enum-internal-python-api).

## Upgrading to 3.0.0 (get_stats alias keys removed)

The deprecated `status_name_counts` / `status_category_counts` keys are gone
from the project-stats payload. They were always exact duplicates of
`by_status` / `by_category` respectively — deprecated in 2.1.0 and removed at
this major boundary.

The keys are dropped from **every** surface that carries `get_stats` output:

- the MCP `stats_get` tool,
- the MCP `summary_get` JSON envelope (under the nested `stats` object),
- the HTTP `GET /api/stats` projection,
- the `filigree stats --json` CLI output.

### What you must do

If you read either removed key, switch to the canonical pair:

| Removed key | Read instead |
| --- | --- |
| `status_name_counts` | `by_status` (counts keyed by literal workflow status name, e.g. `open`, `in_progress`) |
| `status_category_counts` | `by_category` (template categories `open` / `wip` / `done`) |

The values are identical to what the removed keys carried, so this is a
key-name change only. No in-suite sibling read the removed keys; the affected
audience is any **out-of-suite** consumer pinned to the public `GET /api/stats`
endpoint.

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
federation consumer of `/api/weft/changes` (the `/api/loom/changes` endpoint as
of 2.1.1; renamed to `/api/weft/*` in 3.0.0) should begin honouring the new
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
