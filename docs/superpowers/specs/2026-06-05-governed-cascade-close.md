# Governed finding→issue cascade close — design & decision

**Date:** 2026-06-05 · **Status:** Decided (Design A) · **Context:** PR #52 review finding "Legis H-02" (review M-/H-02); umbrella plan `/tmp/filigree-3.0.0-remediation-plan.md`.

## Problem

When a scan finding resolves (`fixed` / `unseen_in_latest`), the finding→issue
cascade auto-closes the linked issue. If that issue is **governed** — it carries
a Legis sign-off, i.e. an `entity_associations` row with a non-null `signature`
(v25, B1) — the close must respect the Legis **closure gate**. Today it does
not: `_close_issue_for_fixed_finding_tx` (`db_files.py:1931`) closes with
`force=True` and never consults Legis. A governed issue is auto-closed on a
scanner's say-so, bypassing governance.

### Corrected premise (the review got this wrong)

The PR #52 review and umbrella-plan v1/v2 asserted that fixing this is
"invasive" because "the cascade runs inside the writer transaction and the data
layer must not make the Legis network call there." **That premise is false for
the close path.** Verified against `db_files.py`:

- The close cascade runs **post-commit, outside the ingest transaction**
  (`db_files.py:1519-1544`; the comment at 1493/1519 says "Post-commit … Runs
  OUTSIDE the ingest transaction").
- The retention/age-out path is the same: `@_in_immediate_tx` sits on the inner
  `_sweep_stale_findings_to_fixed` (`:2003-2051`); the public
  `clean_stale_findings` (`:2053`) runs the sweep, commits, then loops the close
  cascade post-commit (`:2069-2075`).
- The shared wrapper `_close_issue_for_fixed_finding` (`:1922`) carries **no**
  transaction decorator; only the inner `_close_issue_for_fixed_finding_tx`
  (`:1930`) opens its own `BEGIN IMMEDIATE` per close.

So **both** callers reach the close helper post-commit, outside any enclosing
transaction. A Legis network call placed in the wrapper violates no
transaction boundary. This collapses the cost gap between the two designs.

## Decision

**Adopt Design A** (consult the gate; Legis may approve a cascade auto-close of
a governed issue). **Design B is specified below and deferred** as the
zero-network degradation mode / future fallback.

Rationale: with the post-commit correction, A costs essentially the same as B
to build (B's debt machinery + one `evaluate_closure_gate` call + an
approved-close branch), and it gives the better product behaviour — genuinely
resolved governed issues still auto-close when Legis approves, instead of
piling up as manual reconciliation debt. B remains the right design **if** A's
synchronous-network cost (below) becomes unacceptable.

---

## Design A — consult the gate in the post-commit cascade (CHOSEN)

Place the governance check in the shared wrapper `_close_issue_for_fixed_finding`
(`db_files.py:1922`), before delegating to the transactional close. Because
`evaluate_closure_gate` (`governance.py:72`) already short-circuits cheaply —
`NOT_CONFIGURED` when `LEGIS_URL` is unset, `PROCEED` for an ungoverned issue
(no associations with a signature), both **without a network call** — only a
governed issue in a Legis-configured deployment incurs the network round-trip.

Flow per resolved finding (post-commit, both callers):
1. `decision = evaluate_closure_gate(self, issue_id)`.
2. `decision.allowed` → `_close_issue_for_fixed_finding_tx(...)` (existing close;
   re-validates finding-still-resolved / no-open-sibling / not-already-terminal
   under the writer lock).
3. **blocked / unreachable / integrity-failure** → do **not** close; record
   **idempotent** reconciliation debt, append to `stats["warnings"]`, return
   `False`. (This is Design B's behaviour, as A's degraded branch.)

Properties:
- **No `is_governed` helper needed.** `evaluate_closure_gate` encapsulates the
  governed/ungoverned/unconfigured logic. (The architecture review's
  "extract a shared predicate into governance.py" was a Design-B artifact and
  evaporates here — do not port it.)
- **The reopen cascade is intentionally NOT gated.** Governance gates *closure*,
  not reopen; a regressed finding reopening a governed issue is correct and
  needs no Legis approval. Leave `_reopen_issue_for_regressed_finding` untouched.
- **Idempotency required on the blocked branch.** A persistently-blocked
  governed issue is re-evaluated every ingest/sweep; the debt write
  (`finding_issue_cascade.record_reconciliation_debt_comment`, `:58-60`, a plain
  INSERT) must become idempotent (guard on an existing same-issue debt row, or
  `INSERT OR IGNORE` against `(issue_id, author, text)`), else the debt comment
  accumulates per run. An *approved* close is terminal and never re-triggers, so
  only blocked issues churn — less than under B (where all governed issues do).
- **Observability reuses existing wiring.** The post-commit loops already lift
  cascade warnings to `stats["warnings"]` and `logger.warning`
  (`db_files.py:1532-1539`; age-out `:2076-2077`). The blocked-close warning
  flows through the same path — no new surface.

### A's primary cost (and why B exists)

A adds a **synchronous Legis call on the ingest/sweep hot path**, per governed
close, with `legis_client` defaulting to a **5 s timeout**
(`DEFAULT_TIMEOUT_SECONDS`). If Legis is slow or down and a batch resolves *N*
governed findings, the naive loop incurs up to *N × 5 s* of serial timeouts in
the post-commit phase, hanging the scan-ingest response.

**Mitigation (ship with A):** batch short-circuit. Once any gate call returns
`UNREACHABLE`/`INTEGRITY_FAILURE`, treat the remainder of the batch as
deferred-debt **without** re-calling Legis. Implement by having the wrapper
report a distinguishable "unreachable" outcome and the orchestration loop carry
a `legis_down` flag that skips further gate calls for the rest of that batch.
This bounds the worst case to a single timeout per batch.

This synchronous-network cost on the ingest hot path is precisely **why Design B
is retained**: B is the zero-network degradation mode. If the latency or the
operational coupling of A to Legis availability becomes unacceptable, switch the
gate branch to "treat all governed as deferred-debt" (Design B) — the debt
machinery, the list surface, and the post-commit structure are identical, so the
switch is a one-branch change.

---

## Design B — refuse governed cascade closes (DEFERRED · future fallback)

**Behaviour:** the cascade never consults Legis. In the post-commit close
helper, detect governed locally (any associated row has a `signature` — the same
cheap read `evaluate_closure_gate` does first, with the same `is_configured()`
guard so a non-Legis deployment still auto-closes) and **refuse** the auto-close,
recording idempotent reconciliation debt. A governed issue is never auto-closed
by a scan; closing it requires the explicit, already-gated close surface
(`dashboard_routes/issues.py`, `mcp_tools/issues.py`, `cli_commands/issues.py`),
which *does* call `evaluate_closure_gate`.

**Pros:** zero network on the ingest hot path; no coupling of scan-ingest
latency to Legis availability; strictly fail-closed; simplest possible code.

**Cons:** genuinely-resolved governed issues do not auto-close — they accrue as
reconciliation debt requiring an explicit gated close. Debt volume tracks the
*count of governed issues*, not Legis-downtime events, so a project that governs
many issues generates steady manual toil. (Per the systems review, that toil is
also a signal that governance may be applied too broadly.)

**When to switch from A to B:** if A's synchronous Legis call on the ingest path
causes unacceptable latency, flakiness, or an availability coupling that
operators reject. Because A and B share the post-commit structure, the idempotent
debt write, the reconciliation-debt list surface, and the warnings/log wiring,
the switch is changing one branch in `_close_issue_for_fixed_finding` from
"call the gate, close if allowed, else debt" to "if governed, debt; else close."

**Implementation note for B (if ever taken):** B *does* need the
`is_governed(reader, issue_id)` predicate (governed AND `is_configured()`)
extracted into `governance.py` — the architecture review's placement constraint
applies (the `_AssocReader` Protocol at `governance.py:59-64` forbids putting it
in the db layer with governance importing it). A does not need this.

---

## Shared between A and B (already required, ship regardless)

- **Idempotent reconciliation-debt write** (`finding_issue_cascade.py:58-60`).
- **Reconciliation-debt list surface** (umbrella B5): a cross-issue read verb
  (CLY + MCP) that lists issues carrying debt, discriminating on
  `author = 'filigree:reconciliation'` (`RECONCILIATION_DEBT_ACTOR`,
  `finding_issue_cascade.py:22`) — **not** a `LIKE '[reconciliation-debt]%'`
  scan on the unindexed `comments.text`.
- A *retry/sweep* verb (re-attempt deferred closes) is a 3.1.0 follow-up under
  both designs; under A it re-runs the gate, under B it routes through the
  explicit gated close.

## Implementation

See `docs/plans/2026-06-05-governed-cascade-close-design-a.md` for the TDD task
breakdown. Tracking: epic + B2/B5 child issues (filed 2026-06-05).
