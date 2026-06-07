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
