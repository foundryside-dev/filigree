# Resolution: governed→ungoverned bypass via the signature field

**Status:** IMPLEMENTED on release/3.0.0 (owner-approved). Schema v27.
**Repro:** `tests/core/test_governance_signature_bypass.py` (now green).
**Date:** 2026-06-06

## Implementation summary (as built)

Both vectors closed; resolution (b) is **sticky governance + fail-closed-on-drift**.
Three refinements emerged from the pre-implementation scout+critique pass and
were adopted over the original sketch:

1. **Distinct `GateOutcome.STALE`** (not reused `UNAVAILABLE`). The finding cascade
   short-circuits the whole batch on the first `UNAVAILABLE` (treats it as
   "Legis down"); a per-issue drift verdict must not suppress Legis for the rest
   of the batch. STALE renders as `CONFLICT` (409) on every surface (the
   renderers already collapse non-PROCEED/non-INTEGRITY to CONFLICT) and flows
   to reconciliation debt via the cascade's existing non-PROCEED path.
2. **MCP stays signature-preserving** (option b): only Legis signs, via the HTTP
   binding route; an agent's MCP re-attach preserves the existing sign-off and a
   drift surfaces as STALE until Legis re-signs. No MCP schema change.
3. **Debt recording stays caller-side** (cascade only); the gate remains
   side-effect-free. Direct dashboard/MCP/CLI closes render STALE as a 409; the
   "record debt" line applies to the cascade path only.

Edit set: `db_schema.py` (column + v27), `migrations.py` (`migrate_v26_to_v27`
+ backfill), `db_entity_associations.py` (`_normalise_optional_signature`,
sticky-CASE UPSERT, `signed_content_hash` through TypedDict/serializer/3 SELECTs),
`governance.py` (`STALE` + `_signed_row_is_stale` + is-not-None predicate),
`db_meta.py` (import threads the column), `mcp_tools/entities.py` +
`finding_issue_cascade.py` (docstrings). Tests: bypass repro now green; new
drift/legacy/mixed/real-DB gate tests; flipped the "documented clobber" test to
sticky; migration + round-trip + schema-version tests.

---

## Original analysis (decision record)

## Two contract facts that decide this

**Fact 1 — the signature is an HMAC over the content.** It is
`HMAC({issue_id, entity_id, content_hash, signoff_seq}, legis_key)`
(rebrand inventory, `…rebrand-inventory.md:123`, attributed to the user). The
sign-off is **cryptographically bound to a specific content snapshot.** When the
content drifts `h1→h2`, the stored signature is *provably* a signature over `h1`
— not "maybe stale," demonstrably not a signature over `h2`.

**Fact 2 — Filigree is a router, not a verifier or an adjudicator.**
- It never verifies the signature — "it has no key; Legis owns governance"
  (`db_schema.py:430-433`, `legis_client.py:6`).
- The closure-gate call sends Legis **only the issue id** —
  `GET {LEGIS_URL}/filigree/issues/{issue_id}/closure-gate` (`legis_client.py:94`).
  The signature, content hash, and signoff_seq are **never transmitted.**
- The gate re-consults Legis live on every close for governed issues
  (`governance.py:91`).

So in Filigree the `signature` column is a local **"should I call Legis?"
routing flag** — but Fact 1 means its *presence over the right content* is the
property that matters, and Filigree **can see** when that property breaks:
at re-attach it holds both `h1` and `h2` and already records the delta
(`db_entity_associations.py:236-240`; `test_reattach_records_refresh_audit_event`).

> **Correction to an earlier draft of this memo.** A prior version claimed
> "Filigree cannot detect drift, so adjudication is necessarily Legis's." That
> is false — see the re-attach event above. The false premise made
> *preserve-the-signature* look safe. It is not (see Decision (b)).

## Federation posture (who owns what)

| Component | Role re: governance |
|---|---|
| **Legis** | Sole governance authority. Only signer (holds the key). Sole adjudicator. The HMAC binds sign-off to content. |
| **Filigree** | Work-state authority. Routes governed closes to Legis. Must never *make or unmake* governance, and must not let a drifted sign-off pass as valid. |
| **Loomweave** | Entity-identity + content-hash authority. The **drift source**. Read-only to governance. |
| **Wardline** | Assurance / finding producer. Read-only to Legis signatures. |

Cardinal rule the bypass violates: **only Legis confers governance, over
specific content.** Today a routine agent drift-refresh (a work-state op,
Loomweave-driven) silently revokes it.

## Decision

### Vector (a) — empty-string signature — FIX NOW (release gate)
`""` is a non-null value the routing flag misreads as "don't ask," contradicting
DECISION 1A ("governed = non-null signature"). Fix at the boundary **and** the
predicate:
- Normalize `signature = signature or None` at every write boundary
  (`dashboard_routes/entities.py`, `mcp_tools/entities.py`,
  `add_entity_association`) → column is strictly `{real-signature | NULL}`.
- Align the read to `is not None` (`governance.py:89`).

Unambiguous bug; both changes cheap.

### Vector (b) — signatureless re-attach of a governed binding — sticky governance + **fail-closed on drift**
Split the case by whether the content hash actually changed (Filigree knows this
at re-attach):

- **Unchanged hash** (idempotent no-op refresh): **preserve** the signature.
  Nothing drifted; still governed at the same content. Robust, trivial.
- **Changed hash** (genuine drift of a governed binding): the existing signature
  is now an HMAC over the *old* content (Fact 1) — invalid for the new content.
  Keep the issue **governed** (do not silently de-govern — that's the bug) **but
  mark it pending-reverification so the close gate FAILS CLOSED**
  (`UNAVAILABLE`/reconciliation-debt) until **Legis re-signs over the new hash.**

**Why not the cheap "preserve on absence" (2-line CASE-mirror) alone.** It keeps
a signature that is *cryptographically known to be wrong* for the current
content, and — because the gate call is issue-id-only — Legis cannot see the
drift from Filigree's request. Its correctness then hinges entirely on an
**unverified** assumption: that Legis independently learns the new content hash
(from Loomweave) and re-derives the HMAC to BLOCK. If that channel does not
exist, preserve-alone **launders a stale close through a "Legis approved" stamp**
— strictly a fail-open, and worse than today's clobber because it *suppresses*
the "needs review" signal instead of just dropping oversight. We have **no
evidence** that Loomweave→Legis drift channel exists; it lives in a repo not
visible here. **Do not bet the gate on it.**

Fail-closed-on-drift closes the hole **regardless of Legis internals** and is
faithful to Fact 1. It preserves the idempotent-refresh contract Loomweave
depends on (the refresh *succeeds*, the hash updates) — it only refuses to let a
*drifted* governed issue *close* on a stale sign-off.

**Implementation sketch (one option, not prescribed).** A nullable
`signed_content_hash` column (v26): set on signed writes; on a signatureless
re-attach, preserve it while `content_hash_at_attach` advances. Gate logic:
governed = `signature IS NOT NULL`; *fresh* = `signed_content_hash` matches
current content (or is NULL for legacy rows) → consult Legis; *stale* =
mismatch → fail-closed `UNAVAILABLE` "awaiting Legis re-sign." Reuses the
existing `UNAVAILABLE` outcome and reconciliation-debt surface.

**Reject:** *clobber-to-NULL* (silent any-agent de-governance — the bug);
*preserve-alone* (laundered fail-open, see above); *block-the-refresh* (breaks
Loomweave's idempotent drift refresh).

### The two repro tests partition the fix space
- `test_empty_string` → green under predicate-fix **or** preserve.
- `test_signatureless` (NULL/MCP) → green only under sticky-governance fixes; a
  predicate change alone leaves it red.
- A *third* test should be added for **drift** (governed `h1` → signatureless
  re-attach at `h2` → close must NOT proceed without a fresh Legis sign-off).
  Preserve-alone would pass `test_signatureless` but **fail** this drift test —
  which is the whole point.

## The open question the owner must resolve (decision-critical, not additive)
**Does Legis re-adjudicate content drift?** i.e. on the issue-id-only gate call,
does Legis independently know the current content hash (via Loomweave) and BLOCK
when the stored sign-off no longer covers it?
- **If yes:** preserve-alone is *sufficient* (Legis catches it). Fail-closed
  local is then belt-and-suspenders.
- **If no / unknown:** fail-closed-on-drift in Filigree is **required** — it is
  the only option that closes the hole without trusting an unverified channel.

Verify against Legis before sign-off. Until verified, recommend fail-closed.

## North star (post-3.0.0)
- Key governed-ness off **`signoff_seq` monotonicity** (inherently sticky)
  rather than signature-presence.
- Give Legis an **explicit revoke verb**; never rely on a NULL write through a
  work-state surface to remove governance.

## Provenance caveat
The "documented/intentional governed→ungoverned flip" comment
(`db_entity_associations.py:181-184`) should be treated as **suspect**, not
authoritative — it rests on the assumption that re-attachers carry the signature
forward, which the actual surfaces (MCP can't supply one; Legis omits when no
key) contradict. Several governance decisions in this area read as post-hoc
rationalizations of scope ("ship the refresh, declare the flip intentional").
This resolution is re-derived from the contract (Facts 1–2), not from the
inherited comments.
