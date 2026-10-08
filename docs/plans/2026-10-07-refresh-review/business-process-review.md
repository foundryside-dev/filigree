# Filigree 4.0 refresh: workflows as business processes

**Panel lane:** Business-process specialist (workflow soundness, ceremony vs value, planning hierarchy, cross-object lifecycles, multi-actor process, process metrics)
**Date:** 2026-10-07
**Reviewer identity:** `claude-filigree`
**Repo state reviewed:** `release/3.3.0` @ `59053a3` (filigree 3.3.0, clean tree)
**Method:** I read the pack definitions and engine. I queried 14 local Filigree trackers read-only (`sqlite3 -readonly`), including this repo's tracker. I exercised the state machines in a throwaway project (`scratchpad/bpm-probe`, all 9 packs enabled), statically analysed all 24 state machines, and checked prior art in the tracker, the ADRs and the design docs.
**Owner direction folded in:** The next major is **4.0**, agent-first, for the owner's fleet and for external agent users. Weft hub, Legis, Warpline, Lacuna, Tabard and Plainweave are archived; only Loomweave and Wardline are live. The federation *seams* stay (governed close, reverify worklist), but their process specs must be completed. Major rewrites are allowed, and existing types and states are not to be kept just to save migration work.

---

## 1. Executive summary

Filigree ships **9 packs / 24 types / 115 states / 127 forward transitions / 324 reverse ("escape") transitions / 13 hard gates**. As drawn, the state machines are mostly *sound on paper*: no state is unreachable and every non-done state has a forward exit. As *business processes*, though, they mostly do not run. Four findings dominate:

1. **The escape lane is the normal lane, and it falsifies "done".** For real (non-scaffolding) bugs in this tracker, **154 of 491 "fixed" closes (31%)** skipped the `verifying → closed` hard gate through `force` / reverse edges. Across the fleet it is **310 of 1,157 (27%)**. Force-close defaults to the *first* done state (`closed` = fixed), so duplicates, won't-fixes and scratch cleanups are recorded as *fixed*. Every hard gate that guards a done state in any pack can be bypassed this way. This contradicts ADR-005's own contract ("workflow templates remain real contracts for normal work").
2. **No gate produces independent evidence.** `verifying` dwell time is under 1 minute in **405 of 426** cases, and verifier = fixer in **425 of 436**. The `verifying → fixing` rework loop was used **3 times fleet-wide**. Features are self-approved: same actor approves and builds in **17 of 25**, and approval → build takes under 60 s in **21 of 25**. **728** soft-gate warnings were recorded and ignored. Each gate is satisfied by the gated party in the same write.
3. **Planning containers do not roll up, couple or sequence.** The 3.3.0 milestone is still `planning`, with 3 `pending` phases and 21 open children, **35 days after v3.3.0 shipped**. In the probe, a milestone was completed with 0/2 steps done, a step under a blocked phase was `ready` and startable, and children of a completed milestone stayed `ready` and on the critical path. The Feb-2026 design addressed exactly this with "link-aware transition gates" and its review warned "layers become bookkeeping". The fix was never built or tracked.
4. **The findings inbox cannot drain by construction.** The "9,956 not yet bridged" figure is 9,800 `open` plus 158 `unseen_in_latest`. **9,793** of them come from two Wardline *telemetry* rule ids, and only **3 findings have ever been linked to an issue**. The ADR-015 retention sweep has no driver: 158 unseen findings older than 30 days have never moved to `fixed`, and only 4 findings are `fixed` ever. This is not a triage backlog. It is a category error: telemetry is stored as triage inventory.

**Ceremony vs value:** Across 14 trackers (~3,140 issues), **4 core types are 93% of all issues**. **13 of 24 types have never been instantiated anywhere.** Requirements/acceptance-criterion were used in one project and never moved past their initial state. 14 of the 15 `release` rows are the auto-seeded `Future` singleton, which has zero children everywhere. A sibling reviewer independently found **5 of 9 packs enabled nowhere**. Labels already do the work those packs were meant to do: `tech-debt` ×284, `risk:*` ×331, `release:*`/`cluster:*-release-prep` ×129. In this tracker, **30% of issues are scaffolding or scratch**: 260 "[Bug tree]" inspection nodes modelled as bugs, plus MCP-review fixtures that make up 59% of all planning-pack rows.

**Target for 4.0: 24 types → 6.** Three leaf kinds (`task`, `bug`, `feature`) share **one 6-state lifecycle with a `resolution` enum**. Three containers (`epic`, `milestone`, `release`) share **one roll-up lifecycle**, with `release` adding freeze/ship/rollback states. Packs go from 9 built-ins to 1 built-in plus optional installable packs. The 324 reverse edges become **two universal verbs (reopen, release)**. **`force`-close is retired**, because negative exits become first-class forward transitions that can never produce `resolution=completed`. Section 5 has the full model, the lifecycles, the federation-seam specs and migration notes.

---

## 2. Evidence base

| Source | What I used |
|---|---|
| `src/filigree/templates_data.py` | All 9 packs and 24 types; reverse-edge table `:1714-2105`; seeding of reverse edges `:2123-2127` |
| `src/filigree/templates.py` | `container` gate `:245`; release target `:926-960`; backward mode skips `required_at` `:1049-1053` |
| `src/filigree/db_issues.py` | `close_issue` / `force` `:1184-1268`; claim CAS `:1521-1590`; lease constant `:68`; stale claims `:1953-2014` |
| `src/filigree/db_planning.py` | `get_ready` `:350-384`; `get_blocked` `:400-439`; critical path `:441-517`; plan tree `:622-659` |
| `src/filigree/db_files.py` | Finding→issue cascade close (force) `:2195-2243`; `clean_stale_findings` `:2317-2341` |
| `src/filigree/db_events.py` | `archive_closed` `:579-631`; `compact_events` `:634-660` |
| `src/filigree/analytics.py` | `cycle_time` `:33`; `get_flow_metrics` `:170-255` |
| `src/filigree/finding_issue_cascade.py`, `governance.py`, `db_meta.py:101-131`, `db_observations.py:38`, `warpline_consumer.py` | Governed close, reconciliation debt, observation TTL, reverify worklist |
| ADR-005, -010, -011, -013, -015; design docs 2026-02-25 (extensibility, transcript, annex), 2026-05-17 planning deprecation, 2026-06-05 governed cascade (Design A), close-on-fixed cascade | Process contracts and original intent |
| Live tracker `.weft/filigree/filigree.db` (read-only) | 1,188 issues, events, findings, observations, annotations, labels |
| 13 sibling trackers under `/home/john/*` (read-only) | Fleet usage of types, packs and transitions |
| Probe project (all packs enabled) | Force-close, release revert, phase-dependency propagation, container close, archive/reopen, risk escalation, metrics |

**Normalisation.** The dogfood tracker is dominated by review campaigns. I separate **real work** from **scaffolding**: 260 "[Bug tree]" inspection-node bugs, 6 scratch milestones with their phases and steps, and `mcp-review-scratch`-labelled items. Raw counts: 1,188 issues. Excluded: 361 (30%). Real: 827.

| Type | Raw | Scaffolding/scratch | Real |
|---|---|---|---|
| bug | 759 | 265 | 494 |
| task | 287 | 31 | 256 |
| feature | 32 | 0 | 32 |
| step | 49 | 27 | 22 |
| epic | 33 | 20 | 13 |
| phase | 19 | 12 | 7 |
| milestone | 8 | 6 | 2 |
| release | 1 | 0 | 1 (the `Future` seed, cancelled) |

---

## 3. Per-pack / per-type assessment

Legend for **Soundness**: S = sound as drawn; G = gate is bypassable or ceremonial; L = limbo or leak state; X = no forward negative exit (only force).
Fleet counts come from 14 trackers.

| Pack | Type | States / fwd / rev / hard | Fleet usage (rows; trackers) | Dogfood real-usage evidence | Soundness | Verdict |
|---|---|---|---|---|---|---|
| core | task | 3/3/5/0 | 1,141; 11 | 256 real; `open→closed` skipping wip 65/264 (25%) | S; no negative terminal (dupes/wont-do hidden in `closed`); no parked state (`PARKED:` title prefix) | **KEEP → universal leaf lifecycle** |
| core | bug | 7/9/26/1 | 1,523; 10 | 494 real; 31% of fixed-closes bypass verify gate; verify dwell <1 min 95%; rework 0.3% fleet | G (`verifying→closed` hard gate bypassed); X from `fixing`/`verifying`; release from `verifying` → `triage` | **REFRAME** (onto universal lifecycle; verification becomes close evidence) |
| core | feature | 6/8/18/0 | 205; 7 | Self-approval 17/25; AC set on 8/32; `reviewing→building` 0; open features avg 132 d old | G (approval ceremonial); `deferred` is **done**-category (pollutes throughput, hides backlog) | **REFRAME** (onto universal lifecycle; `proposed`=triage, `deferred`=parked) |
| core | epic | 3/2/5/0 | 57; 6 | 22 of 24 `open→closed` were `transition_forced`; 20/33 are Bug-tree folders | L/X: no forward `open→closed`, but containers are not startable, so the normal close needs force | **REFRAME** (container lifecycle with roll-up) |
| planning | milestone | 5/5/14/0 | 16; 4 | 2 real (6/8 scratch); 3.3.0 stuck in `planning` after ship; `planning→completed` 5/7 skip `active` | L: no roll-up; completable with open children | **KEEP as goal container** (exit criteria, roll-up) |
| planning | phase | 4/4/9/0 | 34; 3 | 7 real; `pending→completed` 10/14 skip `active`; phase deps don't propagate to steps | L: sequencing is decorative | **MERGE → epic** (ordered child container) |
| planning | step | 4/4/9/0 | 121; 3 | 22 real (hamlet 55, loomweave 17) | S, but duplicates task | **MERGE → task** (alias during migration) |
| planning | work_package | 5/6/13/0 | 9; 1 (loomweave, used as workstream) | 0 | S; overlaps epic | **MERGE → epic** |
| planning | deliverable | 5/5/14/0 | 0; 0 | 0 | S | **RETIRE** |
| requirements | requirement | 7/10/26/1 | 11; 1 (errorworks; all `drafted`) | 0 | G (`verified` gate bypassable) | **RETIRE** from built-ins (optional pack; or map to feature + AC field) |
| requirements | acceptance_criterion | 2/1/2/0 | 8; 1 (all `draft`) | 0 | S | **RETIRE** (checklist field on parent) |
| risk | risk | 8/10/32/3 | 0 | 0 (`risk:*` labels ×331 used instead) | G (`accepted` gates bypassable); L: `escalated` is open-category, so it sits in the agent ready queue; X from `mitigating` | **RETIRE** from built-ins (optional pack) |
| risk | mitigation | 5/6/13/0 | 0 | 0 | S (good rework loop via `ineffective`) | **RETIRE** |
| roadmap | theme / objective / key_result | 4/4/9/0 · 4/4/9/0 · 4/3/9/2 | 0 | 0 | G (KR met/missed gates bypassable) | **RETIRE** (product strategy is not tracker state) |
| incident | incident | 6/6/14/2 | 0 | 0 | G (`closed` gate bypassable); X: no false-alarm/duplicate exit; 4 wip states | **RETIRE** from built-ins (optional pack) |
| incident | postmortem | 3/3/5/1 | 0 | 0 | G | **RETIRE** |
| debt | debt_item / remediation | 6/7/17/1 · 4/4/9/0 | 0 | 0 (`tech-debt` label ×284 instead) | S | **RETIRE** (label or kind) |
| spike | spike | 5/5/14/1 | 0 | 0 | L: `concluded` is wip with no abandon exit | **RETIRE** → `task` with `kind=spike` + findings required at done |
| spike | finding | 2/1/2/0 | 0 | 0 | S; **name collides with scan findings** | **RETIRE** |
| release | release | 9/13/41/1 | 15; 14 (14 = auto-seeded `Future`, 0 children) | 1 real fleet-wide (loomweave v1.0.0, walked the full lifecycle) | S, but heavy (5 wip states); `Future` seed is ghost inventory | **KEEP → slimmed container** (no auto-seed) |
| release | release_item | 4/4/9/0 | 0 | 0 | S | **RETIRE** (membership via parent/label) |

**States never entered (dogfood):**
- `release` left `planning` only to `cancelled` (7 of 9 states never entered).
- `milestone.closing` was entered once and `phase.active` 4 times.
- `bug.verifying→fixing` (rework) was taken once, and `feature.reviewing→building` never.
- `work_package`, `deliverable` and every extended-pack type have zero rows.

Fleet-wide, the only full walk of the release lifecycle is loomweave v1.0.0.

**Pack verdicts:** `core` keep and reframe. `planning` merge (milestone survives; phase/work_package → epic; step → task; deliverable retired). `release` reframe. `requirements`, `risk`, `roadmap`, `incident`, `debt` and `spike` retire from built-ins to optional installable packs. This preserves the domain-agnostic extensibility goal of the Feb-2026 design without making every project carry them.

---

## 4. Findings

Severity is graded by **process blast radius**:
- **High:** a whole lifecycle fails to deliver its promised outcome, or the system of record for "done" is wrong.
- **Medium:** a lifecycle leaks or leaves things in limbo for a subset of work, or measurement is unreliable.
- **Low:** friction or drift.

Tags:
- **NEW:** no tracker issue covers it.
- **STILL-OPEN:** an open issue covers it.
- **REGRESSED:** a closed fix no longer holds.

### BP-01: The escape lane is the normal close lane; hard gates are bypassable and resolutions are falsified

**Tag:** NEW (contradicts ADR-005) · **Severity:** High · **Confidence:** High

**Evidence:**
- **Real bugs:** closes into `closed` = 337 via `verifying`, plus 154 bypassing it (`triage→closed` 132, `fixing→closed` 14, `confirmed→closed` 8). That is a 31% bypass rate.
- **Fleet:** 847 via verifying vs 310 bypassing (27%). In errorworks and keisei the bypass is the majority (79 vs 42; 38 vs 9).
- **Missing verification:** 142 of 574 `closed` bugs have no `fix_verification`, despite the "HARD" gate (`templates_data.py:78`).
- **Mechanism:**
  - `close_issue(force=True)` switches to `TransitionMode.BACKWARD` (`db_issues.py:1260`).
  - Backward edges "do not inherit target-state `required_at` gates" (`templates.py:1049-1053`).
  - Every pack seeds reverse edges from *every* non-done state into *every* done state (`templates_data.py:1714-2105`).
  - Static analysis shows that every hard gate into a done state is bypassable: `bug.closed`, `requirement.verified`, `risk.accepted`, `key_result.met`/`missed`, `incident.closed`, `postmortem.published`.
- **Resolution falsified:** `force` with no status picks the *first* done state, which is `closed`, meaning *fixed*. Sampled close reasons on forced `triage→closed` include "Duplicate of …", "wontfix — existing … mitigates", "scratch" and "mcp-review-e cleanup", all recorded as *fixed*.
- **Cascade uses the same lane:** the finding→issue cascade closes with `force=True` and reason "linked scan finding resolved" (`db_files.py:2232-2242`).
- **Probe:** `filigree close <triage bug>` is rejected; `filigree close --force` lands it in `closed` with no `fix_verification`.

**Contract re-derived:** ADR-005 says normal work uses the workflow lane, and the cleanup lane is for housekeeping "with clear scoping". Force on a single real fix with "Fixed: …" as the reason is normal work in the cleanup lane, so the contract is violated in practice. The design comment ("preserves historical cleanup semantics") explains the mechanism but does not justify the outcome.

**Recommendation (4.0):**
- Make every negative exit a **forward** transition from every non-done state, requiring `resolution ∈ {duplicate, wont_do, not_a_bug, obsolete, cannot_reproduce}` plus a reason (`duplicate_of` for duplicates).
- `resolution=completed` is reachable **only** through the forward completion edge and its evidence gate.
- Retire `force`-close entirely.
- The cascade closes with its own resolution (`resolved_by_scan`), so it never counts as *completed*.

**Risk if followed:** Medium. Agents lose a one-flag escape. Mitigate with a `discard` verb that takes a resolution.

### BP-02: No gate produces independent evidence; review, verify and approve states are ritual

**Tag:** NEW · **Severity:** High · **Confidence:** High

**Evidence:**
- **Bugs:**
  - `verifying→closed` dwell is under 5 s in 98 cases and under 1 min in 405 of 426.
  - The same actor enters `verifying` and closes in 425 of 436.
  - `fixing→closed` takes under 1 min for 26% of bugs and under 10 min for 69%.
  - `verifying→fixing` rework: 1 in dogfood, 3 fleet-wide (0.3%). `reviewing→building`: 0 in dogfood, 1 fleet-wide.
- **Features:**
  - Same actor runs `proposed→approved` and `approved→building` in 17 of 25.
  - Approval → build takes under 60 s in 21 of 25.
  - Only 8 of 32 have `acceptance_criteria`, which is `required_at: approved` (`templates_data.py:142`) but soft.
- **Soft fields:** 728 `transition_warning` events for soft `required_at` fields (fixing 322, confirmed 228, verifying 161, approved 17). `severity` is set on only 304 of 574 closed bugs, although the guide says it "drives priority ordering".
- **Missing rework edges:** forced edges reveal rework loops agents needed but that are not declared: `building→approved` ×3 and `fixing→confirmed` ×2.
- **Tooling acknowledges the ceremony:** `start_work(advance=True)` auto-walks `triage→confirmed→fixing` (`templates.py:256-323`).

**Contract re-derived:** A review or verify state has process value only if it separates duties (a different actor, or an external check) or holds work while evidence is produced. Neither happens here. Each gate is satisfiable by the gated party in the same write. The loop is self-reinforcing: gate → same-call compliance → gate data distrusted → more gates added.

**Recommendation:**
- Collapse `confirmed/fixing/verifying` and `approved/building/reviewing` into `open → in_progress`.
- Move verification into **completion evidence** on the done edge: verification text plus a commit anchor, hard-gated.
- Keep **one** optional `in_review` state, entered only when a *policy* requires separation of duties (reviewer ≠ implementer, or an external gate). That is the governed-close seam (BP-14).

**Risk:** Low. Real rework that happens outside the tracker becomes visible only if `in_review` is policy-enabled.

### BP-03: Planning containers neither roll up, couple nor sequence; "done" does not propagate

**Tag:** NEW (unremediated Feb-2026 design-review finding; no tracker issue) · **Severity:** High · **Confidence:** High

**Evidence:**
- **Live:** milestone `filigree-b21d7a9f17` ("Release 3.3.0") is `planning` with phases A/B/C `pending`. It has 13 closed and 21 open/pending children (15 tasks + 6 steps labelled `release:3.3.0`), 35 days after v3.3.0 merged. Nothing moved or descoped those children, so scope slipped silently with no trade.
- **ADR-014 milestone** `filigree-73d3293db4` was created and completed the same day: a record written after the fact, not live coordination.
- **History:** `milestone planning→completed` 5 of 7, `phase pending→completed` 10 of 14. The `active` state is skipped.
- **Probe:**
  - (a) Milestone `M1` force-completed with **0/2 steps complete** and one step `in_progress`. Afterwards its phase and step stay in `ready`, and the phases stay on the critical path.
  - (b) `Phase 2` depends on `Phase 1`, but `Step 2.1` under `Phase 2` is **ready and startable** (`get_ready` checks only direct dependencies, `db_planning.py:368-379`). This contradicts the pack guide's "Use phase dependencies to enforce ordering" (`templates_data.py:433`).
  - (c) An `epic` cannot `open→closed` forward, but a milestone can be force-closed regardless of its children.
- **Relationships are decorative:** pack `relationships` (e.g., `milestone_contains_phase`, `templates_data.py:377-406`) are parsed but never enforced.
- **Auto-unblocking is correct but shallow:** `newly_unblocked` is a derived before/after diff of `ready` on close, with no state change (`mcp_tools/issues.py:1270`). It never propagates through container ancestors, so closing `Phase 1` does not unblock the *steps* of `Phase 2` (they were never blocked in the first place, see (b)).
- **Original intent:** design §2.2 "Link-aware transition gates" (`all_in_category` etc.) and the review transcript ("No inter-layer lifecycle coupling — layers become bookkeeping without propagation rules", transcript `:113-117`). Status still "Proposed"; nothing filed.

**Recommendation:**
- Container lifecycle `planned → active → done`:
  - `active` is entered automatically when the first child enters wip.
  - `done` is **hard-gated on "no non-done children"**. Each open child must be completed, discarded with a resolution, or **moved** to another container (a recorded scope trade).
- Blockedness **inherits** down the tree: a child is blocked if any ancestor has an open blocker.
- Container progress is computed from children by resolution.
- Containers never appear in `ready`.

**Risk:** Medium. Existing open children under completed containers need a migration sweep (Section 5.6).

### BP-04: The findings inbox cannot drain; telemetry is modelled as triage inventory and retention has no driver

**Tag:** NEW (related: STILL-OPEN `filigree-56cb5c93f3` kind-filter strictness, `filigree-8f6a1599fb` status/suppression vocabulary; partly addressed by closed `filigree-4d489560e0`) · **Severity:** High · **Confidence:** High

**Evidence:**
- **Volume and source:** `scan_findings` has 9,800 `open` + 158 `unseen_in_latest` + 11 `false_positive` + 4 `fixed`. 9,797 open rows are `wardline`. 8,693 are `WLN-L3-LOW-RESOLUTION` (low) and 1,100 are `WLN-ENGINE-UNKNOWN-IMPORT` (info).
- **Never bridged:** only 3 findings ever linked to an issue.
- **Mislabelled as actionable:** the session banner says "9956 not yet bridged … (9956 actionable: 1 defect-signal, 9955 telemetry/info)", so telemetry is still called *actionable*.
- **Test debris:** scratch and test findings remain `open` (`smoke-test-rule`, `mcp-review-h-scratch`, `mcp-review-d-scratch-2`).
- **Retention never runs:** ADR-015 makes `clean_stale_findings` the retention mechanism (unseen → `fixed` after 30 d). It is reachable only via CLI or the federation route (`db_files.py:2317`, `dashboard_routes/files.py:1095`), and the producer (Wardline) does not call it. The 158 unseen rows were last seen 2026-09-02 (more than 30 days ago) and are still unseen.
- **No retention for open telemetry:** while the scanner keeps emitting it, it stays `open` for ever.

**Process diagnosis:** This is not an un-triaged backlog. It is a stock with no outflow, because the inflow (telemetry) is not work. The banner's alarm figure trains agents to ignore the inbox, which hides the 1 real defect signal.

**Recommendation:**
- Findings carry `kind`.
- A per-source **disposition policy** auto-acknowledges telemetry kinds. They never count as "unbridged/actionable".
- Retention (unseen → fixed) runs on ingest per source, not by a separate call.
- Only defect kinds at or above a severity floor enter the triage inbox.
- Track inbox age, and report bridging SLA on defect kinds only.

**Risk:** Low.

### BP-05: The type catalogue is about 4× larger than any project uses; labels already replace the extended packs

**Tag:** NEW (Feb-2026 review consensus "too many types", transcript `:113`; never acted on) · **Severity:** High (for the 4.0 scope decision) · **Confidence:** High

**Evidence:**
- **Fleet:** 14 trackers, 3,140 issues. task + bug + feature + epic = 2,926 (93.2%).
- **Never instantiated anywhere (13 types):** risk, mitigation, theme, objective, key_result, incident, postmortem, debt_item, remediation, spike, finding, deliverable, release_item.
- **Used once and abandoned:** requirement (11) and acceptance_criterion (8), all in errorworks and all still at their initial state.
- **Single project:** work_package, 9 rows, loomweave only.
- **Release:** 15 rows, of which 14 are the `Future` seed (`core.py:2452-2485`) with 0 children.
- **Packs enabled:** 5 of 9 are enabled nowhere (corroborated by the sibling reviewer); requirements is enabled once.
- **Labels doing the packs' job:** `tech-debt` 284, `risk:low/medium/high` 331, `release:3.3.0`/`release-3.0.0`/`cluster:2.1.0-release-prep` 129.
- **Agent cost:**
  - 24 state vocabularies (115 states).
  - The same name means different categories in different types (`resolved` is **wip** for `incident`, `templates_data.py:1023`, but **done** for `debt_item`, `templates_data.py:1213`).
  - The engine has needed type-aware category predicates to cope (`filigree-b55aa3191f`).

**Recommendation:** 24 → 6 types (Section 5). Ship retired packs as optional installable JSON packs, documented as examples of extensibility.

**Risk:** Medium (perceived loss of breadth). The mitigation is installable packs.

### BP-06: Type shoehorning; inspection checklists modelled as bugs, scratch fixtures in the production tracker

**Tag:** NEW · **Severity:** Medium · **Confidence:** High

**Evidence:**
- 260 "[Bug tree] …" inspection nodes are typed `bug` (22% of all issues). 165 were closed `not_a_bug`, meaning "inspected, clean", and 93 walked the full `triage→confirmed→fixing→verifying→closed` chain for an inspection.
- 20 of 33 epics are Bug-tree folder containers.
- Raw `not_a_bug` rate: 23% of bugs. Real rate: 7 of 494 (1.4%).
- 45 of 76 planning-pack rows (59%) are MCP-review scratch fixtures.

**Impact:** Bug throughput, cycle time and resolution rates are polluted, and the only "planning usage" in dogfood is mostly test data.

**Recommendation:**
- Add a `review` kind, or a checklist field on an epic, for audit and coverage work.
- Give review and smoke runs a scratch project or namespace instead of the production tracker. ADR-005's cleanup lane treats the symptom.

**Risk:** Low.

### BP-07: No resolution model; "done" conflates completion with discard, and deferral exits the backlog

**Tag:** NEW · **Severity:** Medium · **Confidence:** High (mechanism), Moderate (magnitude)

**Evidence:**
- `feature.deferred` is **done**-category (`templates_data.py:123`), so a deferred feature leaves the backlog and counts as throughput.
- task, epic and step have no negative terminal at all.
- A conservative keyword match finds 21 of 228 closed tasks and 11 closed bugs whose reason is duplicate, superseded, already satisfied or scratch, all stored as `closed`.
- `PARKED:` in a task title (`filigree-2575e37a7b`) works around the missing parked state.
- Throughput counts every done state, including `not_a_bug`, `deferred`, `cancelled` and containers (`analytics.py:194-216`). In the probe, deferring a feature and force-completing an empty milestone each added +1 to throughput.

**Recommendation:** A universal `resolution` enum on done, and a `parked` open-category state excluded from `ready`.

**Risk:** Medium (federation consumers read `status_category`; `deferred` moves from done to open).

### BP-08: Archive is a lossy one-way door

**Tag:** NEW (ADR-010 settled the inactive invariant via closed `filigree-aec52efb9b`, but not representation or reversibility) · **Severity:** Medium · **Confidence:** High

**Evidence:**
- `archive_closed` overwrites `status` with the synthetic `archived` (`db_events.py:624-629`). The resolution survives only in events, and `compact_events` deletes all but the 50 most recent per archived issue (`db_events.py:634-660`).
- Dogfood: the 62 archived rows were previously closed (36), not_a_bug (2), completed (15), skipped (8) and cancelled (1). They are now indistinguishable.
- Probe: an archived issue cannot be reopened ("Reverse transition 'archived' -> 'triage' is not declared") or transitioned at all.
- `archived` is special-cased in at least `db_workflow.py:295-334`, `analytics.py:157-201`, `db_issues.py:210-235,1409` and `db_planning.py:334`.

**Recommendation:** Make archive a visibility attribute (`archived_at`) with status and resolution preserved and reopen allowed. Remove the synthetic status.

**Risk:** Low.

### BP-09: The claim lease and handoff model does not fit agent work and is unexercised

**Tag:** NEW (the lease feature `filigree-76d27e95c2` shipped; the mismatch is untracked) · **Severity:** Medium · **Confidence:** Moderate (no concurrent-agent evidence exists; see Caveats)

**Evidence:**
- **Lease vs work duration:** default lease 48 h (`db_issues.py:68`), but claim→done takes under 10 min in 355 of 580 cases and under 1 h in 525 of 580.
- **Lease machinery unused:** fleet-wide 1,488 claims, 21 heartbeats (1.4%), 4 reclaims and 73 releases.
- **Expired claims are not released:** `get_ready` excludes any assigned issue regardless of `claim_expires_at` (`db_planning.py:374`). A crashed agent's item stays out of every queue until someone runs `stale-claims` + `reclaim`.
- **Handoff resets progress:** releasing a bug from `verifying` reverts it to `triage`, via the fallback to `initial_state` (`templates.py:954-959`; probe). The next agent then gets "not directly startable".
- **Claimed but invisible:** forward `wip→open` edges (`risk assessing→assessed`, `requirement reviewing→approved`) leave a *claimed* item in an open state. It is in neither `ready` (assigned) nor WIP.

**Recommendation:**
- Default lease about 2 h.
- Any write by the holder is an implicit heartbeat.
- Expired claims surface in `ready` as `reclaimable`.
- The release target is the last open-category state taken from history (same logic as reopen).
- No `wip→open` forward edges; the universal lifecycle has none.

**Risk:** Low.

### BP-10: Actor identity fragmentation weakens attribution and claim exclusion

**Tag:** STILL-OPEN (`filigree-c2009921cf` sessions, per ADR-011; `filigree-81d3971467` transport-bound identity) · **Severity:** Medium (High once parallel agents are common) · **Confidence:** High

**Evidence:**
- 42 distinct claimant names, including variants: `codex`/`Codex`, `claude`/`Claude`, `claude-opus-4-7`/`-4.7`/`-47`/`opus48`.
- 966 of 2,784 status changes (35%) are attributed to the transport defaults `cli`/`mcp`.
- The claim CAS succeeds when `assignee` equals the caller (`db_issues.py:1574-1580`). Two parallel sessions using the same mandated name (CLAUDE.md: `claude-filigree`) are therefore **not** mutually excluded.

**Recommendation:** Key claims to a session id (minted at session start) and keep the human-readable actor separately. Reject default actors on writes in 4.0.

**Risk:** Medium (every client must pass a session id).

### BP-11: Definition of done is not anchored to integration

**Tag:** NEW (instance STILL-OPEN `filigree-434aa4e145`) · **Severity:** Medium · **Confidence:** High

**Evidence:**
- Tasks were closed while their commits existed only on unmerged local branches: open step `filigree-1544621b0a` "…makes the closed GS-7 task (filigree-bd1abc7243) true on main", step `filigree-afade9b4c6`, and `filigree-434aa4e145`.
- `close_commit` is populated on 17 of 1,139 closes (1.5%).

**Recommendation:** `resolution=completed` requires a commit anchor. An optional policy check (local git, or a Loomweave/Wardline-aware verifier) confirms the anchor is reachable from the integration branch. Otherwise the item stays `in_review` with reason `awaiting-integration`.

**Risk:** Low–Medium (it needs a git-reachability check; keep it opt-in per project).

### BP-12: Flow metrics measure bookkeeping, not flow

**Tag:** NEW · **Severity:** Medium · **Confidence:** High

**Evidence:**
- **Means only:** `get_flow_metrics` returns averages (`analytics.py:248-255`): no percentiles, WIP count, WIP age, arrival rate, time-in-state, reopen or discard rate.
- **Containers counted:** milestone and phase "avg cycle 0.0h".
- **Discards counted as throughput** (BP-07).
- **Bookkeeping cycle time:** bug average cycle time is 0.5 h because state walks happen after the fact (26% of real-bug `fixing→closed` under 1 min).
- **Empty 30-day window:** the tracker has been idle since 2026-09-02, so this is by construction rather than broken.

**Can the events model support real flow metrics?** Yes. `created`, `claimed`, `status_changed` and `released` events are timestamped and per-issue. Two things undermine them: `compact_events` deletes history, and ritual state walks make wip timestamps record bookkeeping. **Claim → done** is a more honest cycle time today than first-wip → done.

**Recommendation:** Report per kind:
- p50/p85 claim→done cycle time;
- lead time;
- WIP count and age;
- arrival vs completed (resolution-aware);
- time in `triage`/`in_review`;
- reopen rate and discard rate;
- gate-bypass rate (should be 0 by construction in 4.0).

Exclude containers.

**Risk:** Low.

### BP-13: Observation TTL silently discards real defects; retention is inconsistent across inboxes

**Tag:** NEW · **Severity:** Medium · **Confidence:** Moderate

**Evidence:**
- 77 observations dispositioned ever: 27 TTL-expired (35%) vs 16 promoted (21%).
- The TTL is a uniform 14 days regardless of priority (`db_observations.py:38`).
- Expired summaries include real defects: "release_claim has a TOCTOU race…", "Flaky full-suite failures from PytestUnraisableExceptionWarning: a leaked sqlite connection…", "server unregister can silently no-op…", "Schema-mismatch message string now triplicated…".
- A keyword check finds later issues for some (TOCTOU, whitespace assignees) but **no** matching issue for "leaked sqlite", "server unregister" or "triplicated".
- Findings, by contrast, never expire (BP-04).

**Recommendation:**
- TTL scales with priority.
- P0–P1 observations never expire silently. At expiry they auto-promote to `triage`, or they appear in a session-start "expiring" digest.
- Write one retention policy document covering observations, findings and annotations.

**Risk:** Low.

### BP-14: The governed-close / human-approval seam has no visible queue and its debt never resolves

**Tag:** NEW · **Severity:** Medium (unexercised in dogfood) · **Confidence:** Moderate–High

**Evidence:**
- No workflow state represents "awaiting sign-off". A blocked governed close leaves the issue in its prior state, plus a `[reconciliation-debt]` *comment* (`finding_issue_cascade.py:164-176`).
- `list_reconciliation_debt` never filters out resolved debt or closed issues (`db_meta.py:101-131`), so debt only accumulates.
- The retry/sweep verb was deferred to "3.1.0 follow-up" (governed-cascade plan `:19`). The CLI is still list-only.
- "Governed" means a signed entity association exists (`governance.py:12-16`), which is data presence rather than policy.
- The probe shows an `escalated` risk ("exceeds current authority") in the agent `ready` queue, and `start-work` moves it to `mitigating`.
- Dogfood exercise: 1 `entity_association_added` event, 0 debt.

Framed per owner direction: this is a deconfliction and availability gap, not a security one.

**Recommendation:** See Section 5.4. Gate verdicts drive explicit `in_review` sub-states, debt is a resolvable record, and there is a retry sweep. Escalation-type states are excluded from agent queues.

**Risk:** Medium (the seam spec must be re-cut now that Legis is archived).

### BP-15: The epic forward path forces agents into the escape lane

**Tag:** NEW · **Severity:** Low · **Confidence:** High

**Evidence:** `epic` declares only `open→in_progress→closed` (`templates_data.py:162-165`). Containers are never startable (`templates.py:245`, commit `c1ba506`). So 22 of 24 epic `open→closed` closes were `transition_forced`, which normalises force for routine work.

**Recommendation:** Subsumed by the container lifecycle (BP-03). In 3.x, add a forward `open→closed` edge.

**Risk:** Low.

### BP-16: "Ready" is not "actionable"; ghost and container inventory sits in the queue

**Tag:** NEW (residual after closed `filigree-406e6b7ee0`) · **Severity:** Low · **Confidence:** High

**Evidence:**
- 12 of 43 `ready` items are not startable: 6 containers (1 milestone, 3 phases, 2 epics) and 6 features stuck in `proposed` ("move to 'approved' first").
- The hint renders as a pseudo-state: "move to 'complete child issues' first".
- The `Future` release seed sits in `planning` (open) in 13 trackers.

**Recommendation:** Keep containers and triage/proposed items out of `ready`, and give them their own views. Drop the auto-seed.

**Risk:** Low.

### BP-17: Docs contradict the close and enforcement contracts

**Tag:** NEW · **Severity:** Low · **Confidence:** High

**Evidence:**
- `docs/workflows.md:74` says close auto-selects the single reachable done target with a warning. The code refuses (`db_issues.py:1210-1217`), and the probe confirms: feature in `building` → "Transition 'building' -> 'done' is not allowed".
- `docs/workflows.md:649` says a task `open→closed` is "allowed but noted". No warning is emitted (probe).
- Default packs are documented as core+planning (`:100-105`). The actual default is core+planning+release (`core.py:1338`).
- `requires_packs` is still unenforced at load (planning-deprecation §3.1 remains true).

**Recommendation:** Regenerate the workflow docs from the registry in 4.0.

**Risk:** Low.

### BP-18: Planning-pack deprecation is frozen on a non-existent dependency

**Tag:** NEW · **Severity:** Low · **Confidence:** High

**Evidence:** The 2026-05-17 plan recommends Option C (keep milestone; retire phase/step/work_package/deliverable) but is blocked on Shuttle, which "has no repo". Meanwhile the planning pack is enabled by default in every new project.

**Recommendation:** Decouple the decision from Shuttle. 4.0 can do the collapse now (Section 5).

**Risk:** Low.

**Regression probe (no REGRESSED finding):** I re-tested `filigree-42045dd065` (archived blockers re-block readiness): blocker closed, archived via label, and the dependent is still `ready`. The fix holds. I also re-checked `filigree-406e6b7ee0` (ready≠startable): the `startable` flag is present. The residual is BP-16. **No regressions found.** Prior-art checks used read-only SQL title/description sweeps plus MCP `issue_search` FTS (`status_category=open`) for "force close bypass", "reverse transition gate", "children parent close rollup", "telemetry findings retention", "workflow pack types retire", "force" and "milestone". The only open hit was `filigree-56cb5c93f3` (already cited under BP-04), so the NEW tags hold (FTS-confirmed).

---

## 5. Target process model for 4.0

Design principles, derived from the evidence:
- (P1) One lifecycle vocabulary for all leaf work, because agents act on categories and verbs, not 24 dialects.
- (P2) Every outcome is a **forward** edge, so there is no escape lane.
- (P3) Gates hold only **evidence** or a **separation of duties**, never a ritual state walk.
- (P4) Containers are computed from their children and gated on them.
- (P5) Every inbox has an outflow (disposition policy, retention, or TTL with escalation).
- (P6) Federation seams are explicit states and verbs with defined failure modes.

### 5.1 Types: 24 → 6

| Role | Type | Notes |
|---|---|---|
| Leaf | `task` | Default kind. Absorbs `step`, `spike` (`kind=spike`, `findings` required at completion), `review` (audit/checklist), `debt` (label or kind) |
| Leaf | `bug` | Same lifecycle as task. Bug-specific fields: `severity` (hard at `triage→open`), `steps_to_reproduce`; completion evidence = verification + commit |
| Leaf | `feature` | Same lifecycle. `acceptance_criteria` hard at `triage→open`; `proposed` = `triage` |
| Container | `epic` | Workstream / ordered sub-container. Absorbs `phase`, `work_package` |
| Container | `milestone` | Outcome target: `exit_criteria`, `target_date` |
| Container | `release` | Shipping container with freeze/ship/rollback. No auto-seeded `Future` |

`kind` is a free sub-classification (`spike`, `review`, `chore`, `reverify`, …) with optional per-kind field schemas. Kinds do not change the lifecycle. Optional installable packs (requirements, risk, incident, roadmap, debt) remain possible for projects that want them. They are not built-ins.

### 5.2 Leaf lifecycle (task / bug / feature)

```
            accept (hard: kind triage fields)          claim (atomic)
  triage(O) ───────────────────────────────► open(O) ─────────────────► in_progress(W)
     │  ▲                                     │  ▲  ◄───── release ───────┘   │   │
     │  └──────────── untriage ───────────────┘  │                            │   │ complete
     │                                  park/unpark                           │   │ (hard: completion
     │                                     parked(O)  (never in ready)        │   │  evidence; no
     │                                                                        │   │  review policy)
     │                                    submit (policy requires review) ◄───┘   ▼
     │                                         in_review(W) ── rework ──► in_progress   done(D)
     │                                              │                                  ▲
     │                                              └── approve (gate verdict) ────────┘
     │
     └─ discard (from ANY non-done state; reason + resolution required) ─────────────► done(D)
  done ── reopen ──► last non-done state (from history)
```

- **States (6):** `triage` (open; inbound inbox, not in `ready`), `open` (open; ready when unblocked), `parked` (open; excluded from `ready`), `in_progress` (wip), `in_review` (wip; policy-driven only), `done` (done).
- **Resolution on `done`:** `completed | duplicate | wont_do | not_a_bug | obsolete | cannot_reproduce | resolved_by_scan`. Only the `complete`/`approve` edges can set `completed`. `discard` can never set it.
- **Completion evidence (hard):** `verification` (text, or a test command and its result) plus `commit` anchor. Per-kind additions: spike → `findings`; feature → `acceptance_criteria` satisfied (checklist).
- **Initial state by creation source:** items filed by automation or other actors (finding promotion, observation promotion, reverify worklist, humans) start in `triage`. Items an agent creates for its own immediate work may start in `open`.
- **Universal verbs:** `claim`, `start` (= claim + `in_progress`), `release`, `submit`, `approve`/`reject`, `complete`, `discard`, `park`/`unpark`, `reopen`. There is no `force`.

### 5.3 Container lifecycle (epic / milestone / release)

```
planned(O) ──(auto: first child enters wip)──► active(W) ──close (hard: no non-done children)──► done(D)
   └────────────── cancel (reason; open children must be moved or discarded) ──────────────────► done(D)
release only:  active ──freeze──► frozen(W) ──ship──► done(D, resolution=released)
               frozen ──unfreeze──► active ;  done(released) ──rollback──► rolled_back(W) ──► active | done(cancelled)
```

- **Resolution:** `achieved | cancelled | superseded`; release also `released`.
- **Roll-up:**
  - Progress = children grouped by resolution.
  - The `close` gate fails while any child is non-done. The error lists the children and offers `move-to <container>` or `discard`, which are recorded scope trades.
- **Sequencing:** blockedness inherits down the tree. A leaf is blocked if it, or **any ancestor**, has an open blocker. Epic `sequence` orders siblings within a milestone.
- **Queues:** containers never appear in `ready` or `start_next`.

### 5.4 Cross-object lifecycles and federation seams (re-specified)

**Finding → bug (Wardline, live)**
- **Finding states:** `open`, `acknowledged`, `fixed`, `false_positive`, `unseen`. Add a per-source **disposition policy**:
  - telemetry kinds → auto-`acknowledged`, never counted as unbridged;
  - defect kinds ≥ severity floor → triage inbox.
- **Retention:** `unseen` older than N days → `fixed`, executed **inside ingest** for that source. This is the ADR-015 semantics with an actual driver.
- **Promotion** creates a `bug` in `triage` with the finding linked.
- **Cascade close** (all linked findings resolved) → `done` with `resolution=resolved_by_scan`, passing through the close gate (below). This is distinguishable from `completed` and can be audited later.
- **Regression reopen** works as today. The archive attribute does not block it (BP-08).

**Observation → issue**
- TTL scales with priority (P0–P1: no silent expiry; they promote to `triage` or appear in a digest).
- Link dispositions (`evidence`/`duplicate`/`superseded`/`related`) stay as they are; they are sound.

**Annotation:** Keep as-is (create / supersede / resolve / promote / carry-forward). The data shows no orphans (7 created, all resolved or superseded). The close-out warning on issue close is advisory, which is appropriate.

**Claim:**
- Claims are keyed to a **session id**; the actor is attribution only.
- Default lease about 2 h, with an implicit heartbeat on holder writes.
- An expired claim → item listed in `ready` as `reclaimable` (atomic `reclaim` CAS, as today).
- `release` returns the item to the last open state from history.

**Governed close (seam kept; provider-agnostic now that Legis is archived)**
- **Policy declares** which (type, kind, resolution) closes need a gate, and what kind: `separation_of_duties` (approver ≠ implementer), `human`, or `external:<provider>`. This replaces "governed = a signed association exists".
- **Flow:** a gated item goes `submit` → `in_review` with `gate_status ∈ {pending, approved, denied, stale, unavailable}`.
  - `approved` → `done(completed)`.
  - `denied` → `in_progress` with the reason recorded.
  - `stale` (bound content drifted, judged via Loomweave SEI freshness) → `in_review/stale` until re-bound.
  - `unavailable` → stays `pending` and is retried by a **sweep verb** with backoff. After a configured timeout it escalates to `human`.
- **Visibility:** the `in_review` queue (filterable by `gate_status`, `required_approver`) *is* the human-approval queue. Agents cannot claim `human`-gated items. `escalated`-type states are excluded from agent queues.
- **Reconciliation debt:** becomes a record with `open/resolved` status. It resolves automatically when the item reaches `done` or the gate passes, and the list shows only open debt.

**Reverify worklist (seam kept; producer archived, contract retained)**
- **Ingest** files `task kind=reverify` in `triage` (today: plain `task`, `warpline_consumer.py:215-217`), carrying an SEI association and the triggering change anchor.
- **Completion resolutions:** `completed` (with evidence `reverified_unaffected | reverified_fixed`), `obsolete` (entity deleted), or spawn a `bug` (regression found) and then `completed`.
- **Dedupe:** at most one open reverify item per SEI. A later worklist links to it instead of filing again (today's behaviour), and re-files only after a `done`.
- **Producer-agnostic:** the v1 envelope is the contract, so Loomweave or a successor can produce it.

**Archive:** a visibility attribute (`archived_at`) on `done` items. Status and resolution are preserved, reopen clears it, and `compact_events` never deletes status/resolution events.

### 5.5 Process metrics (derived from events)

Per kind, containers excluded:
- p50/p85 claim→done cycle time and lead time;
- WIP count and **WIP age**;
- arrival vs completed and **discard rate** by resolution;
- time in `triage`, `parked` and `in_review` (approval latency);
- reopen rate;
- gate-denial rate.

Per inbox:
- defect-finding inbox size and age;
- observation expiry vs promotion.

Gate-bypass rate becomes structurally 0, which serves as a design check.

### 5.6 Migration notes (3.x → 4.0)

A clean mapping exists for every built-in state, so no data needs to be dropped.

| From | To (type / state / resolution) |
|---|---|
| bug `triage` / `confirmed` / `fixing` / `verifying` | bug `triage` / `open` / `in_progress` / `in_progress` (or `in_review` if a review policy is enabled) |
| bug `closed` | `done/completed`. If the closing transition was forced (event `transition_forced`, or old status ∈ triage/confirmed/fixing) **and** `close_reason` matches duplicate/superseded/scratch/wontfix, use the matching discard resolution; otherwise `completed` + `migrated_inferred=true` |
| bug `wont_fix` / `not_a_bug` | `done/wont_do` / `done/not_a_bug` |
| feature `proposed` / `approved` / `building` / `reviewing` / `done` / `deferred` | `triage` / `open` / `in_progress` / `in_review` (or `in_progress`) / `done/completed` / **`parked`** (done→open category change; announce to consumers) |
| task `open` / `in_progress` / `closed` | `open` / `in_progress` / `done/completed` (same reason heuristic as bugs) |
| step | task (`pending`→`open`, `in_progress`, `completed`→`done/completed`, `skipped`→`done/wont_do`) |
| epic / phase / work_package | epic (`open`/`pending`/`defined`/`assigned`→`planned`; `in_progress`/`active`/`executing`→`active`; `closed`/`completed`/`delivered`→`done/achieved`; `skipped`/`cancelled`→`done/cancelled`) |
| milestone | `planning`→`planned`, `active`/`closing`→`active`, `completed`→`done/achieved`, `cancelled`→`done/cancelled` |
| release | `planning`→`planned`, `development`→`active`, `frozen`/`testing`/`staged`→`frozen`, `released`→`done/released`, `rolled_back`→`rolled_back`, `retired`/`cancelled`→`done/cancelled`. Delete the `Future` singleton when it has 0 children; otherwise convert to an "unscheduled" epic |
| requirement / acceptance_criterion (errorworks only) | requirement → feature (`drafted`/`reviewing`→`triage`, `approved`→`open`, `implementing`→`in_progress`, `verified`→`done/completed`, `rejected`→`done/wont_do`, `deferred`→`parked`). AC → checklist field on the parent. Alternatively keep the optional pack installed |
| deliverable, release_item, risk, mitigation, theme, objective, key_result, incident, postmortem, debt_item, remediation, spike, finding | No rows exist fleet-wide (verified). Migration is a no-op; retire them |
| status `archived` | Restore the pre-archive status from the latest `status_changed` event (`SELECT new_value … ORDER BY created_at DESC, id DESC LIMIT 1`), map it as above, and set `archived_at` to the `archived` event time. If events were compacted, use `resolution=unknown` |

**Open-child sweep (container gate):** done containers with non-done children (e.g., the probe's M1). Either reopen the container as `active`, or move the children to an "unscheduled" epic. Never auto-close the children. The live 3.3.0 milestone needs an owner decision before migration: close it and move its 21 open children to a 3.4/4.0 container.

**Scaffolding:** do not migrate "[Bug tree]" nodes as bugs. Map them to `task kind=review` with resolution `completed`, and keep the label.

**Wire compatibility:**
- `status_category` semantics are unchanged (open/wip/done), but `deferred` moves to open.
- The `resolution` field is new.
- Status names change, so 4.0 is the right major for this.
- Live consumers (Loomweave, Wardline) read categories, not literal states. Verify that before cut-over.

---

## 6. Confidence assessment

**Overall confidence:** High on mechanisms and on dogfood and fleet data; Moderate on generalising to external users.

| Finding | Confidence | Basis |
|---|---|---|
| BP-01 force lane / gate bypass / falsified resolution | High | Code paths (`db_issues.py:1260`, `templates.py:1049-1053`), static analysis, event counts, close-reason samples, probe |
| BP-02 ritual gates | High | Dwell and actor queries over 426–436 transitions; fleet rework counts |
| BP-03 no roll-up or sequencing | High | Live 3.3.0 milestone; probe (a)(b)(c); code `db_planning.py:368-379` |
| BP-04 finding inbox cannot drain | High | `scan_findings` counts by rule and status; ADR-015; caller search |
| BP-05 catalogue vs usage | High | 14-tracker survey; config survey; sibling corroboration |
| BP-06 shoehorning | High | Title, label and parent queries |
| BP-07 no resolution model | High (mechanism) / Moderate (magnitude) | Template and analytics code; keyword heuristic is a lower bound |
| BP-08 archive lossy, one-way | High | Code + probe |
| BP-09 lease mismatch | Moderate | Durations and counts are solid; **no concurrent-agent evidence** |
| BP-10 identity fragmentation | High | Claim and event actor distributions; CAS code |
| BP-11 DoD not integration-anchored | High | Open tracker items; `close_commit` coverage |
| BP-12 metrics | High | `analytics.py`; live `filigree metrics` output |
| BP-13 observation TTL loss | Moderate | Keyword match between expired summaries and issues is approximate |
| BP-14 governed-close seam gaps | Moderate–High | Code; seam unexercised in dogfood |
| BP-15–BP-18 | High | Code, probe, docs |
| Target model fits agent work | Moderate | Inferred from usage; not user-tested |

## 7. Risk assessment

**Implementation risk:** High (a cross-cutting rewrite of the workflow core, migration and wire changes).
**Reversibility:** Moderate (the migration can be reversed only if 3.x status and events are kept; recommend storing `legacy_status` for one major).

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Heuristic resolution inference mislabels historical closes | Medium | High | Flag `migrated_inferred=true`; never inflate `completed`; report counts at migration |
| `deferred`→`parked` (done→open) surprises consumers and metrics | Medium | Medium | Announce in the 4.0 contract; Loomweave/Wardline consumers read categories, so verify |
| Retiring packs alienates users who value the breadth | Low | Low | Ship them as installable JSON packs with docs |
| The container close gate frustrates agents ("can't close epic") | Medium | Medium | The gate error lists children and offers `move`/`discard` in one call |
| Short lease plus implicit heartbeat → false reclaims during long reasoning | Medium | Low | Lease configurable per project; `reclaimable` is visible, not auto-taken |
| Policy-driven `in_review` adds latency where humans are slow | Medium | Medium | Off by default; per-kind policy; approval-latency metric |
| Session-id claims break existing clients | Medium | High | Deprecation window: accept actor-only claims with a warning in 4.0.x |

## 8. Information gaps

1. [ ] **External user data.** All usage evidence comes from the owner's 14 local trackers. External agent users may lean on extended packs, though none are visible.
2. [ ] **Concurrent multi-agent traces.** No evidence of parallel agents contending for claims (no stale claims and 4 reclaims ever). BP-09/BP-10 severities are inferred.
3. [ ] **Owner intent for domain-agnostic packs.** The Feb-2026 design aimed at non-software domains (editorial exemplar). If that is still a product goal, the optional-pack route must be first-class.
4. [ ] **Wardline producer roadmap.** Whether Wardline can emit `kind` reliably and call retention, or whether Filigree must classify on ingest.
5. [ ] **Live consumer contracts.** Which literal status names Loomweave and Wardline read, as opposed to categories. Needed to scope 4.0 wire breakage.
6. [ ] **Hidden rework.** Rework may happen outside the tracker (in PR review). PR/CI data would show whether `in_review` should default on.

## 9. Caveats and required follow-ups

**Before relying on this analysis you must:**
- [ ] Re-run the dogfood queries after any tracker cleanup. The figures are as of 2026-10-07, and the tracker has been idle since 2026-09-02, so 30-day metrics are empty by construction.
- [ ] Confirm with the owner how the open 3.3.0 milestone should be dispositioned before migration design.
- [ ] Validate the resolution-inference heuristic on a sample of 50 forced closes.

**Assumptions:**
- "Real work" excludes "[Bug tree]" nodes, the 6 named scratch milestones and their descendants, and `mcp-review-scratch` items.
- Fleet trackers under `/home/john/*` are representative of the owner's fleet.
- Probe behaviour (filigree 3.3.0 CLI, all packs enabled) matches MCP behaviour; both route through the same `db_issues` paths.

**Limitations:**
- I did not exercise Legis- or Loomweave-backed governed closes (Legis is archived; the seam is unexercised in dogfood).
- I did not run Wardline (MCP down this session).
- I did not evaluate dashboard UX of the workflows.
- I did not measure the cost of the target model in agent tokens or calls.

**Recommended next steps:**
1. Owner decision: adopt the 6-type / universal-lifecycle model as the 4.0 workflow contract (ADR).
2. Write the 4.0 policy spec for the close gate and reverify seams (Section 5.4) before code.
3. Prototype the migration on a copy of this tracker and errorworks, and report inferred-resolution counts.
4. Ship metrics v2 (Section 5.5) early. It quantifies the before/after.
5. In 3.x, as quick wins: an epic `open→closed` forward edge (BP-15), retention on ingest (BP-04), a release target from history (BP-09), and doc fixes (BP-17).

---

## 10. Machine-readable summary

```json
{
  "lane": "business-process",
  "date": "2026-10-07",
  "overall_confidence": "High",
  "implementation_risk": "High",
  "reversibility": "Moderate",
  "headline_target": "24 types -> 6 (task/bug/feature on one 6-state leaf lifecycle + resolution enum; epic/milestone/release on one roll-up container lifecycle); 9 built-in packs -> 1 + optional installable packs; force-close retired; 324 reverse edges -> reopen + release verbs",
  "top_findings": [
    {"id": "BP-01", "tag": "NEW", "severity": "High", "confidence": "High", "claim": "Force/escape lane is the normal close lane: 31% of real bug fixed-closes (27% fleet) bypass the verifying hard gate; every hard gate into a done state is bypassable; force defaults to 'closed' so duplicates/wontfix/scratch are recorded as fixed", "evidence": "db_issues.py:1260; templates.py:1049-1053; templates_data.py:1714-2105; db_files.py:2232-2242; events: triage->closed 132, fixing->closed 14, confirmed->closed 8 vs verifying->closed 337"},
    {"id": "BP-02", "tag": "NEW", "severity": "High", "confidence": "High", "claim": "No gate produces independent evidence: verifying dwell <1min 405/426, verifier==fixer 425/436, rework 3 fleet-wide; feature self-approval 17/25; 728 ignored soft-gate warnings", "evidence": "events table dwell/actor queries; templates_data.py:77-78,142"},
    {"id": "BP-03", "tag": "NEW", "severity": "High", "confidence": "High", "claim": "Containers neither roll up, couple nor sequence: 3.3.0 milestone still 'planning' 35 days after release; milestone completable with 0/2 steps; phase dependencies do not block child steps", "evidence": "filigree-b21d7a9f17; probe; db_planning.py:368-379; templates_data.py:433; design 2026-02-25 s2.2 never built"},
    {"id": "BP-04", "tag": "NEW", "severity": "High", "confidence": "High", "claim": "Findings inbox cannot drain: 9,793 of 9,958 unbridged rows are two Wardline telemetry rules; 3 findings ever linked; ADR-015 retention has no driver (158 unseen >30d never fixed)", "evidence": "scan_findings counts; db_files.py:2317-2341; ADR-015; related STILL-OPEN filigree-56cb5c93f3, filigree-8f6a1599fb"},
    {"id": "BP-05", "tag": "NEW", "severity": "High", "confidence": "High", "claim": "13 of 24 types never instantiated across 14 trackers; 4 core types = 93% of 3,140 issues; 5 of 9 packs enabled nowhere; labels replace debt/risk/release packs", "evidence": "fleet sqlite survey; config survey; label counts"},
    {"id": "BP-10", "tag": "STILL-OPEN", "issue": "filigree-c2009921cf", "severity": "Medium", "confidence": "High", "claim": "42 claimant name variants, 35% of status changes by default 'cli'/'mcp'; same-name parallel sessions not mutually excluded by claim CAS", "evidence": "events.claimed; db_issues.py:1574-1580"}
  ],
  "other_findings": ["BP-06 shoehorning (Medium)", "BP-07 no resolution model (Medium)", "BP-08 archive lossy one-way door (Medium)", "BP-09 claim lease mismatch / release-to-triage (Medium)", "BP-11 DoD not integration-anchored (Medium)", "BP-12 metrics measure bookkeeping (Medium)", "BP-13 observation TTL loses defects (Medium)", "BP-14 governed-close seam: no queue, debt never resolves (Medium)", "BP-15 epic forced closes (Low)", "BP-16 ready != actionable (Low)", "BP-17 doc drift (Low)", "BP-18 planning deprecation frozen on Shuttle (Low)"],
  "regressions_found": [],
  "regression_probes": ["filigree-42045dd065 holds", "filigree-406e6b7ee0 holds (residual BP-16)"],
  "blocking_gaps": ["No external-user usage data", "No concurrent multi-agent traces", "Owner intent on domain-agnostic packs", "Live consumer reliance on literal status names"],
  "recommended_next_steps": ["ADR adopting 6-type universal-lifecycle model for 4.0", "Spec close-gate and reverify seams (section 5.4) provider-agnostic", "Prototype migration on copies of filigree + errorworks trackers", "Ship metrics v2 early", "3.x quick wins: epic forward close edge, retention on ingest, release target from history, doc fixes"]
}
```
