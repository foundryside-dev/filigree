# Filigree product-state critique: ahead of the next major refresh

**Date:** 2026-10-07 · **Reviewer:** `product-decision-critic` (actor `claude-filigree`) · **Mode:** read-only red-team
**Lane:** product only (what / why / for-whom / did-it-work, plus the authority boundary). Sibling reviewers cover agent experience, the MCP contract, the business-process model, dashboard UX and the HTTP API. Items in their lanes are routed out in §7.

**Version note.** The brief said "refresh filigree for 3.0". The repo is at **v3.3.0** (`59053a3`, released 2026-09-02, merged to `main` at `8771fb3`). This critique assumes the refresh means the **next major (4.0)**. The refresh package should say so explicitly (§8, Q10).

## Inputs read

- `docs/product/`: `vision.md` (with the authority grant), `roadmap.md`, `metrics.md`, `current-state.md`, `decisions/0001–0004`, `prd-0001-agent-broadcast-board.md`, `2026-06-16-filigree-to-hub-tabard-seam.md`.
- Root `ROADMAP.md`, `README.md` and `CHANGELOG.md` (3.0.0 to 3.3.0), plus `git log` with tags and branch ancestry.
- Live tracker, read-only: `stats_get`, `metrics_get` (90d), `work_ready`, `dependency_critical_path`, `issue_get` on the cited epics, milestone and steps, and the recent-closure list.
- **Fleet evidence (new, read-only):**
  - MCP `tool_call` logs from 14 local projects' `filigree.log`.
  - Read-only `sqlite` counts over 14 local Filigree DBs.
  - The archived Weft hub tracker (`~/archive/weft`).
  - Wardline's tracker and `doctor.py`.
  - `~/project-archive-2026-10-01.md`.
  - Public repo stats (`gh repo view`, read-only).

Nothing was written except this file. No filigree mutations, branch changes or commits were made.

---

## 1. Findings (machine-readable)

```json
{
  "summary": {"high": 6, "med": 8, "low": 2},
  "authority_boundary": "CLEAR",
  "authority_boundary_note": "No ungated irreversible/outward action found. Release sign-off for 3.0.0–3.3.0 is not provable from artifacts (no PDRs); the grant's monthly review is ~4 months overdue; the refresh itself will contain escalate-first actions (breaking public release, deprecations/kills, public ROADMAP rewrite, vision change re: federation).",
  "findings": [
    {
      "id": "P-1", "severity": "high", "tag": "STILL-OPEN (PDR-0002; X-4 filigree-8f6a1599fb, X-5 filigree-af55859975, X-6 filigree-b789da2a1e)",
      "anti_pattern": "Continuity Loss / Strategy Drift", "question": "continuity of what/why",
      "sheet": "product-ownership-operating-model.md / product-state-and-continuity.md",
      "location": "decisions/0002 vs git v3.0.0..HEAD, CHANGELOG 3.1.0–3.3.0, milestone filigree-b21d7a9f17",
      "evidence": "PDR-0002 made agent DX the Now bet and capped the federation tail ('finish these, do not expand'). Since v3.0.0, ~36 of 63 non-merge commits are federation/registry/governance and ~9 are agent-facing (keyword classifier, approximate). 3.1.0 added the Warpline seam (schema v29). 3.3.0 was led by 'owner-set priorities (1) improve integration with Loomweave'. The bounded tail (X-4/X-5/X-6) is still open 16 weeks later. There is no PDR-0005.",
      "failure_mode": "The workspace says the bet is agent DX, but the product was built as a federation integration layer. A refresh shaped from the workspace would chase a strategy nobody is executing, and a refresh shaped from momentum would ratify a pivot nobody decided on the record.",
      "remediation": "Before any refresh bet, write a PDR that explicitly reaffirms or supersedes PDR-0002, citing what actually happened in 3.1–3.3 and why. Reverse a PDR only via a new PDR."
    },
    {
      "id": "P-2", "severity": "high", "tag": "STILL-OPEN (PDR-0001, PDR-0002, metrics.md)",
      "anti_pattern": "The Build Trap + The Acceptance Gap", "question": "did-it-work",
      "sheet": "product-metrics-and-experimentation.md / delivery-orchestration-and-acceptance.md",
      "location": "metrics.md (all rows); epic filigree-18bd3b8c98; src/filigree/mcp_server.py:1007,1025",
      "evidence": "Every metrics.md target is dated 2026-09-30 and has now expired with BASELINE still unset. The Toolkit DX epic (the PDR-0002 Now bet) closed 2026-06-20 with close_reason 'All child work ... is already terminal': a sweep, not an ACCEPT verdict. PDR-0001 required owner-set targets before any bet was accepted, and that never happened. PDR-0002 trigger (b) needs the north-star instrumented, so it cannot fire. The existing tool_call log records error envelopes as INFO success, and schema/unknown-argument rejections return before logging (2 tool_error lines in 5,452 calls), so dead-ends cannot be read from current instrumentation.",
      "failure_mode": "The Now bet was banked on output (children closed) with no reading of the problem it was meant to move. The north-star has never been measured, so no bet (past or refresh) can be accepted or killed on evidence.",
      "remediation": "The refresh must name the instrument and take a baseline reading before committing the bet. Replace the placeholder targets with owner-set, dated numbers. Record an explicit ACCEPT/REJECT/UNKNOWN verdict for PDR-0002 (UNKNOWN is honest)."
    },
    {
      "id": "P-3", "severity": "high", "tag": "NEW",
      "anti_pattern": "The Feature Factory", "question": "what",
      "sheet": "delivery-orchestration-and-acceptance.md / product-anti-patterns.md",
      "location": "README.md:12,24,28; src/filigree/mcp_tools/; templates_data.py; fleet logs + DBs",
      "evidence": "118 registered MCP tools; 66 distinct tools ever called in 5,452 logged calls across 14 projects (2026-03 to 2026-10). The top 12 account for 89.4% of calls and the top 20 for 95%. About 52 tools have zero logged calls, including every annotation_* except one read, all scan_*/scanner_*, warpline_worklist_ingest, finding_report/promote and nearly all plan_*. Five of the 9 workflow packs (risk, roadmap, incident, debt, spike) are not enabled in any local project. Fleet DBs hold 7 annotation rows (all in Filigree's own repo) and 10 entity-association rows. About 25k findings have been ingested; in the four largest stores (~25.4k), 4 are 'fixed', 21 false_positive and 8 linked to an issue. The planning-pack deprecation (2026-05-17 draft) has been in limbo with no decision. There is no recorded 'no' anywhere in the workspace.",
      "failure_mode": "The surface grew on every release with no gate that asked whether the last addition moved anything. Each unused subsystem still carries migration, contract and documentation cost into every major, and dilutes the agent's tool selection (tiers.py exists because of this).",
      "remediation": "Make 'no / not now / kill' a first-class recorded decision (PDR). The refresh package must contain a kill/keep table driven by usage evidence, with the HTTP and CLI gaps closed first (see Caveats). Each kill of a used feature is escalate-first."
    },
    {
      "id": "P-4", "severity": "high", "tag": "STILL-OPEN (filigree-434aa4e145, filigree-1544621b0a, filigree-afade9b4c6)",
      "anti_pattern": "The Acceptance Gap (cross-product)", "question": "did-it-work",
      "sheet": "delivery-orchestration-and-acceptance.md / prd-and-acceptance-criteria.md",
      "location": "filigree-bd1abc7243 (close_commit filigree@79e06d6); hub weft-87443311a0, weft-13f84c77c5, weft-f1cbd27cfb; branches codex/gs7-warpline-worklist, codex/c16-lead-summaries",
      "evidence": "`git merge-base --is-ancestor` shows neither 79e06d6 (GS-7 Warpline oracle) nor ecad149 (C-16 lead summaries) is on HEAD or main. Even so, Filigree closed filigree-bd1abc7243 at 79e06d6, and the hub closed weft-87443311a0, weft-13f84c77c5 (GS-7 parent) and weft-f1cbd27cfb (C-16) on that evidence. The landing steps were filed as P2 under the 3.3.0 milestone, and 3.3.0 shipped without them. Records are wrong in the other direction too: filigree-1627c6fc7a (C-20) is open although C-20 shipped in 3.2.0. (Hub cells weft-6a1fdb0192, weft-096266aa27 and weft-aee5769607 also remain open; Filigree's half of each shipped, but those cells may correctly stay open for other members such as wardline-88a7c08286, so they are not counted as false.) Milestone filigree-b21d7a9f17 is still 'planning' after the 3.3.0 release.",
      "failure_mode": "The product's core promise is being the trustworthy work-state authority, and its own records are false in both directions. A cross-product deliverable was reported done to the federation while it exists in no release.",
      "remediation": "Before refresh scoping, land or revert both evidence branches and reopen/close the affected records in both trackers. Treat this as a precondition, not refresh scope."
    },
    {
      "id": "P-5", "severity": "high", "tag": "NEW",
      "anti_pattern": "Continuity Loss / Strategy Drift (premise invalidated)", "question": "for-whom / why",
      "sheet": "vision-strategy-and-roadmap.md / product-ownership-operating-model.md",
      "location": "~/project-archive-2026-10-01.md; vision.md 'Who it serves'; PDR-0003 goals #1–#2; PDR-0004; roadmap Next; CLAUDE.md Legis/Warpline sections",
      "evidence": "On 2026-10-01 the Weft hub, Legis, Warpline, Tabard, Plainweave and Lacuna were moved to ~/archive. Only Loomweave and Wardline remain active. The hub tracker's last update was 2026-08-07, with 62 issues still open. Filigree's standing frame ('adult in the room of the Weft suite', 'SEI backbone'), PDR-0004, roadmap Next, fail-closed Legis governance and the Warpline seam all presuppose those members. No PDR or vision note records the change.",
      "failure_mode": "The refresh could double down on federation seams whose counterparties the owner has shelved (wasted build plus a permanent contract tax), or strip seams that are still live. In both cases the 'who is it for' question is unanswered.",
      "remediation": "Escalate to the owner, because a vision or strategy change is reserved under the grant. The owner must state which siblings are live and whether the federation remains Filigree's strategy. The refresh bet cannot be chosen before this."
    },
    {
      "id": "P-6", "severity": "high", "tag": "STILL-OPEN (filigree-434aa4e145) + NEW",
      "anti_pattern": "Decision-without-Provenance", "question": "continuity of why",
      "sheet": "product-state-and-continuity.md",
      "location": "docs/product/decisions/ (last PDR 2026-06-16); current-state.md (2026-07-07)",
      "evidence": "Five public releases (3.0.0, 3.0.1, 3.1.0, 3.2.0, 3.3.0) shipped with zero PDRs. Other unrecorded calls: the 3.3.0 priority set, C-20 adoption, the ethereal-to-ephemeral vocabulary change, schema v29 and the Warpline seam. The workspace has been untouched since 2026-07-07. current-state.md describes a dirty checkout on a merged branch, cites a test file that does not exist on main, and anchors closures to unmerged commits. It shipped publicly in 3.2.0.",
      "failure_mode": "The next owner cannot tell deliberate pivots from drift. Every refresh argument will be relitigated from memory rather than resumed.",
      "remediation": "Backfill one PDR covering 3.0–3.3 (decisions, who decided, reversal triggers) and rewrite current-state.md from git and the tracker. Cheapest high-severity finding to close."
    },
    {
      "id": "P-13", "severity": "med", "tag": "NEW",
      "anti_pattern": "Autonomy Overreach (preventive)", "question": "the authority boundary",
      "sheet": "product-ownership-operating-model.md / product-state-and-continuity.md",
      "location": "vision.md 'Authority grant' (last reviewed 2026-06-16, cadence monthly); PDR-0001 reversal trigger",
      "evidence": "The grant review is about 4 cycles overdue, and PDR-0001's own 'revisit once the owner confirms vision and Now/Next' never fired. The refresh will almost certainly contain escalate-first acts: a breaking public release, deprecation of used features (e.g. findings ingest used by Wardline across 4 repos), a public ROADMAP rewrite, and a vision change about the federation.",
      "failure_mode": "An autonomous owner would shape a kill-heavy major under a grant nobody has re-read since the strategy changed.",
      "remediation": "Re-confirm the grant with the owner before the refresh PRD. List every escalate-first act in the refresh package as an explicit owner gate."
    },
    {
      "id": "P-7", "severity": "med", "tag": "STILL-OPEN (PDR-0003, PRD-0001, filigree-9927145adc, filigree-fbc9410ded)",
      "anti_pattern": "Solution-in-Search-of-a-Problem (partial) + unfireable reversal trigger", "question": "why / did-it-work",
      "sheet": "product-discovery-and-opportunity.md / product-metrics-and-experimentation.md",
      "location": "roadmap.md Now; dependency_critical_path; PDR-0003 reversal trigger",
      "evidence": "The broadcast board has been a Now bet since 2026-06-16, and T1 has been unclaimed for 16 weeks. It is still the tracker's critical path (T1 → T5), which session-context surfaces to every agent. The kill clock starts 'within 4 weeks of MVP landing', so it never starts. The 'sleeper hit like observations' rationale is not supported: observe accounts for 122 of 5,452 logged calls (2.2%) and fleet DBs hold 28 live observations (TTL-expiring, so this understates). The collision problem is asserted, but no incident count exists.",
      "failure_mode": "A zombie Now bet occupies the roadmap and points every session's orientation at work nobody is doing. It can neither be killed nor accepted.",
      "remediation": "Make a recorded keep/kill call in the refresh. If it is kept, re-validate the problem with a counted collision baseline and add a start-by time-box to the trigger."
    },
    {
      "id": "P-8", "severity": "med", "tag": "STILL-OPEN (PDR-0004, filigree-81d3971467, hub weft-560f243c95)",
      "anti_pattern": "Decision-without-Provenance (trigger lacks an abandonment branch)", "question": "continuity of why",
      "sheet": "product-state-and-continuity.md",
      "location": "roadmap.md Next 'Tabard consumer adapter'; decisions/0004",
      "evidence": "The bet is gated on a Tabard GO and a hub-blessed handle. Tabard's last commit (2026-06-16) was intake only, with 'no code, no spike brief yet', and the repo is now archived. weft-560f243c95 is open in a hub inactive since 2026-08-07. PDR-0004's trigger anticipates a NO-GO, not abandonment.",
      "failure_mode": "The roadmap carries a bet whose gate counterparty no longer exists, and filigree-81d3971467 stays 'approved' forever.",
      "remediation": "Make a recorded kill-or-reframe call in the refresh PDR. Add an 'abandoned or dependency-dead' branch to reversal triggers going forward."
    },
    {
      "id": "P-9", "severity": "med", "tag": "NEW",
      "anti_pattern": "Roadmap-as-Promise + anti-goal conflict", "question": "what (intent vs commitment)",
      "sheet": "vision-strategy-and-roadmap.md",
      "location": "/ROADMAP.md (content last substantively updated 2026-05-19) vs docs/product/roadmap.md",
      "evidence": "There are two roadmaps telling different stories. The public one stops at v2.1. Its 'Phase 1: Immediate' lists work already shipped (fcntl → portalocker: 0 fcntl imports, portalocker in hooks.py; 6 → 10-character IDs). It also proposes items the vision refuses: a visual workflow designer and bottleneck analytics (human-first PM suite), GitHub bidirectional sync (external party), and a vector-store sidecar for semantic dedup (overlaps Loomweave's semantic search, i.e. annexation).",
      "failure_mode": "External readers get a direction the owner has disowned, and the agent workspace has no authority over the public artifact.",
      "remediation": "The refresh must retire or rewrite ROADMAP.md as intent derived from docs/product/roadmap.md. This is a public artifact, so the rewrite needs an owner gate."
    },
    {
      "id": "P-10", "severity": "med", "tag": "STILL-OPEN (filigree-b21d7a9f17, filigree-6fd5b4db6b, filigree-2575e37a7b)",
      "anti_pattern": "The Acceptance Gap", "question": "did-it-work",
      "sheet": "delivery-orchestration-and-acceptance.md",
      "location": "3.3.0 milestone; fleet config.json; .github live lane",
      "evidence": "3.3.0's headline bet was Loomweave integration, yet every local project runs registry_backend=local, including Loomweave's own repo. The dogfood step filigree-6fd5b4db6b is pending. The only live end-to-end lane is red by design (LOOMWEAVE_STAGING_BASE_URL unprovisioned, PARKED at P4), so validation is against vendored goldens only. The milestone is still 'planning' with three pending phases after the release.",
      "failure_mode": "The integration was shipped as done without anyone, including its author, running it, so there is no evidence it serves a user.",
      "remediation": "Run the ACCEPT step (dogfood or a live lane) or record UNKNOWN, then close the milestone with a verdict before the refresh builds further on it."
    },
    {
      "id": "P-11", "severity": "med", "tag": "NEW",
      "anti_pattern": "Vanity Metrics", "question": "did-it-work",
      "sheet": "product-metrics-and-experimentation.md",
      "location": "README.md:12,24,28; scan_findings tables; metrics_get",
      "evidence": "The public value proposition leads with surface counts ('118 MCP tools', '24 issue types across 9 packs'), numbers that only rise. The findings store is a growing stock: in Filigree's own DB, 9,800 of 9,973 findings are 'open', and of those 8,695 are low and 1,104 info, mostly Wardline engine facts. metrics_get exposes flow metrics only (90-day throughput 15), and nothing reads outcome.",
      "failure_mode": "Bigger surface reads as progress, so the build trap is reinforced from the README down.",
      "remediation": "Position on the outcome (loop completion, handoff without collision) and pair every growth number with a guardrail."
    },
    {
      "id": "P-12", "severity": "med", "tag": "NEW",
      "anti_pattern": "Solution-in-Search-of-a-Problem", "question": "why",
      "sheet": "product-discovery-and-opportunity.md",
      "location": "the refresh brief itself; CHANGELOG majors; fleet stores",
      "evidence": "'A major refresh' is the solution, and no problem statement exists yet. There were three breaking majors in about 5 months (2.0 Apr/May, 3.0 Jun). The owner's own fleet still lags: 6 project dirs carry the legacy `.filigree/` store (keisei and echelon at schema 8), and INSTALL_VERSION ranges from 17 to 29. Each break also invalidates agent instructions and skills in every installed project.",
      "failure_mode": "The version number drives scope instead of a validated problem, and migration cost lands on the only users there are.",
      "remediation": "State the problem and the bet first. A major is justified only if the bet requires a break, and the cost to the fleet must be named."
    },
    {
      "id": "P-14", "severity": "med", "tag": "STILL-OPEN (filigree-60a5103dee)",
      "anti_pattern": "Decision-without-Provenance", "question": "continuity of why",
      "sheet": "product-state-and-continuity.md",
      "location": "CHANGELOG 3.2.0 'Removed ACTOR_MISMATCH warnings'; ADR-012",
      "evidence": "A recorded policy (ADR-012) was reversed in a minor release with no ADR revision or PDR. The open issue itself asks for one. It bears directly on PDR-0004's identity model (verified_actor retained as provenance only).",
      "failure_mode": "The identity story now has three unreconciled sources: ADR-012, PDR-0004 and 3.2.0 behaviour.",
      "remediation": "Record the reversal, and fold identity into the refresh's kill-or-reframe call (P-8)."
    },
    {
      "id": "P-15", "severity": "low", "tag": "STILL-OPEN (filigree-1977c738f1)",
      "anti_pattern": "Scope decision parked under a shipped milestone", "question": "what",
      "sheet": "vision-strategy-and-roadmap.md (route mechanics to MCP-contract reviewer)",
      "location": "pyproject.toml:32 mcp>=1.0,<2; dependabot PR #76",
      "evidence": "The decision on MCP SDK 2.0 / protocol 2026-07-28 is parked as a P2 step under the shipped 3.3.0 milestone. The pin is <2 and Filigree uses the low-level mcp.server.Server, not fastmcp, so installs do not break today.",
      "failure_mode": "The primary user's host protocol moves while the product's major is scoped without deciding whether it moves with it.",
      "remediation": "The refresh scope must state 'in' or 'out' for MCP 2.0. Route the mechanics to the MCP-contract reviewer."
    },
    {
      "id": "P-16", "severity": "low", "tag": "STILL-OPEN (wardline-88a7c08286, hub weft-096266aa27)",
      "anti_pattern": "Cross-product obligation pushed via changelog", "question": "what",
      "sheet": "route to Wardline",
      "location": "wardline src/wardline/install/doctor.py:825",
      "evidence": "Wardline still compares mode == \"ethereal\", while Filigree 3.3.0 writes \"ephemeral\". A fresh ephemeral project falls through to 'no daemon reachable, start it'. The status is still 'ok', so only the remedy text is wrong. A Wardline counterpart issue exists.",
      "failure_mode": "Misleading diagnostic only. It is not a P0.",
      "remediation": "Leave it to Wardline's tracked fix, and include it in the refresh's cross-product obligations list so it is not lost."
    }
  ]
}
```

---

## 2. Executive summary

Filigree's product discipline effectively stopped when the workspace was written (2026-06-16, last touched 2026-07-07), while the product kept shipping: five public releases (3.0.0 to 3.3.0), a schema bump (v29) and a strategic pivot toward Loomweave/federation integration, none of it recorded in a PDR. The dominant cluster is **did-it-work collapse plus drift**:

- The north-star has never been measured, and every falsifiable target expired on 2026-09-30.
- The Now bet was closed by sweep.
- The federation work that PDR-0002 capped grew past its cap.
- The tracker's own records are false about what shipped.
- The strategic premise (a live Weft federation) was archived six days ago.

Before scoping anything, the change that removes the most blast radius is to **get an owner ruling on whether the federation is still the strategy (P-5) and take a real north-star baseline (P-2)**. Every other refresh decision depends on those two.

## 3. Authority-boundary verdict: **CLEAR**, with three conditions

- **No ungated action was found.** No artifact shows an irreversible or outward-facing action taken by the agent without the human gate. The 3.3.0 milestone text records "Owner-set priorities (2026-09-02)", and commits carry the owner's identity.
- **Release gates are not provable.** No PDR records sign-off for the 3.0.0 to 3.3.0 public releases, so the gate is assumed, not evidenced (P-6).
- **The grant is stale.** Its review cadence is monthly, it was last reviewed 2026-06-16, and the strategy it governs has since changed (P-5, P-13).
- **The refresh contains escalate-first acts.** Each of these must be gated by the owner, never by the agent:
  - a breaking public release (PyPI, tag, GitHub release)
  - any kill or deprecation of a used feature, e.g. findings ingest is consumed by Wardline in 4 repos, and the planning pack is used in every project
  - a rewrite of public `ROADMAP.md`
  - any change to `vision.md` "Who it serves" or the anti-goals in light of the federation archive

## 4. Top-3 risks by product blast radius

1. **P-5, the federation premise.** The refresh may invest in or preserve seams whose counterparties are shelved, or cut ones that are live. **Cost:** the wrong thing gets built, and the contract tax is permanent.
2. **P-2, no baseline.** No refresh bet can be accepted or killed on evidence. The fourth major would repeat the build trap at larger scale. **Cost:** value is never validated.
3. **P-4, false records.** The work-state authority reports cross-product work as delivered when it is in no release. **Cost:** the product's core promise is broken in its own repo, and the "did-it-work" record cannot be trusted for any acceptance.

---

## 5. Reality reconciliation (the workspace against what is actually true)

| Workspace claim (`current-state.md` 2026-07-07, `roadmap.md` 2026-06-16) | Reality on 2026-10-07 | Source |
|---|---|---|
| Checkout dirty on `feat/weft-suppression-conformance` | Clean `release/3.3.0`; that branch merged via PR #82 into 3.2.0 | `git status`, `80050fb` |
| C-16 lead summaries "branch evidence complete", hub closed | `ecad149` on no release; landing step `filigree-afade9b4c6` pending | `merge-base --is-ancestor` |
| GS-7 oracle closed at `filigree@79e06d6` | `79e06d6` not on main; test file absent from tree; `filigree-1544621b0a` pending | same; `filigree-434aa4e145` |
| "Weft critical path moved to Loomweave `weft-7931a32599`" | Closed 2026-07-07; hub inactive since 2026-08-07; hub archived 2026-10-01 | `~/archive/weft` tracker |
| Now: agent DX (PDR-0002) | Epic closed by sweep 2026-06-20; 3.1–3.3 mostly federation | §1 P-1, P-2 |
| Now: broadcast board | T1 untouched 16 weeks | `filigree-fbc9410ded` |
| Now: bounded federation tail X-4/5/6 | All three still open; unbounded federation work shipped around them | `work_ready` |
| Next: Tabard adapter, gated on GO + hub handle | Tabard never spiked, now archived; hub archived | `~/archive/tabard` |
| metrics.md targets "by 2026-09-30" | All expired, BASELINE unset | `metrics.md` |
| 3.0.0 "ready, owner-gated synchronised push" | Shipped 2026-06-17, followed by 3.0.1, 3.1.0, 3.2.0 and 3.3.0 | tags |
| Unrecorded changes | 3.2.0 removed ACTOR_MISMATCH; C-20 injections cut 5739 → 792 B; 3.3.0 renamed `ethereal` → `ephemeral` and added Loomweave auth posture | CHANGELOG |

**Credit where due.** On 2026-09-02 the 3.3.0 work was run through the claim → work → close loop with commit anchors: 12 of 13 closures were claimed, with claim-to-close times mostly 24 to 40 minutes and `close_commit=release/3.3.0@…`. The loop itself works in dogfood. C-20 (3.2.0) is real agent-DX value: the always-loaded context is about 7× smaller.

## 6. Findings walk-through (by failure mode)

### 6.1 Did-it-work has never been answered (P-2, P-10, P-4)

**Anti-pattern:** Build Trap and Acceptance Gap. **Closing sheets:** `product-metrics-and-experimentation.md`, `delivery-orchestration-and-acceptance.md`.

`metrics.md` was honest that its numbers were placeholders. The failure is what came after: no one read them, the dates passed, and a bet was closed anyway. The "Toolkit DX" epic, the Now bet, closed because its children were terminal. That is "done means merged", which is the definition of the build trap.

The instrument problem is concrete:

- `mcp_server.py` logs `tool_call` at INFO after *any* handler return, including an `INVALID_TRANSITION` error envelope.
- Unknown-argument and schema-validation rejections return before logging.
- So "claim → close without a dead-end" **cannot** be computed from existing data. The 2 `tool_error` lines in 5,452 calls say nothing about the dead-end rate. Do not read them as a high completion rate.

The same pattern appears at release level. 3.3.0's Loomweave integration was accepted without dogfood (every local project is on `registry_backend=local`, including Loomweave's own repo) and without a live lane (red by design).

At record level (P-4), closures were banked on unmerged commits and propagated to the hub. That is the acceptance gap crossing a product boundary. Per the owner's rule, it is flagged high rather than left as the P2/P3 "land via PR" steps it was filed as.

**Remediation:** take the baseline before the bet; give PDR-0002 and 3.3.0 an explicit verdict (UNKNOWN is acceptable and honest); land or revert the evidence branches and correct both trackers as a precondition.

### 6.2 Feature factory and vanity framing (P-3, P-11)

**Anti-pattern:** Feature Factory and Vanity Metrics. **Closing sheets:** `delivery-orchestration-and-acceptance.md`, `product-metrics-and-experimentation.md`.

**Surface usage evidence** (local MCP stdio logs, 14 projects, 5,452 calls, 2026-03-15 to 2026-10-07):

| Slice | Tools | Share of calls |
|---|---|---|
| Top 8 (`get_issue`, `create_issue`, `close_issue`, `add_comment`, `update_issue`, `start_work`, `search_issues`, `list_issues`) | 8 | 81.4% |
| Top 12 (+ `add_dependency`, `observe`, `get_comments`, `session_context`) | 12 | 89.4% |
| Top 20 | 20 | 95.0% |
| Ever called | 66 of 118 | 100% |
| Never called | ~52 | 0% (annotations, scanners/scans, plans, warpline ingest, finding report/promote, most admin) |

**Fleet DB evidence** (14 DBs, about 3,140 issues):

- **Annotations:** 7 rows, all in Filigree's own repo.
- **Entity associations** (the "SEI backbone" binding): 10 rows.
- **Findings:** about 25k ingested. In the four largest stores, 4 are fixed, 21 false-positive and 8 linked to an issue. In Filigree's own DB, 98% are open low/info Wardline engine facts.
- **Packs:** no project enables risk, roadmap, incident, debt or spike.
- **Activity:** 7 projects show activity since 2026-09-01.

The brief's "~150 MCP tools" is an overcount. 118 are registered (README; `Tool(` count; this session's tool list).

None of this proves those surfaces are worthless. Federation consumers use HTTP, not MCP, and CLI use is not logged (see Caveats). It does prove three things:

- No surface addition has ever been gated on, or followed up by, an outcome reading.
- No "no" has ever been recorded. The planning-pack deprecation has sat in draft since May with its migration target (Shuttle) nonexistent.
- The README markets the size of the surface as the value.

### 6.3 Strategy drift and an invalidated premise (P-1, P-5, P-8)

**Anti-pattern:** Continuity Loss / Strategy Drift. **Closing sheets:** `product-ownership-operating-model.md`, `product-state-and-continuity.md`, `vision-strategy-and-roadmap.md`.

PDR-0002's rationale was explicit: "what is being built (federation) is not what moves the metric." The next three releases built federation anyway: the Warpline seam (schema v29), Loomweave auth posture, drift lanes, rename lineage, fail-closed renderers. The bounded tail that was supposed to be *finished* (X-4/5/6) was not. The owner may well have chosen this. The milestone says "owner-set". It still contradicts a standing PDR without a superseding one, which is the definition of drift.

On 2026-10-01 the hub and four of the siblings Filigree built for were archived locally. That changes the "for-whom" of half the 3.x investment. It needs an owner ruling, not an agent inference: a local archive is not necessarily discontinuation.

PDR-0004 (Tabard) and its roadmap Next item are the clearest casualties. The counterparty never ran its spike and is now archived, and the reversal trigger has no branch for that.

### 6.4 The zombie Now bet (P-7)

**Anti-pattern:** Solution-in-Search-of-a-Problem (partial) and an unfireable trigger. **Closing sheets:** `product-discovery-and-opportunity.md`, `product-metrics-and-experimentation.md`.

The broadcast board is well-guarded on scope: the anti-bloat clause and the relevance-gated reply are good product writing. It still fails on three counts (the third: PRD-0001 criterion 2's reflexive-ack ratio has no definition, and the PRD marks its instrument OPEN, so the guardrail is not yet falsifiable):

1. **The problem is asserted, not counted.** No collision incidents are logged anywhere.
2. **Its kill trigger arms only after an MVP that was never started.**

Its analogy ("a sleeper hit like observations") is weak on the fleet's own data: `observe` is 2.2% of calls. Meanwhile it is the tracker's critical path, so every agent's session start points at it.

### 6.5 Provenance (P-6, P-14) and the public roadmap (P-9)

**Anti-pattern:** Decision-without-Provenance and Roadmap-as-Promise. **Closing sheets:** `product-state-and-continuity.md`, `vision-strategy-and-roadmap.md`.

- **Releases:** five public releases with zero PDRs.
- **Policy reversal:** ACTOR_MISMATCH was removed with no record (`filigree-60a5103dee` asks for one).
- **Public workspace is false:** `current-state.md` is public (released in 3.2.0) and wrong (`filigree-434aa4e145`).
- **Root `ROADMAP.md`:**
  - It is a second, unowned roadmap.
  - It lists shipped work as "Immediate".
  - It proposes vision anti-goals: a human-first PM suite, GitHub sync (an external party), and a semantic-dedup sidecar that annexes Loomweave's lane.
  - It says "directions, not commitments", which limits the promise risk. The anti-goal conflict is still the problem.

### 6.6 Refresh framing and the authority gate (P-12, P-13)

**Anti-pattern:** Solution-in-Search-of-a-Problem and Autonomy Overreach (preventive). **Closing sheets:** `product-discovery-and-opportunity.md`, `product-ownership-operating-model.md`.

"Do a major" was decided before "what problem". Three majors in about 5 months is a pattern with a cost: the owner's own fleet still has legacy stores at schema 8, and every break re-teaches every agent. The grant that would govern a kill-heavy refresh has not been reviewed since the strategy changed.

## 7. Routed-out items (not critiqued here)

- **MCP contract reviewer:**
  - the mcp 2.0 / protocol 2026-07-28 move (`filigree-1977c738f1`)
  - `tool_call` logging using pre-namespacing internal names
  - error-envelope instrumentation (the mechanics of P-2's instrument)
  - `filigree-cb5dfdcfb0` (sync Loomweave probe on the event loop)
- **HTTP API reviewer:** what HTTP usage evidence siblings generate. Needed before any kill of findings, entity-association or scan-results surfaces.
- **Agent-experience reviewer:** whether the 12-tool core is sufficient in practice, and whether session-context should stop surfacing a stalled critical path.
- **`/program-management`:**
  - CI hang with no timeouts (`filigree-bb3505e85c`)
  - the parked live lane (`filigree-2575e37a7b`)
  - the real-Legis test (`filigree-10ad50dacf`; moot if Legis is shelved)
  - sequencing of the reconciliation preconditions
- **Wardline:** `wardline-88a7c08286` (`ethereal` literal, P-16).

## 8. What the refresh package must answer

Each question carries the evidence that forces it. The package should answer these *before* it names scope or a version.

| # | Question | Why it is forced |
|---|---|---|
| Q1 | **Who is the user?** The owner's own agent fleet (about 7 active repos), or external adopters? Is "popular" (PDR-0003 goal #1) still a goal, and what measures it? | The public repo has 1 star, 0 forks and 0 GitHub issues. Every log and DB in evidence is one operator's machine. An internal-fleet bet and an external-adoption bet imply opposite refreshes (stability and onboarding vs. whatever the fleet needs). |
| Q2 | **Is the Weft federation still the strategy, and which siblings are live?** Escalate-first: this is a vision change. | P-5. Half of 3.x was built for members now archived. Answer per seam: Loomweave registry, Wardline findings ingest, Legis closure gate, Warpline seam, Tabard. |
| Q3 | **What is the bet, as a falsifiable hypothesis, and what is its baseline reading?** Name the instrument and read it *before* committing. | P-2. The north-star has never been measured, and current logs cannot measure dead-ends. |
| Q4 | **Does PDR-0002 hold?** Write a new PDR that reaffirms or supersedes it, with a verdict on the closed Toolkit DX epic (ACCEPT / REJECT / UNKNOWN). | P-1, P-2. The standing Now bet is contradicted by delivery and unadjudicated. |
| Q5 | **What does it kill?** Give a kill/keep table with usage evidence for: about 52 zero-call MCP tools; the 5 never-enabled packs; annotations (7 rows); the planning pack (decide the May deprecation draft); the broadcast board (P-7); the Tabard seam (P-8); the 'deeper agent-systems primitives' under epic filigree-ed2ccaf10d (open since 2026-05-06; filigree-c2009921cf proposed and filigree-6549e739de approved, both untouched for about 5 months, and the only roadmap-Next item that targets the north-star); each federation seam with a shelved counterparty. Each kill of a used feature is escalate-first. | P-3. No "no" has ever been recorded. |
| Q6 | **Why a major?** What does the bet require to break, what will migration cost the fleet (legacy `.filigree/` stores at schema 8, INSTALL_VERSION 17–29), and what agent-instruction churn follows? | P-12. The version number currently precedes the problem. |
| Q7 | **What is the reversal trigger?** It must be metric-bound, time-boxed from the bet's *start* (not its ship), and have an explicit "abandoned / dependency-dead" branch. | P-7 and P-8. Both prior triggers are structurally unfireable. |
| Q8 | **Which guardrails must not degrade?** Define the currently BASELINE-unset ones: federation-contract regressions, agent-reported defects per release, CI health (hang with no timeout), and a live-lane status that is not "red by design". | P-2, P-10, P-11. |
| Q9 | **Are the reconciliation preconditions done?** Land or revert `ecad149` and `79e06d6`; correct both trackers; close the 3.3.0 milestone with a verdict; rewrite `current-state.md`; retire or rewrite root `ROADMAP.md` (owner gate: public); re-confirm the authority grant. | P-4, P-6, P-9, P-13. The refresh cannot be resumed from false state. |
| Q10 | **Is it "3.0" or 4.0, and what is in scope?** | The brief says 3.0; the repo is at 3.3.0. |
| Q11 | **Is the MCP 2.0 / protocol 2026-07-28 move in or out?** | P-15. The primary user's host protocol is moving. Route the mechanics. |

## 9. Re-review triggers

Run this critique again in any of these cases:

- after the owner rules on Q1 and Q2 (the federation and the user)
- after a north-star baseline exists
- after a refresh PRD with falsifiable criteria is drafted (`/write-prd` falsifiability pass)
- before any refresh tag or PyPI publish
- before any public `ROADMAP.md` rewrite
- if any kill/deprecation list is proposed, to check that each item is gated and evidenced against HTTP and CLI usage, not MCP logs alone

---

## 10. Confidence Assessment

- **Available:** the full product workspace; git (tags, ancestry, commits since v3.0.0); CHANGELOG; README; root ROADMAP; the live tracker (read-only); 14 local MCP tool-call logs; 14 local Filigree DBs (read-only); the archived hub tracker; Wardline source and tracker; the home-directory archive report; public repo stats.
- **Absent:** PyPI download stats; HTTP route-level usage by siblings; CLI usage (not logged); usage on other machines or CI; any owner statement on why siblings were archived; the owner's sign-off trail for 3.x releases.
- **Per finding:**
  - **High confidence:** P-2, P-4, P-6, P-7, P-8, P-9, P-10, P-14, P-15 and P-16 rest on primary artifacts (files, git ancestry, tracker rows, code lines).
  - **Moderate confidence:**
    - P-1: the commit split uses a keyword classifier, so it is approximate, though the direction is unambiguous.
    - P-3 and P-11: MCP-only usage evidence, one operator.
    - P-5: archive ≠ discontinuation; the owner's intent is inferred.
    - P-12: the fleet-lag evidence includes abandoned projects.
    - P-13: staleness is factual; the risk is forward-looking.
- **Overall: Moderate-High** on the facts, **Moderate** on the strategic interpretation. The two most consequential inferences, the federation premise (P-5) and the usage-driven kill evidence (P-3), are explicitly owner-confirmable.

## 11. Risk Assessment

| Finding | Blast radius | Reversibility |
|---|---|---|
| P-1 Strategy drift | **what**: the refresh may optimise the wrong strategy | Cheap to fix now (one PDR); expensive after a major ships |
| P-2 No baseline | **did-it-work**: no bet can be accepted or killed | Instrumenting is reversible; a fourth unvalidated major is not |
| P-3 Feature factory | **what**: each unused surface taxes every future major | Kills are deprecations: **escalate-first**, one-way for users who depend on them |
| P-4 False records | **did-it-work** + **provenance**: cross-product records wrong | Reversible (land or revert, correct trackers), but it erodes trust in the product's core claim each day it stands |
| P-5 Federation premise | **what / for-whom**: invest or strip on a wrong premise | A strategy call, **owner-reserved**; seams once removed are a breaking change for any live consumer |
| P-6 Provenance | **provenance**: decisions cannot be defended or resumed | Cheapest high to fix |
| Authority verdict | Grant present, no breach found; release gates unprovable; grant stale | The refresh's breaking release and kills are irreversible and outward-facing. They must be **gated, not graded** |

## 12. Information Gaps

1. **Owner's intent behind the 2026-10-01 archive** (blocking for Q2): does it mean "shelved", "finished", or "tidied"? This is the highest-value gap.
2. **HTTP API usage by siblings** (Wardline findings ingest, Loomweave and Warpline reads). Needed before any kill of federation-facing surfaces. The daemon `server.log` shows only 17 `POST /api/p/{key}/weft/…` lines and no per-tool data.
3. **CLI usage.** It is not logged. Agents also use the CLI (the "background subagents" path), so MCP logs undercount some verbs.
4. **External adoption** (PyPI downloads). This would confirm or refute the n=1 reading behind Q1.
5. **Owner sign-off trail for 3.0.0 to 3.3.0** (chat or PR approvals). It would move the authority verdict from "not provable" to "evidenced".
6. **A real dead-end measure.** No current instrument captures it (P-2).
7. **Log retention.** `RotatingFileHandler` is in use. No rotated files were present in the projects sampled, but older calls on rotated or other machines may be missing.

## 13. Caveats

- **n = 1 operator.** Every usage number comes from one person's machine and agent fleet. "Users" in this critique means John's agents. That is the actual user base on current evidence, but it is not a market.
- **MCP logs undercount.** They are not a full usage census. They record handler-level internal names (pre-namespacing), exclude HTTP federation consumers and the CLI, and include sessions in now-abandoned projects. Zero MCP calls is evidence for a kill question, not a kill verdict.
- **Commit categorisation is approximate.** It uses a keyword classifier over subject lines.
- **Archive ≠ discontinued.** The archived siblings remain on disk and possibly on GitHub. This critique treats the archive as an unrecorded strategy signal that must be ruled on, not as a decision.
- **Static review cannot observe intent.** Owner-directed decisions may have been made in-session. The findings are about their *absence from the record*, which is what the next session inherits.
- **Scope.** This critique audits product discipline only. It does not evaluate delivery feasibility, the implementation plan, architecture, the MCP or HTTP contracts, dashboard UX or research method (routed in §7). It does not design the refresh bet; that is `product-shaping-architect`, and the owner decides.
