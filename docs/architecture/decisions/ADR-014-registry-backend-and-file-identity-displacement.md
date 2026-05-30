# ADR-014: `registry_backend` Flag and File-Identity Displacement to Clarion

**Status**: Accepted
**Date**: 2026-05-19
**Deciders**: John (project lead)
**Context**: Closes the Filigree-side hole that Clarion ADR-014 (2026-04-18) named as a v0.1 prerequisite and that Clarion ADR-029 (2026-05-16) explicitly deferred. The work is Filigree's; the sibling decision is Clarion's.

## Summary

Filigree gains a pluggable `RegistryProtocol` selected by a `registry_backend` config flag with two modes:

- `local` (default, unchanged behaviour) — Filigree's native UUID-derived file IDs.
- `clarion` (opt-in, per-project) — Filigree delegates file-identity resolution to Clarion's HTTP read API; `file_records.id` stores Clarion's symbolic entity ID (`core:file:{hash}@{path}` per Clarion ADR-003).

A `FILE_REGISTRY_DISPLACED` error code surfaces direct file-registration attempts that conflict with `clarion` mode. The `registry_backend` value is published in `GET /api/files/_schema.config_flags` for capability probing. Fail-closed startup applies only under `clarion` mode (an `--allow-local-fallback` escape exists for single-operator recovery).

The new column `file_records.content_hash` stores the hash Clarion supplied at resolution time, reusing the same drift-vocabulary that ADR-029's `entity_associations.content_hash_at_attach` introduced. There is one drift signal across both surfaces.

## Context

### The gap ADR-029 leaves on the table

ADR-029 (the entity-associations binding) is shipping and is right. It does not, however, close the file-identity split between Filigree and Clarion. Concretely, today, on the 2.1.0 branch:

- `POST /api/loom/scan-results` (`dashboard_routes/files.py:401-417`) routes to `db.process_scan_results(**parsed)`.
- `process_scan_results` (`db_files.py:857-926`) iterates findings and calls `_upsert_file_record(path=f["path"], …)` for each.
- `_upsert_file_record` (`db_files.py:640-678`) mints a Filigree-native ID (`f"{prefix}-f-{uuid4().hex[:10]}"`) the first time it sees a path.
- No code path consults `scan_source` (or the `metadata.clarion.*` payload) to thread Clarion's entity ID through as the `file_records.id`.

The result: every Clarion-sourced scan POST mints a shadow file row whose ID is Filigree-native; an issue with an `entity_associations` row pointing at `python:function:auth.tokens::issue_token` and a `file_associations` row pointing at the file that function lives in carries **two unrelated identities for the same code**. `loom.md` §2's claim that "Clarion owns the file registry" is, today, false at the storage layer.

Two further auto-create paths exhibit the same shadow-mint behaviour: `db_observations.register_file` (`db_observations.py:223`) and the three call sites of `tracker.register_file` in `mcp_tools/scanners.py` (`:657`, `:746`, `:964`).

ADR-029 explicitly named this gap as out of scope and called the registry-backend work "still-scheduled." This ADR is the schedule.

### Why ADR-029's approach is not a substitute

ADR-029's defence — opaque-string IDs, no schema surgery, no Clarion-runtime dependency — answers the question *"how do we let Filigree issues reference Clarion entities without coupling the products?"*. It does not answer *"how do we make the file_id Filigree stores be the same identifier Clarion stores?"*. Those are different questions; ADR-029 solves the first, this ADR solves the second.

### The "thrown away" history

Clarion ADR-014 (2026-04-18) designed this displacement in detail: `RegistryProtocol` trait, `local`/`clarion` modes, `FILE_REGISTRY_DISPLACED` error code, capability probe via `_schema.config_flags`, fail-closed startup, `--allow-local-fallback` recovery flag. The Filigree-side ADR was never drafted; the WP10 work package on the Clarion side was deferred to v0.2 by the Sprint 2 scope amendment (2026-05-16). This ADR adopts Clarion ADR-014's design near-verbatim and is the Filigree-side counterpart that closes the cross-product story.

## Decision

### 1. `RegistryProtocol` interface

A new module `filigree.registry` defines:

```python
class RegistryProtocol(Protocol):
    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile: ...

    def is_displaced(self) -> bool: ...

class ResolvedFile(TypedDict):
    file_id: str           # opaque to Filigree; semantics owned by the backend
    content_hash: str      # opaque to Filigree; used as drift signal only
    canonical_path: str    # backend's preferred canonical form of `path`
    language: str          # may be empty; backend may infer
```

Two implementations:

- `LocalRegistry` — current behaviour. `file_id` is `f"{prefix}-f-{uuid4().hex[:10]}"`. `content_hash` is the empty string under `local` mode (the schema stores a non-null empty-string default; see §3). `is_displaced()` returns `False`.
- `ClarionRegistry` — issues `GET {clarion_base}/api/v1/files?path=…&language=…` and returns Clarion's `{entity_id, content_hash, canonical_path, language}` reshaped into `ResolvedFile`. `is_displaced()` returns `True`. Connection failures surface as `RegistryUnavailableError` (see §6).

The protocol is composed into `FiligreeDB` at construction time; the three auto-create surfaces (`_upsert_file_record`, `register_file`, `tracker.register_file`) take a `registry: RegistryProtocol` parameter instead of generating IDs inline.

### 2. `registry_backend` configuration

`registry_backend` is a project-scoped setting in `.filigree.conf`:

```yaml
registry_backend: local              # default
clarion:
  base_url: http://localhost:9111
  timeout_seconds: 5
  allow_local_fallback: false
```

`local` is the forever-default. Filigree-the-project, every existing Filigree dogfood, and every existing third-party Filigree deployment continue to operate without change. Clarion mode is strictly opt-in per project.

### 3. Schema additions

```sql
ALTER TABLE file_records ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE file_records ADD COLUMN registry_backend TEXT NOT NULL DEFAULT 'local';
```

Both columns survive a backend swap: a row created under `local` and re-resolved under `clarion` updates `content_hash`, `registry_backend`, and (one-time, see §5) the row's `id`.

Schema version bumps; migration is forward-only and additive (no FK rewrites, no row identity churn under default `local` mode).

### 4. Capability probe

`GET /api/files/_schema` gains a `config_flags` block:

```json
{
  "config_flags": {
    "registry_backend": "local",
    "registry_backend_features": ["local"],
    "allow_local_fallback": false
  }
}
```

`registry_backend_features` enumerates what this Filigree build *can* serve (always `["local"]` after Phase B; `["local", "clarion"]` after Phase C). Clarion's startup probe reads this; absent the field, Clarion enters shadow-registry mode.

### 5. ID rewrite policy under backend swap

A project that flips from `local` to `clarion` mid-life will have existing `file_records` rows with Filigree-native IDs and multiple consumers pointing at those IDs. The displacement story therefore needs a row-ID rewrite path:

- A new CLI verb `filigree migrate-registry --to clarion [--dry-run]` issues `resolve_file` for every existing row, fetches Clarion's entity ID, and rewrites `file_records.id`, six relational file-ID consumers (`scan_findings.file_id`, `file_associations.file_id`, `file_events.file_id`, `observations.file_id`, `observation_links.file_id`, and `annotations.file_id`), and `scan_runs.file_ids` JSON references inside a single SQLite transaction. The `entity_associations` table introduced in PR #42 is verified to *not* hold file IDs, only entity IDs, so it is untouched here.
- Rows whose paths Clarion cannot resolve (deleted-on-disk, outside-project, etc.) are flagged in the manifest; the operator chooses delete-row or keep-as-orphan.
- Rollback uses the same manifest in reverse.

The migration is not run automatically. A capability-probe mismatch (registry says `clarion` but rows have `registry_backend = 'local'`) raises `RegistryStateMismatch` on next write and halts auto-create paths until the operator runs the migration or reverts the flag.

### 6. `FILE_REGISTRY_DISPLACED` error code

Under `clarion` mode, the following direct-mutation paths return `FILE_REGISTRY_DISPLACED`:

- MCP tool `register_file`.
- CLI verb `filigree register-file`.
- HTTP `POST /api/files` direct-create (if/when it exists; currently not exposed).

The error message includes the Clarion read URL the operator should use instead.

Implicit auto-create paths route through `RegistryProtocol` and never raise this code — they get Clarion IDs transparently. This is intentional asymmetry: direct operator requests to create file identity in Filigree are displaced, while issue/scan/annotation workflows that need a file row as supporting metadata resolve that identity through Clarion. The implicit surface currently includes:

- `FiligreeDB.register_file` when called by internal DB and scanner flows.
- `FiligreeDB.process_scan_results` through `_upsert_file_record`.
- `ObservationsMixin.create_observation`.
- `AnnotationsMixin.annotate_file`.
- Scanner and finding surfaces such as `report_finding`, `preview-scan`, `trigger-scan`, and `trigger-scan-batch`.

`delete_file_record` is intentionally not displaced. It is Filigree-local
administrative cleanup of a stored tracking row and its local associations or
findings; it does not delete or mutate the Clarion entity identified by
`file_id`. Operators may delete stale Filigree rows in `clarion` mode by
explicit file ID, subject to the same local conflict/`--force` rules as
`local` mode.

### 7. Fail-closed startup under `clarion` mode

If `registry_backend: clarion` is configured but the Clarion HTTP read API is unreachable at Filigree startup, implicit auto-create paths return `503 Service Unavailable` with `RegistryUnavailableError`. Read paths (`GET /api/loom/files`, `GET /api/loom/issues/.../files`) continue to operate against stored rows.

`allow_local_fallback: true` (in `.filigree.conf` or via `--allow-local-fallback`) downgrades the failure to a `WARN` and routes auto-creates through `LocalRegistry`. The flag is for single-operator recovery, not steady-state operation; the dashboard surfaces a banner while it is active.

### 8. Living surface, classic surface

The `registry_backend` flag affects *behaviour*, not API shape. Both classic (`/api/v1/scan-results`) and loom (`/api/loom/scan-results`) handlers continue to accept identical payloads. Under `clarion` mode, the `file_id` returned in responses is a Clarion entity ID rather than a Filigree-native ID; the *shape* is unchanged (`file_id: str`). This is a contract-level addition, not a break: ADR-002 generation freezes apply to shapes, not to ID grammars.

## Alternatives Considered

### Alternative 1 — Keep ADR-029 only; never close the file-identity split

`entity_associations` covers the cross-product reference need for issues. Files keep Filigree-native IDs forever; `loom.md` §2's "Clarion owns the file registry" is informally downgraded to "Clarion owns the entity catalog; Filigree owns the file mapping."

**Why rejected**: the downgrade is real but unstated; consumers reading `loom.md` get one story, the code does another. Either fix the code or fix the doctrine. Fixing the code is the cheaper of the two because the design already exists (Clarion ADR-014) and the surface is bounded (5–8 files; ~17 test files reference `file_id` directly).

### Alternative 2 — Single mode, always-Clarion (no flag)

Drop `local`; always delegate. Filigree without Clarion fails to start.

**Why rejected**: violates `loom.md` §4 composition law and §5 enrichment failure test. Filigree-the-project (which uses Filigree to track its own work) would require Clarion to operate, which is absurd. The flag is the price of staying federated.

### Alternative 3 — Generalize `entity_associations` to carry file IDs too

Add an `association_kind: 'file' | 'entity'` discriminator to `entity_associations`; let files ride.

**Why rejected**: same reason ADR-029 rejected merging file_associations and entity_associations — overloading. `file_records.id` is referenced by six relational consumers plus `scan_runs.file_ids` JSON references; routing those references through a discriminated union would touch more code than the `RegistryProtocol` refactor and would leave `file_records.id` itself still shadowed.

### Alternative 4 — Schema-level join across two DBs (Filigree + Clarion)

`file_records.id` becomes a foreign key into `.clarion/clarion.db`.

**Why rejected**: `loom.md` §6 — no shared store. Each product owns its storage. The HTTP-mediated `RegistryProtocol` is the federation axiom expressed as code.

## Consequences

### Positive

- `loom.md` §2 "Clarion owns the file registry" becomes honest at the storage layer.
- Cross-tool "same file" queries get a deterministic answer: same ID across products under `clarion` mode.
- Reuses ADR-029's drift vocabulary (`content_hash`); one mental model for both file-level and entity-level drift.
- `local` stays default; no impact on Filigree-only deployments.

### Negative

- Two code paths per auto-create operation. Test surface doubles for file-registry behaviour (parameterise the test suite over `registry_backend ∈ {local, clarion}`).
- One synchronous RPC hop per Filigree write that touches `file_records` under `clarion` mode. Loopback HTTP cost ~1–5ms; acceptable for developer workloads. `ClarionRegistry` does not retry failed HTTP calls in this release; failures surface immediately as `RegistryUnavailableError` so operators do not accidentally duplicate writes across products. Batched resolution and retry policy are deferred together for high-throughput scans, where Clarion can define idempotency and partial-failure semantics explicitly.
- Cross-product launch sequencing: under `clarion` mode the operator must start Clarion's HTTP read API before Filigree, or set `--allow-local-fallback` for recovery.
- The `migrate-registry` CLI verb is a one-way operation in practice (rollback only works inside the reversibility window). Documented as a hard boundary.

### Neutral

- Classic and loom HTTP shapes unchanged. ADR-002 generation discipline applies to shape, not ID grammar; `clarion` mode's swap of ID grammar is contract-compatible.
- `entity_associations.clarion_entity_id` is still opaque to Filigree under `clarion` mode — the two surfaces remain independent. The same Clarion entity ID may appear in both `file_records.id` (for the file the entity lives in) and `entity_associations.clarion_entity_id` (for the entity itself, e.g. a function inside that file); the relationship between them is Clarion's domain, not Filigree's.

## Sequencing (cross-project)

The work has a fixed one-way dependency: Filigree's `clarion` mode is a no-op until Clarion ships an HTTP read API.

| Phase | Owner | Scope |
|---|---|---|
| **A** | Clarion | Add an `axum`-based HTTP read server to `clarion-cli/src/serve.rs`. Expose `GET /api/v1/files?path=&language=` returning `{entity_id, content_hash, canonical_path, language}`. Wire into `clarion serve`. Surface in `clarion.yaml`. Document in Clarion's contracts directory. |
| **B** | Filigree | Land `RegistryProtocol` interface and `LocalRegistry`; refactor `_upsert_file_record`, `register_file`, and the three `tracker.register_file` call sites to consume the protocol. Behavior-preserving — no flag yet, default-only. Schema migration adds `content_hash` and `registry_backend` columns (empty values under `local`). |
| **C** | Filigree | Add `registry_backend` config flag, `ClarionRegistry` impl, capability probe (`_schema.config_flags`), `FILE_REGISTRY_DISPLACED` error code, fail-closed startup, `--allow-local-fallback` escape, the `migrate-registry` CLI verb. |
| **D** | Both | Cross-process integration tests against a live Clarion read API. Parity tests parameterised over `registry_backend ∈ {local, clarion}`. Capability-probe handshake tests. |
| **E** | Both | Documentation: Filigree `docs/federation/contracts.md` references the Clarion read surface; Clarion's `loom.md` §2 claim is restated as factual rather than aspirational; cross-project launch runbook published. |
| **F** | Both | Clarion 1.0 wire-contract closures (landed 2026-05-19, day-of-1.0 prep): batch resolve via `POST /api/v1/files/batch` (CONTRACT-1, chunked at 256); Bearer auth via `Authorization` header sourced from env var named by `clarion.token_env` (CONTRACT-2, default `CLARION_LOOM_TOKEN`); briefing-blocked surfaces as HTTP 403 + `{"code": "BRIEFING_BLOCKED"}` and propagates uncaught past the local-registry fallback (CONTRACT-3). |

### Path normalization (CONTRACT-4)

Paths sent to Clarion on both `GET /api/v1/files` and `POST /api/v1/files/batch` are **lexical**, **forward-slash**, **project-relative** strings. Filigree normalizes paths at the boundary (`db_files._normalize_scan_path`); backslashes are converted to forward slashes and `.` / `..` segments are removed. **Disk presence is not required**: Clarion looks up entries by its `source_file_path` catalog column, not a filesystem probe. A path that resolves cleanly inside the project root but has no file on disk still has an entry in Clarion's catalog and resolves successfully (this is the normal case for synthetic findings produced by static scanners against transient build artefacts).

Phase A must ship before Phase C can land an integration that does anything observable; B is independent and can ship first behind a flagless refactor. Phase F lands once Clarion 1.0 freezes its wire surface.

## Related Decisions

- **ADR-002** (this repo) — `registry_backend` is *behaviour*, not a generation. Loom/classic HTTP shapes are unchanged; this ADR is contract-compatible by construction.
- **ADR-029 of Clarion** — entity_associations is the peer concept; this ADR closes the file-side of the same split. Same drift vocabulary (`content_hash`).
- **ADR-014 of Clarion** — original 2026-04-18 design; this ADR is its Filigree-side counterpart and adopts the design near-verbatim.
- **ADR-015 of Clarion** — Wardline→Filigree native emitter; not in scope here. Wardline's findings continue to flow via Clarion's SARIF translator under both backends.
- **Loom URI spec (draft 2026-05-17)** — orthogonal; URIs and registry-backend are independent decisions. Not yet ratified; not used as a cross-tracker reference primitive in this ADR.

## References

- Clarion ADR-014: `/home/john/clarion/docs/clarion/adr/ADR-014-filigree-registry-backend.md`.
- Clarion ADR-029: `/home/john/clarion/docs/clarion/adr/ADR-029-entity-associations-binding.md`.
- Clarion v0.1 plan §WP10: `/home/john/clarion/docs/implementation/v0.1-plan.md` (the cross-product work package this ADR fulfils).
- Sprint 2 scope amendment (defer): `/home/john/clarion/docs/implementation/sprint-2/scope-amendment-2026-05.md`.
- Clarion integration recon: `/home/john/clarion/docs/clarion/v0.1/reviews/pre-restructure/integration-recon.md` (auto-create paths and FK survey).
- Filigree auto-create paths (verified 2026-05-19):
  - `src/filigree/db_files.py:186` `register_file`
  - `src/filigree/db_files.py:663` `_upsert_file_record`
  - `src/filigree/db_observations.py:223` `register_file`
  - `src/filigree/db_annotations.py:655` `register_file`
  - `src/filigree/cli_commands/scanners.py:382,:579,:1051` `tracker.register_file`
  - `src/filigree/mcp_tools/scanners.py:657,:746,:964` `tracker.register_file`
