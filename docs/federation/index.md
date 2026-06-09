---
title: Filigree in the Weft Federation
description: How Filigree — the federation's work-state surface — engages each member of the Weft Federation, and the two structural facts (SEI and the weft generation) that hold the federation together.
---

# Filigree in the Weft Federation

Filigree is the **work-state surface** of the [Weft Federation](https://github.com/foundryside-dev/weft)
— an agent-first family of small, local-first developer tools. Each member is
authoritative for exactly one domain, useful entirely on its own, and
**enrich-only — never load-bearing — when composed**: removing any one member
never breaks another member's core flow. Filigree's core flow (tracking work)
runs with no sibling present.

This page explains what Filigree owns in the federation, the two structural
facts that let the members compose at all, and — member by member — how
Filigree engages each one, with each binding's caveat carried in full.

!!! info "Authority"
    This page is authoritative for **Filigree's own facts**. The way Filigree
    *composes* with siblings is governed by the federation hub: the
    [integration matrix](https://github.com/foundryside-dev/weft/blob/main/federation-map.md),
    the [contracts index](https://github.com/foundryside-dev/weft/blob/main/contracts-index.md),
    and the [asterisk register](https://github.com/foundryside-dev/weft/blob/main/asterisk-register.md).
    Where a binding carries a caveat, it is reproduced here verbatim — dropping a
    caveat overstates the binding's maturity.

## What Filigree owns

Filigree is the federation's system-of-record for **work state**. It owns:

- **Issues** — the work items themselves, across **24 issue types** in **9
  workflow packs**, each with an enforced state machine.
- **Dependencies and the critical path** — a dependency graph with a ready-queue
  and critical-path analysis.
- **Workflow state machines** — type-specific transitions; "ready" is not the
  same as "startable", and the workflow refuses illegal transitions.
- **Observations** — a fire-and-forget scratchpad for incidental defects, with a
  14-day TTL unless promoted.
- **Files and scan-findings** — registered files and the findings attached to
  them (the intake siblings post scan results into).
- **The `entity_associations` table** — bindings from a Filigree issue to an
  **opaque external entity id**. Filigree stores the id verbatim and **never
  parses it**; drift detection is the consumer's job (see
  [SEI](#sei-the-connective-tissue) below).

And — structurally — Filigree **hosts the named `weft` HTTP generation** that
several siblings use as their transport into the tracker.

## The two structural facts

Two facts hold the whole federation together. Everything in the per-member
matrix below rests on them.

### SEI — the connective tissue

Every cross-tool binding in the federation keys on the **Stable Entity Identity
(SEI)**, owned by **Loomweave** (the authority). SEI is a durable, opaque
identifier for a code entity that survives renames and moves — so a binding made
today still points at the right function after the code is refactored.

- **SEI is LOCKED** (2026-06-05): the interface is frozen. Remaining member work
  is conformance under the locked standard, not interface change.
- **Filigree stores SEI opaque and never parses it.** The `entity_associations`
  table holds the id as a string; Filigree does not interpret its structure.
- **Drift detection is the consumer's job.** Filigree records a
  `content_hash_at_attach` alongside the binding; the *consumer* (the sibling
  tool reading the binding back) compares against that hash to detect that the
  underlying entity has drifted. Filigree does not chase drift itself.

A combination is only as strong as its weakest binding: a tool that keys on a
mutable locator instead of SEI silently orphans every combination it is in.

See the [SEI standard](https://github.com/foundryside-dev/weft/blob/main/sei-standard.md).

### The `weft` generation is the federation transport

Filigree exposes its HTTP API in two **generations**:

- **classic** — `/api/*` (and `/api/v1/*`), Filigree's own long-standing surface.
- **weft** — `/api/weft/*`, the **named federation transport**.

Siblings pin to a *named, versioned generation* rather than to raw endpoints.
**Evolution is additive**: a breaking change ships as a *new* generation; an
existing generation is never mutated out from under the members pinned to it.

Per Filigree **ADR-002 ("loose cooperation")**, every `weft` endpoint is
**functional with peers absent** — the transport is there for siblings to use,
but Filigree never depends on a sibling being present to serve it.

## How Filigree engages each member

Each cell below is a cross-tool binding from the federation
[integration matrix](https://github.com/foundryside-dev/weft/blob/main/federation-map.md).
Section numbers (§) reference the
[contracts index](https://github.com/foundryside-dev/weft/blob/main/contracts-index.md).

### Loomweave ↔ Filigree — entity ⇄ issue

**Binding (contracts §1).** Loomweave entities and Filigree issues are bound
through the `entity_associations` table, which lives **on Filigree's side**.
Loomweave's reverse lookup (`issues_for`) makes the binding **drift-aware**:
given an entity, it finds every issue bound to it and can tell when the entity
has changed since attach.

Filigree stores the entity id (an SEI going forward) **opaque** and never parses
it; drift is detected by the consumer against `content_hash_at_attach`. This is
the cleanest, most mature binding Filigree has — it is also the surface Legis
reuses for sign-offs (§7, below).

The surface is reachable over Filigree's **classic** generation
(`GET|POST /api/issue/{issue_id}/entity-associations`,
`DELETE …?entity_id=…`, and `GET /api/entity-associations?entity_id=…` for the
reverse lookup) and over MCP (`entity_association_add`,
`entity_association_remove`, `entity_association_list`,
`entity_association_list_by_entity`).

### Wardline → Filigree — findings become tracked work

**Binding (contracts §4).** Wardline trust-boundary findings reach Filigree's
scan-results intake, where they become tracked work — and the emit **pins each
finding's suppression provenance** so a baselined/waived/judged finding is not
re-minted as a fresh issue on ingest.

!!! warning "Caveat — asterisk A-1 (LIVE)"
    Today the (Wardline, Filigree) finding flow is **pipeline-coupled through
    Loomweave's SARIF translator**: the legacy path routes Wardline's SARIF
    through Loomweave's `loomweave sarif import` into the classic
    `POST /api/v1/scan-results`.

    Wardline's **native Filigree emitter has shipped** (posting directly to the
    federation generation, `POST /api/weft/scan-results`), **and** Filigree's
    receiving route is **shipped on `release/3.0.0`**. But asterisk **A-1 stays
    LIVE** until (Wardline, Filigree) composition **with Loomweave absent** is
    demonstrated **end-to-end** — and that live e2e is currently **skipped**.
    Agreement to the direction is not retirement; the asterisk retires only when
    the Loomweave-absent composition is *demonstrated*, not merely agreed.

    So: this binding is **shipping, asterisk live** — not yet fully direct/done.
    See [asterisk-register.md A-1](https://github.com/foundryside-dev/weft/blob/main/asterisk-register.md).

### Legis → Filigree — governed sign-offs on issues

**Binding (contracts §7).** Legis binds **SEI-keyed governed sign-offs** to
Filigree issues, reusing the entity-association surface (§1) plus its own
sign-off endpoints. **Filigree retains issue-lifecycle authority**; Legis adds
governance on top — it does not take ownership of the issue's state machine.

### Charter → Filigree — requirement ↔ work (planned)

**Binding — PLANNED only.** Charter is designed to link requirements to Filigree
work items (requirement ↔ work), but **Charter is scaffold-state**: the
federation adapter is designed in ADRs and **not yet built**. Treat this binding
as a future, not a current capability.

## What is *not* in the federation

To avoid overstating the architecture, three explicit anti-claims:

- **There is no `weft://` URI scheme.** That design space is **closed by SEI** —
  do not imply such a scheme exists.
- **There is no federation registry or broker.** Members compose pairwise on
  SEI and the `weft` transport; there is no central runtime, registry, or broker
  process. (The federation runs **zero** brokers by design.)
- **Lacuna is not a member.** Lacuna is the deliberately-flawed demonstration
  specimen the whole federation is *run against* — point the Weft tools at it and
  they pick up its seeded bugs. It is adjacent to the federation, not part of it.

## Further reading

- [Federation hub (`weft`)](https://github.com/foundryside-dev/weft) — the
  authoritative integration matrix, contracts, and asterisk register.
- [Doctrine](https://github.com/foundryside-dev/weft/blob/main/doctrine.md) — the
  federation axiom and the failure test every binding must pass.
- [SEI standard](https://github.com/foundryside-dev/weft/blob/main/sei-standard.md)
  — the connective tissue every binding keys on.
- [Federation map](https://github.com/foundryside-dev/weft/blob/main/federation-map.md)
  — the at-a-glance integration matrix this page draws from.
- [Federation contracts](contracts.md) — Filigree's own contract detail for the
  bindings above.
