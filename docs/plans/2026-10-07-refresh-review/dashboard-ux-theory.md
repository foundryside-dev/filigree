# Filigree Web Dashboard: First-Principles UX Theory Review

Date: 2026-10-07. Reviewer: ux-theorist (actor `claude-filigree`). Scope: human-facing dashboard only (agent MCP/CLI, business process and strategy are covered by sibling reviews). Read-only: no source edits, no state mutation. The only UI interaction on the live dashboard was view navigation, opening one issue detail, and dismissing the first-run tour.

## 0. Stated product purpose (cited)

`docs/product/vision.md`: Filigree "exists to make AI coding agents *first-class operators* of their own work-tracking." Primary user: AI agents over MCP, including "multi-agent fleets that must claim, hand off, and recover without colliding." Secondary: "the human developer driving those agents, via the CLI ... and the web dashboard," plus Weft siblings. Anti-goal: "A human-first PM suite. Burndown-chart / sprint-ceremony surface area for human teams is declined; the agent is the primary user, the human the driver."

Consequence for UX: the dashboard is a supervision console for a driver, not a work-management tool for workers. Its job is to answer "can I trust what my agents are doing and what they claim they did, and where do I need to step in?" Every surface is adjudicated against that.

## 1. Evidence base

- Frontend is modularized: `dashboard.html` 486 lines plus ~7,800 lines of vanilla JS (`app.js` 717, `router.js` 172, `ui.js` 536, `filters.js` 538, `views/*` 11 files). The "~2300-line single file" note is stale. Tailwind still loads from a vendored CDN build and logs a "should not be used in production" warning.
- Six nav views: Kanban, Ready, Graph, Releases, Insights, Files. Header also carries: New, Ready(43) toggle, Blocked(6) toggle, search, Select (multi-select), Open/Active/Done pills, Filters (priority, presets, Save Preset, updated-in-N-days), health badge (97), settings gear (reload server, theme, workflow diagram). Footer: counts, sparkline, stale badge, version.
- Backend exposes far more than the UI consumes. Routes exist for `/findings/{id}/dossier`, `/session-evidence`, `/findings/promote`, `/findings/promote-and-attach`, `/findings/clean-stale`, `/scanners`, `/observations`, `/changes`, `/scan-runs`. The issue payload carries `claimed_at`, `last_heartbeat_at`, `claim_expires_at`, `claim_commit`, `close_commit`, `blocked_by`, `data_warnings`. Grep of `static/js` shows the UI uses none of the heartbeat / claim-expiry / claim_commit / close_commit fields, and `api.js` has no dossier, session-evidence, findings-promote, clean-stale or scanners client.
- Polling, not push: `app.js` `setInterval(fetchData, REFRESH_INTERVAL=15000)`. SSE is only a ROADMAP item. Stale detection (`metrics.js updateStaleBadge`) is "WIP with no `updated_at` change in >2h", not a heartbeat or lease test.
- Live observations (http://localhost:8834, project filigree, 2026-10-07; screenshots in `screenshots/`: `kanban.png`, `insights.png`, `graph.png`):
  - A 6-step onboarding tour auto-opens on load and says "The dashboard has 5 views" while the nav shows 6 (premise drift evidence).
  - Kanban Board: Open 49, In Progress 0, Done 0 (done hidden by default pill). Two-thirds of the screen is empty; all 49 open items stack in one column, mixing P1 phases/milestones/steps with tasks. Milestone, phase, step, epic and feature rows sit alongside leaf tasks in a "work" column.
  - Counts: 1,139 done vs 49 open; 759 of 1,188 issues are bugs; 172 `not_a_bug`, 833 `closed`. The board is a view of the 4% that is open; the history that matters for oversight is invisible by default.
  - Graph: opens to "Select items from the sidebar ... 0 nodes, 0 edges." Explorer lists 8 milestones including five `[mcp-review-*] Scratch milestone` / `[scratch]` entries (agent litter surfaced as first-class).
  - Releases: "No active releases" even though a "Release 3.3.0" milestone and a `release` type issue exist (released state is hidden behind a checkbox; only one `release` typed issue).
  - Insights: Throughput 0, cycle time and lead time "—" for 7 days; embedded "Recent Activity (15 events)" last event 2026-09-02 (35 days old), all `created` by `claude-filigree`.
  - Files: 9,958 findings; "Top Hotspot Files" first row `<engine>` with 8,850 findings (a synthetic non-file bucket dominating); severity bar medium 1 / low 8,852 / info 1,105; Scan coverage 96%; "Recent Scan Activity" 141 days old with no staleness warning. File table has Critical/High/Medium/Low columns that are blank for nearly all rows.
  - Detail panel (opened `filigree-6549e739de`): description, comments, timeline, Actions, "+ Add blocker". The comment in this issue records that `GET /api/weft/session-evidence` exists but no UI consumes it.
  - Header: the "Updated 10:08:32 AM" label overlaps the Filters control at 1440 px width.

## 2. Stage 1: Premises being relitigated

| # | Premise | Origin | Status |
|---|---------|--------|--------|
| P1 | The product's primary human surface is a Kanban board (Kanban is default, `#kanban`, first tab). | Human-PM convention; v1.2 "Dashboard UX overhaul" in ROADMAP; vision.md explicitly declines "human-first PM suite". | **Reject as primary.** |
| P2 | Moving a card between columns (drag-and-drop status change) is a first-class human action. | ROADMAP v1.2 "drag-and-drop status changes with transition validation". | **Reject.** Agents drive status; a human drag bypasses claim/evidence semantics. |
| P3 | Work state is open / in progress / done (3 columns). | Board mode "Three columns: Open, In Progress, Done". | **Reject.** Real workflow has per-type states (approved, proposed, planning, not_a_bug...) and a claim lifecycle (claimed, heartbeat, expired, released). |
| P4 | The human wants to *do* the work items (claim, close, comment, create, batch-update) from the UI. | Detail panel Actions; Select bar (Set Priority, Close All); New. | **Relitigate.** Triage and approval: yes. Execution-shaped mutation: mostly no. |
| P5 | One undifferentiated issue list serves everyone; all issue types (phase, step, milestone, epic, bug, task) are cards in the same column. | Single `/issues` list. | **Reject.** Containers are navigation; leaves are work. |
| P6 | The dashboard is a *browser of records* (browse, filter, search) rather than an *event-driven monitor*. | Polling + filter-heavy header. | **Reject.** The human's problem is attention allocation, not retrieval. |
| P7 | Flow metrics (throughput, cycle time, lead time, sparkline) matter to this user. | Insights; footer sparkline. | **Relitigate.** Vision declines burndown/ceremony; agent-fleet analogues (retry rate, evidence-less closes, claim expiry) are what matter. |
| P8 | The Weft-federation data (Files, findings, hotspots, health score) deserves a top-level tab because the hub is the work-state authority. | Files & Code Health design doc (2026-02-21), "7 tabs -> 5". | **Relitigate.** Findings are an inbox of decisions; a file table is a data browser. |
| P9 | A single numeric "system health" score (97) is a useful oversight signal. | healthBadge, `health.js`. | **Relitigate.** Unexplained composite; invisible to the failure modes that matter. |
| P10 | The dashboard is used by a person at a keyboard, in one project, in a long session. | Hash routing, project switcher, detail slide-over. | **Relitigate.** Likely a glance (second monitor, phone, between agent runs) plus a periodic review. |
| P11 | Tab count was the problem ("7 -> 5") and consolidation fixed it. | ROADMAP. | **Reject as method.** Count is not the issue; the organizing question is wrong (object type rather than human question). Now 6 again. |

## 3. Stage 2: Audience model

Evidence level: all personas are **derived** from vision.md, ROADMAP, route inventory and issue content (e.g. session-evidence, agent broadcast board). No user research or analytics exist. They are marked accordingly.

| Persona | Evidence | Goal at this UI | Needs on screen | Does NOT need |
|---------|----------|-----------------|-----------------|----------------|
| **A. Fleet overseer** (the "driver", e.g. the owner running 1..N agents) | vision.md "the human developer driving those agents"; strongest | "Am I needed? Are my agents making real progress? Did anything go wrong while I wasn't looking?" | Now-board of live claims (who, what, how long, heartbeat age); attention queue (stuck/expired, blocked-on-human, approval-waiting, close-without-evidence); recent change feed filtered by actor | Drag-and-drop, flow-metric ceremony, 49-card backlog columns |
| **B. Approver / triager** (same human in review mode) | Workflow states `proposed`, `approved`, `pending`, findings promote/dismiss, observations promote | Make decisions agents cannot (approve proposals, promote/dismiss findings and observations, set priority, accept a close) | Decision inbox with context bundle (issue + evidence + diff/commit + related findings), one-key accept/reject/ask | Full project tree, graph |
| **C. Release manager** | `release` type, Releases view, release tree, 3.3.0 milestone, `release:` labels | "Can we cut 3.3.0? What's left, what blocks it, what shipped?" | Release readiness: open blockers, children rollup, evidence of closes, target date, history | Kanban, Insights |
| **D. Code-health reviewer** | Files & Code Health design (2026-02-21), scan runs, Loomweave/Wardline findings | "Which new findings need a human call, which regressed since the last scan?" | New/regressed findings by severity with dossier, scan freshness | A 10k-row file table, hotspots polluted by `<engine>` |
| **E. Auditor / post-hoc investigator** | `session-evidence` route, Legis audit trail, timeline events | "What exactly did agent X do in this window, and was it verified?" | Actor-scoped timeline, closes with close_reason / commit, entity associations | Live board |
| **F. Agent as dashboard consumer** | Dashboard is also an HTTP API (`/api/weft`) | Not a UI persona: agents use MCP/CLI; the `/api` surface is for siblings. The UI must not be shaped to serve it. | n/a | n/a |
| **G. Peer-tool operator (Loomweave/Legis/Wardline)** | Federation banners (registry fallback, Loomweave rotation) | Know when the federation is degraded and why | Federation health/degradation banner (exists) | n/a |

### Anti-personas (surfaces built for them, no evidence)

| Anti-persona | Why no evidence | Surfaces built for them |
|--------------|-----------------|--------------------------|
| **The scrum team lead / human assignee pool** | vision.md "Explicitly not ... Jira/Linear class"; workers are agents, `assignee` is an actor string | Kanban Board with drag-and-drop, Cluster-by-epic with progress bars, Select/Batch bar (Set Priority, Close All), Agent Workload bar chart (as if capacity planning for humans), Save Preset |
| **The project manager reading flow reports** | Anti-goal: "Burndown-chart / sprint-ceremony surface area ... declined" | Insights throughput/cycle/lead time, "By Type" avg cycle table, footer sparkline, 7/30/90-day selector |
| **The manual developer who files and works issues in the browser** | Agents create and close via MCP/CLI | "+ New", Create form, claim modal, comments box, "Add blocker", dependency editor, Reopen/Close modals |
| **The repo-wide static-analysis analyst** | Scanning is Wardline/Loomweave's job (vision anti-goal: "composes, does not annex") | Files table with 9 columns, hotspot widget, donut by severity, coverage widget |
| **The workflow designer** | ROADMAP "Visual workflow designer" is Phase-future; no evidence anyone edits packs in UI | Settings > "Workflow diagram", `workflow.js` (362 lines, now inline in kanban) |
| **The occasional first-time visitor needing a 6-step tour** | Single-operator localhost tool | Auto-opening tour (its copy is already wrong: "5 views") |

## 4. Stage 3: Conceptual model audit

| Persona | Borrowed model | Mismatch? | Better model |
|---------|----------------|-----------|--------------|
| A. Overseer | Jira/Trello board (work moves between columns by human hands) | **Severe.** Agents move work; the human supervises. "In Progress: 0" board looks dead even if five agents are claimed or are heartbeating (claim fields not rendered). The board answers "what exists", not "what is happening". | **Operations console / mission control**: live claims with heartbeat age, an attention queue, a change feed. |
| B. Approver | Issue browser + edit modal | **Moderate.** Decisions are scattered (proposed items inside the Open column; findings inside file detail; observations only as a badge saying "use `list_observations`" in the CLI). | **Inbox / review queue** with accept-reject-defer and a context bundle. |
| C. Release manager | Accordion tree of children | **Partial.** Tree is right for rollup; wrong when it only shows "active" releases and a released release disappears. | **Readiness checklist / go-no-go**: blockers, unverified closes, open children, with history one click away. |
| D. Code-health reviewer | Spreadsheet/data grid (sortable file table, 9 columns) plus dashboard widgets | **Strong.** The unit of human decision is the finding, not the file. Aggregates are dominated by a synthetic `<engine>` row (8,850). | **Triage feed of findings** (new since last scan, severity, promote/dismiss/link), with scan freshness. |
| E. Auditor | Activity feed buried inside Insights (15 events) | **Moderate.** Chronology is right, but it is global, undeclared, and not actor-scoped; the actor-scoped backend (`/session-evidence`) is unconsumed. | **Actor/session timeline** (flight recorder) with evidence per close. |
| All | IDE-ish persistent chrome (fixed 6-tab header + dense filter strip + status footer) | **Moderate.** Chrome is permanent, while the human visit is episodic; the header is overloaded at 1440 px (text overlap on "Updated"). | **Question-first landing** ("needs you", "happening now", "recently done"), with browse/search as secondary. |

## 5. Stage 4: Surface-by-surface adjudication

Verdicts: Keep / Reframe / Kill. "Evidence" is file or view name plus what I observed.

| # | Surface | Verdict | Reasoning and evidence |
|---|---------|---------|-------------------------|
| 1 | **Kanban > Board** (`views/kanban.js` `renderStandardKanban`) | **Reframe** (demote from default) | Serves anti-persona (human assignee pool). Live: Open 49 / In Progress 0 / Done 0, two columns empty and 49 cards in one. Becomes a secondary "Backlog by state" browse; in its place a Now/Needs-you landing. |
| 2 | **Kanban > Cluster** (epic progress bars) | **Reframe** -> fold into Release/Plan rollup | Rollup-by-parent is a valid persona-C need; it duplicates Releases tree and Graph explorer. Merge into one hierarchical "Plan" view. |
| 3 | **Kanban > List** | **Keep** (as the one canonical "Issues" browse/search table) | The only mode that scales (1,188 issues). Make it default for browse; add actor, claim age, evidence columns. |
| 4 | **Drag-and-drop status change** (`initDragAndDrop`, `computeDragTargets`) | **Kill** | P2 rejected. A human drag bypasses claim ownership and close evidence; no persona needs it. Replace with explicit, evidenced actions (approve/reject/reopen). |
| 5 | **Type-filter + workflow-state columns** (`renderTypeKanban`, `filterType`) | **Reframe** | Per-type states (approved/proposed/planning) are the real workflow; useful inside the Backlog view, not as a Kanban-in-Kanban. |
| 6 | **Ready tab** (`views/ready.js`, 63 lines) | **Reframe** -> becomes a facet of Issues list (and Ready(43) header toggle already duplicates it) | Same data as header "Ready (43)" toggle and `/ready`. Ready is an agent concept ("what can an agent claim next"); the human wants "ready but unclaimed for N days" as a stall signal. Duplicate entry points: kill one. |
| 7 | **Ready (43) / Blocked (6) header toggles** | **Reframe** | Counts are good signals; Blocked(6) should link to *why* and *on whom* (human decision vs agent dependency). |
| 8 | **Graph tab** (`graph.js`, `graphSidebar.js`: 992 lines) | **Reframe** (secondary, contextual) | Opens blank ("0 nodes, 0 edges"; requires pre-selecting from a 100+ item sidebar). Dependencies are most useful *in context* (why is this blocked, what is the critical path of this release). Embed as "Blockers & critical path" within Release/Issue detail; keep a standalone explorer for investigators only. Scratch milestones (`[mcp-review-*]`, `[scratch]`) pollute the explorer: needs an archived/ephemeral filter by default. |
| 9 | **Releases tab** (`views/releases.js`, 801 lines) | **Keep + Reframe** | Persona C is real and evidenced. Fails today: "No active releases" while 3.3.0 shipped and its milestone is still in Kanban; history hidden behind a checkbox. Add readiness (open blockers, unverified closes) and "released" history as first-class; fold milestone/phase/step into this view rather than the Kanban Open column. |
| 10 | **Insights tab: throughput/cycle/lead time cards** (`metrics.js`) | **Kill** (as designed) | Anti-persona (PM reading flow reports). Live: all "—"/0 for the window. Replace with fleet-oversight metrics: close-without-evidence rate, reopen rate, claim-expiry/reclaim rate, time-in-state outliers. |
| 11 | **Insights: By Type avg cycle table, 7/30/90 selector, "Try 90-day window" empty state** | **Kill** | Same. |
| 12 | **Insights: Agent Workload bar chart** (`renderAgentWorkload`) | **Reframe** -> **Now board** (promote to landing) | Right idea, wrong form: computed only from `wip` + assignee, a count bar. Must show per-agent: current claim(s), claim age, heartbeat age/expiry, last event, last close with evidence. Live: absent entirely (0 WIP). |
| 13 | **Insights: Observation stats** (`renderObservationStats`) | **Reframe** -> approver inbox | Counts only; actions (promote/dismiss) exist only on MCP. Observations are literally "agent notes for human triage" and should be an inbox. |
| 14 | **Insights: embedded Recent Activity** (`activity.js`, 15 events, "Show 11 more") | **Reframe** -> promote to a first-class **Activity / Flight recorder** | The most oversight-relevant surface is the least visible (inside a metrics tab, truncated at 15 events, no actor filter, no event-type filter, no evidence). Live: all `created` by one actor from 35 days ago. |
| 15 | **Files tab > Code Quality Overview** (`health.js`: hotspots, donut, coverage, recent scans) | **Reframe** | Valid health summary but mis-shaped: `<engine>` bucket (8,850) dominates hotspots; 8,852 low + 1,105 info drowns 1 medium; scan activity 141 days stale with no freshness warning. Replace with "findings needing a decision" + scan freshness + new-since-last-scan. |
| 16 | **Files tab > file table** (9 columns, sort, path filter, Critical only) | **Reframe** -> secondary drill-down | Persona D works from findings, not files; keep as a lookup reached from a finding or an issue. Live: Critical/High/Medium/Low columns are blank for the top-of-table rows (sorted by last update), so the table is mostly empty cells. |
| 17 | **File detail > Findings tab** (`renderFindingDetail`, dismiss/update via PATCH) | **Keep, promote** | Core persona-D decision surface; surface as top-level "Findings" triage. Wire in the dossier, promote, promote-and-attach (backend exists, UI missing). |
| 18 | **File detail > Timeline tab** | **Keep** | Evidence trail per file for persona E; low cost. |
| 19 | **Link-issue modal / file association** | **Reframe** | Association is an agent action; the human needs to *see* provenance (who linked, when), not necessarily create. |
| 20 | **Issue detail slide-over** (`detail.js`) | **Keep + Reframe** | Essential drill-down. Missing for oversight: claim holder, claim age, heartbeat, expiry, `claim_commit`/`close_commit`, close reason prominence, evidence, "who last acted". Actions block mixes execution (claim, release, add blocker) with decision (approve/close) controls. |
| 21 | **Detail > Claim modal / Release / Close modal / comments box / dependency editor** | **Reframe** | Keep human override (force-release a dead agent's claim; reopen), kill the "claim as human" default path; add "release stale claim" with reason. Comments: keep as the human's channel *to* agents (needed, and comments are what agents read on resume). |
| 22 | **+ New / create form** | **Reframe** | Humans do create (requests to agents). Demote; keep light "Ask / request" form with type + priority + body. |
| 23 | **Select / batch bar (Set Priority, Close All)** | **Reframe** | Batch is right for triage (dismiss 40 findings, approve 10 proposals). "Close All" of issues without evidence is exactly the failure mode the oversight job is meant to catch: require a reason/evidence. Move to the inbox. |
| 24 | **Filters strip: Open/Active/Done pills, Priority, Presets, Save Preset, updated-in-N-days, Search** | **Reframe** | Search: Keep (global find). Status pills tied to the 3-column model (P3). Save Preset is an anti-persona (analyst) feature: kill unless usage is demonstrated; replace with a few named, opinionated views. |
| 25 | **Stale badge in footer** (`updateStaleBadge`, >2h no `updated_at`) | **Reframe** -> core signal | Right idea, wrong test: should key on `last_heartbeat_at` / `claim_expires_at` (both in API, unused) and distinguish "agent alive but slow" from "dead". Move to the landing. |
| 26 | **Health badge (97) + breakdown** | **Reframe or Kill** | Unexplained composite; verify what it measures vs the failure modes (stuck claims, evidence gaps). Either decompose into named signals or remove. |
| 27 | **Footer counts + 14-day sparkline** | **Reframe** | Counts are glanceable and fine; sparkline of throughput is the PM metric (kill); replace with "agents alive: n, claims: n, needs-you: n". |
| 28 | **Project switcher** | **Keep** | Multi-project registry is real and federated; show per-project attention counts. |
| 29 | **Registry-fallback and Loomweave-rotation banners** | **Keep** | Persona G: system-degradation notices are exactly right chrome. Keep, but unify into one status area. |
| 30 | **Settings gear: Reload server** | **Reframe** | Operator action, not oversight; place in a diagnostics area. |
| 31 | **Settings: Toggle theme** | **Keep** | Cheap, expected. |
| 32 | **Settings: Workflow diagram modal** (`workflow.js`) | **Kill** (move to docs / detail contextually) | Anti-persona (workflow designer); a state diagram is only useful in the context of a specific type's transitions (detail panel already fetches transitions). |
| 33 | **Onboarding tour (6 steps)** | **Kill and replace with empty states** | Copy already wrong ("5 views" vs 6 tabs); a modal that covers the first screen on every fresh browser profile; single-operator localhost tool. |
| 34 | **"Updated 10:08:32" label / refresh indicator** | **Keep, fix** | Data-freshness signal is vital for a monitor; currently collides with Filters control at 1440 px. With 15 s polling, staleness of truth should be explicit. |
| 35 | **Hash routing & deep links** (`router.js`, `#kanban&issue=...`) | **Keep** | Linkability from agent comments / PR descriptions to the dashboard is a real overseer flow; ALIASES shim for removed tabs (`health`, `activity`, `workflow`) is dead history, remove when views are reorganized. |
| 36 | **Dashboard `/api` + `/api/weft` HTTP surface** | **Keep** | Not UI, but it is the contract the new UI should consume; session-evidence, dossier, and claim fields already exist. |

Summary:

- Killed: drag-and-drop; flow-metrics cards/By-Type table/sparkline; Save Preset; Workflow diagram modal; onboarding tour; (probably) health score as an unexplained composite.
- Reframed: Kanban Board -> backlog browse; Cluster -> Plan rollup; Ready tab -> list facet; Graph -> contextual blockers; Insights Agent Workload -> Now board; embedded Activity -> first-class Activity; Files overview -> findings triage; stale badge -> heartbeat-based attention queue; batch bar -> evidence-gated inbox actions.
- Kept: List mode, Releases (with fixes), Findings detail, Timeline, Issue detail, search, project switcher, federation banners, theme, deep links, freshness indicator.

## 6. Stage 5: Audience tensions

| Tension | Persona A | Persona B | Resolution |
|---------|-----------|-----------|------------|
| Monitoring vs deciding | A wants a glanceable, passive "is everything OK" display that interrupts rarely | B wants to enter a focused queue and clear items with context | **Mode separation** ("Now" vs "Inbox") sharing one attention count. Progressive disclosure does not resolve it: the goals differ (watch vs decide). |
| Live vs historical | A/E need real-time claims and an actor-scoped log | C wants stable rollups and history | **Different time horizons on separate views**; do not mix filters (the current status pills/time bounds mean different things across views). |
| Human override vs agent authority | A needs a force-release / reopen when an agent is dead | Agent claim semantics (optimistic lock) require that humans not casually mutate | **Sensible defaults + friction**: read-only by default, mutations as named, reasoned, audit-attributed actions (not drag-drop). Layered density for expert vs occasional. |
| Breadth (federation data) vs focus (work) | D wants thousands of findings with search and bulk | A wants noise suppressed | **Separate surface** (Findings triage) feeding a count into the attention queue; do not co-mingle on the board. |
| Expert (daily operator) vs occasional (weekly release manager) | Dense, keyboard, saved views | Release readiness with plain language | **Layered density**: same list, density toggle; release view stays narrative. Keyboard navigation already exists in the release tree and is a good seed. |
| Human affordance vs agent-first purpose | Dashboard writes (create, claim, close) mimic a human PM tool | Vision says the agent is primary, human the driver | **Reframe actions as directives to agents** (comment/request/approve), reserve direct state change for override. |

## 7. Jobs and needs list

| ID | Job (human) | Served today? |
|----|-------------|---------------|
| J1 | "What are my agents doing right now?" (who, which issue, for how long, last sign of life) | **No.** Board shows In Progress 0 and the Agent Workload bar chart is a WIP count; heartbeat/claim fields unused. |
| J2 | "What needs me?" (proposed/approved-awaiting, blocked on a human decision, findings to triage, observations to promote) | **Partially/scattered.** Blocked(6) count without reason; observation count with CLI hint; findings only inside file detail. |
| J3 | "What's stuck?" (claims expired, WIP not progressing, ready-but-never-claimed, blocked chains) | **Weak.** Footer stale badge uses `updated_at` > 2h; no heartbeat/claim_expires; Ready queue age unseen. |
| J4 | "What did agents close without evidence?" | **No.** Close reason is optional text in a modal; close_commit/claim_commit unused; no filter for closes lacking reason/commit/test result. |
| J5 | "What did agent X do since I last looked?" (since-last-visit digest) | **No.** Activity inside Insights, 15 events, global, no actor/since-visit. `/session-evidence` unused. |
| J6 | "Can I cut release R?" (readiness, blockers, unverified closes) | **Partial.** Releases view exists but hides released history; no readiness verdict. |
| J7 | "Which new findings need a decision?" (new since last scan, severity) | **Partial/weak.** Aggregates dominated by `<engine>` row and 8,852 low items; scan freshness absent. |
| J8 | "Why is this blocked / what is the critical path?" | **Partial.** Graph requires manual selection of nodes; blank on load. |
| J9 | "Tell the agents something / change priority / reopen / force-release a dead claim" | **Yes**, via detail panel; but framed as manual PM work, not directive. |
| J10 | "Is the system itself healthy?" (federation degraded, registry, schema mismatch) | **Yes** (banners), plus a mysterious 97. |
| J11 | "Find a specific issue/file/finding by id or keyword and jump to it from an agent's message" | **Yes** (search, deep links). |
| J12 | "Did the agents behave? (reopen rate, retries, reclaims, churned claims)" | **No.** |
| J13 | "Show me the trail for audit" (who/when/what, per entity, per actor) | **Partial** (issue timeline, file timeline); no actor-centric. |

## 8. Unserved oversight jobs (priority-ordered)

1. **J1 + J3, "What are my agents doing right now, and who is stuck?"** (the single most important). The data already exists: `claimed_at`, `last_heartbeat_at`, `claim_expires_at`, `work_stale_list`-style semantics, per-actor events. The current dashboard cannot even show a live claim; with 0 WIP it presents an apparently dead board.
2. **J4, closed without evidence.** Highest trust-risk. The dashboard lets a human or agent close with an *optional* reason, never surfaces `close_commit`, and treats 1,139 closed issues as one undifferentiated "Done 0/hidden" column (833 `closed`, 172 `not_a_bug`, 5 `wont_fix`). `not_a_bug` at 172 is a particularly under-audited bucket in a project where 759 of issues are bugs.
3. **J5, since-last-visit digest per actor.** Needs `session-evidence` consumed in UI.
4. **J2, a single decision inbox** (proposals/approvals, findings, observations, blocked-on-human).
5. **J12, agent-behavior signals** (reopen rate, reclaim rate, claim churn).
6. **J6, release readiness verdict** (and releases history).
7. **J7, scan freshness and new-since-last-scan** (a 141-day-old scan is shown without warning).

## 9. Derived UX requirements

**MUST** (product fails its purpose for a derived persona otherwise)
1. A default landing that answers "needs you / happening now / recently closed" without any selection or filter (J1, J2, J3).
2. A live claims view driven by `claimed_at`, `last_heartbeat_at`, `claim_expires_at` with explicit "alive / slow / expired" states; replace the `updated_at > 2h` stale test.
3. Close evidence made visible: close reason, `close_commit`, linked findings/tests per closed issue; a filter "closed without reason/commit" (J4).
4. Actor-scoped activity (since-last-visit, per agent) as a first-class view, consuming `/api/weft/session-evidence` (J5).
5. A decision inbox unifying proposals/approvals, observations and findings with accept/reject/defer and context (J2).
6. Data-freshness honesty: last-updated visible, polling interval stated, scan freshness warnings; consider SSE (ROADMAP) because "now" is the headline job.
7. Remove or demote any mutation that bypasses claim/evidence semantics (drag-and-drop, bulk close without reason).
8. Keep deep links, search, project switcher, federation degradation banners.

**SHOULD**
1. Hierarchical separation: containers (milestone/phase/epic/step) as plan navigation, leaves as work.
2. Release readiness verdict with unverified-close and blocker counts; released history without hidden checkbox.
3. Findings triage fed by new-since-last-scan; exclude synthetic buckets (`<engine>`) from hotspots.
4. Decompose or drop the unexplained health score.
5. Fleet-behavior metrics (reopen, reclaim, churn) replacing flow-time metrics.
6. Ephemeral/scratch/archived milestones hidden by default in Graph and Plan views.
7. Phone-width or glance mode for the Now view; a keyboard-first inbox.
8. Replace the tour with good empty states (e.g. "No agents active. Last agent activity 35 days ago.").
9. Fix header overload and overlap (Updated vs Filters at 1440 px).

**WON'T** (written down so it does not drift back)
1. Human-assignee workflow, swimlanes, sprint ceremonies, burndown (vision anti-goal).
2. Drag-and-drop status changes.
3. A visual workflow designer.
4. A general static-analysis browser (that belongs to Wardline/Loomweave).
5. Saved-filter/preset management for analyst use (revisit only with usage evidence).
6. Multi-tenant, accounts, or sharing features.
7. Tour and onboarding wizard for a single-operator localhost tool.

## 10. Suggested information architecture (for the next sibling pass, not a design)

Questions, not object types: **Now** (live claims, attention counts) | **Inbox** (decisions) | **Activity** (actor flight recorder, with evidence) | **Plan** (releases, milestones, epics, blockers; includes graph in context) | **Issues** (the list/search/backlog) | **Findings** (triage, files as drill-down). Six surfaces again, but each maps to a persona job, and Kanban/Insights are gone as primary.

## 11. Confidence Assessment

- **High** (verified in source and live): the structural facts (6 views, no drag-drop justification in vision, API fields unused by UI, empty Kanban In-Progress column, `<engine>` hotspot dominance, stale scan data, tour copy mismatch, activity buried in Insights).
- **Medium-high**: that J1/J3/J4 are the top unserved jobs. This is inferred from vision.md and the nature of agent fleets, plus the existence of heartbeat/claim_expires/commit fields, session-evidence and stale-claim triage in issue history. It is not observed user behavior.
- **Medium**: persona C (release manager) and D (code-health reviewer) weights: both are evidenced by built features, but I could not verify that anyone uses them.
- **Low-medium**: kill verdicts on Insights flow metrics, Save Preset and the workflow modal rest on anti-goal alignment, not usage telemetry.

## 12. Risk Assessment

- **Pushback likely** on killing drag-and-drop and Kanban-as-default: the v1.2 UX overhaul invested in them and board metaphors are familiar. Counter: vision.md anti-goal, and the live board is 2/3 empty.
- **Anti-persona belief risk**: the owner may in fact use Insights/Kanban personally when dogfooding. If so, that is one user's habit, not a persona; test with the owner before final kills (see gap 1).
- **Over-index risk**: building a Now board against a project with 0 active WIP (current live state) means no real data shaped this review; the claim/heartbeat design must be validated with an active multi-agent session.
- **Scope-creep risk**: adding Inbox + Now + Activity risks becoming a second PM suite; keep each surface answering exactly one question.
- **Security/deconfliction**: human override actions (force release, reopen) are the only mutations that can corrupt agent coordination; keep them attributed and reasoned. Don't over-egg this; it is a functional deconfliction concern.

## 13. Information Gaps

1. No usage data/analytics: which tabs the owner actually opens, dwell time, mobile use.
2. No active-fleet sample: live project had 0 WIP, so I could not observe the Now-view data (heartbeat cadence, expiry settings, how many concurrent agents typical).
3. Not inspected: `healthBadge` computation, the "Filters" and "Done time-bound" popovers in detail, the Cluster/List modes live, the Release tree with a released release, file detail finding tab with real findings, narrow/mobile viewport, keyboard shortcuts, theme.
4. Observations (agent scratchpad) volume and TTL behavior on this project were not measured; counts only exist in code.
5. Whether `close_reason`/`fix_note` are mandatory in any workflow template (could change the J4 severity); did not read all templates.
6. Sibling reviews (MCP/CLI, process model, strategy) may change what the dashboard needs to expose (e.g. session/run checkpoints, `filigree-c2009921cf`).

## 14. Caveats

- This is theory, derived from stated purpose, source and one live snapshot; it is not user research. Usability testing is still required, especially for the Now and Inbox concepts.
- Screenshots were taken at 1440x900 only.
- The live project is the maintainers' own dogfood repository (agent-built, heavy bug history), which may not represent other projects.
- Screenshots were moved from `.playwright-mcp/` into `screenshots/` next to this report.
- No design, visual, or accessibility critique is offered (use `ux-critic` and `accessibility-auditor` after a candidate design exists).
