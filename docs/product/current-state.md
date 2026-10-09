# Current State — Filigree        Checkpoint: 2026-10-09 — 4.0 remediation Phase 0 in progress

## The Bet Right Now

The Now bet is the **4.0 remediation plan**
(`docs/superpowers/plans/2026-10-09-filigree-4.0-remediation.md`). Its goal is a
4.0 that the owner's agent fleet can trust on real projects:

- no silent failures,
- a retry-safe core loop,
- an honest "done",
- a surface that shrinks to work state (B2: Wardline owns findings).

Phase 0 is the 3.4.0 do-now work. The formal record of the owner's rulings
(D-A…D-K) is PDR-0006 (plan §1.1), which is not yet written. PDR-0002's
Toolkit-DX bet has the verdict UNKNOWN (PDR-0005).

The Weft critical path has collapsed into this plan. Legis, Warpline, Tabard
and the hub were archived on 2026-10-01. The owner's D2 ruling folds their
useful contracts into Filigree and revives no code. Under D-A, 4.0 has no
runtime coupling to Loomweave.

## In Flight / Recently Closed

- **3.3.0 shipped 2026-09-02** (`main@8771fb3`, tag `v3.3.0`). Its milestone
  `filigree-b21d7a9f17` was closed 2026-10-09 with verdict UNKNOWN: the
  Loomweave integration was never dogfooded, and every fleet project runs
  `registry_backend: local`. Its 20 open children (21 at the D-K ruling, one closed by Task 0.6) wait for
  owner triage.
- **Phase 0 tasks landed on `release/3.3.0`** (since `f9215a5`):
  - 0.1: call-outcome logging and the population tag.
  - 0.2: archived Legis never wedges a close.
  - 0.3: honest session-context READY list.
  - 0.4: holder-checked release and undo; idempotent start-next.
  - 0.8: per-finding `failed[]` on scan ingest.
  - 0.5a: defects-only scan ingest.
  - 0.5c: `filigree finding export` for telemetry findings.
  - 0.6: close responses carry `warnings[]`, with a commit-anchor reachability
    warning (`close_commit_reachable`). This checkpoint and PDR-0005 also
    belong to 0.6.
- **0.5b is done in the Wardline repo** (a defects-only emit gated on
  `accept_kinds`).
- **Still to run in Phase 0:** 0.7 (federation token reconcile), 0.10, 0.9
  (install writes the actor into the hook), 0.11 (Loomweave CI lanes
  non-blocking). 0.12 is an owner release gate.
- **Tracker corrected 2026-10-09:**
  - `filigree-bd1abc7243` was reopened and then closed `wont_do`. Its anchor
    `79e06d6` never reached `main`.
  - `filigree-1544621b0a` was closed `wont_do`, because Warpline is folded in
    (D2).
  - `filigree-1627c6fc7a` (C-20) was closed against `main@4a0a21f`, which
    shipped in 3.2.0.

## Repo / Branch Reality

- `/home/john/filigree` is on `release/3.3.0`, clean after each Phase 0 commit,
  ahead of `origin/main` and not pushed. Merging to `main` is owner-gated.
- Two local-only branches are still checked out in superpowers worktrees. They
  hold evidence that was never merged, and neither is an ancestor of
  `origin/main`:
  - `codex/c16-lead-summaries` (`ecad149`): the recommendation is to abandon
    it, pending an owner decision (PDR-0005).
  - `codex/gs7-warpline-worklist` (`79e06d6`): abandoned under D2. Deleting
    it is pending owner action.
- Nothing was released, tagged or pushed in this checkpoint.

## Open Questions / Blocked-On-Owner

- Rule on `codex/c16-lead-summaries` (abandon or PR). `filigree-afade9b4c6`
  stays open until then.
- Remove both worktrees and delete both evidence branches.
- `filigree-434aa4e145` (this file was false) is addressed by this rewrite.
  Close it once the rewrite is reviewed.
- Triage the 3.3.0 milestone's 20 open children (D-K). The Loomweave-phase
  items are proposed for `wont_do`. The rest need individual verdicts.
- Sign off the D-A…D-K rulings in PDR-0006 and re-confirm the authority grant.
  The grant's monthly review is about four cycles overdue.
- Metrics: `metrics.md` still has no baseline. The Phase 1.6 reading of the
  Task 0.1 call logs is the first measured reading. It decides whether
  PDR-0002's UNKNOWN becomes ACCEPT or REJECT.

## Last Checkpoint Did

- Replaced the 2026-07-07 checkpoint. That checkpoint described a dirty
  `feat/weft-suppression-conformance` checkout and recorded two
  "branch-qualified" closes whose commits never reached `main`.
- Wrote PDR-0005, the 3.0–3.3 retrospective. It covers five releases that had
  no PDR and gives the PDR-0002 verdict as UNKNOWN.
- Made the tracker edits listed above.

## Next Session, Start Here

Continue Phase 0 of the 4.0 plan at Task 0.7 (see
`.superpowers/sdd/2026-10-09-filigree-4.0-remediation/progress.md`). Then bring
the owner-gated items above to the owner. Do not start Phase 1 work packages
until PDR-0006 records the rulings.
