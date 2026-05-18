# Registry-Backend Launch Runbook

This runbook covers Filigree ADR-014 rollout when a project opts into
Clarion-owned file identity. Filigree-only projects do not need this runbook:
`registry_backend: local` is still the default and keeps existing behavior.

## Preconditions

- Filigree is built with ADR-014 support. Verify
  `GET /api/files/_schema` includes `config_flags.registry_backend_features`
  with both `local` and `clarion`.
- Clarion Sprint 3 C-WP10.1 through C-WP10.4 are deployed for the sibling
  project. At minimum, `clarion serve` must expose
  `GET /api/v1/files?path=&language=` and return
  `{entity_id, content_hash, canonical_path, language}`.
- The operator has a restorable backup of `.filigree/filigree.db`.
- The Clarion base URL is stable from the Filigree process.

## Fresh Project Setup

1. Start Clarion's read API for the same project/worktree.
2. Probe a known file:

   ```bash
   curl 'http://127.0.0.1:9111/api/v1/files?path=src/main.py&language=python'
   ```

3. Configure `.filigree.conf`:

   ```yaml
   registry_backend: clarion
   clarion:
     base_url: http://127.0.0.1:9111
     timeout_seconds: 5
     allow_local_fallback: false
   ```

4. Start Filigree and confirm the handshake:

   ```bash
   curl http://127.0.0.1:8377/api/files/_schema
   ```

   The response must show `registry_backend: clarion`.

5. Submit a small scan-result payload and verify the stored file ID is a
   Clarion entity ID rather than a Filigree-native `*-f-*` ID.

## Existing Project Migration

1. Stop writers that can create file records.
2. Back up `.filigree/filigree.db` and keep the backup outside the project
   database directory.
3. Configure `.filigree.conf` for `registry_backend: clarion` and the Clarion
   base URL.
4. Run the dry run:

   ```bash
   uv run filigree migrate-registry --to clarion --dry-run --json
   ```

5. Inspect every `unresolved` row. Delete stale file rows or repair Clarion
   indexing before executing. Do not execute with unresolved rows.
6. Execute with a manifest:

   ```bash
   uv run filigree migrate-registry --to clarion --execute --manifest registry-migration.json --json
   ```

7. Start Filigree and check:

   ```bash
   curl http://127.0.0.1:8377/api/files/_schema
   uv run filigree list-files --json
   ```

8. Keep `registry-migration.json` with the deployment record. It is required
   for rollback inside the supported reversibility window.

## Rollback

Rollback is manifest-based and intended for immediate recovery before new
Clarion-mode writes accumulate:

```bash
uv run filigree migrate-registry --rollback registry-migration.json --json
```

After rollback, set `registry_backend: local` or stop Filigree until Clarion is
healthy. Re-run `GET /api/files/_schema` and a small scan ingest before
returning writers to service.

## Failure Modes

- If Clarion is unreachable in `clarion` mode, auto-create write paths return
  `503 Service Unavailable` with an IO error.
- `--allow-local-fallback` is for single-operator recovery. It routes
  auto-creates through `LocalRegistry` while the project remains configured for
  `clarion`; do not leave it enabled after the incident.
- Direct local file registration returns
  `FILIGREE_FILE_REGISTRY_DISPLACED`. Use Clarion's read API instead.
- `entity_associations` is a peer primitive and is not migrated by
  `migrate-registry`; file identity displacement is additive over it.

## Ownership Boundary

Filigree issues for ADR-014 track Filigree code, schema, tests, and docs.
Clarion Sprint 3 work for C-WP10 is tracked in `/home/john/clarion/.filigree/`
and should not be filed or closed from the Filigree tracker.
