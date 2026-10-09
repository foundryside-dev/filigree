# Filigree 4.0 refresh: panel synthesis

**Date:** 2026-10-07 · **Branch at review:** `release/3.3.0` (clean, v3.3.0 shipped 2026-09-02) · **Status:** for owner review. No issues have been filed yet.

This brief is the map. The seven reports beside it hold the detail. Reviewer names appear only as citations.

| Report | Lens | Size |
|---|---|---|
| [`llm-agentic-experience.md`](llm-agentic-experience.md) | LLM specialist: how an agent sees Filigree (orientation, instructions, tool prose, recovery, context cost) | 19 findings (LX-01 to LX-19), 9 recommendations (R1 to R9) |
| [`mcp-contract-critique.md`](mcp-contract-critique.md) | MCP server critic: schemas, envelopes, idempotency, bounding, catalog | 28 findings, catalog proposal (118 to 76 tools) |
| [`business-process-review.md`](business-process-review.md) | Business process: 9 packs and 24 types treated as processes | 18 findings (BP-01 to BP-18), target model (24 to 6 types) |
| [`product-critique.md`](product-critique.md) | Product decision critic: `docs/product/` checked against reality | 16 findings (P-1 to P-16), 11 must-answer questions |
| [`dashboard-ux-theory.md`](dashboard-ux-theory.md) | UX theorist: dashboard from first principles | 36-surface keep/reframe/kill table, 13 jobs |
| [`federation-http-api-review.md`](federation-http-api-review.md) | API reviewer: `/api/weft` and the sibling seams | 21 findings (F1 to F21) |
| [`application-boundaries.md`](application-boundaries.md) | Solution architect: Filigree / Loomweave / Wardline split | 9 misallocations (M-1 to M-9), 6 options, ADR draft, seams S-1 to S-9 |
| [`leverage-analysis.md`](leverage-analysis.md) | Systems thinking (leverage analyst): review of *this brief* | System map, archetypes, Meadows placement, 10 missed interventions (X1 to X10), re-ranked action list. **Its ordering supersedes §2 and §3; see §0.** |

**Provenance.** I re-ran two claims myself in a scratch project:
- A non-holder `release` strips a live claim.
- `undo` walks back one more event per call and ignores who holds the claim.

Every other number is reviewer-reported. Most were measured against live databases or reproduced in scratch projects; each report records how.

Some counts are one measurement cited by two reviewers, not two independent confirmations. "5 of 9 packs enabled nowhere" and the MCP usage figures come from the same fleet DBs and logs. The real cross-checks are the cases where different lenses arrived at the same prescription (section 4).

---

## Owner rulings (2026-10-07)

**Context.** The owner pulled Filigree and Loomweave out of all real projects after Loomweave's reliability problems surfaced. Those projects are on hold until both tools are restored to a useful, best-in-class state. **Trust is the bar for 4.0, and the paused projects are its acceptance test.** Loomweave is to be restored, which overrides the 2026-10-07 Loomweave memo's "do not rebuild" verdict; its measurements still define the bar.

| Decision | Ruling |
|---|---|
| **Re-entry gate** (new) | **Reliability bar plus a pilot.** Four conditions: no silent failures (failures are logged and surfaced to the agent); outage detection; a retry-safe core loop (call-twice suite green); then one real project as a pilot, read over a fixed window, before wider rollout. Do-now fixes and the minimal core come first; the rest of 4.0 follows. |
| **D1, findings** | **B2.** Wardline owns findings, telemetry and the accept/suppress verdict. Filigree holds evidence references and receives promoted defects. Wardline waiver expiry becomes mandatory as a co-requisite. About 25k legacy rows are exported. |
| **D2, archived members** | **Fold in, revisit later.** Legis becomes Filigree close-evidence policy, Wardline waivers and a reserved `external:<provider>` gate slot. Warpline becomes the generic promote seam plus `git log`. Tabard becomes a connection-bound actor convention. The hub becomes a runtime-free `weft-contracts` package. A product is rebuilt only if a real project needs it. Consequence: abandon `codex/gs7-warpline-worklist` and close `filigree-1544621b0a`. |
| **D5, compatibility** | **Clean break at 4.0.** No compatibility layer. A short ADR records the 3.0 `loom` break; the frozen-generation promise starts at 4.0. |
| **D3 + S2, D4 + S6, D6 + S1, D7** | Accepted as recommended, with the leverage amendments: done = anchor machine-checked against the integration branch; identity ships with session keying; a light standing outcome registry; the dashboard ships "Now" and "closed without evidence" first. |

**Framing (owner, 2026-10-07).** Filigree is a **living tool set, not a mass-market product**. This first-principles review is happening partly because current LLMs are far more capable at architecture and security than the models that shaped 1.x–3.x. This **supersedes the earlier "own fleet plus external users" answer**. External adoption is not a design driver.

What the framing changes:

| Area | Change |
|---|---|
| **Builder-as-user loop** (leverage R2) | **Intended, not a defect.** Agents evolving the tool they use *is* the living tool set. The fix is the balancing loop (outcome readings, kill-by dates, recorded "no"s, readings taken on real projects), not separating the populations. |
| **Standing re-review** | Make this panel a **recurring practice**: a first-principles review by current models at each major, or when a reading crosses a trigger. Pair it with the outcome registry (S1) and the "read and decide" stage. |
| **Separation-of-duties gates** | Use **an independent agent as verifier or reviewer** (a different session or model, with its own evidence), not a human by default. This fixes BP-02 (verifier = fixer) without a human queue that stalls when owner attention is bursty. Human gates are reserved for outward-facing or irreversible acts. |
| **Agent instructions** | Assume capable agents. Prefer principles, contracts and machine-checkable evidence over step-by-step recipes and ritual states. |
| **D5 compatibility** | *Proposed adjustment, needs confirmation:* no long-lived frozen-generation promise even after 4.0. Instead use versioned contracts plus conformance goldens, with the suite co-evolving in lockstep and short deprecations. |
| **Senior-user review (SUR)** | **Restart as a standing practice once there is something to evaluate.** A subagent uses the tool as the representative user and reports friction. Reviews A–H ran 2026-05-06 to 05-12 and were consolidated into `2026-05-13-mcp-senior-user-review-master-checklist.md`. Lessons from this panel for the restart: **(1) Isolation.** Run in a throwaway project, never the production tracker. May's reviews ran against this repo's live `.filigree/`, and their fixtures became 59% of planning-pack rows (BP-06). **(2) A real user's task.** The reviewer works a real task from a *paused project*, in a scratch clone of that repo, not a tour of Filigree's tools inside Filigree's own repo. This avoids the builder-as-user bias. **(3) Findings become regression tests.** A finding closes only when a call-twice or golden-conversation test pins it. The May checklist marked rows "Done" that were never tested adversarially (MCP Disagreement 1), and several later regressed (LX-08, LX-14, LX-16). **(4) A fixed scenario script plus free exploration**, so runs are comparable and dead-ends per scenario become a reading for the outcome registry (S1). **(5) An independent reviewer**: a different session or model from the one that built the change. **(6) Scope covers Loomweave and cross-tool flows**, not only Filigree MCP. **Placement:** SUR is the synthetic-user gate after Phase 0 and before the pilot, then a standing step at each release's "read and decide" stage. |
| **Design posture for capable agents** *(owner hypothesis, partly from the owner's own observation; stress-test once the design is finished, before treating as settled)* | Current models lean on *expressive* operations to cut tool calls. For example, they write one Python call that edits a dozen files, where older models demanded narrow, precise tools. **The May SUR findings and any "STILL-OPEN vs May checklist" tags are evidence of older-model behaviour, not requirements.** Re-derive each one from how current models actually work. Design implications: **(1) Composability over narrow precision.** Prefer fewer, more expressive verbs: one filter or query expression instead of proliferating filter parameters, and an `apply(ops[])`-style batch or transaction primitive. **(2) The CLI with JSON I/O is a first-class scripted surface.** Agents compose it in one shell call. Keep MCP context-bounded by default, and route bulk reads through export or pipe so they're processed outside the context window. **(3) Loud, structured failure is the precondition for composition.** A script cannot notice a silent drop. Atomic or per-item batch results, idempotency keys and `retryable` matter *more*, not less. **(4) MCP usage logs undercount scripted CLI use,** so CLI logging (do-now #1) is required before any kill decision. **(5) The restarted SUR should observe how the current model *chooses* to work** (scripted CLI? batch? MCP?) and design for that, rather than grading against the old checklist. |
| **Demoted** | External onboarding polish (LX R8 is kept only for new agent sessions and new fleet projects); optional installable packs (ship one opinionated workflow); a public OpenAPI for third parties (F20, kept only if generation helps agents); public `ROADMAP.md` and adoption metrics (product P-3's star and fork evidence is moot); solo install as a boundary driver (B2 still stands, on reliability grounds). |

---

## 0. Leverage review: what changes in this brief (added after the systems review)

**The systems review's verdict.** The diagnosis below holds and the 4.0 sketch is structurally sound, **but the brief prunes the surface once and installs nothing that governs how it changes afterwards.**
- About half the brief's interventions are parameter or structure changes (Meadows Levels 12–10).
- None is at Level 4: the rules for how the system changes itself.
- So the forces that grew 118 tools and 24 types are untouched, and 4.x would regrow the surface.

**The loop the brief missed: builders are the users.**
- About 70% of logged MCP calls and 68% of issues fleet-wide come from the suite repos that *build* Filigree: Filigree, Loomweave and Wardline.
- The friction agents notice while building becomes the backlog, and the cheapest fix is always additive.
- Nothing prunes: no outcome reading has ever been taken, and the `feature` type cannot even record a rejection.
- The review panel itself is inside this loop: seven agents, reading logs dominated by suite construction, proposing a rebuild for agents to execute.

**Three corrections to readings below** (from the event data):
1. **Gate bypass is not erosion under load.**
   - The verify-gate bypass rate fell from 76% (April) to 17% (May) while throughput rose. When bypass fell, ritual rose: 94% of May verify dwells were under a minute.
   - The gate never bound anything, because it asked one actor to verify itself.
   - 117 of 154 bypassing closes *did* cite a commit or test in prose. **The problem is evidence that is self-asserted and unchecked, not missing evidence.**
2. **D3's "commit anchor" as a field would have passed the P-4 false closure.** `79e06d6` *was* an anchor; nobody checked it. Only 17 of about 1,139 closes carry an anchor at all.
3. **Stage 0 must not bulk-mark telemetry as `fixed`.** That falsifies resolutions and inflates any "fixed" metric about 2,500-fold. Use a distinct disposition (`not_work`), or export and drop.

**Re-ranked strategic order** (leverage × loop-breaking value; full table in `leverage-analysis.md` §7):

| Rank | Intervention | Level | What changed from the brief |
|---|---|---|---|
| S1 | **Standing outcome loop.** A registry where every tool, type and seam carries an outcome reading and a kill-by date. A release requires a reading and a decision record. Add a `rejected` terminal state, and a post-cut "Stage 5: read and decide". | 5 → 4 | D6 moves from 6th and one-off to co-first and standing |
| S2 | **Done = integrated and machine-checked.** The server verifies the commit anchor is reachable from the integration branch, plus the resolution enum and an `awaiting-integration` state. Can warn in 3.x. | 3 / 5 | Lifted out of D3, where it was a typed field |
| S3 | **D1 = B2, generalised as a rule:** no copied fact without a generator or a reconciler. Make Wardline waiver expiry mandatory at the same time. Otherwise B2 moves the escape lane into ownerless, non-expiring waivers. | 5 | D1 stays first among the rulings; adds a cross-product co-requisite |
| S4 | **Integrity signals where decisions happen.** For the owner: Now / Inbox / "closed without evidence" plus a since-last-visit digest. For agents: the actor-aware brief. Design both for an owner whose attention arrives in bursts; undecided inbox items age to a safe state. | 6 | D7 moves up from last |
| S5 | **Separate the builder signal from the user signal.** Read the north-star on projects that *use* Filigree, and keep review campaigns out of the production tracker. | 6 → 3 | New |
| S6 | **D4 identity shipped *with* session keying.** The server mints a session per connection rather than refusing writes. Otherwise parallel sessions collapse into one claim holder. | 10 | D4 is not "low controversy" without this |
| S7–S12 | Single-source generation of guide, CLI, OpenAPI and catalog · the D3 type collapse (after S2) · the contract-safety envelope, then the bounding rule (only *after* an honest READY) · D5 · D2 · **replace the tool-count targets with an attention budget** | 10 / 8 / 5 | Counts are Level 12 and gameable |

**Re-ranked do-now order:**
1. Instrumentation: log errors, warnings and no-ops as outcomes, add HTTP and CLI logging, and tag each project as suite-construction or product-use. Was #10.
2. Legis: warn, don't wedge.
3. Honest banner and READY. Was #7.
4. The undo, release and next-claim defects.
5. Stage 0, with a non-`fixed` disposition.
6. Reconcile the tracker, prototype the reachability check as a warning, and write one PDR for 3.0–3.3.
7. Housekeeping.

**How agents resist rules.** Agents don't argue with a rule; they satisfy it in the same write or route around it. **Every new rule needs a paired measurement of *how* it is being satisfied.** The analysis flags these risks, each with a leading indicator:
- `discard` becoming the new escape lane;
- the "unscheduled" epic becoming a dumping container;
- opt-in profiles and packs becoming holding pens nobody kills;
- the notices channel becoming the next commons;
- Goodhart on the dead-end metric (errors turned into warnings);
- `no_limit` removal before READY is honest.

---

## 1. The answer

**The core loop is sound and heavily used. Almost everything around it is oversized, unmeasured, or held by the wrong owner.**

**The core loop works.** Claim → work → close with ready-queue, dependencies and events accounts for about 95% of 5,485 logged MCP calls across 15 projects. Twelve tools take 89% of calls. The 3.3.0 dogfood ran through that loop cleanly: 12 of 13 closes were claimed, in 24 to 40 minutes, with commit anchors.

**The periphery is mostly unused or misplaced.**
- 52 of 118 MCP tools have never been called.
- 13 of 24 issue types have never been instantiated.
- About 30% of Filigree's Python (findings warehouse, file registry, annotations, LLM scanner runner, governance client) stores code facts Filigree can't keep correct, for under 1% of calls.
- 9,955 of the 9,973 findings in this tracker are Wardline engine telemetry. Three have ever been linked to an issue.

**"Done" can't be trusted.**
- 27–31% of bug "fixed" closes bypass the verify gate via force-close. This is an all-time aggregate: bypass fell from 76% to 17% between April and May, and most bypasses cite evidence in prose (§0).
- The verifier is the fixer in 425 of 436 cases. Evidence is self-asserted and never machine-checked.
- Planning containers don't roll up. The 3.3.0 milestone is still `planning`, 35 days after release.
- Closures were recorded against commits that shipped in no release.

**4.0 should do three things:**
- **Shrink** Filigree to work state.
- **Make closure honest.** One lifecycle, a resolution enum, no force-close, completion evidence, and containers gated on their children.
- **Make the contract safe for agents.** Launch-bound identity, retry-safe verbs, one bounded read rule, and one error envelope with `retryable`.

Sections 3 and 4 below hold up under any of the owner rulings in section 2.

---

## 2. Owner decisions (ranked by how much else they unblock)

### D1. Findings boundary: Option A or Option B2. **This ruling reshapes three other reviews.**

All four lenses that looked at findings agree on the diagnosis. Findings are a stock with no outflow, and telemetry is being stored as triage inventory (LX-01, BP-04, M-1/M-3, dashboard `<engine>` row, F2). The disagreement is about the fix.

**Option A: findings stay in Filigree.** Fix them at ingest with `kind`/`signal_class`, a per-source disposition policy that auto-acknowledges telemetry, and retention executed inside ingest. Proposed by the process and LLM reviews (BP §5.4, R9).

**Option B2: producers own their facts; Filigree holds only evidence references.**
- Wardline keeps its findings, telemetry and accept/suppress verdict, which lives in the repo and is what CI already reads.
- Filigree holds opaque, typed, snapshotted **evidence references** on issues (`path | symbol | finding | commit | sei | url`).
- Producers **promote** defects (S-2, idempotent on producer + scheme + fingerprint) and post **state updates** (S-3).
- The findings warehouse, file registry, annotation anchors and scanner runner are retired.
- Proposed by the boundaries review (ADR-F4-001 draft).

**My recommendation is B2.** The evidence for it:
- 3 linked findings ever.
- About 30% of the code serves under 1% of calls.
- **Four unsynchronised "accepted" stores**: Wardline, Filigree, Loomweave, Legis. A dismissal in Filigree never reaches the CI gate (M-2).
- It matches your "no keeping old stuff to save work".
- A Filigree-only install becomes a complete tracker, and Wardline + Filigree work as a pair without Loomweave.

**B2 is owner-gated.** It deprecates an ingest that Wardline emits to in 4 repos, it deletes or exports about 25k finding rows fleet-wide, and it reverses hub doctrine ("finding lifecycle lives in Filigree").

**What changes if you pick B2:**
- The MCP catalog drops further, from 76 to about 52 tools; the `finding_*`, `file_*`, `annotation_*`, `scan*` and `scanner_*` families go.
- The dashboard's proposed "Findings triage" surface either goes or becomes a read-only view of Wardline's artifacts.
- The process review's disposition policy is no longer needed.
- Its promote → `bug` in `triage` flow, and cascade-close with `resolution=resolved_by_scan`, map directly onto S-2 + S-3.

**Shipping under either option: "Stage 0".**
- Wardline emits only `kind=defect` to Filigree.
- Wardline stabilises engine fingerprints. They currently embed a changing percentage, so every scan mints new rows.
- Filigree refuses `path="<engine>"`.

Stage 0 is in the do-now list (§3).

### D2. The archived members: rebuild as products, or fold their ideas in. **This contradicts your earlier direction, so it needs a fresh call.**

You said every component is going to be rebuilt. The boundaries review recommends **not** rebuilding Legis, Warpline or Tabard as products:

| Member | Disposition the boundaries review recommends | Its evidence |
|---|---|---|
| **Legis** | "Close requires evidence" becomes Filigree workflow policy. Finding governance becomes Wardline waivers. Branch/PR context comes from `git`/`gh`. | Its only live path into a remaining product is a fail-closed trap (M-7). |
| **Warpline** | The reverify worklist becomes the generic promote seam. Churn comes from `git log`. | Its data is re-derivable from git. It has 0 Filigree-side uses, and Loomweave's churn tools are hollow proxies to it. |
| **Tabard** | Becomes a connection-bound actor convention. | It is a 19-line spec. |
| **Weft hub** | Replaced by a runtime-free `weft-contracts` package: markdown, JSON Schema and golden vectors. | The hub drifted because it mixed PM narrative into its contracts. |
| **Lacuna** | Kept as the cross-product conformance corpus. | |
| **Plainweave** | Stays archived. | |

**Filigree 4.0's design does not depend on this ruling.** The process review's governed-close model is provider-agnostic: an `in_review` queue with `gate_status`, gate kinds `human | separation_of_duties | external:<provider>`, a retry sweep, and debt records that resolve. The reverify worklist becomes a promote with `kind=reverify` and one open item per reference. A rebuilt Legis plugs in as `external:legis`, and a rebuilt Warpline is just a producer. Either way Filigree builds the same thing, so you can rule on D2 later without blocking Filigree.

**One concrete consequence.** The unlanded branch `codex/gs7-warpline-worklist` has diverged by about +1.4k / −3.4k lines against main (F17, `filigree-1544621b0a`). Under "rebuild", rebase it. Under "drop", abandon it and close the issue.

### D3. Process model: 24 types to 6, force-close retired

The process review's target model (BP §5) is:
- **Leaf types:** `task`, `bug` and `feature` share one 6-state lifecycle: triage, open, parked, in_progress, in_review, done.
- **Resolution enum on done:** `completed | duplicate | wont_do | not_a_bug | obsolete | cannot_reproduce | resolved_by_scan`.
- **Completion evidence:** required, as verification plus a commit anchor.
- **Container types:** `epic`, `milestone` and `release` share a roll-up lifecycle. A container can't close while any child is open, and blockedness inherits down the tree.
- **Escape lanes:** force-close is gone. The 324 reverse edges become two verbs, `reopen` and `release`.
- **Packs:** one built-in pack. The other packs become optional installs.

The migration mapping covers every built-in state (BP §5.6).

> **Leverage-review amendment:** "completion evidence" must be a **server-side check**, not a typed field: the commit anchor must be reachable from the integration branch (S2 in §0). As a field it would have passed the P-4 false closure. Also watch `discard` becoming the new escape lane, and the "unscheduled" epic becoming a dumping container.

It is a wire break, because status names change, so it belongs in a major. Two parts need your gate:
- Deprecating the planning pack. The product review says it is used in every project; this model absorbs `phase` and `work_package` into `epic`.
- The status-name change itself.

### D4. Identity becomes a first-class primitive

**Four lenses independently reached this prescription** (LX-11/R1, MCP F10, BP-09/10, boundaries S-6), plus the HTTP review's F19:
- Bind the actor when the server launches: `filigree-mcp --actor`, and `FILIGREE_ACTOR` for the CLI, both written by `filigree install`.
- Drop the per-call `actor` from 62 tool schemas.
- Refuse mutations that have no identity.
- Key claims to a session.

This identity work underpins the actor-aware orientation and the dashboard "Now" board in §5. It needs your sign-off because it changes every agent's setup.

> **Leverage-review amendment:** this is not "low controversy" unless **session keying ships in the same change**. Without it, every session, subagent and worktree launched from one MCP config shares one actor and collapses into a single claim holder. The server should mint a session per connection rather than refuse unidentified writes.

### D5. HTTP generation policy, and the `loom` retirement debt

`loom` was retired in 3.0.0 as a hard rename to `weft`: no alias, no ADR, and none of the 12 months' notice that ADR-002 §3/§8 requires (F3). Any successor generation's "frozen" promise is hollow until that is recorded.

Recommendation:
1. Write the retroactive `loom`-retirement ADR first.
2. Then mint a successor generation for what can't be fixed additively:
   - the error envelope (F4);
   - mandatory bounding (F5);
   - federation resources, which under B2 become S-1 to S-4.
3. Keep `weft` frozen alongside it.

### D6. The north-star baseline: precondition for any 4.0 success criterion

- **No target has ever had a reading.** Every target in `metrics.md` expired on 2026-09-30 with no baseline (P-2).
- **The dead-end rate can't be measured yet.** Error responses are currently logged as successful calls.
- **MCP calls are the only logged channel.** HTTP and CLI usage aren't logged, so the kill decisions in D1 and D3 rest on MCP logs alone. Check the HTTP and CLI channels before killing anything a sibling might call.
- **Order of work:** fix the instrumentation (§3), take a reading, then write the 4.0 PRD with falsifiable criteria and a reversal trigger.

### D7. Dashboard re-architecture

The UX review proposes organising the dashboard by question, not by object type:
- **Now:** live claims and heartbeat age.
- **Inbox:** decisions waiting on the human.
- **Activity:** an actor-scoped flight recorder.
- **Plan:** releases and containers, with blockers shown in context.
- **Issues:** list and search.
- **Findings:** depends on D1.

It kills drag-and-drop status change, the flow-metric cards, Save Preset, the workflow-diagram modal and the onboarding tour. The top unserved job is *"what are my agents doing right now, and who is stuck?"*. The data for it (`claimed_at`, `last_heartbeat_at`, `claim_expires_at`, `/session-evidence`) is already in the API, and no view uses it. The second job is *"what was closed without evidence?"*. The personas are derived from docs and code, not researched.

### Context you should know: a parallel Loomweave review

An untracked memo in `~/loomweave/docs/implementation/2026-10-07-loomweave-rebuild-or-recover.md`, written today, presumably by another session, recommends **not** rebuilding Loomweave as-is. It reports:
- no active consumer (elspeth retired it on 2026-09-25, aurora removed it on 2026-10-05);
- agent adoption of 0.18–0.8% against `rg`;
- two silent multi-week outages.

Its verdict contradicts your "rebuild everything". The boundaries review used only its **measurements** and chose B2 partly *because* B2 holds whether Loomweave survives or not (SEI becomes one optional reference kind). I haven't adjudicated it; that is your call, and the call of whoever owns that memo.

---

## 3. Do now: ships in 3.x whatever you decide in §2

| # | Item | Evidence | Provenance | Source |
|---|---|---|---|---|
| 1 | **`admin_undo_last`: require `expected_event_id` and enforce the holder check.** Each call currently undoes one more event, and any actor can undo anyone's changes. | Scratch: bob undid alice's priority, then title, then claim, in three calls | **Verified by me** | MCP F1 (blocker) |
| 2 | **`work_release`: holder-checked by default.** Today `expected_assignee` is ignored unless `if_held=true`. Also fix the skill's stale-claims recipe, which tells agents to release a peer's claim. | Scratch: bob released alice's live claim and got success | **Verified by me** | MCP F3, LX-05 |
| 3 | **`work_start_next` / `work_claim_next` retry claims a second issue** and strands the first for a 48h lease. | Stdio probe | Reviewer-reproduced | MCP F4 |
| 4 | **Legis: warn, don't wedge.** Governed closes fail closed when Legis is unreachable, and Legis is archived. Any project with `LEGIS_URL` set has its closes wedged **today**. The synchronous 5s gate also runs on the event loop, so a batch close stalls the whole daemon for about 5N seconds. This applies whatever D2 says. | Code path, `governance.py`, `dashboard_routes/issues.py:623,1476` | Reviewer-reported | M-7, F1 |
| 5 | **Federation token: file vs env mismatch gives 401.** The `.weft` file token is rejected while the env token works. Seen by two reviewers independently, the second through Loomweave's Filigree join. | Live GETs against :8834 | Reviewer-reproduced ×2 | F14, M-6 |
| 6 | **Stage 0 telemetry cut** (see D1): Wardline emits only defects, engine fingerprints are stabilised, Filigree refuses `<engine>`. Existing telemetry rows get a **non-`fixed`** disposition (`not_work`), or are exported and dropped. Marking them `fixed` would falsify resolutions. Either way it needs your gate (data change). | 9,793 of 9,958 unbridged rows come from two telemetry rules | Reviewer-measured | M-1, BP-04 |
| 7 | **Session banner and READY.** The banner calls 9,955 telemetry rows "actionable", and its hint triggers a call of about 38K tokens. READY ignores `startable`: 8 of 15 items shown, including 3 of the top 4, can't be started. `init` seeds a "Future" release that shows up as ready work. | Live session-context | Reviewer-reproduced | LX-01, LX-02, R8 |
| 8 | **Scan-results `failed=[]` is hardcoded.** Over-cap drops show up only in free-text `warnings[]` under HTTP 200. | `generations/weft/adapters.py:360` | Reviewer-reported | F2 |
| 9 | **Reconcile the tracker with reality.** Land or revert `ecad149` (C-16) and `79e06d6` (GS-7), and correct the closures recorded against them. Close the 3.3.0 milestone with a verdict. Rewrite `docs/product/current-state.md` (dated 2026-07-07). | `merge-base --is-ancestor` | Reviewer-reported | P-4, BP-03 |
| 10 | **Instrumentation: log error responses as errors.** This is the precondition for D6. | MCP call logs | Reviewer-reported | P-2 |
| 11 | **`CLAUDE.md` names Legis and Warpline as live tools.** Both are archived and neither MCP server is connected. | Session tool list | Verified by me | (housekeeping) |

---

## 4. Where reviewers with different lenses converged

**One error envelope, with retry semantics.**
- Domain errors return `isError=false`; SDK validation errors return plain text with no code (MCP F2).
- `CONFLICT` means both "policy said no" and "dependency unreachable" (F4).
- No `retryable` field or `Retry-After` header exists anywhere.
- Recovery hints are static even when the payload already knows the next step (LX-18).
- **Target:** `{error, code, retryable, cause_kind, hint}`, correct `isError`, computed hints, and `renamed_to` tombstones (S-7).

**Retry safety as a declared property.**
- Create, comment and plan calls duplicate when retried. Undo and release are non-idempotent. Batch close lists an id as both succeeded and failed (MCP F1, F3–F5, F8). HTTP writes have no idempotency key (F6).
- **Target:** optional `client_request_id` on every mutation, explicit tool annotations, one `BatchResponse` shape, and a call-twice test per tool (MCP F26).

**One bounding rule.**
- Four regimes exist today. `issue_list` returns 217 KB for one default page, while `issue_search` returns 7 KB. Agents call `no_limit=true` at about 25K tokens per call (LX-04). `/api/weft/ready` returns 68 KB with `has_more:false` hard-coded (F5).
- **Target:** slim by default, limit about 25, cursor-paged, full detail only via `issue_get` or `response_detail=full`, and no `no_limit` escape.

**Actor-aware orientation.**
- READY is actor-blind and lists unstartable items (LX-02, LX-03). The dashboard can't show who is doing what (J1).
- **Target:** session-context becomes a brief of about 40 lines: who you are, your claims, what you can start, who else is active, and attention items.
- Notices ride on `work_start`, `issue_get` and `issue_close` responses, so critical notes reach the agent *before* the work (LX-09, R3).
- The dashboard "Now" board reads the same claim and heartbeat data.

**A smaller surface, measured by usage.**

| Metric | Today | Target | Source |
|---|---|---|---|
| MCP tools | 118 | 76 (MCP critic); 40–50 default profile (LLM review); ~52 under B2 (boundaries) | MCP critique, LX/R4, boundaries |
| Issue types | 24 | 6 | BP §5.1 |
| Built-in packs | 9 | 1 | BP §5 |

- Reference data becomes MCP resources.
- Single and batch twins merge.
- Claim-only verbs fold into flags on `work_start` / `work_next`.
- Observation and annotation collapse into one "note" (R4, M-4).
- Admin tools move behind an opt-in profile.
- Tiers move into `_meta`, and every description opens with "Use when … / not for …" (R5).
- **Caveat:** kills rest on MCP logs only (D6).

**One generated agent guide.** There are five parallel copies of "how to use Filigree" prose, and they have drifted from behaviour (LX-14, LX-15). Generate `instructions.md`, the skill, `workflow_guide_get` and `docs/agent-integration.md` from one source, and pin behavioural claims with fixture tests (R6).

**Contracts live in a neutral, runtime-free place.**
- Sibling contract coverage is uneven: the Legis closure gate and the Warpline worklist have no vendored goldens (F17). `contracts.md` contradicts the live auth scope (F15).
- **Target:** a `weft-contracts` package with markdown, JSON Schema and golden vectors. Each seam owner writes its spec, and consumers vendor the goldens (boundaries §7). Generated OpenAPI per generation (F20).

**What works today and must survive the rewrite** (LX "What works", HTTP strengths):
- The SessionStart contract: about 700 tokens, never fails the hook, honest when empty.
- The 17-line managed block plus progressive disclosure to the skill.
- `INVALID_TRANSITION`, which carries `valid_transitions`, `missing_fields` and `next_action`.
- The structured `CONFLICT`.
- The slim `work_ready` and `issue_search` rows.
- Per-generation shape adapters with pinned fixtures.
- The registry-driven sibling drift check.
- The scan-ingest write window.

---

## 5. Sketch of 4.0 if the recommendations are adopted

- **Scope:** work state only. Issues, workflow, claims, leases, dependencies, plans, events, notes (observations and annotations merged), and close evidence. It links to code only through evidence references (S-1).
- **Process:** 6 types, a resolution enum, completion evidence, containers gated on their children, provider-agnostic governed close, and process metrics derived from events (BP §5.5).
- **Identity:** launch-bound, with claims keyed to sessions.
- **Agent surface:** a default profile of about 40–52 tools, the one envelope, retry-safe verbs, the one bounding rule, `outputSchema`, `contract_version` in `serverInfo`, and an actor-aware session brief with a notices channel.
- **HTTP:** a successor generation (after the `loom` ADR) carrying S-1 to S-5, discovery/auth (S-6) and the envelope (S-7), with OpenAPI generated per generation.
- **Dashboard:** Now / Inbox / Activity / Plan / Issues; Findings depends on D1.
- **Seams:** promote (S-2) and state update (S-3) from Wardline. Loomweave is an optional `sei` reference producer. External gate providers use a reserved slot.

**Sequencing.** Each stage is gated on an observable criterion, not a date:

| Stage | What happens | Gate to move on |
|---|---|---|
| **0. Now (3.x patch)** | The §3 list, plus Stage 0 of the telemetry cut | New Filigree rows per scan ≈ the number of defects; the `<engine>` count stops growing; the undo, release and next-claim probes pass |
| **1. Decide** | Your rulings on D1–D7. Take the baseline reading. Write the 4.0 PRD (`/write-prd`, falsifiable criteria, reversal trigger), ADR-F4-001 and the `loom` retirement ADR. | All D-items ruled; baseline recorded |
| **2. Specs first** | Write the specs in `weft-contracts`: S-1 references, S-2 promote, S-3 state update, S-6 identity, S-7 envelope, S-8 finding record. Ship the additive parts in the last 3.x minor, alongside deprecation `_meta`. | Lacuna end-to-end tests green with Loomweave absent; Wardline promote idempotent across the 15 fleet projects |
| **3. 4.0 rebuild** | Rebuild the core (identity, process model, catalog, envelope), the new dashboard IA, migrations with parity checks, and tombstones. | Migration parity on every issue with linked evidence; the call-twice and golden-conversation suites green |
| **4. Cut** | Owner-gated breaking release. Rewrite the public `ROADMAP.md`. | Your sign-off |
| **5. Read and decide** (added by the leverage review) | Take outcome readings on product-use projects, then kill or keep each surface item against its registry entry. This becomes a standing step for every later release. | A decision record per item; at least one recorded "no" |

---

## 6. Where the reviewers disagree

| Topic | Positions | How to resolve it |
|---|---|---|
| Findings | Keep in Filigree with `signal_class` and a disposition policy (BP, LX) **vs** producers own them, Filigree holds references (boundaries) | **D1.** Stage 0 ships under both. |
| Archived members | Rebuild everything (your earlier direction) **vs** drop Legis, Warpline and Tabard as products (boundaries) | **D2.** Filigree's design is identical either way. |
| Annotations | Keep as-is (BP: no orphans) **vs** collapse into "note" (LX R4) **vs** remove the anchor-drift store (boundaries M-4: 7 rows, 1,280 lines) | Folds into D1/B2. Note-with-references satisfies all three. |
| Catalog size | 76 (MCP) / 40–50 (LX) / ~52 (boundaries under B2) | Same direction, different scope. Apply MCP's contract rules to whichever catalog D1 produces. |
| HTTP federation resources | Entity associations and worklist ingest as first-class weft resources (F9, F12) **vs** S-1 to S-4 references, promote and lookup (boundaries) | Same mechanism (a successor generation); the resource set follows D1. |
| Dashboard Findings view | Promote to a top-level triage surface (UX) **vs** remove, or read Wardline's artifacts (boundaries) | Follows D1. |

---

## 7. Housekeeping and caveats

- **"3.0 or 4.0" (product Q10) is resolved: 4.0.** The other product questions are covered by D1–D7. Q1 is answered: both your own fleet and external users, agent-first.
- **Kill evidence is MCP-only.** HTTP and CLI aren't logged. Check them before removing anything a sibling or script calls (D6).
- **Personas** in the dashboard review are derived, not researched. The live project had 0 WIP, so a populated "Now" view was never observed.
- **Side effects of the review:**
  - One reviewer's `uv run` recreated the gitignored `.venv`.
  - Dashboard screenshots were moved from `.playwright-mcp/` into [`screenshots/`](screenshots/).
  - No tracked file was modified, and no tracker writes were made. All probes ran in scratch projects.
- **The `project_4_0_refresh_direction` memory** currently records "keep the seams dormant/intact". Update it once D2 is ruled.
- **Owner gates (escalate-first):**
  - deprecating any used feature: findings ingest, the planning pack, the Files view;
  - deleting or exporting data: about 25k finding rows, the telemetry bulk-mark;
  - doctrine changes;
  - the breaking public release;
  - the public `ROADMAP.md` rewrite;
  - any change to `vision.md`.
