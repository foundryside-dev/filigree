# Filigree 4.0 refresh review: structured digest

Source directory: `/home/john/filigree/docs/plans/2026-10-07-refresh-review/` (9 reports, read in full). Citation convention: `brief` = `00-refresh-brief.md`; `leverage` = `leverage-analysis.md`; `boundaries` = `application-boundaries.md`; `MCP Fn` = `mcp-contract-critique.md` Finding n; `BP-nn` = `business-process-review.md`; `LX-nn`/`Rn` = `llm-agentic-experience.md`; `P-n` = `product-critique.md`; `UX #n`/`Jn` = `dashboard-ux-theory.md` surface/job; `HTTP Fn` = `federation-http-api-review.md`; `Xn`/`Sn`/`Tn` = leverage-analysis interventions/strategic/tactical ranks; `S-n`/`M-n` = boundaries seams/misallocations.

Precedence rule (brief header table + §0): `leverage-analysis.md` "ordering supersedes §2 and §3" of the brief. The owner rulings table and framing rows are the top authority.

---

## 1. Owner rulings (brief, "Owner rulings (2026-10-07)" table + §0)

### 1.1 Context (verbatim)

> "The owner pulled Filigree and Loomweave out of all real projects after Loomweave's reliability problems surfaced. Those projects are on hold until both tools are restored to a useful, best-in-class state. **Trust is the bar for 4.0, and the paused projects are its acceptance test.** Loomweave is to be restored, which overrides the 2026-10-07 Loomweave memo's 'do not rebuild' verdict; its measurements still define the bar."

### 1.2 Decision table (verbatim cells)

| Decision | Ruling (verbatim) |
|---|---|
| **Re-entry gate** (new) | "**Reliability bar plus a pilot.** Four conditions: no silent failures (failures are logged and surfaced to the agent); outage detection; a retry-safe core loop (call-twice suite green); then one real project as a pilot, read over a fixed window, before wider rollout. Do-now fixes and the minimal core come first; the rest of 4.0 follows." |
| **D1, findings** | "**B2.** Wardline owns findings, telemetry and the accept/suppress verdict. Filigree holds evidence references and receives promoted defects. Wardline waiver expiry becomes mandatory as a co-requisite. About 25k legacy rows are exported." |
| **D2, archived members** | "**Fold in, revisit later.** Legis becomes Filigree close-evidence policy, Wardline waivers and a reserved `external:<provider>` gate slot. Warpline becomes the generic promote seam plus `git log`. Tabard becomes a connection-bound actor convention. The hub becomes a runtime-free `weft-contracts` package. A product is rebuilt only if a real project needs it. Consequence: abandon `codex/gs7-warpline-worklist` and close `filigree-1544621b0a`." |
| **D5, compatibility** | "**Clean break at 4.0.** No compatibility layer. A short ADR records the 3.0 `loom` break; the frozen-generation promise starts at 4.0." |
| **D3 + S2, D4 + S6, D6 + S1, D7** | "Accepted as recommended, with the leverage amendments: done = anchor machine-checked against the integration branch; identity ships with session keying; a light standing outcome registry; the dashboard ships 'Now' and 'closed without evidence' first." |

### 1.3 Framing (verbatim)

> "Filigree is a **living tool set, not a mass-market product**. This first-principles review is happening partly because current LLMs are far more capable at architecture and security than the models that shaped 1.x–3.x. This **supersedes the earlier 'own fleet plus external users' answer**. External adoption is not a design driver."

Note: brief §7 still says "Q1 is answered: both your own fleet and external users, agent-first." That line is superseded by this framing row (the framing row is explicit that it supersedes).

### 1.4 What the framing changes (verbatim cells)

| Area | Change |
|---|---|
| **Builder-as-user loop** (leverage R2) | "**Intended, not a defect.** Agents evolving the tool they use *is* the living tool set. The fix is the balancing loop (outcome readings, kill-by dates, recorded 'no's, readings taken on real projects), not separating the populations." |
| **Standing re-review** | "Make this panel a **recurring practice**: a first-principles review by current models at each major, or when a reading crosses a trigger. Pair it with the outcome registry (S1) and the 'read and decide' stage." |
| **Separation-of-duties gates** | "Use **an independent agent as verifier or reviewer** (a different session or model, with its own evidence), not a human by default. This fixes BP-02 (verifier = fixer) without a human queue that stalls when owner attention is bursty. Human gates are reserved for outward-facing or irreversible acts." |
| **Agent instructions** | "Assume capable agents. Prefer principles, contracts and machine-checkable evidence over step-by-step recipes and ritual states." |
| **D5 compatibility** | "*Proposed adjustment, needs confirmation:* no long-lived frozen-generation promise even after 4.0. Instead use versioned contracts plus conformance goldens, with the suite co-evolving in lockstep and short deprecations." **UNCONFIRMED** — contradicts the D5 ruling row ("the frozen-generation promise starts at 4.0"). Both positions exist in the same table; the plan must get this confirmed. |
| **Senior-user review (SUR)** | "**Restart as a standing practice once there is something to evaluate.** A subagent uses the tool as the representative user and reports friction. Reviews A–H ran 2026-05-06 to 05-12 and were consolidated into `2026-05-13-mcp-senior-user-review-master-checklist.md`. Lessons from this panel for the restart: **(1) Isolation.** Run in a throwaway project, never the production tracker. May's reviews ran against this repo's live `.filigree/`, and their fixtures became 59% of planning-pack rows (BP-06). **(2) A real user's task.** The reviewer works a real task from a *paused project*, in a scratch clone of that repo, not a tour of Filigree's tools inside Filigree's own repo. This avoids the builder-as-user bias. **(3) Findings become regression tests.** A finding closes only when a call-twice or golden-conversation test pins it. The May checklist marked rows 'Done' that were never tested adversarially (MCP Disagreement 1), and several later regressed (LX-08, LX-14, LX-16). **(4) A fixed scenario script plus free exploration**, so runs are comparable and dead-ends per scenario become a reading for the outcome registry (S1). **(5) An independent reviewer**: a different session or model from the one that built the change. **(6) Scope covers Loomweave and cross-tool flows**, not only Filigree MCP. **Placement:** SUR is the synthetic-user gate after Phase 0 and before the pilot, then a standing step at each release's 'read and decide' stage." |
| **Design posture for capable agents** *(owner hypothesis, partly from the owner's own observation; stress-test once the design is finished, before treating as settled)* | "Current models lean on *expressive* operations to cut tool calls. For example, they write one Python call that edits a dozen files, where older models demanded narrow, precise tools. **The May SUR findings and any 'STILL-OPEN vs May checklist' tags are evidence of older-model behaviour, not requirements.** Re-derive each one from how current models actually work. Design implications: **(1) Composability over narrow precision.** Prefer fewer, more expressive verbs: one filter or query expression instead of proliferating filter parameters, and an `apply(ops[])`-style batch or transaction primitive. **(2) The CLI with JSON I/O is a first-class scripted surface.** Agents compose it in one shell call. Keep MCP context-bounded by default, and route bulk reads through export or pipe so they're processed outside the context window. **(3) Loud, structured failure is the precondition for composition.** A script cannot notice a silent drop. Atomic or per-item batch results, idempotency keys and `retryable` matter *more*, not less. **(4) MCP usage logs undercount scripted CLI use,** so CLI logging (do-now #1) is required before any kill decision. **(5) The restarted SUR should observe how the current model *chooses* to work** (scripted CLI? batch? MCP?) and design for that, rather than grading against the old checklist." |
| **Demoted** | "External onboarding polish (LX R8 is kept only for new agent sessions and new fleet projects); optional installable packs (ship one opinionated workflow); a public OpenAPI for third parties (F20, kept only if generation helps agents); public `ROADMAP.md` and adoption metrics (product P-3's star and fork evidence is moot); solo install as a boundary driver (B2 still stands, on reliability grounds)." |

Consequence of the design-posture row for this digest: every item tagged STILL-OPEN / REGRESSED against the May checklist (LX-01, LX-02, LX-04, LX-06, LX-07, LX-08, LX-11, LX-14, LX-16, LX-17; MCP F7, F9, F10, F14, F19, F20, F28; BP-10; HTTP F3, F17, F18, F19, F20) carries its tag below but is to be re-derived from current-model behaviour, not treated as a requirement.

### 1.5 §0 leverage amendments adopted into the rulings

- S1–S12 strategic order (brief §0, leverage §7.2), verbatim:
  - **S1** "Standing outcome loop. A registry where every tool, type and seam carries an outcome reading and a kill-by date. A release requires a reading and a decision record. Add a `rejected` terminal state, and a post-cut 'Stage 5: read and decide'." Level 5 → 4. "D6 moves from 6th and one-off to co-first and standing."
  - **S2** "Done = integrated and machine-checked. The server verifies the commit anchor is reachable from the integration branch, plus the resolution enum and an `awaiting-integration` state. Can warn in 3.x." Level 3/5. "Lifted out of D3, where it was a typed field."
  - **S3** "D1 = B2, generalised as a rule: no copied fact without a generator or a reconciler. Make Wardline waiver expiry mandatory at the same time. Otherwise B2 moves the escape lane into ownerless, non-expiring waivers." Level 5.
  - **S4** "Integrity signals where decisions happen. For the owner: Now / Inbox / 'closed without evidence' plus a since-last-visit digest. For agents: the actor-aware brief. Design both for an owner whose attention arrives in bursts; undecided inbox items age to a safe state." Level 6. "D7 moves up from last."
  - **S5** "Separate the builder signal from the user signal. Read the north-star on projects that *use* Filigree, and keep review campaigns out of the production tracker." Level 6 → 3. New. (Reframed by the owner's Builder-as-user row: the *populations* are not separated; the readings are still taken on real projects.)
  - **S6** "D4 identity shipped *with* session keying. The server mints a session per connection rather than refusing writes. Otherwise parallel sessions collapse into one claim holder." Level 10.
  - **S7–S12** "Single-source generation of guide, CLI, OpenAPI and catalog · the D3 type collapse (after S2) · the contract-safety envelope, then the bounding rule (only *after* an honest READY) · D5 · D2 · replace the tool-count targets with an attention budget." Leverage §7.2 detail: S7 = single-source generation from the X1 registry (C6, C7, HTTP F20, MCP F20); S8 = D3 structural collapse (6 types, container roll-up, `discard`, `parked`), specify after S2, add X8 (lease); S9 = contract safety (envelope, retry, call-twice test) then bounding rule after T3 and S4; S10 = D5 `loom` ADR then successor generation; S11 = D2 archived members, rule on the Loomweave memo before S-9 (SEI); S12 = replace C5 count targets with the X2 budget.
- Three corrections to brief readings (brief §0; leverage §0): (1) gate bypass fell 76% → 17% Apr→May while throughput rose; 94% of May verify dwells (364/386) under one minute; 117 of 154 bypassing closes cite a commit/test in prose: "the problem is evidence that is self-asserted and unchecked, not missing evidence"; (2) D3's commit-anchor-as-field would have passed the P-4 false closure (`79e06d6` was an anchor; 17 of ~1,139 closes carry one); (3) Stage 0 must not bulk-mark telemetry `fixed` (inflates "fixed" ~2,500-fold); use `not_work` or export-and-drop.
- Re-ranked do-now order (brief §0): 1 instrumentation (was #10); 2 Legis warn-don't-wedge; 3 honest banner + READY (was #7); 4 undo/release/next-claim; 5 Stage 0 with non-`fixed` disposition; 6 reconcile tracker + prototype reachability warning + one PDR for 3.0–3.3; 7 housekeeping.
- Naming gap to resolve in the plan: S1/X1 add a **`rejected`** terminal; BP §5.2's resolution enum has `wont_do`/`obsolete` but no `rejected`. Leverage X1 says "Add a `rejected` / `wont_do` terminal to the feature lifecycle ... BP's resolution enum does this in 4.0" — i.e. leverage treats BP's `wont_do` as satisfying it. Pick one name.

---

## 2. Do-now list (3.x) in the re-ranked order (brief §0 ↔ brief §3 ↔ leverage §7.1)

| Re-rank | Leverage | Brief §3 # | Defect | File / line cited | Evidence | Finding IDs |
|---|---|---|---|---|---|---|
| 1 | T1 | #10 (+ broadened) | Error responses logged as INFO successes; schema/unknown-arg rejections return before logging; HTTP and CLI not logged at all; projects not tagged by population. Precondition for D6/S1 and every kill decision. | `mcp_server.py:1007,1025` (P-2 location); `tool_call` INFO after any handler return; `RotatingFileHandler` in use | 2 `tool_error` lines in 5,452 calls; daemon `server.log` shows only 17 `POST /api/p/{key}/weft/…` lines | P-2; leverage B1 sensor, T1, X5; brief D6; MCP F2 (envelope mechanics); owner design-posture (4) |
| | | | T1 scope beyond §3 #10: "log errors, warnings, no-op sentinels and schema rejections as outcomes; add HTTP and CLI call logging; tag each project `suite-construction` or `product-use`". Dead-end definition must include warnings, no-op sentinels (`{status:"empty"}`, `{undone:false}` — MCP F23), retries within N s, and `list_issues no_limit` fallback (leverage §6 row 6). | | | |
| 2 | T2 | #4 | **Legis: warn, don't wedge.** Two defects: (a) governed closes fail closed when Legis unreachable, and Legis is archived — any project with `LEGIS_URL` set has closes wedged today, including the cascade path; (b) the synchronous 5 s gate runs inside `async def` handlers on the event loop, so a batch close of N governed issues stalls the whole daemon ~5N s (scan ingest, `/api/health`, every sibling); batch path does not thread `legis_known_down`. | `governance.py:12-24,362` (DECISION 2); `dashboard_routes/issues.py:623,1476` and `:91-130` (`_gate_batch_failures`); `legis_client.py:52` (5 s urllib); `dashboard.py:1272` (single worker); `finding_issue_cascade.py:92-106,150` | Code path | M-7; HTTP F1; BP-14; (related F21: `LEGIS_URL` unset = silent fail-open, owner decision) |
| 3 | T3 | #7 | **Honest banner and READY.** Banner calls 9,955 telemetry rows "actionable"; its MCP hint (`finding_list` no filter) is ~137 KB / ~38K tokens; CLI advice `--kind defect` returns 2 rows both `fixed` and misses the kind-less open signal; READY ignores `startable` (8 of 15 shown unstartable, 3 of top 4); `init` seeds a "Future" release that shows as ready; IN PROGRESS is actor-blind; critical path stalled 36 d shown without age. | `hooks.py:163-173` (banner), `:125-135` (ready), `:104-110` (in-progress), `:120,134` (truncation lines); `summary.py:159-173`; `core.py:2453-2483` (Future seed); `db_files.py:3150-3156` (kind-less counted defect-side) | Live session-context; 43 ready / 12 unstartable | LX-01, LX-02, LX-03, LX-19, R8, BP-16; leverage R4; C3 depends on this (leverage §6 row 7) |
| 4 | T4 | #1 | **`admin_undo_last` walks back one more event per call and has no holder check.** No `expected_event_id`; any actor undoes anyone's changes; `{undone:false}` is a success-shaped sentinel. | tool `admin_undo_last`, input schema `{issue_id, actor}` | Scratch: bob undid alice's priority, title, claim in three calls. **Verified by the brief author.** | MCP F1 (blocker), F23; leverage N1 |
| 4 | T4 | #2 | **`work_release` unconditional and non-idempotent.** `expected_assignee` ignored unless `if_held=true`; default `actor` literal `"mcp"`; second identical call errors `CONFLICT`. Skill's Stale Claims recipe teaches releasing a peer's claim; skill never mentions `work_stale_list`/`work_reclaim`/`work_heartbeat`/`work_release_mine`. | `db_issues.py:1669-1691`; `_handle_release_claim`; `skills/.../team-coordination.md:169-177` | Scratch: bob released alice's live claim. **Verified by the brief author.** | MCP F3, LX-05; leverage N2 |
| 4 | T4 | #3 | **`work_start_next` / `work_claim_next` retry claims a second issue** and strands the first for the 48 h lease; no idempotency token, no "already holding" signal. | `work_start_next`, `work_claim_next`; lease constant `db_issues.py:68` | Stdio probe (reviewer-reproduced) | MCP F4; leverage N3; BP-09 (lease size) |
| 5 | T5 | #6 | **Stage 0 telemetry cut.** Wardline emits only `kind=defect`; stabilise engine fingerprints; Filigree refuses `path="<engine>"`. Existing telemetry rows get a **non-`fixed`** disposition (`not_work`) or export-and-drop. **Owner gate (data change).** Note: boundaries migration-sketch Stage 0 row says "marks existing telemetry rows `fixed`"; brief §0 correction 3 / §3 #6 / leverage §6 row 11 override that. | Wardline `core/filigree_emit.py:103-133`, `core/finding.py:265-271`, `scanner/diagnostics.py:102`, `scanner/taint/propagation.py:557-563`; Filigree `scan_findings` no `kind` column | 9,793 of 9,958 unbridged rows from two telemetry rules; 8,850 `WLN-L3-LOW-RESOLUTION` on `<engine>`, 1,100 `WLN-ENGINE-UNKNOWN-IMPORT` | M-1, M-3, BP-04, LX-01; leverage D1-S0, D1-S0b |
| 6 | T6 | #9 (+ broadened) | **Reconcile the tracker with reality.** Land or revert `ecad149` (C-16) and `79e06d6` (GS-7); correct closures recorded against them (Filigree `filigree-bd1abc7243` closed at `filigree@79e06d6`; hub `weft-87443311a0`, `weft-13f84c77c5`, `weft-f1cbd27cfb`); `filigree-1627c6fc7a` (C-20) open though shipped in 3.2.0; close the 3.3.0 milestone `filigree-b21d7a9f17` with a verdict (21 open children need an owner decision); rewrite `docs/product/current-state.md` (dated 2026-07-07). Under D2, `codex/gs7-warpline-worklist` is abandoned and `filigree-1544621b0a` closed. T6 adds: **prototype the reachability check on close as a warning** and **write one PDR covering 3.0–3.3**. | `git merge-base --is-ancestor` vs `origin/main` (local `main` is stale at `80050fb`) | 17 anchors: 15 reachable, `79e06d6` not, 1 cross-repo | P-4, P-6, P-10, BP-03, BP-11; leverage N9, X3, X4 |
| 7 | T7 | #5 | **Federation token file vs env mismatch gives 401.** `.weft/filigree/federation_token` rejected; env `WEFT_FEDERATION_TOKEN` works; boot never reconciles; resolved once at `create_app`. | `federation_token.py` (`mint_token_file`); `dashboard.py:141`; `cli_commands/admin.py:989` | Live GETs against :8834; seen by two reviewers (second via Loomweave's Filigree join 401) | HTTP F14, M-6; related `filigree-806dc04161` |
| 7 | T7 | #8 | **Scan-results `failed=[]` hardcoded**; over-cap drops only in free-text `warnings[]` under HTTP 200; replayed batch returns `succeeded:[]`, `failed:[]`. | `generations/weft/adapters.py:355-360`; `db_files.py:1273-1301`; `files.py:640-675`; commit `20bc918` | Code | HTTP F2; leverage N8 |
| 7 | T7 | #11 | **`CLAUDE.md` names Legis and Warpline as live tools**; also tells agents to pass `--agent-id`, which `filigree-mcp` does not accept (`mcp_server.py:1472-1473`). Generate rather than hand-edit (X7). | project `CLAUDE.md`; session tool list | Verified by the brief author | brief housekeeping; LX-11; leverage N11, X7 |

3.x quick wins proposed by individual reports that are *not* in the brief's list (candidates for the plan): BP §9 — epic forward `open→closed` edge (BP-15), retention on ingest (BP-04; moot under B2), release target from history (BP-09), doc fixes (BP-17); LX next steps — LX-07 claim-description lead line, LX-08 served names in error text and `suggested_actions`, LX-14 CONFLICT shape and path fixes; MCP follow-up (1) — F1/F3/F4 additive fixes in a 3.3.x patch; HTTP B list — F6 idempotency key, F7 body cap, F11 registry error unification, F15 contracts.md fix, F17 goldens, F18 `/api/weft/_capabilities`; leverage T6 — reachability warning; X8 — lease ~2 h.

---

## 3. 4.0 work packages (every concrete change that survives the owner rulings, grouped by subsystem)

### 3(a) Work-state core and lifecycle

**Types collapse 24 → 6** (BP-05, BP §5.1; brief D3; leverage S8 — "specify after S2"):
- Leaf types `task`, `bug`, `feature` on one lifecycle; `task` absorbs `step`, `spike` (`kind=spike`, `findings` required at completion), `review` (audit/checklist; BP-06), `debt` (label or kind). `bug`: `severity` hard at `triage→open`, `steps_to_reproduce`. `feature`: `acceptance_criteria` hard at `triage→open`; `proposed` = `triage`.
- Container types `epic` (absorbs `phase`, `work_package`; ordered sub-container with `sequence`), `milestone` (`exit_criteria`, `target_date`), `release` (freeze/ship/rollback; **no auto-seeded `Future`** — BP-16, LX-02, R8).
- Retire 13 never-instantiated types (risk, mitigation, theme, objective, key_result, incident, postmortem, debt_item, remediation, spike, finding, deliverable, release_item) and requirement/acceptance_criterion (errorworks only, 11+8 rows, all initial state). `kind` is a free sub-classification (`spike`, `review`, `chore`, `reverify`, …) with optional per-kind field schemas; kinds never change the lifecycle.
- Packs 9 → 1 built-in. Owner demoted "optional installable packs" to "ship one opinionated workflow" (rulings Demoted row); leverage §6 row 1 warns opt-in packs become holding pens — if retained at all, they live out of tree with an owner and kill-by date (X1).
- Owner gate: deprecating the planning pack (used in every project per P-3) and the status-name change (brief D3). BP-18: decouple from Shuttle, which has no repo.

**Leaf lifecycle (BP §5.2):** 6 states `triage`(O, not in ready) → `open`(O) → `in_progress`(W) → `done`(D), plus `parked`(O, never in ready) and `in_review`(W, policy-driven only). Verbs: `claim`, `start` (= claim + `in_progress`), `release`, `submit`, `approve`/`reject`, `complete`, `discard` (from any non-done state; reason + resolution required), `park`/`unpark`, `reopen` (→ last non-done state from history). No `force`. Initial state by creation source: automation/other-actor-filed items start in `triage`; an agent's own immediate work may start in `open`. No `wip→open` forward edges (BP-09).

**Resolution enum on `done`** (BP-01, BP-07, BP §5.2): `completed | duplicate | wont_do | not_a_bug | obsolete | cannot_reproduce | resolved_by_scan`. Only `complete`/`approve` edges can set `completed`; `discard` never can. Container resolutions `achieved | cancelled | superseded` (+ `released` for release). Leverage S1/X1 adds a `rejected` terminal (see §1.5 naming gap). `deferred` moves from done-category to `parked` (open) — announce to consumers (BP-07 risk, BP §5.6 wire compat).

**Force-close removal** (BP-01, D3; leverage D3b): retire `close_issue(force=True)` / `TransitionMode.BACKWARD` (`db_issues.py:1260`; `templates.py:1049-1053` backward edges skip `required_at`; `templates_data.py:1714-2105` reverse-edge table); 324 reverse edges → two universal verbs `reopen` and `release`. Cascade close uses `resolution=resolved_by_scan` (`db_files.py:2232-2242` today uses `force=True`). Paired measurement required: discard rate by actor and resolution in the owner digest (leverage §6 row 2). Do **not** use "gate-bypass rate = 0 by construction" (BP §5.5) as a success metric.

**Completion evidence = server-side check, not a field** (S2/X3; owner ruling "done = anchor machine-checked against the integration branch"): `resolution=completed` requires a commit anchor the **server** verifies is reachable from the *fetched* integration ref (X3; leverage §1 "origin/main, not main" — 13 valid 3.3.0 anchors read falsely against stale local `main`); otherwise the item waits in `in_review` with reason `awaiting-integration` (BP-11's state); on by default in git repos; cross-product consumers may cite only `completed` items; typed verification text stays but is labelled *asserted*. Open spec items (leverage §9, §10 #7, §11): squash merges (resolve by PR or tree equivalence), cross-repo anchors (explicit `repo` qualifier; Legis merge `25d64e2` is an example), fall back to `awaiting-integration` never silent pass; per-project integration-ref setting if "done on a branch" is wanted. Limit: governs only tracker-visible closes (~240 fleet closes had no tracker-visible start, leverage §2.4). Working indicator: share of `completed` closes with a reachable anchor from 1.5% to >90%; zero closes against unreachable commits; `awaiting-integration` age small. Can warn in 3.x (T6).

**Collapse ritual states** (BP-02): `confirmed/fixing/verifying` and `approved/building/reviewing` → `open → in_progress`; keep one optional `in_review` entered only when a policy requires separation of duties. Owner framing: the verifier is an **independent agent** (different session/model), not a human by default.

**Container roll-up** (BP-03, BP §5.3; leverage D3e): `planned`(O) → `active`(W, auto on first child wip) → `done`(D, **hard-gated on no non-done children**; error lists children and offers `move-to <container>` or `discard` as recorded scope trades); `cancel` requires open children moved or discarded. Release adds `frozen`(W) `ship`→`done(released)`, `rollback`→`rolled_back`(W). Blockedness inherits down the tree (fix `get_ready` direct-deps-only check, `db_planning.py:368-379`); progress computed from children by resolution; containers never in `ready`/`start_next`. Risk: the "unscheduled" epic (BP §5.6 migration target for 3.3.0's 21 open children) becomes a dumping stock (leverage §6 row 2d) — give it an aging review.

**Governed close, provider-agnostic** (BP-14, BP §5.4; boundaries §9 adopts it; D2 ruling): policy declares which (type, kind, resolution) closes need a gate: `separation_of_duties` | `human` | `external:<provider>` (reserved slot). Flow `submit` → `in_review` with `gate_status ∈ {pending, approved, denied, stale, unavailable}`; `unavailable` retried by a sweep verb with backoff then escalates to `human`; `in_review` queue filterable by `gate_status`/`required_approver` is the human-approval queue; agents cannot claim `human`-gated items; escalation states excluded from agent queues; reconciliation debt becomes an `open/resolved` record (today `db_meta.py:101-131` never filters resolved debt). Owner framing amendment: prefer `separation_of_duties` with an independent agent; human gates only for outward-facing/irreversible acts; inbox items age to `parked`, never `approved` (leverage §6 row 9).

**Claims and leases** (BP-09, X8, S6; leverage §7.4 D7 prerequisite): default lease ~2 h (today 48 h vs sub-10-minute median, `db_issues.py:68`); implicit heartbeat on any holder write; expired claims listed in `ready` as `reclaimable` (today `get_ready` excludes any assigned issue regardless of expiry, `db_planning.py:374`); `release` returns to last open state from history (today falls to `initial_state`, `templates.py:954-959`); claims keyed to session id. MCP F22: clear claim fields on close; reopen leaves unassigned or requires `claim:true`.

**Archive as attribute** (BP-08): `archived_at` on done items, status + resolution preserved, reopen allowed; remove synthetic `archived` status (special-cased in `db_workflow.py:295-334`, `analytics.py:157-201`, `db_issues.py:210-235,1409`, `db_planning.py:334`); `compact_events` never deletes status/resolution events.

**Observations / notes** (BP-13; LX R4; M-4; brief §6): collapse observation + annotation into one **note** with `ttl`, `critical`/`must_consider` flags and optional S-1 `path`/`symbol` references with snapshot, no anchor-drift machinery. TTL scales with priority; P0–P1 never expire silently (auto-promote to `triage` or appear in a session-start digest). Keep link dispositions (`evidence`/`duplicate`/`superseded`/`related`). Keep Loomweave guidance proposals via observations (boundaries §7 "Keep as is"). One retention policy doc covering observations/findings/annotations (BP-13).

**Process metrics v2** (BP-12, BP §5.5): per kind, containers excluded: p50/p85 claim→done cycle, lead time, WIP count and age, arrival vs completed by resolution, discard rate, time in `triage`/`parked`/`in_review`, reopen rate, gate-denial rate; per inbox size/age. Replace means-only `get_flow_metrics` (`analytics.py:248-255`). Ship early (BP §9 step 4).

**Migration (BP §5.6):** full built-in state mapping (bug/feature/task/step/epic/phase/work_package/milestone/release/requirement/AC/archived); forced closes with duplicate/superseded/scratch/wontfix reasons map to discard resolutions, otherwise `completed` + `migrated_inferred=true` (validate heuristic on 50 forced closes, BP §9); "[Bug tree]" nodes → `task kind=review` resolution `completed`; delete `Future` singleton when 0 children; store `legacy_status` for one major (BP §7); open-child sweep for done containers (reopen as `active` or move children; never auto-close); prototype on copies of this tracker and errorworks. Verify live consumers (Loomweave, Wardline) read categories not literal status names before cut-over (BP §5.6, BP gap 5).

**Scratch/review isolation** (BP-06; S5; SUR lesson 1): review and smoke runs in a scratch project, never the production tracker; `source:dogfood` label on dogfood-friction issues (X5).

### 3(b) Identity and session keying (D4 + S6)

- Bind identity at launch: `filigree-mcp --actor <id>` (stdio), `FILIGREE_ACTOR` for the CLI, both written by `filigree install` and echoed in the managed block ("You are `<id>`"); HTTP via header (`Mcp-Agent-Id`) or token claim (MCP F10; LX R1; boundaries S-6 `--agent-id` + HTTP header).
- Drop per-call `actor`/`assignee`/`author` from 61 (MCP F10) / 62 (LX-11) tool schemas (~4.9 KB / ~1.4K tokens); keep explicit override only on coordinator verbs (`work_reclaim`, peer `work_release` with `override:true` — MCP caveats).
- **Leverage amendment (owner-adopted):** the server **mints a session id per connection** and attributes writes to it rather than refusing unidentified writes; actor label is descriptive. Claims keyed to session id, actor is attribution (BP-10, BP §5.4). "Ship identity and session together, or not at all" (leverage §6 row 5; risk table High). Paired measurement: distinct sessions per actor; claim conflicts between sessions of the same actor.
- Keep a `verified_principal` seam field for a future verifier (`filigree-81d3971467`); Tabard dependency gone (D2).
- Fix false `--agent-id` claim in the CLAUDE.md Weft block (LX-11; `mcp_server.py:1472-1473` accepts only `--project`). Record the 3.2.0 ACTOR_MISMATCH removal (ADR-012 reversal) in a PDR (P-14, `filigree-60a5103dee`).
- Interim error text: "No actor supplied (defaulted to 'mcp'); holder is 'agent-a'. Pass actor=<your identity>." (LX-11).
- Related STILL-OPEN: `filigree-c2009921cf` (sessions, ADR-011 deferral; untouched ~5 months), `filigree-81d3971467`.
- Risk: multi-agent setups sharing one server process (HTTP/streamable) — bind per connection; test the dual-agent-one-server case (LX risk table).

### 3(c) MCP contract

**Envelope** (MCP F2; HTTP F4; S-7; brief §4): `{error, code, retryable, cause_kind, hint}`; `isError=true` on every non-success; re-emit SDK validation failures as `{error, code:"VALIDATION", details:{path, constraint}}`; computed hints from `missing_fields`/`next_action`/reachable done states (LX-18, R7); `renamed_to` tombstones with `details:{renamed_to, removed_in, migration}` (MCP F9, LX-08); split `CONFLICT` into `BLOCKED_BY_POLICY` vs `DEPENDENCY_UNAVAILABLE` (HTTP F4); `outputSchema` + `structuredContent` generated from the `types/api.py` TypedDicts, compact JSON (F17); `serverInfo.version = filigree.__version__`, non-empty `instructions`, `_meta.contract_version` bumped on breaking shape change (F18); `additionalProperties:false` and delete the hand-rolled validator (F21); one `no_op` sentinel `{result:"no_op", reason}` for benign no-ops (F23); label pattern `^[a-z0-9][a-z0-9:_-]*$` (F24); protocol error for degraded resource reads (F25); SCHEMA_MISMATCH remedy composed from `install_context` with "do not retry; surface to the user" (LX-17); did-you-mean for near-miss params (LX-08); `work_start_next` explains skipped unstartable items (LX-12); drop `parent_id` duplicate, rename `*_id` stragglers to `*_issue_id` (F19); silent-success-on-bad-filters → `VALIDATION` with valid values (F12).

**Retryable / idempotency** (MCP F1, F3, F4, F5, F8, F11, F26; HTTP F6): optional `client_request_id` on every mutation stored on the event ("same id + same args → original; same id + different args → CONFLICT"); per-tool annotations declared in the registration table (`idempotentHint`, `destructiveHint`, `openWorldHint`, `title`), with a test that every tool sets all four; one `BatchResponse` `{succeeded:[{id,...}], unchanged:[{id,reason}], failed:[{id,code,error}], summary:{requested,applied}}`, dedupe ids first, already-in-target-state → `unchanged`, id-shape problems per-item, empty change set → `VALIDATION`, declare non-atomic or offer `atomic:true`; table-driven **call-twice test** per mutating tool declaring `expected_second_call: noop|dup|error` (F26; this is the re-entry gate's "retry-safe core loop (call-twice suite green)"); golden-conversation replay tests. Owner design posture (1): an `apply(ops[])`-style batch/transaction primitive is preferred over narrow verbs.

**Bounding rule** (MCP F6, F7; LX-04, R2; HTTP F5; brief §4): slim projection by default on every list tool (id, title, status, category, priority, type, assignee, `updated_at`, `startable`); default limit ~25 (MCP: 25–50), hard server max (~200) with no bypass, `has_more` + opaque keyset cursor on `(sort_key, id)`, `truncated` flag + total; reject over-max `limit` with `VALIDATION` not silent clamp; delete `no_limit`; full detail only via `issue_get` or `response_detail=full`; truncate description/notes with `truncated:true`; writes return slim ack + `changed_fields`. **Sequence after T3/S4** (honest READY) — leverage §6 row 7: agents already route around `work_ready` (36 calls) via `list_issues no_limit` (151 calls). Owner design posture (2): route bulk reads through export/pipe (CLI JSON) outside the context window.

**Handlers off the event loop** (MCP F15; HTTP F1): run handlers via `anyio.to_thread.run_sync`; make `admin_restart_dashboard` unavailable over HTTP.

**Reference data as resources** (MCP F13): `filigree://types`, `filigree://types/{type}`, `filigree://packs`, `filigree://schema`, `filigree://workflow/statuses`, `.../explain/{type}/{status}`, `.../guide/{pack}`, `filigree://labels/taxonomy`, `filigree://scanners`, `filigree://prompt-packs`; one fallback `reference_get(topic, key?)`; collapse the pulse triple (`session_context_get`, `summary_get`, `filigree://context`).

**Profiles / tiers** (MCP F16, F27; LX R4, R5): `--profile agent|admin` (default `agent`); admin tools not served by default, path-taking tools stdio-only; tier moves to `_meta`; re-tier by observed usage (LX R5 core list: `issue_get`, `issue_close`, `comment_add`, `issue_update`, `issue_search`, `issue_list`, `comment_list`, `issue_create`, `work_start`, `workflow_transition_list`, `observation_create`, `session_context_get`, `work_ready`); every description opens "Use when … / not for … (use X)"; description on every parameter as a CI gate (88 of 469 missing); strip ticket/ADR/phase IDs and archived-seam jargon from served prose (LX-16); claim-only descriptions stop teaching the forbidden two-step (LX-07). Leverage §6 row 1: opt-in profiles are holding pens — they carry the same X1 registry entry.

**Notices channel** (LX R3, LX-09, LX-10): slim `notices`/`attention` block on `work_start`, `work_start_next`, `issue_get`, `issue_update`, `issue_close` responses (critical `must_consider` notes, peer activity on same issue/file, unread broadcasts, own stale-claim/lease warnings); SessionStart is the backstop; **cap at 3 items, measure ignore rate** (leverage §6 row 8; X2 budget). Broadcast board (PRD-0001, T4) if kept rides this channel; P-7 says make a recorded keep/kill call.

**Actor-aware session brief** (LX R1; S4): ~40 lines: who you are; YOUR CLAIMS with lease expiry; STARTABLE NOW with age and parent; OTHERS ACTIVE: do not touch; ATTENTION; one-line counts (containers, blocked, stale claims, observations, defect-only analyzer signal); age-gated critical path (show only if changed within 14 d, else "critical path stalled 36d", LX-19). Budgeted under X2.

**Catalog 118 → 76 (MCP critique) → ~52 under B2 (boundaries §5) → attention budget instead of a count (S12/X2).** Full MCP proposal table (MCP "Major-refresh catalog proposal"):

| Current | Action | Next-major name / note |
|---|---|---|
| issue_get | KEEP | `include_files` removed; slim default |
| issue_list | KEEP | slim, `response_detail`, validated filters, cursor, no `no_limit` |
| issue_search | KEEP | same bounding rule |
| issue_create | KEEP | + `client_request_id` |
| issue_update | KEEP | |
| issue_close | KEEP | already-closed → `no_op` |
| issue_reopen | KEEP | clears claim (F22) |
| issue_delete | KEEP | destructive hint |
| issue_validate | KEEP | |
| issue_batch_update / issue_batch_close | KEEP | unified `BatchResponse` |
| issue_event_list | KEEP | bounded |
| issue_subtree_label | KEEP | survivor of the pair |
| plan_label_tree | MERGE → issue_subtree_label | tombstone |
| admin_undo_last | RENAME → issue_undo | `expected_event_id` required (F1) |
| issue_file_list | MERGE → file_list(issue_id) | |
| issue_annotation_list | MERGE → annotation_list(issue_id) | |
| work_ready / work_blocked | KEEP | bounded |
| work_start | KEEP | the one claim-and-transition verb |
| work_start_next | RENAME → work_next | `start:true` default |
| work_claim | MERGE → work_start(advance=false) | |
| work_claim_next | MERGE → work_next(start=false) | |
| work_release | KEEP | holder-checked default (F3) |
| work_release_mine / work_reclaim / work_heartbeat / work_stale_list | KEEP | |
| dependency_add / dependency_remove | KEEP | |
| dependency_critical_path | RENAME → dependency_critical_path_get | |
| plan_dependency_retarget | MERGE → dependency_retarget | |
| plan_create | KEEP | + `client_request_id` |
| plan_create_from_file | MERGE → plan_create(source) | path form admin-only |
| plan_get / plan_step_add / plan_step_move | KEEP | |
| label_add / label_remove | MERGE (take `issue_ids[]`) | validate pattern |
| label_batch_add / label_batch_remove | MERGE → label_add/remove | |
| label_list | KEEP (`detail=taxonomy`) | |
| label_taxonomy_get | MERGE → label_list(detail=taxonomy) | |
| comment_add | KEEP | + `client_request_id` |
| comment_batch_add | KILL | |
| comment_list | KEEP | bounded |
| file_list / file_get / file_timeline_get / file_register / file_association_add / file_delete | KEEP (MCP) | **REMOVED under B2** |
| file_annotation_list / annotation_list / annotation_attention_list | MERGE → annotation_list | **REMOVED under B2** |
| annotation_create / get | KEEP (MCP) | **REMOVED under B2** (→ note) |
| annotation_update / resolve / supersede | MERGE → annotation_update | **REMOVED under B2** |
| annotation_link / unlink | MERGE → annotation_update | **REMOVED under B2** |
| annotation_promote / carry_forward | KEEP (MCP) | **REMOVED under B2** |
| observation_create / list | KEEP | (→ note under R4/M-4) |
| observation_dismiss + batch | MERGE → observation_dismiss(`observation_ids[]`) | |
| observation_link + batch | MERGE → observation_link(`observation_ids[]`) | |
| observation_promote + _to_issue + batch | MERGE → observation_promote(`observation_ids[]`, `mode: each|merge`) | |
| finding_list / get / report | KEEP (MCP) | **REMOVED under B2** |
| finding_update + batch + dismiss | MERGE → finding_update | **REMOVED under B2** |
| finding_promote + _and_attach_entity | MERGE → finding_promote(`attach_entity`) | **REMOVED under B2** (promote becomes S-2, producer-side) |
| entity_association_add / remove | KEEP (MCP) | **REMOVED under B2** (→ S-1 references) |
| entity_association_list + _by_entity | MERGE | **REMOVED under B2** |
| warpline_worklist_ingest | KEEP (MCP, `federation` profile) | **REMOVED under B2/D2** (→ S-2 promote `kind=reverify`) |
| scan_trigger + batch | MERGE | **REMOVED under B2** (M-5 kill scanner runner) |
| scan_preview / scan_status_get | KEEP (MCP) | **REMOVED under B2** |
| scanner_list + available_list | MERGE | **REMOVED under B2** |
| scanner_enable / disable | KEEP (MCP) | **REMOVED under B2** |
| prompt_pack_list | RESOURCE | (scanner-related; moot under B2) |
| template_get | RESOURCE `filigree://types/{type}` | |
| type_get | KILL | documented alias |
| type_list, pack_list, schema_get | RESOURCE | |
| workflow_status_list / explain / guide_get | RESOURCE | |
| (new) reference_get(topic, key?) | ADD | fallback for resource-blind hosts |
| workflow_transition_list | KEEP | |
| stats_get | MERGE → summary_get(format=json) | |
| summary_get / metrics_get / change_list / reconciliation_debt_list | KEEP | |
| session_context_get | KEEP* | subject to LX verdict (LX keeps it as the brief) |
| mcp_status_get | RENAME → status_get | |
| admin_archive_closed, admin_compact_events, db_checkpoint, admin_export_jsonl, admin_import_jsonl, admin_restart_dashboard, admin_reload_templates | PROFILE admin | restart unavailable over HTTP |

Arithmetic: MCP default tally = issue 14 + work 9 + dependency 4 + plan 4 + label 3 + comment 2 + file 6 + entity-association 3 + warpline 1 + scan 3 + scanner 3 + annotation 6 + observation 5 + finding 5 + reference/transition 2 + diagnostic 6 = **76** (+7 admin profile, 10 → resources). B2 removes file 6 + entity-association 3 + warpline 1 + scan/scanner 6 + annotation 6 + finding 5 = 27 → 49, plus ~3 reference tools (S-1 attach/list/state) ≈ **52** (boundaries §5 B2). LX R4 target 40–50 reached after the observation/annotation → note collapse. Then **S12/X2 replaces the count with a byte/concept budget** for the default profile and the ~40-line brief: "one in, one out", measured by schema bytes per 4.x minor and a 20-query ToolSearch golden set (LX gap 2). Counts are gameable by mode-flag merges (leverage X2 cites `observation_promote(mode)` and `annotation_update(...)` as examples).

Migration shims (MCP, under D5 clean break): one tombstone table, not a live alias layer; announce one minor ahead with `_meta:{deprecated:true, replaced_by}` and an old-name counter in `status_get` (`deprecated_tool_name_calls`); parameter-level shims for merges for one major (note D5 "no compatibility layer" — the plan must decide whether even these survive); bump `_meta.contract_version` and `serverInfo.version` together; ship resource URIs in the last 3.x minor; re-key `_all_handlers`/`TIER_MAP`/argument maps to served names and demote `RENAME_MAP` to data-only (MCP Disagreement 2, LX-08). MCP 2.0 / protocol 2026-07-28 in or out (P-15, Q11, `filigree-1977c738f1`).

### 3(d) CLI / JSON scripted surface

- Owner design posture (2): the CLI with JSON I/O is a **first-class scripted surface**; agents compose it in one shell call; bulk reads go through export/pipe.
- Generate the CLI from the same registration table as MCP (one source for noun-verb names, params, defaults) (MCP F20, S7/X1); keep verb-noun spellings as hidden aliases (closes `filigree-4c73f6cf22`). Parity gaps today: CLI-only `finding clean-stale`, `file migrate-registry`, `doctor`, `server *`; MCP-only annotation_update/supersede/promote/link/unlink/attention_list, `issue_subtree_label`/`plan_label_tree`, `entity_association_*`, `warpline_worklist_ingest`, `plan_step_move`/`plan_dependency_retarget`; defaults drift (CLI `list --limit` 100 vs MCP 50; actor `cli` vs `mcp`).
- CLI logging (T1) is required before any kill decision (design posture 4). CLI error JSON already matches MCP (`filigree show nope --json` → `{"error":...,"code":"NOT_FOUND"}`, exit 1) — keep.
- `FILIGREE_ACTOR` env for the CLI (R1). Owner digest also available as a CLI digest (X4).
- Regenerate workflow docs from the registry (BP-17: `docs/workflows.md:74,649,100-105` contradict code; `requires_packs` unenforced).

### 3(e) Findings boundary B2 (D1 ruled; boundaries §5–7, ADR-F4-001; S3/X7)

**What Filigree keeps / builds (~1.5–2.5k lines):**
- **Evidence references** on issues (S-1): `{kind ∈ path | symbol | finding | commit | sei | url (extensible), producer?, value (opaque, never interpreted), scheme?, snapshot{title, rule_id, severity, message, path, line, at_commit} with byte caps, state ∈ {present, gone, suppressed, unknown}, state_at, state_commit}`; generalises ADR-029 entity associations, file associations, `claim_commit`/`close_commit`. Per-type workflow policy hooks ("close requires ≥1 evidence ref of kind commit"). Filigree core never resolves; resolution is optional client-side enrichment. Snapshots go stale by design: render `state_at` age; `unknown` after freshness window (RSK-2).
- **Promote seam** (S-2): producer → Filigree work candidate; payload = S-8 finding record + priority/labels; idempotency key `(producer, scheme, fingerprint)` returning the existing open issue; `kind=defect` only; batch envelope (`succeeded`/`unchanged`/`failed`); size caps; project scoping; creates a `bug` in `triage` with the finding reference (BP §5.4 mapping). Replaces `scan-results` bulk ingest, promote-by-fingerprint, `warpline_worklist_ingest`.
- **Evidence state update** (S-3): state-based (re-sending current state is idempotent), `at_commit`, `completeness` (partial scan may assert `present`, never `gone`); Filigree records and surfaces "evidence gone" in attention; auto-close only via explicit policy (`resolution=resolved_by_scan`). Two open spec questions: (1) how the producer learns which references to report on (S-4 lookup by `(kind=finding, producer)` or a local promoted-set); (2) the fingerprint-scheme bump rule (`wardline rekey`, `core/rekey.py:835-880` "no remap endpoint") — producer must post old→new remap or re-promote, else B2 inherits today's orphaning.
- **Work-by-reference lookup** (S-4): query by `(kind, value)` with path prefix match; bounded, cursor-paged, project-scoped, fail-closed on ambiguity; slim issue rows + reference snapshot. Replaces classic `GET /api/entity-associations?entity_id=` (HTTP F9). Answers "what work touches X" without a peer (M-9 gap 2).
- ~3 reference tools in the MCP catalog.
- Migration (boundaries Stage 2): convert linked findings, file associations and entity associations into references on their issues; everything else exported to `archive/findings-3x.jsonl` (**owner gate**); tombstones with `renamed_to`/`migration`; parity check: every issue that had a linked finding/file/entity has an equivalent reference. Give the archive an owner and a deletion date (leverage §6 row 4d).

**What moves to Wardline / is retired from Filigree:**
- Wardline owns findings, run telemetry and the accept/suppress verdict (baseline, waiver, judged files in `.weft/wardline/`), serves its own finding queries. Loomweave owns its own findings and map. No product aggregates code facts.
- Retire: findings warehouse (`scan_findings`, `db_files.py` ingest/update/clean-stale/list), file registry (`register_file`, `registry.py` `LocalRegistry`/`LoomweaveRegistry` 1,915 lines; ADR-014 delegation unused in 0/15 projects), annotation anchors (`db_annotations.py` 1,280 lines, 7 rows fleet-wide), LLM scanner runner (M-5: `scanners.py`, `scanner_runtime.py`, `bundled_scanners.py`, `scanner_prompts.py`, `scanner_reporting.py`, `scanner_scripts/`; 4 runs ever), governance/Legis client, Warpline consumer, SEI backfill, registry client (~19k lines not rebuilt; boundaries §2.8). Seams retired with tombstones: `POST /api/weft/scan-results` and `/api/v1/scan-results`, `POST /api/weft/findings/clean-stale`, `GET /api/weft/findings` and `/api/weft/files` (Flow B), `/api/v1/files*` registry delegation, Legis closure gate and sign-off binding, `warpline_worklist_ingest`, Loomweave→Warpline churn, Wardline→Loomweave taint-fact store (unless Loomweave keeps it and a consumer exists; 1,628 rows written once 2026-07-12), SARIF→Filigree translation.
- Supersedes in part: ADR-007 (`report_finding`), ADR-014, ADR-015, ADR-017, ADR-029; hub doctrine §2/§6 ("finding lifecycle lives in Filigree"; Wardline `core/finding.py:4-7`).
- The BP/LX Option A disposition policy and `signal_class` at ingest are no longer needed (brief D1; boundaries §9): "classify at the producer (only defects are promoted), not at the consumer."

**Stage 0 telemetry cut** (do-now #5): Wardline emits only `kind=defect` (flag, default on); stable engine fingerprints (today `sha256(rule_id, message)` with the ratio embedded); Filigree refuses/ignores `path="<engine>"`; legacy telemetry gets `not_work` disposition or export-and-drop (owner gate). Gate to move on: new Filigree rows per scan ≈ defects; `<engine>` count stops growing. Rollback: turn the Wardline flag off. Stage 0 also measures what B2 assumes: defects per scan, promotes per defect (boundaries caveats).

**Health/outcome metrics for B2:** promotes per 100 defect findings (needs an owner — RSK-1 confident-empty); Wardline prints "N defects, M promoted"; waiver count and age reported wherever defects are reported (S3 co-requisite); triage aging rule. Reversal triggers (ADR-F4-001): ≥100 promotable defects/month fleet-wide needing cross-producer dedupe → reconsider Option C; S-2/S-4 round-trip failure >5% over 4 weeks → reconsider Option E; Loomweave passes kill-date and gains a consumer needing findings on entities → B1-style read view reading Wardline's artifact. Unwind: re-introduce a `kind=defect`-only bulk ingest as a new generation (2–4 engineer-weeks). Next review 2027-04-07.

**Owner's Demoted note:** "solo install as a boundary driver" is demoted; B2 stands on reliability grounds.

### 3(f) Federation HTTP API (HTTP F1–F21) under the "living tool set" framing

| ID | Disposition | Where it lands |
|---|---|---|
| F1 (gate blocks event loop) | Keep; do-now #2 (T2) | Off-loop network probe; thread `legis_known_down`; moot once Legis gate removed in 4.0 |
| F2 (`failed[]` hardcoded) | Keep; do-now #8 (T7), additive | `adapters.py:355-360`; superseded by S-2 batch envelope in 4.0 |
| F3 (`loom` retired without ADR) | Keep; D5 ruling: "A short ADR records the 3.0 `loom` break" | Write first; `/api/loom/*` structured 410/301 with `details.successor`; re-scope `filigree-bc80a673e1` |
| F4 (CONFLICT conflates retryable/terminal; status mapping inconsistent) | Keep → S-7 envelope in the successor generation | `retryable`, `cause_kind`, `BLOCKED_BY_POLICY` vs `DEPENDENCY_UNAVAILABLE`; one mapper |
| F5 (`/ready`, `/blocked` unbounded; `has_more:false` hard-coded; 68 KB) | Keep → successor generation bounding rule | Interim: optional `limit`/`offset`, mark `unbounded` in contracts.md |
| F6 (no idempotency key; counters double-count on replay) | Keep, additive | `Idempotency-Key`/`batch_id`; distinct-finding counting; moot for scan-results under B2 but applies to issue/comment creation |
| F7 (no body cap, no deadline) | Keep, additive | 8 MiB cap before parse → 413 envelope; ingest deadline |
| F8 (unmapped status → VALIDATION; 404 no migration hint) | Keep | Generation-aware 404 handler; map 405/413 |
| F9 (entity associations only on classic; outside token gate and fail-closed guard) | Keep as mechanism; resource set follows D1 → S-1/S-4 | Successor generation carries references + reverse lookup, project-scoped, gated |
| F10 (ad-hoc living surface; singular/plural split) | Keep | Drop the living surface or generate the gate list from the router; test that weft-owned routes are inside the gate |
| F11 (registry errors inconsistent; fail-closed ingest under `registry_backend=loomweave`) | Keep, additive; registry client retired in 4.0 (B2) | `registry_startup_error_response`; `Retry-After` on 503 |
| F12 (Warpline ingest MCP-only, non-atomic, no cap) | **Dropped** by D2 (abandon branch) | Replaced by S-2 promote `kind=reverify` |
| F13 (token gate = generation marker, not access boundary) | Keep as documentation | "gate = generation marker"; optional `Deprecation`/`Link` header on classic |
| F14 (token file vs env 401) | Keep; do-now (T7) | Boot reconciliation; `auth.source` + `file_matches_active` on `/api/health` |
| F15 (contracts.md contradicts live auth scope) | Keep, additive | Fix docs; docs-contract test diffing `auth_scope`; folds into weft-contracts/X7 |
| F16 (`/changes` cursor keys undocumented; unstable `/issues` order) | Keep → S-5 change feed | One cursor over all entity kinds incl. references; tombstones; stable ordering |
| F17 (no goldens for Legis closure gate / Warpline worklist; `codex/gs7-warpline-worklist` +1.4k/−3.4k) | Legis half moot (D2 fold-in); Warpline half **abandoned** (D2 ruling: close `filigree-1544621b0a`) | Goldens in `weft-contracts` for S-1…S-8 instead |
| F18 (`/api/health` shallow; no capability discovery) | Keep | `/api/weft/_capabilities` with `generation`, `successor`, `limits`, `auth.source` (S-6) |
| F19 (HTTP actor self-asserted) | Keep as-is | Connection-bound actor via header (S-6); `verified_principal` seam field |
| F20 (no OpenAPI per generation) | **Demoted**: "kept only if generation helps agents" | Generated from the X1 registry (S7) if kept |
| F21 (`LEGIS_URL` unset = silent fail-open for signed bindings) | Owner decision; moot once Legis gate removed | Return `UNAVAILABLE` or warn |

Successor generation (brief D5; HTTP A/C): decide whether the major carries a new generation (ADR-002 §3 says a bump does not imply one; F4/F5/F9 in scope ⇒ it does); choose the name thematically; keep `weft` frozen alongside (D5 ruling) — but see the unconfirmed D5 adjustment (no long-lived freeze; versioned contracts + goldens + short deprecations). Owner Demoted row: public OpenAPI for third parties is demoted.

### 3(g) Folded-in archived members (D2 ruling; boundaries §4)

| Member | Fold-in | Concrete changes |
|---|---|---|
| **Legis** | "close requires evidence" → Filigree workflow-pack policy (local, no network; required evidence refs/commit/reason per type); real gates via BP §5.4 `human` / `separation_of_duties` with a visible `in_review` queue; `external:<provider>` reserved; "govern a finding" → Wardline waiver (reason, expiry, optional approver) reviewed in the PR; branch/commit/PR context → `git`/`gh` directly | 3.x: `LEGIS_URL` set-but-unreachable warns not wedges (T2). 4.0: remove `governance.py`, `legis_client.py`, governed-binding columns, signature/signoff_seq on entity associations (HTTP F9). Drop the real-Legis test `filigree-10ad50dacf` (P-7 routed). Remove Legis closure-gate text from `error-codes.md:43-49` (LX-14). |
| **Warpline** | reverify worklist → generic promote seam S-2 with `reference` = SEI/path, `kind=reverify`, "one open item per reference" dedupe (BP §5.4 adopted); churn/recent change → `git log` (delete Loomweave proxies); blast radius → Loomweave callers + `git diff` | Abandon `codex/gs7-warpline-worklist`; close `filigree-1544621b0a`; remove `warpline_consumer.py`, `mcp_tools/federation.py` ingest, `warpline_worklist_ingest`; strip "(warpline seam)" prose from `work_start`/`issue_close` `commit` params (LX-16); schema v29 commit-anchor fields become S-1 `commit` references. |
| **Tabard** | connection-bound actor convention in `weft-contracts` (S-6): `--agent-id` at MCP launch, HTTP header, per-call `actor` as override, mutations without identity refused (amended by S6: mint session rather than refuse) | PDR-0004 and roadmap Next "Tabard consumer adapter" need a recorded kill-or-reframe (P-8); `filigree-81d3971467` reframed. |
| **Weft hub** | runtime-free `weft-contracts` package: versioned markdown + JSON Schema + golden vectors; no runtime, no PM content; seam owner writes the spec, consumers vendor goldens; contents S-1…S-9, 11-class `weft-reason` vocabulary, SEI standard only if Loomweave keeps SEI | Risk RSK-3: drifts like the hub did → contracts-only scope, goldens per seam, drift lane per seam. Fix dangling `~/weft/...` paths in code comments (Wardline `filigree_emit.py:248`). |
| **Lacuna** | keep as the cross-product conformance/e2e corpus for S-2/S-3/S-4 (Wardline+Filigree pair, Loomweave absent) | Stage 1/2 gate: "Lacuna end-to-end tests green with Loomweave absent"; gives first defect-volume reading (gap 5). |
| **Plainweave** | stays archived; if revived binds via S-1 `requirement` reference kind | — |

"A product is rebuilt only if a real project needs it" (D2). The memory `project_4_0_refresh_direction` ("keep the seams dormant/intact") must be updated (brief §7).

### 3(h) Dashboard (D7 + S4; owner: ship "Now" and "closed without evidence" first)

IA by question (UX §10; brief D7): **Now** (live claims, heartbeat age, attention counts) | **Inbox** (decisions) | **Activity** (actor flight recorder with evidence) | **Plan** (releases, milestones, epics, blockers, graph in context) | **Issues** (list/search/backlog) | **Findings** — under B2 the Files/health view is **removed** (ADR-F4-001 scope); any human findings inbox reads Wardline's artifacts, never a Filigree mirror; Filigree's inbox shows promoted work and evidence-state changes only (boundaries §9).

Owner-side digest (X4, S4): since-last-visit digest at the top of the dashboard and as a CLI digest: discard and bypass rate by actor and resolution; self-verified share; closes without a reachable anchor; containers open after release; expired metric targets; age of latest PDR and grant review. Designed for bursty attention; Inbox items age to `parked` (never `approved`); exception-only entries; fixed line budget (leverage §6 row 9, §9).

UX MUST list (UX §9): default landing "needs you / happening now / recently closed"; live claims from `claimed_at`/`last_heartbeat_at`/`claim_expires_at` with alive/slow/expired (replace `updated_at > 2h` stale test, `metrics.js updateStaleBadge`); close evidence visible (reason, `close_commit`, linked refs) + filter "closed without reason/commit" (J4); actor-scoped activity consuming `/api/weft/session-evidence` (J5); decision inbox (J2); data-freshness honesty (polling 15 s stated; consider SSE); remove mutations bypassing claim/evidence semantics; keep deep links, search, project switcher, federation banners. SHOULD: containers as plan navigation; release readiness verdict + released history (J6); fleet-behaviour metrics (reopen, reclaim, churn — J12) replacing flow-time metrics; hide scratch/ephemeral milestones by default; phone-width/glance mode; empty states replace the tour; fix header overlap at 1440 px. WON'T: human-assignee workflow/swimlanes/burndown; drag-and-drop; visual workflow designer; static-analysis browser; saved presets; multi-tenant; tour.

Keep/reframe/kill summary (UX §5): **Killed** — drag-and-drop (#4); Insights throughput/cycle/lead cards, By-Type table, 7/30/90 selector (#10, #11); footer sparkline (#27); Save Preset (#24); Workflow diagram modal (#32); onboarding tour (#33); health score as unexplained composite (#26, "reframe or kill"). **Reframed** — Kanban Board → secondary backlog browse (#1); Cluster → Plan rollup (#2); type-filter columns inside backlog (#5); Ready tab → Issues facet (#6); Ready/Blocked toggles link to why/on whom (#7); Graph → contextual blockers/critical path, scratch milestones filtered (#8); Releases + readiness + history (#9); Agent Workload → Now board (#12); Observation stats → inbox (#13); embedded Activity → first-class Activity (#14); Files overview → findings-needing-decision + scan freshness (#15, removed under B2); file table → drill-down (#16, removed under B2); link-issue modal → provenance view (#19); issue detail adds claim holder/age/heartbeat/expiry/commits/evidence (#20); claim/release/close modals → human override with reason (#21); + New → light "Ask/request" form (#22); batch bar → evidence-gated inbox actions, "Close All" requires reason/evidence (#23); filters strip (#24: search keep, pills reframe); stale badge → heartbeat-based (#25); footer counts → "agents alive / claims / needs-you" (#27); Reload server → diagnostics area (#30). **Kept** — List mode (#3), file-detail Findings (#17, moves to Wardline under B2) and Timeline (#18), issue detail slide-over (#20), project switcher (#28), federation banners (#29), theme (#31), freshness label fixed (#34), hash routing/deep links with ALIASES shim removed (#35), `/api` + `/api/weft` surface (#36).

Caveats: personas derived not researched; live project had 0 WIP so a populated Now view was never observed; Now board needs reliable identity (D4) and a sized lease (X8) — "a dead agent looks alive for 48 h" (leverage §7.4). Stale note: `dashboard.html` is 486 lines + ~7,800 JS, not a 2,300-line single file.

### 3(i) Outcome loop / registry / kill-by dates / Stage 5 (S1; X1, X2, X4, X10; owner: "a light standing outcome registry", standing re-review)

- **X1 registry:** every MCP tool, CLI verb, HTTP route, type, pack, profile, seam, session-brief line and guide section declared once with job, owner, outcome reading + instrument, **kill-by date**; catalog/CLI/docs/OpenAPI generated from it (S7); CI fails when a kill-by date passes without a keep/kill PDR; new entries need a declared reading; `rejected` terminal so a "no" can be stored. Working: ≥1 recorded kill or "no" per quarter (today 0 ever); default-profile schema bytes flat or falling; 100% entries with current reading. Failing: kill-by dates extended en masse → escalate to X2. Prerequisite unmet: single registration source (`RENAME_MAP` old-name indirection still the identity, MCP F9; five guide copies).
- **X2 attention budget:** fixed bytes/concepts for the default profile and the ~40-line brief; one in, one out; caps the notices channel; failing indicator: "temporary" budget raises.
- **X4 release preconditions:** tagging fails (CI job or `filigree` release precondition) when a release container has non-done children; metric targets expired without a reading; no PDR since the last release; closes without reachable anchors. Working: PDR count ≥ release count; zero releases with open container children; every release carries a dated metric reading. Failing: gate overridden every release — count overrides.
- **X10 / Stage 5 "read and decide":** 4–6 weeks after the 4.0 cut, re-read the baseline on the product-use population, record ACCEPT/REJECT/UNKNOWN in a PDR, reversal trigger with an "abandoned or dependency-dead" branch (P-7, P-8); gate: a decision record per item, at least one recorded "no". Standing step for every later release; SUR runs at this stage; the standing re-review panel pairs with it.
- **X9 fleet adoption gate:** no new major until the previous major's migration is automatic and a stated share of active projects runs it; fleet version census as a Stage 2 exit criterion (`INSTALL_VERSION` 17–29; 6 legacy `.filigree/` stores, keisei/echelon at schema 8).
- **X6 paradigm test:** every proposal names the agent failure mode it contains (retry duplication, hallucinated/self-certified completion, context exhaustion, identity collision, stale prose) or the outcome it moves, else `parked`; review test "can Filigree keep this fact correct without a peer and without a code change? If not, it is a reference, not a stock."
- **Product workspace** (P-1, P-2, P-6, P-13): PDR reaffirming/superseding PDR-0002 with an ACCEPT/REJECT/UNKNOWN verdict on the Toolkit DX epic; 4.0 PRD via `/write-prd` with falsifiable criteria and a reversal trigger, written **after** the baseline reading; re-confirm the authority grant (~4 monthly cycles overdue); replace placeholder `metrics.md` targets (all expired 2026-09-30, BASELINE unset) with owner-set dated numbers; guardrails (Q8): federation-contract regressions, agent-reported defects per release, CI health (hang with no timeouts `filigree-bb3505e85c`), live-lane status not "red by design" (`filigree-2575e37a7b`).

### 3(j) Instrumentation (T1; P-2; X5; D6)

- Log error envelopes as errors; log schema/unknown-argument rejections (today they return before logging); log warnings and no-op sentinels as outcomes; add HTTP route-level and CLI call logging; use served (post-namespacing) tool names in `tool_call` logs (P-7 routed item: logs record "pre-namespacing internal names").
- Tag each registered project `suite-construction` or `product-use` (X5); compute north-star, usage and dead-end rate per population; weight keep/kill to product-use (hamlet, aurora, simic, errorworks, keisei, skillpacks and others: ~1,650 calls, ~30%). If product-use is too small to read (n=1 operator), record UNKNOWN. Owner reframes: populations are not separated as audiences; readings are taken on real (paused) projects via the pilot.
- Dead-end definition includes warnings, no-op sentinels, retries within N s, and `list_issues no_limit` fallback (Goodhart guard, leverage §6 row 6); leading indicator: warnings-to-errors ratio.
- Take the first two-week reading after T1 + T3 ship (leverage §11 step 2).
- Agent-experience eval harness as a 4.0 acceptance gate: 20-query ToolSearch golden set, scripted "cold start → claim → close" transcript, token budgets per call (LX next steps 3).

### 3(k) Product docs / tracker reconciliation (do-now #6/#9; P-4, P-6, P-9, P-10, BP-03)

- Land or revert `ecad149` (C-16 lead summaries; `filigree-afade9b4c6` pending) and `79e06d6` (GS-7 oracle; under D2 the Warpline branch is abandoned, so revert/correct). Correct `filigree-bd1abc7243` (closed at `79e06d6`), `filigree-434aa4e145`, hub `weft-87443311a0`, `weft-13f84c77c5`, `weft-f1cbd27cfb`; close `filigree-1627c6fc7a` (C-20 shipped in 3.2.0).
- 3.3.0 milestone `filigree-b21d7a9f17`: close with a verdict (ACCEPT/REJECT/UNKNOWN — Loomweave integration never dogfooded, every project `registry_backend=local`, live lane red by design); owner decision on its 21 open children (15 tasks + 6 steps `release:3.3.0`; phases A/B/C `pending`; `filigree-6fd5b4db6b` dogfood step pending) before migration design (BP §9).
- Rewrite `docs/product/current-state.md` (2026-07-07; describes a dirty checkout on a merged branch, cites a nonexistent test file, anchors closures to unmerged commits; shipped publicly in 3.2.0).
- One PDR covering 3.0–3.3 (five releases, zero PDRs; the 3.3.0 priority set, C-20 adoption, ethereal→ephemeral, schema v29, Warpline seam, ACTOR_MISMATCH removal).
- Root `ROADMAP.md`: demoted by the owner ("public ROADMAP.md and adoption metrics" demoted); still a second roadmap listing shipped work as "Immediate" and proposing vision anti-goals (P-9); retire or regenerate from `docs/product/roadmap.md` (owner gate, public artifact).
- README leads with surface counts (P-11): position on outcome.
- Broadcast board PRD-0001 (P-7): recorded keep/kill; if kept, counted collision baseline and start-by time-box.
- Record the ADR-012 reversal (P-14).

### 3(l) Housekeeping

- `CLAUDE.md` names Legis and Warpline as live tools; claims `--agent-id` (LX-11); generate the managed block from the registry rather than hand-edit (X7).
- Five prose copies → one generated source (R6/S7; LX-14, LX-15): `data/instructions.md`, `SKILL.md` + five references, MCP prompt `_WORKFLOW_TEXT_STATIC` (`mcp_server.py:719-750`, still says `.filigree/`), `workflow_guide_get` (re-sends the full 118-tool catalog, ~4.5 KB per call), `docs/agent-integration.md`. Specific drift: `team-coordination.md:3,11,48` "2.0" framing, `:182` and `agent-integration.md:87` CONFLICT shape `current_assignee` (live is `{issue_id, observed, expected}`), `:201` false hook claim, `error-codes.md:43-49` Legis text, `:67-69` `.filigree.conf`, `commands.md:113-120,136`, `observations.md` missing link/merge/actor filter. `instructions.md:4` duplicates the hook's ~700 tokens.
- Skill rewrite: lease lifecycle (stale → reclaim, heartbeat, release-mine at session end), observation link/merge, note/finding/issue decision table, no "2.0", no Legis text.
- Prose guard `tests/mcp/test_no_old_names_in_runtime_prose.py` extended to string literals equal to any retired name (LX-08).
- Wardline `doctor.py:790-831`/`:825` keys on `"ethereal"` vs Filigree 3.3 `"ephemeral"` (P-16, `wardline-88a7c08286`) — cross-product obligation list.
- Update memory `project_4_0_refresh_direction` once D2 is ruled (it is; now records "fold archived members in").
- Tailwind CDN build logs a production warning (UX §1); `.playwright-mcp/` screenshots moved into `screenshots/`; one reviewer's `uv run` recreated `.venv`.
- `docs/mcp.md` batch semantics comment contradicts behaviour (MCP Disagreement 4).
- ADR-002 still says `loom` throughout; `contracts.md` says weft "Introduced in 2.0" (HTTP F3).

---

## 4. Application-boundary seams S-1..S-9 and misallocations M-1..M-9 (boundaries §7, §3)

All seams live in `weft-contracts` (markdown + JSON Schema + golden vectors); the **owner** writes the contract, consumers vendor goldens; every seam must state the three idempotency fields (upstream delivery, consumer dedup with out-of-window behaviour, handler idempotency), the honesty envelope, and peer-down behaviour. All assume **B2** (Filigree = work state + opaque typed references; producers own facts; no aggregator).

| ID | Seam | Owner → consumer(s) | Status | Contract shape | Peer-down |
|---|---|---|---|---|---|
| **S-1** | Evidence reference (work ↔ code link) | Filigree → everyone | New (generalises ADR-029 associations, file associations, `claim_commit`/`close_commit`) | Kinds `path | symbol | finding | commit | sei | url` (extensible); `value` opaque; `producer`; `scheme`; snapshot fields with byte caps; `state ∈ {present, gone, suppressed, unknown}` + `state_at` + `state_commit`; per-type workflow policy hooks | Filigree core never resolves; resolution is optional client-side enrichment |
| **S-2** | Promote (work candidate → issue) | Filigree ← Wardline, Loomweave, agents, any producer | Rewrite (replaces `scan-results` bulk ingest, promote-by-fingerprint, `warpline_worklist_ingest`) | Payload = S-8 record + priority/labels; idempotency key `(producer, scheme, fingerprint)` returning the existing open issue; `kind=defect` only; batch envelope `succeeded`/`unchanged`/`failed` (MCP F8); size caps; project scoping | Producer fails soft, retries next scan; key prevents duplicates |
| **S-3** | Evidence state update | Producer → Filigree | New (replaces absent-fingerprint sweep and close-on-fix cascade inference, `db_files.py:1794`) | State-based (idempotent re-send); `at_commit`; `completeness` (partial scan asserts `present` only); Filigree records and surfaces "evidence gone"; auto-close only via explicit policy (`resolution=resolved_by_scan`). Open: how producer learns which refs to report (S-4 lookup or local promoted-set); fingerprint-scheme bump/`rekey` remap rule | Lost updates self-heal on next full scan; `unknown` after freshness window |
| **S-4** | Work-by-reference lookup | Filigree → Loomweave, Wardline dossier, hooks | Rewrite (replaces classic `/api/entity-associations?entity_id=`; HTTP F9) | Query by `(kind, value)` with path prefix match; bounded, cursor-paged; project-scoped, fail-closed on ambiguity; slim issue rows + reference snapshot | Callers report `unavailable` with a `weft-reason` |
| **S-5** | Change feed | Filigree → consumers | Harden (HTTP F16) | One cursor over all entity kinds incl. references; tombstones; ordering and VACUUM stability | — |
| **S-6** | Discovery, auth, actor identity | `weft-contracts` convention; every product implements | Rewrite (three token chains; HTTP F14; Wardline `doctor` mode skew) | `.weft/<member>/endpoint` + `token`; one resolution order; `/_capabilities` with contract versions; connection-bound actor (`--agent-id`; HTTP header), per-call `actor` override only; mutations with no identity refused (amended by S6: mint a session) | Missing token is a distinct reason class, never an ambiguous 401 |
| **S-7** | Result honesty + error envelope | `weft-contracts` | Rewrite (MCP F2; HTTP F4) | `{error, code, retryable, cause_kind, hint}`; MCP `isError`; 11-class `weft-reason` vocabulary on every partial/empty cross-product result | — |
| **S-8** | Finding record (producer-neutral) | `weft-contracts` (Wardline primary author) | New (three schemas today: Wardline `Finding`, Filigree `scan_findings`, Loomweave `findings`) | `kind`, severity (one scale, no mapping of telemetry to "low"), `fingerprint {scheme, value}` with stability rule (no volatile inputs), `location`, `symbol`, optional `sei`, `rule_id`, `message`, `suppression_state` | — |
| **S-9** | SEI (conditional) | Loomweave → consumers | Keep only if Loomweave's lean core keeps SEI (owner has ruled Loomweave is restored; the SEI question is still open — leverage S11 "rule on the Loomweave memo before S-9") | Existing locked standard + determinism across a from-scratch rebuild (hub gap map NOTE-2) | Consumers degrade to `path`/`symbol` references |

Sequencing (boundaries next steps): draft S-1, S-2, S-3, S-8 first (they carry B2); then S-6 and S-7 jointly with the MCP/HTTP envelope work; Stage 1 ships S-1…S-4 additively in the last 3.x minor under a new HTTP generation alongside `weft`, with deprecation `_meta` on the finding/file/annotation/scan tools; gate: every Wardline-promoted defect across the 15 fleet projects lands as an issue with a `finding` reference, idempotent on rerun; Lacuna e2e green with Loomweave absent. Keep as is: Loomweave guidance proposals via Filigree observations.

**Misallocations M-1..M-9 and what moves where:**

| ID | Misallocation | Moves to |
|---|---|---|
| M-1 | Tracker stores analyzer telemetry as defects (Wardline emits all `Kind`s, `filigree_emit.py:103-133`; INFO→`low`, NONE→`info`, `finding.py:265-271`; `scan_findings` has no `kind`; `<engine>` is a `file_records` row; fingerprint `sha256(rule_id, message)` with embedded ratio churns) | Run telemetry stays in the producer's run report; the seam carries a *work candidate* (S-2), not findings. Cross-product wire work, not a P3. |
| M-2 | Four unsynchronised "accepted" stores (Wardline files, the only one CI reads `run.py:603-626`; Filigree `status` + copied `metadata.wardline.suppression_state`, "asymmetric by design" `contracts.md:631-650`; Loomweave `status` + `filigree_issue_id`, NG-17 deferred; Legis cells); nothing flows back; `wardline rekey` orphans statuses | Acceptance is versioned with the code: "not a real problem" → producer suppression (waiver with reason + expiry, or baseline); "real, needs work" → Filigree issue. No third/fourth store. |
| M-3 | Findings stock with no outflow (~6k engine rows upserted per scan; sweep runs per *scanned path* so `<engine>` never becomes `unseen_in_latest`; `clean_stale_findings` only drains `unseen_in_latest`; triage attention destroyed) | Rule-level fix: who may hold the stock (B2). Reject Option A as making the quick fix cheaper (Shifting the Burden). |
| M-4 | Tracker maintains derived code facts: file registry (ADR-014 delegation unused in 15/15), annotations shelling to git with anchor-drift state machine (`db_annotations.py:188,373,1229`; 7 rows), file timeline (not git history) | Code knowledge (explanation, warning, gotcha) → comments/repo docs/Loomweave guidance; work knowledge (handoff, hypothesis, decision, breadcrumb) → issue comments/observations (→ note with S-1 refs, no anchor machinery). |
| M-5 | Scan orchestrator inside the tracker (`codex`/`claude` bug-hunt processes from TOML; 4 runs ever, 0 tool calls; Wardline separately has an LLM judge) | Kill in 4.0; an LLM bug hunter is a producer emitting observations or promote calls. |
| M-6 | Loomweave: hollow reach-ins (`entity_recent_change_list`/`entity_high_churn_list` → Warpline only, `shortcuts.rs:899-990`; `entity_todo_list` unsupported); Flow B joins a copy of a copy (`filigree.rs:745-781`, null qualname on bulk rows, 401 live); third finding lifecycle store; no active consumer, two silent outages | Keep the map, delete the reach-ins, do not make Loomweave a hub; deliver by push (edit-time hook). |
| M-7 | Governance and identity built for members that do not exist: Legis fail-closed trap (`governance.py` DECISION 2 `:16-24`, incl. cascade path); Tabard 19-line spec while every stdio agent shares `verified_actor` | 3.x warn-don't-wedge; 4.0 remove; connection-bound identity (S-6). |
| M-8 | Capabilities implemented more than once: git rename/commit reading (Loomweave `sei_git.rs`, Legis `git/surface.py`, Warpline `git.py`); Python entity extraction (Loomweave, Warpline); issue↔code link store (Filigree, Plainweave); actor registry (Filigree, Plainweave, Legis keys, Warpline authors); verification evidence (Legis checks DB, Warpline `verification_events`, Plainweave `verification_evidence`); LLM code triage (Filigree scanners, Wardline judge); finding schema (×3); token resolution (×3) | One home each: Loomweave for git renames (agents use `git` directly); Loomweave for extraction; Filigree evidence references; per-product connection identity + one naming convention; Filigree close-evidence references; Wardline judge; S-8 record stored only at producers; S-6 convention. |
| M-9 | Gaps nobody owns: (1) "producer asserts evidence gone at commit X" (today inferred from absence on full scans only); (2) "what work touches this file/symbol" without a peer; (3) a home for contracts (SEI standard, `weft-reason`, seam index, glossary in an archived repo); (4) agent identity at connection time | S-3; S-4; `weft-contracts`; S-6. |

Boundaries §2.8 code disposition under B2: work state ~19,400 lines (29%) keep/rebuild; findings/files/scanners ~13,200 (20%) remove → references/promote/state (~1.5–2.5k); federation glue ~6,900 (10%) keep server mode/token/generations, remove registry client/backfill/governance/Warpline consumer (~3.6k); observations ~2,700 keep; annotations ~2,200 remove; remainder ~22,300 keep.

---

## 5. Cross-repo dependencies (what Loomweave and Wardline must change for Filigree 4.0)

### Wardline

| Ask | Source | Quoted / cited |
|---|---|---|
| Emit only `kind=defect` to Filigree (flag, default on); keep telemetry in the run report | boundaries Stage 0, M-1; brief D1 Stage 0 | "Wardline: emit only `kind=defect` to Filigree (flag, default on)"; today `core/filigree_emit.py:103-133` "Emits ALL finding kinds" |
| Stabilise engine fingerprints — no volatile values in fingerprint inputs | boundaries M-1, S-8; brief D1 | "The engine fingerprint is `sha256(rule_id, message)` (`scanner/diagnostics.py:102`). For LOW-RESOLUTION the message embeds the ratio ... (`scanner/taint/propagation.py:557-563`). Every call-count change therefore mints a new row." |
| Fix severity map: no INFO→`low`, NONE→`info` for telemetry | boundaries M-1, S-8 | `core/finding.py:265-271`; "severity (one scale, no mapping to 'low' for telemetry)" |
| **Waiver expiry mandatory** + owner/approver; report waiver count and age wherever defects are reported | owner D1 ruling ("co-requisite"); leverage S3, §6 row 4, §9 | "Expiry is optional (`waivers.py:37`) and there is no owner or approver ('No governance', `:10`): a stock with no outflow once it fills. It is empty today: no verdict YAML in any of the 5 projects." "Make waiver expiry mandatory and report waiver count and age ... This is cross-product wire work: ship it with S-2/S-3, not as a deferred P3." |
| Promote-with-payload client (S-2), state-update emitter (S-3), remove bulk emit (~1–2k lines, S–M) | boundaries §5 B2 effort | "Wardline: S–M, ~1–2k. Promote-with-payload, a state-update emitter, the bulk emit removed, telemetry kept in the run report, stable engine fingerprints." Wardline already has the promote client (`core/filigree_issue.py:32-67`, promote-by-fingerprint, needs prior ingest else 404). |
| Post old→new remap or re-promote under the new scheme on `wardline rekey` | boundaries S-3 | "`core/rekey.py:835-880` ... 'there is no remap endpoint', so Filigree statuses and issue links on the old fingerprints are lost" |
| Print "N defects, M promoted"; own the "promotes per 100 defects" metric | boundaries RSK-1; leverage §6 row 4e | "Make Wardline print 'N defects, M promoted'." |
| Primary author of S-8 finding record; vendor S-1/S-2/S-3 goldens | boundaries §7 | "S-8 ... `weft-contracts` (Wardline primary author)" |
| `doctor.py:825` compares `mode == "ethereal"`; Filigree writes `"ephemeral"` | P-16 (`wardline-88a7c08286`); boundaries §2.7 | "Wardline `doctor` still keys on `'ethereal'` (`install/doctor.py:790-831`)" |
| One token resolution chain (S-6) — Wardline `filigree/config.py:11-16, 79-103` | boundaries §2.7, S-6 | "Three products each resolve the token their own way." |
| Dangling `~/weft/...` paths in comments (`filigree_emit.py:248`) | boundaries §2.7 | — |
| Human findings review happens in Wardline surfaces and PR-reviewed waiver files (RSK-4 adoption risk; Wardline summary in the PR template) | boundaries ADR consequences | "The human reviews findings in Wardline's surfaces and in PR-reviewed waiver files." |
| Taint-fact store → Loomweave retired unless a consumer exists | boundaries §7 retire list | 1,628 rows written once 2026-07-12 |
| Whether Wardline can emit `kind` reliably and call retention (moot under B2 but BP gap 4) | BP §8 gap 4 | — |

### Loomweave

| Ask | Source | Quoted / cited |
|---|---|---|
| Delete Flow B (reads Wardline findings from Filigree's mirror) | boundaries M-6, Stage 3 | `crates/loomweave-federation/src/filigree.rs:745-781`; `wardline_reconcile.rs` byte-equal qualname; "~90% of the Filigree volume can never bind" |
| Delete churn/recent-change proxies (Warpline-only, dead) | boundaries M-6, D2 Warpline fold-in | `catalogue/shortcuts.rs:899-990`; `query.rs:1303-1306` "does not populate `git_churn_count` in v1.0"; live returns `warpline-disabled` |
| Delete Filigree emit and SARIF → Filigree translation | boundaries Stage 3, §7 | `emit_findings` default false, `config.rs:1068-1096`; "Loomweave: net deletion" |
| `entity_issue_list` uses S-4 (by SEI or path) if kept; must work with Filigree absent and configured | boundaries Stage 3, S-4 | `crates/loomweave-mcp/src/tools/graph.rs:612-780` today calls classic `GET /api/entity-associations?entity_id=` and `/api/weft/issues/{id}`; live 401 |
| Token/join behaviour: resolve the federation token per S-6; the live 401 (F14) hit Loomweave's Filigree join | boundaries M-6, §2.7; HTTP F14 | `filigree.rs:401-418`; "Its Filigree findings join returned HTTP 401 in this session." |
| SEI: keep only if the lean core keeps it (S-9); determinism across from-scratch re-index (NOTE-2); memo proposes deleting "SEI signing/HMAC" and the federation HTTP API | boundaries S-9, §2.5; leverage S11 | "Not deterministic across a from-scratch re-index (hub gap map NOTE-2)." "B2 is indifferent; `sei` becomes one optional reference kind." |
| Keep guidance proposals via Filigree observations (the one seam kept unchanged) | boundaries §7 | `crates/loomweave-mcp/src/lib.rs:2373-2602` |
| Reliability: two silent multi-week outages (42 analyze runs in September, none completing); adoption 0.18–0.8% vs `rg`; no active consumer (elspeth retired 2026-09-25, aurora removed 2026-10-05) — the **owner has ruled Loomweave is restored**, overriding the memo's "do not rebuild"; its measurements define the bar | brief Context + "Context you should know"; boundaries M-6 | Memo `~/loomweave/docs/implementation/2026-10-07-loomweave-rebuild-or-recover.md` (untracked, non-normative); recommends a lean core: plugins, storage, ~6 queries, CLI, edit-time hook (memo:186-188) |
| Edit-time push hook composing Wardline defects + callers + open work on the path | boundaries §5 B2 agent UX, M-6 | "Its value reaches agents best by **push** (edit-time hook), not as yet another query server." |
| Stale index residue: duplicate `api_loom_*` rows with `sei: null` after the rename | HTTP caveats | "Loomweave's entity identity did not carry across the rename." |
| Verify Loomweave reads status *categories* not literal state names before cut-over (`deferred` → open) | BP §5.6, BP gap 5 | "Live consumers (Loomweave, Wardline) read categories, not literal states. Verify that before cut-over." |
| SUR scope covers Loomweave and cross-tool flows | owner SUR lesson 6 | — |

### Both / suite

- `weft-contracts` package: each seam owner writes its spec, consumers vendor goldens (boundaries §7); drift lane per seam; Lacuna as the conformance corpus with Loomweave absent.
- Stage 2 exit criteria: "Lacuna end-to-end tests green with Loomweave absent; Wardline promote idempotent across the 15 fleet projects" (brief §5).
- Fleet version census before the cut (X9); legacy `.filigree/` stores at schema 8 (keisei, echelon).
- Sibling drift check (`tests/federation/test_sibling_drift.py`) and per-generation shape adapters with pinned fixtures are on the "must survive" list (brief §4).

---

## 6. Leverage-analysis risk indicators (leverage §6; brief §0 "How agents resist rules")

Rule: "Agents do not argue with a rule. They satisfy it in the same write, or they route around it with another call. That is invisible unless it is measured. **Every rule ... needs a paired measurement of *how* it is being satisfied.**"

| # | Recommendation | Predicted resistance / side effect | Mitigation | Leading indicator |
|---|---|---|---|---|
| 1 | Shrink the catalog (C5) | Regrowth (R1/R2 intact); parameter inflation via mode flags/arrays; opt-in profiles and installable packs become holding pens hidden from usage logs | X1, X2; profiles/packs out of tree with owner and kill-by, or same registry entry | Schema bytes per 4.x minor; count of profile-only tools |
| 2 | Retire force-close (D3b) | (a) `discard` becomes the new escape (`obsolete`/`wont_do` to avoid evidence); (b) boilerplate evidence in the same write; (c) WIP left open (lease loop inert); (d) "unscheduled" epic becomes a dumping stock; (e) work done outside the tracker closed retrospectively (~240 closes without tracker-visible start) | X3 (a check the agent cannot type); discard rate by actor/resolution in the owner digest (X4); aging review on the "unscheduled" container; **do not use "gate-bypass = 0 by construction" as a success metric** | Discard rate by actor; share of `completed` with reachable anchor; unscheduled-container size |
| 3 | D3 completion evidence as a typed field | Ritual compliance identical to today's verify gate; passes P-4 | X3 | — |
| 4 | B2 producers own findings | (a) escape lane moves into Wardline waivers (optional expiry, no governance, empty today); (b) triage inbox of promoted bugs becomes the new stock if defect volume rises; (c) `state: unknown` references accumulate if S-3 not sent; (d) 25k-row archive export is a dead stock; (e) "promotes per 100 defects" has no owner | Mandatory waiver expiry + waiver count/age shipped with S-2/S-3; triage aging rule; archive owner + deletion date; Wardline prints "N defects, M promoted" | Waiver count and age; promotes per defect; triage age |
| 5 | Launch-bound identity (D4) | (a) all sessions/subagents/worktrees from one MCP config share one actor → claim CAS treats them as one holder; cheap half (flag) ships while sessions defer again; (b) refusing anonymous writes → arbitrary strings in the actor field | Ship identity and session together or not at all; server mints a session id per connection; actor label descriptive | Distinct sessions per actor; claim conflicts between sessions of the same actor |
| 6 | North-star instrumentation (D6) | Goodhart: errors turned into warnings or success-shaped no-ops so the dead-end rate falls | Dead-end includes warnings, no-op sentinels, retries within N s, `list_issues no_limit` fallback; read on product-use population | Ratio of warnings to errors over time |
| 7 | Remove `no_limit` (C3) before orientation is fixed | Agents page through cursors (more calls/tokens) or fall back to search | Sequence T3 and C4 before or with the bounding rule | `work_next`/`work_ready` share of orientation calls |
| 8 | `notices` channel (LX R3) | Becomes the next commons | X2 budget; three-item cap; measure ignore rate | Notices per response |
| 9 | Dashboard Inbox (D7) | Next stock with no outflow under bursty attention; human-gated items stall agents | Aging defaults (`parked`, not `approved`); exception-only; prefer machine checks (X3) to `human` gates | Inbox age; share decided per visit |
| 10 | One-off reconciliations (N9, N11) | Fixes that Fail: recur | Pair with X3, X4, X7 | Recurrence at the next review |
| 11 | Stage 0 bulk-mark `fixed` | Falsifies resolution; inflates "fixed" ~2,500-fold | `not_work` disposition or export-and-drop | — |
| 12 | "Rebuild everything" | Reproduces the 3.x inventory; fourth break in ~8 months; instruction churn in every project | Start from the kill/keep table; every 4.0 surface item passes X1 admission | Share of 3.x surface re-admitted with a reading |

Burden-shifting inside the recommendations themselves: Option A; D3d typed field; the "unscheduled" epic; optional packs and profiles. X1/X2/X4 failure modes: kill-by dates extended en masse; "temporary" budget raises; release gate overridden every time (count overrides). Leverage §9 risk table adds: X3 false-blocks on no-git/squash/cross-repo (resolve by PR or tree equivalence, explicit `repo` qualifier, fall back to `awaiting-integration`); X5 product-use population too small (record UNKNOWN; do not borrow the suite population); X6 paradigm stated without enforcement (bind to review test + X1 admission fields).

---

## 7. Open questions, disagreements and owner gates

### 7.1 Brief §6 reviewer disagreements (with resolution status after rulings)

| Topic | Positions | Resolution |
|---|---|---|
| Findings | Keep in Filigree with `signal_class` + disposition policy (BP §5.4, LX R9) vs producers own them, Filigree holds references (boundaries) | **Ruled: B2.** Stage 0 ships under both. |
| Archived members | Rebuild everything (earlier direction) vs drop Legis/Warpline/Tabard as products (boundaries) | **Ruled: fold in, revisit later.** |
| Annotations | Keep as-is (BP: no orphans) vs collapse into "note" (LX R4) vs remove the anchor-drift store (M-4: 7 rows, 1,280 lines) | Folds into D1/B2: note-with-references satisfies all three. |
| Catalog size | 76 (MCP) / 40–50 (LX) / ~52 (boundaries under B2) | Same direction; apply MCP's contract rules to whichever catalog D1 produces; **S12: replace counts with an attention budget.** |
| HTTP federation resources | Entity associations and worklist ingest as first-class weft resources (HTTP F9, F12) vs S-1 to S-4 (boundaries) | Same mechanism (successor generation); resource set follows D1 → S-1…S-4; F12 dropped by D2. |
| Dashboard Findings view | Promote to top-level triage (UX) vs remove or read Wardline artifacts (boundaries) | Follows D1 → removed; any human findings inbox reads Wardline's artifacts. |

### 7.2 Brief §7 housekeeping and caveats

- "3.0 or 4.0" (Q10) resolved: 4.0. Q1 ("both your own fleet and external users") is **superseded** by the living-tool-set framing.
- Kill evidence is MCP-only; HTTP and CLI unlogged — check before removing anything a sibling or script calls (D6; design posture 4).
- Dashboard personas derived, not researched; live project had 0 WIP.
- `project_4_0_refresh_direction` memory must be updated after D2.
- Owner gates (escalate-first): deprecating any used feature (findings ingest, the planning pack, the Files view); deleting or exporting data (~25k finding rows, the telemetry bulk-mark); doctrine changes; the breaking public release; the public `ROADMAP.md` rewrite; any change to `vision.md`.

### 7.3 Product critique's 11 must-answer questions (P §8) with current status

| # | Question | Status after rulings |
|---|---|---|
| Q1 | Who is the user? Own fleet (~7 active repos) or external adopters? Is "popular" (PDR-0003 goal #1) still a goal? | **Answered by framing:** living tool set for the owner's agent fleet; external adoption is not a design driver; P-3's star/fork evidence moot. |
| Q2 | Is the Weft federation still the strategy, and which siblings are live? (vision change, escalate-first) | **Answered:** D2 fold-in; live trio = Filigree, Loomweave (restored), Wardline; seams S-1…S-9. `vision.md` "Who it serves"/anti-goals still need the recorded change (owner gate). |
| Q3 | What is the bet as a falsifiable hypothesis, and its baseline reading? | **Open.** Precondition: T1 instrumentation, then a reading, then the 4.0 PRD (D6; leverage: PRD criteria must be outcome readings, not build/contract gates). |
| Q4 | Does PDR-0002 hold? Verdict on the Toolkit DX epic (ACCEPT/REJECT/UNKNOWN). | **Open.** T6 "one PDR for 3.0–3.3". |
| Q5 | What does it kill? Kill/keep table with usage evidence (~52 zero-call tools; 5 never-enabled packs; annotations; planning pack; broadcast board P-7; Tabard seam P-8; `filigree-ed2ccaf10d` primitives incl. `filigree-c2009921cf`, `filigree-6549e739de`; each seam with a shelved counterparty). Each kill of a used feature escalate-first. | **Partly answered** by B2/D2/D3 and the MCP catalog table; the registry (X1) is the standing mechanism; HTTP/CLI logging required first. Broadcast board and `filigree-ed2ccaf10d` still need recorded calls. |
| Q6 | Why a major? What must break, fleet migration cost (schema-8 stores, `INSTALL_VERSION` 17–29), instruction churn? | **Partly answered:** status-name change, identity binding, catalog consolidation, envelope are wire breaks (D3, D4, D5). X9 adoption gate and X12 cost still to be named in the PRD. |
| Q7 | What is the reversal trigger? Metric-bound, time-boxed from bet start, with an "abandoned / dependency-dead" branch. | **Open**; X10 Stage 5 supplies the shape. |
| Q8 | Which guardrails must not degrade? (federation-contract regressions, agent-reported defects per release, CI health — hang with no timeouts `filigree-bb3505e85c`, live-lane status not red by design `filigree-2575e37a7b`) | **Open.** |
| Q9 | Are the reconciliation preconditions done? (`ecad149`, `79e06d6`, both trackers, 3.3.0 milestone verdict, `current-state.md`, root `ROADMAP.md`, authority grant) | **Open** — do-now #6 (T6). |
| Q10 | 3.0 or 4.0? | **Resolved: 4.0.** |
| Q11 | MCP 2.0 / protocol 2026-07-28 in or out? (`filigree-1977c738f1`, pin `mcp>=1.0,<2`, low-level `mcp.server.Server`) | **Open.** |

### 7.4 Items each report flags as needing an owner gate or ruling

| Gate | Source |
|---|---|
| Deprecate used features: findings ingest (Wardline emits to it in 4 repos), `finding_*`/`file_*`/`annotation_*`/`scan*` tools, the Files view, the planning pack | ADR-F4-001 gate 1; brief §7; P §3 |
| Data deletion: export and drop `scan_findings`, `file_records`, `annotations`, `scan_runs` (~25k rows); Stage 0 telemetry disposition (`not_work` vs export-and-drop) | ADR-F4-001 gate 2; brief §3 #6; leverage §6 row 11 |
| Doctrine change: "finding lifecycle lives in Filigree" (hub doctrine §2/§6; Wardline `core/finding.py:4-7`) | ADR-F4-001 gate 3 |
| Any public release (breaking 4.0 cut, PyPI, tag) | ADR-F4-001 gate 4; brief §5 Stage 4; P §3 |
| Public `ROADMAP.md` rewrite/retire | P-9; brief §7 (demoted but still a public artifact) |
| `vision.md` changes ("Who it serves", anti-goals, federation premise) | P-5, P §3; brief §7 |
| Re-confirm the authority grant (~4 monthly cycles overdue) | P-13 |
| 3.3.0 milestone disposition and its 21 open children before migration design | BP §9; brief §3 #9 |
| D3 status-name change and planning-pack deprecation | brief D3 |
| D4 identity (changes every agent's setup) | brief D4 |
| D5 generation question: does the major carry a new generation? successor name? **Confirm the "no long-lived freeze" adjustment** | HTTP A1–A3; brief rulings table |
| S1 governance rule under the grant (registry fields, kill-by rule, release preconditions, Stage 5) — draft as a short PDR alongside D1 | leverage §7.2 S1, §11 step 3 |
| S5: confirm the R2 framing (owner has: builder-as-user is intended) | leverage §11 |
| X3 done definition: "shipped to the integration branch" vs "done on a branch" (per-project integration ref) | leverage §11 assumptions; owner has ruled "machine-checked against the integration branch" |
| Loomweave memo and SEI survival (S-9) before S9 is specced | boundaries gap 1; leverage S11; owner: Loomweave restored, SEI not explicitly ruled |
| F21: governed close with `LEGIS_URL` unset (silent fail-open) | HTTP D |
| R4 concept merge observation + annotation → note | LX R4 ("needs an owner call") |
| Broadcast board keep/kill (PRD-0001); if kept, collision baseline + start-by time-box | P-7; LX-10 |
| MCP 2.0 in/out | P-15 |
| Whether a coordinator override is intended for release/undo (then `override:true`, not silence) | MCP caveats |
| Owner intent for domain-agnostic packs (Feb-2026 editorial exemplar) — owner demoted optional packs to "one opinionated workflow" | BP gap 3 |
| Design-posture hypothesis: stress-test once the design is finished | owner rulings table |

### 7.5 Other open questions and information gaps the plan should carry

- Whether any project sets `LEGIS_URL` (boundaries gap 4) — decides whether M-7's trap is live anywhere.
- Whether the prose evidence in the 117 bypass closes is true (leverage gap 3) — sample the cited shas.
- Decomposition of the ~240 closes without a tracker-visible start (leverage gap 4).
- Squash-merge and cross-repo anchor semantics in the fleet (leverage gap 7) — sets X3's false-block rate.
- Defect volume on a codebase with declared trust boundaries (boundaries gap 5; leverage gap 6) — Lacuna gives the first reading.
- Route-around pilot: two-week pilot of `discard` + reachability warning in one product-use project (leverage gap 8).
- Concurrent multi-agent traces (BP gap 2) — BP-09/BP-10 severities are inferred.
- Which literal status names Loomweave/Wardline read (BP gap 5).
- Hidden rework in PR review (BP gap 6) — whether `in_review` should default on.
- Host behaviour for eager vs deferred catalogs; PreToolUse/PostToolUse injection per host (LX gaps 3, 4); no tokenizer (LX gap 1); ToolSearch ranker opaque (LX gap 2).
- `docs/plans/2026-05-17-weft-uri-spec.md` not read; sibling repos' retry behaviour not inspected (HTTP gaps).
- Rotated or off-host logs (leverage gap 9; P gap 7).
- Owner's actual review habits (bursty attention inferred from commit/PDR dates; leverage gap 5).

---

## 8. Numbers (with source)

### Usage and surface

| Figure | Value | Source |
|---|---|---|
| MCP tools served | 118 (1 resource, 1 prompt); brief's earlier "~150" was an overcount | MCP critique; P §6.2; LX |
| Tool count by tag | 43 (v1.0.0) → 53 → 58 → 71 → 109 (v2.0.0) → 114 → 118 (v3.0.0–v3.3.0); never decreased across 14 tags | leverage §2.1 |
| Python LOC | 9.6k → 66.7k (×7); 66,693 Python + 7,875 JS at 3.3.0 | leverage §2.1; boundaries §2.8 |
| CHANGELOG | 23 "Added" vs 2 "Removed" sections | leverage §2.1 |
| Logged MCP calls | 5,485 across 15 projects (brief, boundaries, leverage) / 5,452 across 14 projects, 2026-03-15 → 2026-10-07 (product, LX). Cite both. | brief §1; P §6.2 |
| Core loop share | ~95% of calls; top 8 = 81.4%; top 12 = 89.4%; top 20 = 95.0% | brief §1; P §6.2 |
| Tools ever called | 66 of 118; ~52 never called | P-3 |
| Suite-construction share | 3,835 of 5,485 calls (70%); 2,132 of ~3,129 issues (68%); all 22 suite-only tools are periphery; non-suite called `list_findings` 4 times total; product-use ≈ 1,650 calls (30%) | leverage §2.2 R2, X5 |
| Periphery share by population | 3.4% of suite calls; 4.3% of non-suite calls | leverage §7.3 |
| Code-facts code | ~30% of Python (~20k of 66.7k) for <1% of calls | boundaries §0 |
| Agent-visible tools across suite | 118 + 48 (Loomweave) + 18 (Wardline) = 184 → ~80 under B2 | boundaries §5 |
| Catalog bytes | ~91 KB (~25K tokens) eager; 21-tool loop ~23 KB (~6.3K tokens); Claude Code deferred names ~1.2K tokens; ~85.6K chars names/descriptions/schemas | LX R4, summary; MCP Disagreement 3 |
| Tiers | core 12, common 35, niche 71; core-12 covers 84.6% of this repo's 532 calls; swapping three tools → 92.5% | LX-06, summary |
| This repo's MCP log | 532 calls, 29 distinct tools, 2026-06-07 → 2026-10-07; `list_issues` 40 calls mostly `no_limit:true` | LX method, LX-04 |
| Fleet orientation route-around | `get_ready` 36, `start_next_work` 6, `list_issues` 209 (151 with `no_limit=true`) | leverage R4 |
| Fleet closes vs starts | `close_issue` 633 vs `start_work` 373 + `claim_issue` 12 + `start_next_work` 6 = 391; ~240 closes without tracker-visible start | leverage §2.4 |
| Annotations | 13 tools; 7 rows fleet-wide (all in Filigree's repo); 1 logged call; `db_annotations.py` 1,280 lines | boundaries §2.4; P-3 |
| Entity associations | 10 rows fleet-wide; 1 MCP call | boundaries §2.5 |
| Observations | 221 calls (4.0%); `observe` 122 of 5,452 (2.2%); 28 live fleet-wide; 77 dispositioned: 27 TTL-expired (35%), 16 promoted (21%) | boundaries §2.1; P-7; BP-13 |
| Scanner runner | 4 runs ever (May 2026); 0 `scan_*`/`scanner_*` calls | boundaries M-5 |
| `actor` parameter | on 61 of 118 tools (MCP F10) / 62 tools, ~4.9 KB, ~1.4K tokens (LX-11) | — |
| Annotations census | 69 tools no annotations; 47 `readOnlyHint` only; 2 `destructiveHint`; 0 `idempotentHint`; 0 `outputSchema`; 0 `additionalProperties:false`; 88 of 469 params undocumented | MCP F11, F17, F21; LX-16 |
| Payload sizes | `issue_list` default page 217,109 B (synthetic) / 115,903 B (~32K tokens, live 50 rows) / 90,875 B (~25K tokens, 49 open); `no_limit` 290,486 B for 68; `issue_search` 7,200 B / 144-B rows; `work_ready` 10,981 B for 65 rows; `/api/weft/ready` 68 KB `has_more:false`; `finding_list` unfiltered 136,923 B (~38K tokens); `change_list` 54,842 B for 98 events; `workflow_guide_get` 6,895 B (~4.5 KB catalog); `plan_create` 10,527 B; `comment_add` echo ~1.6 KB; SessionStart ~700 tokens; managed block 17 lines | MCP F6, F7; LX-01, LX-04, LX-15; HTTP F5 |
| Pagination regimes | 4 (cap-50 + `no_limit`; default 100/max 10000; limit no max; no limit param) | MCP F7 |

### Process and closure

| Figure | Value | Source |
|---|---|---|
| Packs / types / states | 9 packs, 24 types, 115 states, 127 forward, 324 reverse transitions, 13 hard gates | BP §1 |
| Types never instantiated | 13 of 24; 4 core types = 93.2% of 3,140 issues across 14 trackers; 5 of 9 packs enabled nowhere | BP-05 |
| Labels doing pack work | `tech-debt` 284; `risk:*` 331; `release:*`/`cluster:*` 129 | BP-05 |
| This tracker | 1,188 issues (1,187 created; 6 by an identifiable human); 361 scaffolding (30%): 260 "[Bug tree]" bugs, 6 scratch milestones, mcp-review items; 45 of 76 planning-pack rows (59%) scratch | BP §2, BP-06; leverage R2 |
| Bug bypass | real bugs 154 of 491 fixed-closes (31%) bypass `verifying→closed` (`triage→closed` 132, `fixing→closed` 14, `confirmed→closed` 8); fleet 310 of 1,157 (27%); 142 of 574 closed bugs lack `fix_verification`; by month 76% (Apr) → 17% (May) | BP-01; leverage §0 |
| Bypass evidence | 117 of 154 cite a sha or test in prose; 22 none | leverage §0 |
| Ritual gates | verify dwell <1 min 405/426 (<5 s in 98); May 364/386; verifier = fixer 425/436; `verifying→fixing` 1 dogfood / 3 fleet; feature self-approval 17/25, approval→build <60 s 21/25; AC on 8/32; 728 soft-gate warnings; `severity` set on 304/574 | BP-02 |
| Epic closes forced | 22 of 24 | BP-15 |
| Containers | 3.3.0 milestone `planning` 35 days after release, 13 closed / 21 open children; `milestone planning→completed` 5/7; `phase pending→completed` 10/14 | BP-03 |
| Commit anchors | `close_commit` on 17 of ~1,139 closes (1.5%): 15 reachable from `origin/main`, `79e06d6` not, 1 cross-repo | BP-11; leverage §0 |
| Claims | lease 48 h; claim→done <10 min in 355/580, <1 h in 525/580; DB cut: 1,488 claims, 21 heartbeats (1.4%), 4 reclaims, 73 releases (BP-09); log cut: heartbeat 3, reclaim 1, `release_my_claims` 0 (leverage). Different sources — cite both. | BP-09; leverage §2.1 |
| Identity | 42 distinct claimant names (BP-10) / 54 distinct event actors, 18 `claude*` variants (LX-11); 966 of 2,784 status changes (35%) by `cli`/`mcp`; May 2026: 1,927 of 5,674 events (34%) anonymous; logged window 257/257 MCP mutations carried identity | BP-10; LX-11 |
| Throughput pollution | `deferred` counts as done; raw `not_a_bug` 23% vs real 1.4% (7/494); 21 of 228 closed tasks + 11 bugs are discards stored as `closed` | BP-06, BP-07 |
| Archive | 62 archived rows (36 closed, 2 not_a_bug, 15 completed, 8 skipped, 1 cancelled) now indistinguishable | BP-08 |
| Ready queue | 43 ready, 12 unstartable (28%); 15 rendered, 8 unstartable (53%), 3 of top 4; `Future` seed in 13 trackers | LX-02; BP-16 |
| Lead vs cycle (90 d) | avg lead 382 h vs avg cycle 0.5 h; 90-day throughput 15 | leverage §2.3; P-11 |

### Findings and federation

| Figure | Value | Source |
|---|---|---|
| Findings in this tracker | 9,973 total; 9,955 Wardline telemetry; 9,800 open + 158 unseen + 11 false_positive + 4 fixed; "9,956 not yet bridged" (banner) / 9,958 (BP/dashboard); 9,793 of 9,958 from two rules (8,850 or 8,693 `WLN-L3-LOW-RESOLUTION`, 1,100 `WLN-ENGINE-UNKNOWN-IMPORT`); 3 linked to an issue; `PY-WL-101` 2 (both fixed); 16 scratch | brief §1; BP-04; boundaries M-1; LX-01 |
| Fleet findings | ~25k ingested; four largest stores ~25.4k: 4 fixed, 21 false_positive, 8 linked | P-3 |
| Engine rows | ~6k upserted per scan; 6,144 `<engine>` rows `last_seen_at` 2026-09-02; ~2,700 from June can never sweep | boundaries M-3 |
| Wardline self-scan | 6,318 findings, zero policy defects | boundaries §2.2 |
| Wardline waivers | 0 verdict files in all 5 projects with `.weft/wardline/` | leverage §2.1 |
| Loomweave | 11,995 entities in Filigree's index; 4 findings rows in Filigree; 144 in its own repo (all open); 1,628 taint-fact rows written once; adoption 0.18–0.8% vs `rg`; 42 September analyze runs, none completing | boundaries §2.2–2.5, M-6 |
| Scan freshness on dashboard | 141 days old, no warning; `<engine>` top hotspot 8,850; health badge 97 | UX §1 |
| Dashboard | 486-line HTML + ~7,800 JS; 6 views; Kanban Open 49 / In Progress 0 / Done 0; 1,139 done vs 49 open; 759 of 1,188 bugs; 172 `not_a_bug`, 833 `closed`; polling 15 s | UX §1 |
| HTTP | 17 `POST /api/p/{key}/weft/…` lines in daemon `server.log`; 29 pinned weft fixtures; `codex/gs7-warpline-worklist` ~+1.4k/−3.4k vs main | P gap 2; HTTP strengths, F17 |
| Product | 5 releases since last PDR (all 4 PDRs dated 2026-06-16); workspace untouched since 2026-07-07; grant ~4 cycles overdue; ~36 of 63 non-merge commits federation vs ~9 agent-facing since v3.0.0; repo 1 star, 0 forks, 0 issues; 960 of 1,371 commits Claude co-authored (70%); commits/month Feb–Sep 386/210/149/324/245/9/3/45 | P-1, P-6, P-13, Q1; leverage §2.1, R2 |
| Fleet skew | `INSTALL_VERSION` 17–29; 6 legacy `.filigree/` dirs (keisei, echelon at schema 8); three majors in ~5 months; 7 projects active since 2026-09-01 | P-12; leverage §2.3 |
| Hub | last update 2026-08-07, 62 open; archived 2026-10-01 with Legis, Warpline, Tabard, Plainweave, Lacuna | P-5 |

### Discrepancies to resolve before quoting
- Call total 5,485 (15 projects) vs 5,452 (14 projects).
- `actor` on 61 vs 62 tools.
- Unbridged findings 9,955 / 9,956 / 9,958; LOW-RESOLUTION 8,850 (boundaries, by `<engine>`) vs 8,693 (BP, by status) vs 8,852 (dashboard severity bar).
- Lease machinery DB cut (BP-09) vs log cut (leverage).
- Distinct actors 42 (claimants, BP-10) vs 54 (events, LX-11).
- Brief §1 "27–31%" bypass is an all-time aggregate; monthly 76% → 17%.
- Boundaries migration Stage 0 says mark telemetry `fixed`; brief/leverage say `not_work`. Brief wins.
