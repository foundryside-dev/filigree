# ADR-029: Entity-Association Opacity

**Status**: Accepted
**Date**: 2026-05-16 (adopted Filigree-side, schema v15 / 2.1.0); rebrand pass 3.0.0 (schema v26)
**Deciders**: John (project lead)
**Context**: Filigree must let an issue reference a code entity (function, class, module, file) owned by a sibling product — Loomweave — without coupling the two products. This ADR was cited by the implementation before it was written down; this document backfills the decision the code already enforces.

> **Numbering note.** Filigree's own ADR sequence otherwise runs 001–018. The
> number **029** is deliberate: it mirrors the suite-wide entity-association
> concept number (the peer Loomweave decision, originally *Clarion
> ADR-029-entity-associations-binding*), and it is the number cited throughout
> the Filigree code (`db_entity_associations.py`, `mcp_tools/entities.py`,
> `dashboard_routes/entities.py`, `migrations.py`, `db_schema.py`). The four
> numbered Decisions below are referenced from those modules — in particular
> **Decision 3** is cited verbatim as the contract for *who computes drift*.

## Summary

Filigree binds an issue to a sibling-product entity by storing an **opaque
external `entity_id` string** in an `entity_associations` table keyed by
`(issue_id, entity_id)`. Filigree never parses the ID grammar, never resolves it
against the sibling's runtime, and never computes drift — it stores the
caller-supplied `content_hash` verbatim at attach time and returns raw rows.
This keeps two products bound at the data layer with **zero coupling** to the
sibling's identity scheme, runtime, or release cadence. The cost is that
Filigree cannot, by itself, answer "is this binding stale?" — that is the
consumer's job (Decision 3).

## Context

Loomweave (the code-archaeology sibling) owns a queryable map of entities and
mints a stable identifier for each. An agent triaging an issue wants to bind it
to "the function this issue is about," and an agent reading code wants the
reverse: "what issues are about the entity I'm looking at?"

The constraints:

- **No runtime coupling.** Filigree must not embed a Loomweave client, call its
  API on the write path, or fail a write because Loomweave is down or on a
  different version.
- **No identity-grammar coupling.** The identifier scheme is Loomweave's to
  evolve. It has already moved from a mutable **locator**
  (`{plugin}:{kind}:{qualname}`) to a durable, opaque **SEI**
  (`loomweave:eid:<hex>`, per the SEI authority decision; see ADR-017). Filigree
  must survive that migration without a schema change to the *meaning* of the
  column.
- **No schema coupling.** The binding must not be wedged into the existing
  `file_associations` / `file_records` identity, which has six relational
  consumers (see ADR-014).
- **Drift is real.** Code moves; a hash captured at attach time can diverge from
  the entity's live hash. Someone must detect that — but Filigree cannot see the
  live hash.

## Decision

We will store entity bindings as opaque rows and push every interpretation
concern across the product boundary. The contract is four numbered decisions.

### Decision 1: The `entity_id` is opaque; Filigree never parses it

`entity_id` is stored verbatim. It **may** be a `loomweave:eid:<hex>` SEI or a
legacy locator (`{plugin}:{kind}:{qualname}`) — Filigree treats both as opaque
strings and does not validate, canonicalise, or infer structure from them. The
only sanctioned inspection is a **prefix-level** check of the `loomweave:eid:`
marker, used solely by the one-time locator→SEI value migration
(`sei_backfill.py`); no read or write path depends on the ID's internal grammar.
Entity *kind* is never inferred from the ID — it is optional, caller-supplied
metadata (`entity_kind` / its synonym `external_entity_kind`).

### Decision 2: No schema coupling — its own table, no discriminated union

`entity_associations` is a standalone table. It is **not** merged with
`file_associations`, and `entity_id` is **not** routed through `file_records.id`.
Overloading the file-identity column with a discriminated union of "file id or
opaque entity id" would touch every `file_records.id` consumer for no benefit;
the binding earns its own table (this is the same overloading-avoidance reasoning
ADR-014 applies to the file-identity split).

### Decision 3: Drift detection is the **consumer's** job

Filigree stores the caller-supplied `content_hash` as `content_hash_at_attach`
**verbatim** at attach time, and the list endpoints return **raw rows**.
Filigree does **not** compute, cache, or surface a `drift_warning`. The consumer
— Loomweave's `issues_for` read path — compares the stored
`content_hash_at_attach` against the entity's **live** `entities.content_hash` at
query time and decides whether the binding is fresh or stale.

This is deliberate and load-bearing: Filigree cannot see the live hash (that
would require the runtime coupling Decision 1 forbids), so it cannot be the
authority on freshness. It records what was true at attach time and hands the
comparison to the side that can see "now." Both `entity_association_list` and
`entity_association_list_by_entity` (and their HTTP equivalents) therefore return
rows without a drift verdict, by contract — a caller that wants freshness must do
the comparison, not expect Filigree to have done it.

### Decision 4: Project isolation is by DB file

The reverse lookup `entity_association_list_by_entity` returns every issue **in
this project's database** bound to a given `entity_id`. There is no tenant column
and no cross-project query: isolation between projects is the SQLite file
boundary itself. Every row the reverse index (`ix_entity_assoc_entity`) can reach
already belongs to the project hosting that database.

## Surface

Reachable over both transports (the `entity_id` is opaque on every one):

- **MCP**: `entity_association_add`, `entity_association_remove`,
  `entity_association_list`, `entity_association_list_by_entity`,
  `finding_promote_and_attach_entity`.
- **HTTP**: `GET`/`POST /api/issue/{issue_id}/entity-associations`,
  `DELETE …?entity_id=…`, and the reverse `GET /api/entity-associations?entity_id=…`.

`add` is idempotent on the composite key `(issue_id, entity_id)`: re-attaching
refreshes `content_hash_at_attach` and `attached_at` while preserving the
original `attached_by` actor.

## Rebrand note (3.0.0 / schema v26)

The binding shipped (schema v15, 2.1.0) under the sibling's pre-rebrand name: the
column was `clarion_entity_id` and SEIs carried the `clarion:eid:` prefix. The
3.0.0 Loomweave/Weft rebrand renamed the column to `loomweave_entity_id` and the
v26 data migration rewrote every stored `clarion:eid:` prefix to `loomweave:eid:`
in place (across the association column, the `deleted_issues` tombstone
`entity_ids` array, and the association audit events). **The opacity contract
above is unchanged** — only the names moved. The HTTP/MCP request parameter is
and remains `entity_id` (opaque); the renamed identifier surfaces in the *row*
returned by the list endpoints as `loomweave_entity_id`.

## Alternatives Considered

### Alternative 1: Store a typed foreign key to a sibling entity table

**Pros**: referential integrity; Filigree could validate the reference.

**Cons**: couples Filigree's schema to Loomweave's; a Loomweave identity-scheme
change (locator→SEI) becomes a Filigree migration; Filigree cannot own a table it
does not write.

**Why rejected**: the whole point is product decoupling — a typed FK is the
coupling we are avoiding.

### Alternative 2: Resolve the entity against Loomweave's runtime on write

**Pros**: Filigree could reject a bad `entity_id` and could compute drift itself.

**Cons**: puts a cross-product network call on the write path; a write fails when
the sibling is down or version-skewed; binds release cadences together.

**Why rejected**: violates the no-runtime-coupling constraint; trades a write
that always works for one that works only when two products agree.

### Alternative 3: Compute drift inside Filigree

**Pros**: a single `entity_association_list` call returns a freshness verdict.

**Cons**: Filigree has no view of the entity's *live* hash — only the snapshot it
was handed at attach time. To compute drift it would have to call Loomweave
(Alternative 2) or cache live hashes (a second coupling).

**Why rejected**: Filigree structurally cannot be the freshness authority; that
authority belongs to the side that can see "now" — hence Decision 3.

### Alternative 4: Merge into `file_associations` / `file_records.id`

**Pros**: one identity table.

**Cons**: overloads a column with six relational consumers plus `scan_runs.file_ids`
JSON references; a discriminated-union FK touches more code than a new table.

**Why rejected**: same overloading-avoidance as ADR-014 — the binding earns its
own table.

## Consequences

### Positive

- **Zero coupling** to Loomweave's identity scheme, runtime, or release cadence;
  the locator→SEI and Clarion→Loomweave migrations needed no change to the
  binding's *meaning*.
- Writes never fail because of a sibling's availability or version.
- One drift vocabulary (`content_hash`) shared with the file-identity split
  (ADR-014).

### Negative

- Filigree **cannot** answer "is this binding stale?" on its own — every
  freshness consumer must run the Decision 3 comparison.
- Opaque IDs are **unvalidated**: a typo'd or malformed `entity_id` is stored as
  faithfully as a correct one; Filigree will never flag it.
- The reverse lookup requires a dedicated index (`ix_entity_assoc_entity`).

### Neutral

- `entity_kind` is advisory metadata only; it never participates in
  identity or drift.

## Related Decisions

- **Peer**: Loomweave's entity-associations decision (the cross-product concept this number mirrors). Filigree owns the *store* side; Loomweave owns the *consume/drift* side.
- **Related to**: [ADR-014: Registry Backend and File-Identity Displacement](./ADR-014-registry-backend-and-file-identity-displacement.md) — closes the *file*-side of the same Filigree↔sibling identity split, reusing this ADR's `content_hash` drift vocabulary.
- **Related to**: [ADR-017: SEI Conformance — Two-Axis Freshness and Backfill](./ADR-017-sei-conformance-two-axis-freshness-and-backfill.md) — the locator→SEI value migration that runs over the opaque `entity_id` stored here.

## References

- `src/filigree/db_entity_associations.py` — the CRUD + Decision 3 implementation
- `src/filigree/mcp_tools/entities.py`, `src/filigree/dashboard_routes/entities.py` — the wire surfaces
- `src/filigree/migrations.py` (`migrate_v25_to_v26`) — the rebrand data pass
- `src/filigree/sei_backfill.py` — the sanctioned `loomweave:eid:` prefix inspection
- CLAUDE.md — "Cross-product entity bindings (ADR-029)"
