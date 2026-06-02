# ADR-017: SEI Conformance — Two-Axis Freshness and the Locator→SEI Backfill

**Status**: Accepted
**Date**: 2026-06-02
**Deciders**: John (project lead)
**Context**: The Loom suite is moving every cross-tool entity binding off the mutable **locator** (`{plugin}:{kind}:{qualname}`) and onto the durable, opaque **SEI** (`clarion:eid:<hex>`). Clarion is the authority (Clarion ADR-038): it mints an SEI for every entity and serves `POST /api/v1/identity/resolve[:batch]`, `GET /api/v1/identity/sei/{sei}`, and a `_capabilities.sei` advertisement. Filigree stores Clarion entity ids opaquely in `entity_associations.clarion_entity_id` (Clarion ADR-029, implemented Filigree-side in `db_entity_associations.py`, schema v15). The roadmap (`docs/superpowers/specs/2026-06-01-filigree-roadmap-to-first-class.md` §2.1 / App. A) states that opaque storage is *necessary but not sufficient* for SEI conformance: Filigree is conformant only once (1) the one-time locator→SEI value migration has run and (2) the shared §8 conformance oracle passes. This ADR records the Filigree-side decisions that close that gap.

## Summary

Three Filigree-side decisions:

1. **The two-axis freshness model is split by ownership.** Filigree owns the **content axis** (FRESH/STALE) via `content_hash_at_attach`; Clarion owns the **identity axis** (ALIVE/ORPHANED) via `resolve_sei`. Neither axis infers the other; Filigree never serves a binding without the stored content hash that lets a caller inspect freshness, and never silently re-points a binding.
2. **The locator→SEI migration rewrites the value in place — it is not a schema migration and is not auto-run.** The `clarion_entity_id` column, its wire shape, and its opaque storage are unchanged; only the stored *value* changes. The rewrite is an operator-invoked CLI command (`filigree sei-backfill`) because it makes outbound Clarion calls, which the entity-association data layer forbids.
3. **No-false-green is preserved end to end.** A locator that no longer resolves is flagged ORPHAN and kept verbatim — never silently dropped — and the historical `deleted_issues.entity_ids` tombstones are migrated too so the `affected_entities` change feed is SEI-only after cutover (REQ-F-01).

## Decision

### 1. Two-axis freshness, split by ownership

A binding has a clean, no-false-green truth only when *both* axes agree; neither is inferred from the other.

- **Content axis (Filigree-owned): FRESH / STALE.** `content_hash_at_attach` is snapshotted at attach time and returned verbatim on every read (`list_entity_associations`, `list_associations_by_entity`). FRESH means the entity body is unchanged since attach; STALE means re-verify. Filigree computes nothing and compares nothing here — the consumer (Clarion's drift check) does the comparison. This axis is **unchanged** by SEI.
- **Identity axis (Clarion-owned): ALIVE / ORPHANED.** Whether the entity still exists under a carried SEI is Clarion's `resolve_sei` answer. Filigree's contribution is to store the SEI opaquely and expose it on its read surface so a dossier call can key on it; it does not attempt to track liveness itself.

"Same entity + unchanged code" therefore requires *both* a live identity axis (from Clarion) and a FRESH content axis (from Filigree). Filigree's no-false-green obligation is the two negatives: it never serves an association without `content_hash_at_attach`, and it never silently re-points an association to a different id.

### 2. Value migration in place, operator-invoked, never auto-run

The migration rewrites `entity_associations.clarion_entity_id` from a locator to its resolved SEI. The column name, the `TEXT NOT NULL` storage, and the wire shape are **unchanged** — opacity is exactly what makes this safe across the frozen surface: a consumer honouring the opacity contract sees no break; only a consumer that had illegally depended on the three-segment locator grammar is affected, and that was already forbidden.

It is **not** a `apply_pending_migrations` schema migration and is **not** run at startup, for one load-bearing reason: it makes **outbound Clarion calls** (`POST /api/v1/identity/resolve:batch`). The entity-association data layer is covered by a federation sentinel test (`tests/test_entity_associations_federation.py::test_filigree_runs_with_no_outbound_clarion_calls`, plus `::test_no_clarion_module_import`) that forbids any outbound socket from that path. The resolve client therefore lives on the network-allowed registry layer (`registry.py::ClarionRegistry.resolve_locators_batch`), and the orchestration lives in `sei_backfill.py`, driven by the operator-invoked `filigree sei-backfill` verb (default dry-run; `--execute` applies).

The only schema change is **additive**: a nullable `entity_associations.migration_orphaned_at` marker column (schema v22). NULL is healthy; a timestamp flags an orphan for review. It is metadata about the migration, not part of the opaque id.

#### Idempotent and resumable (REQ-F-02, proven not assumed)

A stored value already carrying the reserved `clarion:eid:` prefix is skipped without a network call — the single sanctioned inspection of the otherwise-opaque id. Clarion additionally **rejects** any SEI-shaped input to `resolve` with HTTP 400 (REQ-F-02, verified against Clarion's `validate_locator`), so even if a re-run ever submitted an SEI it would be rejected, never mis-resolved. A backfill that fails partway is simply re-run: already-migrated rows are skipped and it converges. The cursor is implicit: "rows whose value still lacks the `clarion:eid:` prefix and aren't orphan-stamped."

A primary-key collision — two locators on one issue resolving to the same SEI — is collapsed to a single row (newest `attached_at` wins; `attached_by` preserved), not a crash.

### 3. No-false-green: orphans kept, tombstones migrated (REQ-F-01)

- **Orphans are flagged, never dropped.** When Clarion answers `alive:false` (its `not_found` channel) or rejects a malformed locator (its `invalid` channel), the locator is kept verbatim and stamped `migration_orphaned_at`; the `sei-backfill` report lists every orphan with a reason (`unresolved` vs `invalid`) for human review.
- **Historical tombstones are migrated too.** `deleted_issues.entity_ids` (schema v21) holds the locators captured when an issue was hard-deleted; the synthetic `issue_deleted` record on `GET /api/loom/changes` surfaces them as `affected_entities`. The playbook requires a single hard cutover with **no mixed locator/SEI window** on any federation feed. The backfill therefore rewrites these JSON arrays locator→SEI as well (orphans kept verbatim, deduped), so the `affected_entities` feed is SEI-only after cutover.

### 4. The production run is owner-scheduled, not fired by this command

`filigree sei-backfill` is the *machinery*, not the *trigger*. The actual production cutover is a coordinated cross-tool release (Clarion mints and serves SEIs; Filigree and Wardline re-key together under a write freeze; feeds flip atomically; see Clarion's `sei-migration-playbook.md`). This command exists so that, when the suite owner schedules the cutover, Filigree's re-key is a proven, idempotent, resumable operation rather than a bespoke script.

## Conformance — proven, not asserted (no grandfathering)

Filigree participates in the shared §8 oracle (`fixtures/sei-conformance-oracle.json`, vendored from Clarion at `tests/federation/fixtures/`). The producer obligations are proven two ways:

- **Fast lane** (`tests/federation/test_sei_conformance_oracle.py`): all six scenarios from the producer side, plus every backfill branch, against the Clarion HTTP stub. A drift-check test pins the vendored fixture to Clarion's canonical copy.
- **Faithful lane** (`tests/federation/test_sei_oracle_live_clarion.py`, `@pytest.mark.integration`): round-trip + opacity + orphan against a live `clarion serve`. The Clarion-internal carry semantics (rename/move/ambiguous) are proven on the authority side by Clarion's own run of the same fixture (`cargo test -p clarion-storage --test sei_conformance_oracle`).

## Consequences

- **Frozen surface preserved.** No wire break: a new opt-in CLI verb, additive capability detection, and an additive nullable column. SEI conformance is a one-time backfill a team switches on, not a tax every project pays.
- **`affected_entities` is SEI-only after cutover** — no consumer needs a mixed-format tolerance window on that feed (resolves the REQ-F-01 question the roadmap flagged).
- **Degrades honestly against a pre-SEI Clarion.** If `_capabilities.sei.supported` is false/absent, the backfill refuses cleanly ("identity unavailable; nothing to migrate") and the tracker keeps working on locators — the oracle's `capability_absent` scenario.

## Alternatives considered

- **Add a separate `sei` column alongside `clarion_entity_id`.** Rejected: it contradicts the roadmap's "the stored value changes, nothing else does," doubles the storage and every read's branching, and re-introduces the grammar-coupling the opacity contract forbids.
- **Run the backfill inside `apply_pending_migrations`.** Rejected: it would put outbound network calls in the offline schema-migration path and trip the federation sentinel; migrations must stay deterministic and network-free.
- **Drop unresolvable locators during the backfill.** Rejected outright: silently dropping a binding is the canonical no-false-green violation. Orphans are flagged and retained.
