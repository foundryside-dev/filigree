# Registry-Backend Launch Runbook

This runbook covers Filigree ADR-014 rollout when a project opts into
Loomweave-owned file identity. Filigree-only projects do not need this runbook:
`registry_backend: local` is still the default and keeps existing behavior.

Refreshed for 3.3.0: the preconditions gain Loomweave's authentication
posture, and "Failure Modes" describes the `REGISTRY_UNAVAILABLE` envelope
every surface now renders instead of a traceback, plus the 16 KiB batch body
cap. Rollback and the unresolved-rows rule are unchanged.

## Preconditions

- Filigree is built with ADR-014 support. Verify
  `GET /api/files/_schema` includes `config_flags.registry_backend_features`
  with both `local` and `loomweave`.
- Loomweave Sprint 3 C-WP10.1 through C-WP10.4 are deployed for the sibling
  project. At minimum, `loomweave serve` must expose
  `GET /api/v1/files?path=&language=` and return
  `{entity_id, content_hash, canonical_path, language}`.
- `GET /api/v1/_capabilities` on that Loomweave answers `api_version: 1`,
  `registry_backend: true` and `file_registry: true`. Filigree gates on these
  at every DB open (the startup capability probe) in that order, then on the
  authentication block below.
- Loomweave's serving mode is one Filigree implements. The capabilities
  `authentication` block must advertise `protected_routes` of `none` or
  `bearer` and `capabilities_probe` of `none`. Filigree sends only
  `Authorization: Bearer` and owns no HMAC contract, so an HMAC-mode Loomweave
  is refused at the probe with `cause_kind=auth_mode_unsupported` — pick a
  bearer or unauthenticated Loomweave bind for the Filigree pairing. A
  Loomweave that omits the block (pre-ADR-056) is treated as unauthenticated
  and keeps working.
- If Loomweave advertises `protected_routes: bearer`, the Filigree process
  must have the Loomweave token exported in the environment variable named by
  `loomweave.token_env` in `.filigree.conf` (default `WEFT_TOKEN`). With the
  variable unset or empty the probe fails fast with
  `cause_kind=auth_token_missing` naming the variable. Filigree only attaches
  a bearer token to a loopback Loomweave origin (`localhost` or a loopback
  IP); a token-bearing request to any other `base_url` is refused before it
  is sent.
- The operator has a restorable backup of the project database
  (`filigree.db` under the store directory — `.weft/filigree/`, or legacy
  `.filigree/`).
- The Loomweave base URL is stable from the Filigree process.

## Fresh Project Setup

1. Start Loomweave's read API for the same project/worktree.
2. Probe the capabilities and a known file:

   ```bash
   curl 'http://127.0.0.1:9111/api/v1/_capabilities'
   curl 'http://127.0.0.1:9111/api/v1/files?path=src/main.py&language=python'
   ```

   Check `authentication.protected_routes` in the first response against the
   preconditions above.

3. Configure `.filigree.conf`. It is a JSON object; add the registry keys
   beside the existing `prefix` and `db` keys (`token_env` is optional and
   defaults to `WEFT_TOKEN`; unknown keys under `loomweave` are rejected):

   ```json
   {
     "prefix": "filigree",
     "db": ".weft/filigree/filigree.db",
     "registry_backend": "loomweave",
     "loomweave": {
       "base_url": "http://127.0.0.1:9111",
       "timeout_seconds": 5,
       "allow_local_fallback": false,
       "token_env": "WEFT_TOKEN"
     }
   }
   ```

4. Start Filigree and confirm the handshake:

   ```bash
   curl http://127.0.0.1:8377/api/files/_schema
   ```

   The response must show `registry_backend: loomweave`. If the process
   instead exits 1 with a `Registry unavailable while opening project
   database (backend=loomweave, cause_kind=...)` line, read the remedy line
   under it — see "Failure Modes".

5. Submit a small scan-result payload and verify the stored file ID is a
   Loomweave entity ID rather than a Filigree-native `*-f-*` ID.

## Existing Project Migration

1. Stop writers that can create file records.
2. Back up the project database and keep the backup outside the project
   database directory.
3. Configure `.filigree.conf` for `registry_backend: loomweave` and the Loomweave
   base URL (and `token_env` if Loomweave serves in bearer mode), as in step 3
   above.
4. Run the dry run:

   ```bash
   uv run filigree migrate-registry --to loomweave --dry-run --json
   ```

5. Inspect every `unresolved` row. Delete stale file rows or repair Loomweave
   indexing before executing. Do not execute with unresolved rows — this rule
   is unchanged in 3.3.0 and `--execute` refuses while any row is unresolved.
   A row whose `error` starts with `BODY_TOO_LARGE:` is a single path too long
   to fit Loomweave's 16 KiB request body cap on its own (see "Failure
   Modes"); it is that row's problem, not a batch-sizing problem, and its
   neighbours still resolve.
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
Loomweave-mode writes accumulate. Nothing here changed in 3.3.0:

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

- **Fail-closed outage renders an envelope, not a traceback.** With
  `registry_backend: loomweave`, `allow_local_fallback: false` and a
  Loomweave that is unreachable, declines the role, or fails the
  authentication gates, the startup capability probe raises
  `RegistryUnavailableError` and every DB-open surface renders the shared
  `REGISTRY_UNAVAILABLE` envelope (filigree-8fd300e2f7, PR #85):
  - **CLI.** Every verb exits 1 with one stderr line — `Registry unavailable
    while opening project database (backend=loomweave, cause_kind=<kind>):
    <detail>` — followed by one remedy line. With `--json` the envelope goes
    to stdout carrying `code: REGISTRY_UNAVAILABLE`, `details.cause_kind`,
    `details.url`, `details.backend: loomweave` and the same remedy under
    `details.hint`. `filigree init` renders the same envelope, and the
    `filigree session-context` hook puts the error and remedy lines inside
    the project snapshot it emits (`WARNING: ...`) rather than failing the
    hook.
  - **MCP stdio.** The server starts in degraded mode instead of dropping the
    transport: `mcp_status_get` answers `status: "registry_unavailable"` with
    the envelope fields, `guidance` (the remedy) and `registry_retry:
    {interval_seconds: 10.0, last_retry_at, next_retry_after}`; every other
    tool call answers the envelope. `call_tool` re-attempts startup at most
    once per 10 s and clears degraded mode once Loomweave is back, so no MCP
    restart is needed after the outage ends.
  - **Dashboard.** Ephemeral startup prints the envelope line and the remedy
    on stderr and exits 1. In server mode a project's lazy open answers the
    envelope with HTTP 503 (other projects keep serving), as does the `/mcp`
    HTTP transport's project resolution. A registry failure raised while
    serving a request against an already-open project (for example
    `POST /api/observations` resolving a new file) is a 503 whose `error`
    reads `while handling request`, not a claim that the database failed to
    open.
  - **The remedy line is specific to `cause_kind`**
    (`registry_errors.registry_unavailable_hint`): `auth_token_missing` and
    `auth` name the token env var to export or fix; `auth_mode_unsupported`
    and `role_declined` point at Loomweave's serving configuration;
    `invalid_response` says to verify `base_url` and match versions and
    offers no fallback (a reachable Loomweave that violates the contract
    fails closed at resolve time even with fallback on); only the
    reachability kinds (`network`, `timeout`, `http_error`, `unknown`) say to
    start Loomweave (`loomweave serve`) or set `allow_local_fallback: true`.
- **`api_version` mismatch** (Loomweave advertises something other than
  `1`) is `LOOMWEAVE_REGISTRY_VERSION_MISMATCH` on the same surfaces, with
  `details.expected` / `details.advertised`. No fallback applies: upgrade
  Filigree or Loomweave to a matching pair.
- `filigree dashboard --allow-local-fallback` (the only surface with a flag;
  every other surface reads the `loomweave.allow_local_fallback` conf key) is
  for single-operator recovery. It routes
  auto-creates through `LocalRegistry` while the project remains configured for
  `loomweave`, and it downgrades every probe-time `RegistryUnavailableError`
  (including `auth_mode_unsupported` and `auth_token_missing`) to a warning;
  do not leave it enabled after the incident. `migrate-registry` never
  accepts the downgrade: a row the fallback resolved locally is reported
  unresolved with a diagnostic naming the mismatch.
- **16 KiB request body cap / HTTP 413 / `BODY_TOO_LARGE`.** Loomweave caps
  every `/api/v1/*` body at 16 KiB and answers an over-cap body with a bare
  `413` before parsing it. Filigree chunks every batch POST by serialised
  bytes (15 KiB target) as well as by the 256-query count, and halves and
  retries a chunk Loomweave still refuses, so a 413 in the logs is only a
  warning about a split, never a failed batch (filigree-b57d4eb7d9). The one
  outcome an operator sees is a single path or locator that cannot fit on its
  own:
  - `POST .../scan-results` ingests the rest of the batch and reports the
    path on the response's `warnings[]` — findings for an unregistered
    over-cap path are not ingested; findings for an already-registered one
    are ingested against the stored identity. Only a batch with nothing left
    to ingest is rejected.
  - `migrate-registry` lists the row under `unresolved` with
    `error: "BODY_TOO_LARGE: ..."` (see step 5 above).
  - `sei-backfill` exits 1 with a `REGISTRY_UNAVAILABLE` envelope quoting the
    HTTP 413 — a locator over 16 KiB is malformed, not a sizing problem.
  - The closure-gate drift check marks only that binding's freshness
    UNKNOWN; a deterministic 4xx never counts as Loomweave being down for the
    rest of a cascade batch.
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

# Make a missing/unusable loomweave a failure instead of a skip:
FILIGREE_REQUIRE_LIVE_LOOMWEAVE=1 uv run pytest tests/integration/test_loomweave_phase_d_e2e.py -m integration
```

The test creates its own tempdir project (calls `loomweave install`,
writes `loomweave.yaml` with an HTTP bind on a free loopback port, spawns
`loomweave serve`) so no project layout is required on disk. CI lanes that
also build Loomweave can opt in by including the integration marker in
their pytest invocation; lanes that do not will silently skip.

`FILIGREE_REQUIRE_LIVE_LOOMWEAVE` uses the shared arming parser in
`tests/federation/_oracle.py`: `1` / `true` / `yes` / `on` arm, `0` /
`false` / `no` / `off` / unset do not, and any other value raises rather
than silently disarming. The scheduled `live-loomweave` CI lane arms it and
points `LOOMWEAVE_STAGING_BASE_URL` at a staging Loomweave for
`tests/integration/test_loomweave_staging_smoke.py`; a manual
`workflow_dispatch` with `require_live_loomweave` additionally runs the Phase
D e2e test and `tests/federation/test_sei_oracle_live_loomweave.py`.

Contract drift against the sibling checkouts is a separate, network-free
lane: `tests/federation/test_sibling_drift.py` compares the vendored goldens
byte-for-byte with the sibling repos located via `LOOMWEAVE_REPO` /
`WARDLINE_REPO` / `LEGIS_REPO` (or `.siblings/<name>` next to the checkout),
skipping when a sibling is absent unless the per-sibling
`FILIGREE_REQUIRE_LOOMWEAVE_REPO` / `FILIGREE_REQUIRE_WARDLINE_REPO` /
`FILIGREE_REQUIRE_LEGIS_REPO` env is armed (same parser). The scheduled
`federation-drift` CI lane checks the three siblings out under `.siblings/`
(private siblings need the `FEDERATION_CHECKOUT_TOKEN` secret), arms all
three, and runs `uv run pytest -m federation_contract
tests/federation/test_sibling_drift.py`.

## Ownership Boundary

Filigree issues for ADR-014 track Filigree code, schema, tests, and docs.
Loomweave Sprint 3 work for C-WP10 is tracked in `/home/user/loomweave/.filigree/`
and should not be filed or closed from the Filigree tracker.
