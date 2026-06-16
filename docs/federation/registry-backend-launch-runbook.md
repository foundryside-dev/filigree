# Registry-Backend Launch Runbook

This runbook covers Filigree ADR-014 rollout when a project opts into
Loomweave-owned file identity. Filigree-only projects do not need this runbook:
`registry_backend: local` is still the default and keeps existing behavior.

## Preconditions

- Filigree is built with ADR-014 support. Verify
  `GET /api/files/_schema` includes `config_flags.registry_backend_features`
  with both `local` and `loomweave`.
- Loomweave Sprint 3 C-WP10.1 through C-WP10.4 are deployed for the sibling
  project. At minimum, `loomweave serve` must expose
  `GET /api/v1/files?path=&language=` and return
  `{entity_id, content_hash, canonical_path, language}`.
- The operator has a restorable backup of `.filigree/filigree.db`.
- The Loomweave base URL is stable from the Filigree process.

## Fresh Project Setup

1. Start Loomweave's read API for the same project/worktree.
2. Probe a known file:

   ```bash
   curl 'http://127.0.0.1:9111/api/v1/files?path=src/main.py&language=python'
   ```

3. Configure `.filigree.conf`:

   ```yaml
   registry_backend: loomweave
   loomweave:
     base_url: http://127.0.0.1:9111
     timeout_seconds: 5
     allow_local_fallback: false
   ```

4. Start Filigree and confirm the handshake:

   ```bash
   curl http://127.0.0.1:8377/api/files/_schema
   ```

   The response must show `registry_backend: loomweave`.

5. Submit a small scan-result payload and verify the stored file ID is a
   Loomweave entity ID rather than a Filigree-native `*-f-*` ID.

## Existing Project Migration

1. Stop writers that can create file records.
2. Back up `.filigree/filigree.db` and keep the backup outside the project
   database directory.
3. Configure `.filigree.conf` for `registry_backend: loomweave` and the Loomweave
   base URL.
4. Run the dry run:

   ```bash
   uv run filigree migrate-registry --to loomweave --dry-run --json
   ```

5. Inspect every `unresolved` row. Delete stale file rows or repair Loomweave
   indexing before executing. Do not execute with unresolved rows.
6. Execute with a manifest:

   ```bash
   uv run filigree migrate-registry --to loomweave --execute --manifest registry-migration.json --json
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
Loomweave-mode writes accumulate:

```bash
uv run filigree migrate-registry --rollback registry-migration.json --json
```

After rollback, set `registry_backend: local` or stop Filigree until Loomweave is
healthy. Re-run `GET /api/files/_schema` and a small scan ingest before
returning writers to service.

### Lost Rollback Manifest

There is no supported `migrate-registry --to local` reconstruction path after
the rollback manifest is lost. The manifest is the only artifact that records
the old Filigree-local file IDs and every rewritten reference. If it is missing,
restore the pre-migration database backup from step 2, or keep the project in
`loomweave` mode and repair Loomweave availability/indexing. Do not attempt a
hand-written local rollback against a live database.

## Failure Modes

- If Loomweave is unreachable in `loomweave` mode, auto-create write paths return
  `503 Service Unavailable` with an IO error.
- `--allow-local-fallback` is for single-operator recovery. It routes
  auto-creates through `LocalRegistry` while the project remains configured for
  `loomweave`; do not leave it enabled after the incident.
- Direct local file registration returns
  `FILE_REGISTRY_DISPLACED`. Use Loomweave's read API instead.
- `entity_associations` is a peer primitive and is not migrated by
  `migrate-registry`; file identity displacement is additive over it.
- **Briefing-blocked files surface as `RegistryFileNotFoundError` (HTTP 404
  from Loomweave).** A scan-results POST that targets a file whose Loomweave entity
  is `briefing_blocked` will fail rather than mint a shadow row. To diagnose:
  1. Query Loomweave directly: `curl 'http://127.0.0.1:9111/api/v1/files?path=<path>&language=<lang>'`.
     A 404 with the file otherwise present in the project is the briefing-block
     signature.
  2. Inspect the entity properties in Loomweave to confirm `briefing_blocked` is
     set, then lift the block in Loomweave (or accept that findings for the
     blocked file will not be ingested while the block is in place).
  3. Re-run the failed scan-results ingest once the block is lifted.
  This behaviour is intentional under ADR-014 §"Briefing-block masking".

## Validating Against a Live Loomweave Build

The Filigree test suite ships a Phase D end-to-end test that spawns
`loomweave serve` against a tempdir project and asserts that a Filigree
scan-results ingest threads Loomweave's entity ID into stored file records.
The test is opt-in by tool availability:

```bash
# Prerequisite: both binaries built and on PATH.
which loomweave filigree

# Run only the e2e test (skips automatically when loomweave is absent):
uv run pytest tests/integration/test_loomweave_phase_d_e2e.py -m integration -v

# Or filter to the integration marker across the suite:
uv run pytest -m integration
```

The test creates its own tempdir project (calls `loomweave install`,
writes `loomweave.yaml` with an HTTP bind on a free loopback port, spawns
`loomweave serve`) so no project layout is required on disk. CI lanes that
also build Loomweave can opt in by including the integration marker in
their pytest invocation; lanes that do not will silently skip.

## Ownership Boundary

Filigree issues for ADR-014 track Filigree code, schema, tests, and docs.
Loomweave Sprint 3 work for C-WP10 is tracked in `/home/user/loomweave/.filigree/`
and should not be filed or closed from the Filigree tracker.
