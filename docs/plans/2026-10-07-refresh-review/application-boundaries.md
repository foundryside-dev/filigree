# Application boundaries: Filigree, Loomweave, Wardline (and the archived members)

- **Date:** 2026-10-07
- **Reviewer:** solution architect on the refresh panel (actor `claude-filigree`)
- **Mode:** read-only. A brownfield boundary reassessment, not a full SAD.
- **Owner question:** *"Does it make sense for Loomweave and Filigree to do what they do?"*
- **Owner direction (2026-10-07), treated as constraints:**
  - Every component is rebuilt; Filigree's next major is 4.0.
  - Only Filigree, Loomweave and Wardline are live. The hub, Legis, Warpline, Lacuna, Tabard and Plainweave are archived.
  - Seams are kept, but their specs are fixed and rewritten where justified.
  - The users are the owner's agent fleet plus external agent users: agent-first.

**Inputs.**
- Filigree source and live tracker: read-only, with DB counts taken from a scratch copy.
- Loomweave and Wardline source, read directly. Wardline's MCP server was down.
- The archived hub specs: `federation-map.md`, `federation-topology.md`, `contracts-index.md`, `doctrine.md`, `members/*`, and `pm/2026-06-29-federation-interface-gap-map.md`.
- The archived member repos (Legis, Warpline, Lacuna, Tabard, Plainweave).
- The four sibling refresh reviews in this directory. `llm-agentic-experience.md` and `business-process-review.md` landed at 10:30, after this draft began. They were read afterwards for findings, annotations, tool count and seams, and are reconciled in §9.
- Today's untracked Loomweave memo, `~/loomweave/docs/implementation/2026-10-07-loomweave-rebuild-or-recover.md`. This is non-normative evidence, not a decision.
- Three read-only sub-investigations (Loomweave, Wardline, archived repos). Every load-bearing line number they returned was re-checked before it was cited.
---

## 0. The answer

**No, not as currently allocated.** Each product's *core* job is right. What is wrong is the territory around the core, and the main problem is that code facts are stored by the wrong owner.

- **Filigree's core job is right and heavily used.** That job is coordinating agent work: claims, leases, the ready queue, dependencies, plans, events and closes. It accounts for 95% of 5,485 logged MCP calls across 15 projects.
  - About **30% of Filigree's Python** (~20k of 66.7k lines) instead stores and re-derives code facts it cannot keep correct: a scan-findings warehouse, a file registry, line-anchored annotations, an LLM scanner runner, and a fail-closed governance client.
  - Of the 9,973 "findings" in Filigree's own tracker, 9,955 are Wardline engine telemetry. Three are linked to an issue.
  - Filigree's own vision forbids this: "It composes, it does not annex."
- **Loomweave's core job (the code map plus identity) is technically sound but unadopted.** Its own memo from today says it has *no active consumer*. elspeth retired it on 2026-09-25 and aurora removed it on 2026-10-05. Agent adoption was 0.18–0.8% against `rg`. It also had two silent multi-week outages.
  - About a third of its source serves federation consumers that are now archived.
  - Two of its tools are hollow proxies for the archived Warpline.
  - Its Filigree findings join returned HTTP 401 in this session.
- **Wardline is the only product whose cross-product seam carries real volume.** That volume is almost entirely telemetry it already calls "noise" in its own code (`core/resolution_posture.py:8-9`).

**Recommendation (option B2, §5–6).** Make Filigree 4.0 the work-state authority *only*. It links work to code through **opaque, typed evidence references** (path, symbol, finding fingerprint, commit, and SEI when available), each with a snapshot taken at attach time. It holds no copies of code facts.

**Each producer owns its facts end to end.** That covers the finding, the run telemetry, and the accept/suppress verdict, which is versioned in the repo. A producer hands Filigree a work candidate through one idempotent **promote** seam and later posts **evidence-state updates** ("gone at commit X").

**No product aggregates code facts.** This is a deliberate change from the "make Loomweave the code-facts hub" shape I started with. The decisive input is Loomweave's adoption and reliability record: the recommended boundary must not depend on Loomweave surviving its own kill-date experiment.

The memo's *verdict* ("do not rebuild") contradicts the owner's direction ("rebuild everything"). This report uses only the memo's *measurements*: no active consumer, adoption share against `rg`, and two silent outages. B2 was chosen to hold under either ruling on Loomweave.

**Archived members.** Legis, Warpline and Tabard are not rebuilt as products. Their few durable ideas become specs or small features (§4). The hub is replaced by a small, runtime-free contracts package.

---

## 1. First principles: the question each product answers

A capability belongs to the product whose question it answers *and* that holds the data needed to keep the answer correct.

| Product | Core question | Facts it can keep correct alone | Facts it cannot keep correct |
|---|---|---|---|
| **Filigree** | "What work exists, who holds it, what is next, and what happened to it?" | Issues, workflow state, claims/leases/heartbeats, dependencies, plans, comments, events, observations (agent notes), and what was *asserted* at claim or close (reason, commit, evidence references) | Anything that changes when the **code** changes without a Filigree write: file identity, line anchors, whether a finding still exists, entity drift |
| **Loomweave** | "What is this code, where is it defined, what calls it, and what is its durable identity?" | Entities, edges, subsystems, SEI and lineage, index freshness relative to a commit, and its own structural/secret findings | Work state; the acceptance verdict on another tool's finding; anything needing a live peer it does not control |
| **Wardline** | "Does this code honour its declared trust boundaries, and does it pass the gate?" | Rules, the taint lattice, findings at a commit, run telemetry, and suppression verdicts (baseline, waiver, judged) stored **in the repo** beside the code | Work state; entity identity across refactors (it consumes SEI) |

The archived hub doctrine gave Filigree "finding triage state" and "finding lifecycle" (`/mnt/data/archive/weft/doctrine.md` §2, §6). Wardline's Finding record repeats the assignment: "finding *lifecycle* … is Filigree's domain" (`wardline/src/wardline/core/finding.py:4-7`). Section 3 shows that this single line of doctrine is the root of the largest misallocation in the suite.

---

## 2. Capability inventory

**A/D column:**
- **A**: authoritative (the only source of the fact).
- **D**: derived (a copy or re-computation of a fact owned elsewhere, or re-derivable from code or git).
- **D!**: derived *and* held by an owner with no refresh path, so stale by construction.

**Usage column:** MCP `tool_call` counts across the 15 local `filigree.log` files (5,485 calls, 2026-03 to 2026-10), grouped by capability cluster. CLI calls and sibling HTTP calls are not logged (see §8).

### 2.1 Work state (Filigree core)

| Capability | Owner | Where | Consumers | A/D | Usage |
|---|---|---|---|---|---|
| Issues, types, workflow state machines, packs | Filigree | `db_issues.py`, `db_workflow.py`, `templates.py`, `templates_data.py` | Agents (MCP/CLI), dashboard, siblings (`/api/weft/issues`) | A | The top 8 tools carry 81% of all calls |
| Claims, leases, heartbeats, reclaim | Filigree | `db_issues.py:1521` `claim_issue`, `:1600` `release_claim`, `:2108` `claim_next`, `:2194` `start_work` | Agents | A | `start_work` is in the top 8 |
| Dependencies, ready queue, critical path | Filigree | `db_planning.py:221`, `:350` `get_ready`, `:441` `get_critical_path` | Agents, dashboard | A | `add_dependency` is in the top 12 |
| Planning (milestone, phase, step) | Filigree | `db_planning.py:831` `create_plan` | Agents | A | Nearly all `plan_*` tools: 0 calls |
| Events, change feed, undo | Filigree | `db_events.py:302`; `/api/weft/changes` (`dashboard_routes/analytics.py:575`) | Agents, siblings | A | Low |
| Observations (agent notes for triage, 14-day TTL) | Filigree | `db_observations.py:230`, `:895`, `:1051` | Agents; Loomweave's guidance proposals | A | 221 calls (4.0%), the only non-core surface in real use |
| Orientation snapshot (`context.md`, session context) | Filigree | `summary.py:74` | Agents | D (from its own A data) | `session_context` is in the top 12 |
| Claim/close commit anchors (`branch@sha`, caller-supplied, opaque) | Filigree | `db_issues.py:803-845`, `:1038-1075` | Warpline (archived); not shown in the dashboard | A (as an assertion) | On every claim and close |
| Actor on writes | Filigree (self-asserted per call; `verified_actor` = the OS user) | `actor_identity.py`; MCP critique F10 | All writes | A (weak) | `actor` is on 61 of 118 tools |

### 2.2 Findings: three producers, three lifecycle stores, one suppression vocabulary that does not reach the gate

| Capability | Owner | Where | Consumers | A/D | State |
|---|---|---|---|---|---|
| Taint findings and engine telemetry | Wardline | `core/finding.py:68-73` (`Kind`: defect, fact, classification, metric, suggestion); `ENGINE_PATH="<engine>"` (`:27`); telemetry from `scanner/diagnostics.py:50-105` and `scanner/taint/propagation.py:557-563` | Wardline CLI/MCP; Filigree; Legis and Plainweave (archived) | A (at a commit) | Wardline's own self-scan: 6,318 findings, **zero policy defects** |
| Suppression verdicts: baseline, waiver (reason required, expiry optional), judged (LLM FALSE_POSITIVE) | Wardline | `.weft/wardline/{baseline,waivers,judged}.yaml` (`core/paths.py:77-86`); precedence waiver > judged > baseline (`core/finding_identity.py:44-64`); only DEFECT is suppressible (`core/suppression.py:99-101`) | The Wardline gate; Filigree (copied into `metadata.wardline.suppression_state`) | **A** | No owner or approver field; "No governance" (`waivers.py:10`) |
| Emit to Filigree | Wardline | `core/filigree_emit.py:103-133` ("Emits ALL finding kinds"); severity map `core/finding.py:265-271` (INFO→`low`, NONE→`info`); URL auto-discovered, so **on whenever a Filigree daemon is up** (`core/config.py:413-528`) | Filigree `POST /api/weft/scan-results` | — | Fail-soft |
| Finding store and lifecycle (open, acknowledged, fixed, false_positive, unseen_in_latest) | **Filigree** | `scan_findings`: no `kind` column; severity CHECK allows defect severities only. `db_files.py:1586` `process_scan_results`, `:2027` `update_finding`, `:2317` `clean_stale_findings`, `:2507` `list_findings_global` | Agents (17 `list_findings` calls ever), dashboard Files view, `/api/weft/findings`, Loomweave Flow B | **D!** | 9,973 rows here; 9,955 are WLN telemetry; 3 linked, 4 fixed, 11 false-positive |
| Promote finding → issue; close-on-fix cascade; reconciliation debt | Filigree | `db_files.py:2649`, `:2969`; `finding_issue_cascade.py:55`; ingest closes linked issues on a resolving rescan (`db_files.py:1794`) | Agents; Wardline `core/filigree_issue.py:32-67` (promote-by-fingerprint; needs prior ingest, else 404) | A (the issue) + D! (the link state) | `finding_promote*`: 0 MCP calls; ~8 links fleet-wide out of ~25k findings |
| Manual finding report | Filigree | `finding_report` (ADR-007) | Agents | A | 0 calls; overlaps observations |
| Structural, secret and lifecycle findings | Loomweave | `findings` table with **its own lifecycle**: `status ∈ {open, acknowledged, suppressed, promoted_to_issue}` plus `filigree_issue_id` (`crates/loomweave-storage/migrations/0001_initial_schema.sql:104-135`); written only by `analyze` | Agents via `entity_finding_list` / `project_finding_list`; optional push to Filigree (`emit_findings`, **default false**, `crates/loomweave-federation/src/config.rs:1068-1096`) | A (re-derived per analyze) | Filigree's index has 4 rows, including an `LMWV-SEC-SECRET-DETECTED` ERROR on `.env` that never reached Filigree. Loomweave's own repo: 144 rows, all open, none linked |
| Dead code, cycles, coupling hotspots | Loomweave | Computed at query time, never stored (`catalogue/shortcuts.rs`) | Agents | D | — |
| LLM bug-hunt scanner runner | **Filigree** | `scanners.py`, `scanner_runtime.py`, `bundled_scanners.py`, `scanner_prompts.py`, `scanner_reporting.py`, `scanner_scripts/` (`scan_utils.py` 973 lines); `mcp_tools/scanners.py:818`, `:1072` | Agents | A (scan runs) | 4 runs ever (May 2026); every `scan_*` / `scanner_*` tool: 0 calls |
| LLM false-positive judge | Wardline | `core/judge_run.py:123-147` → `judged.yaml` | The Wardline gate | A | A second LLM-in-the-loop analysis feature in the suite |

### 2.3 Files

| Capability | Owner | Where | Consumers | A/D | State |
|---|---|---|---|---|---|
| File registry (Filigree-minted ids, path, language, content hash) | **Filigree** (`local`), or delegated to Loomweave (`registry_backend=loomweave`, ADR-014) | `db_files.py:415` `register_file`; `registry.py:1023` `LocalRegistry`, `:1064` `LoomweaveRegistry` (1,915 lines including the SEI resolve/lineage client); Loomweave serves `/api/v1/files*` (`crates/loomweave-cli/src/http_read.rs:678-750`) | Findings, file associations, annotations, dashboard | **D!** under `local` | **0 of 15** local projects use `loomweave`, so ADR-014's displacement never happened. `<engine>` is registered as a "file" |
| File/module entities with SEI | Loomweave | Catalog `core:file:*`, `python:module:*` | SEI consumers | A | 11,995 entities in Filigree's index |
| File ↔ issue associations | Filigree | `db_files.py:3211` | Agents, dashboard | A (link), D! (path) | 2 calls |
| File timeline (finding and association events; **not** git history) | Filigree | `db_files.py:3409` | Dashboard, agents | D | Low |
| Code-health views (hotspots, severity donut, scan coverage) | Filigree dashboard | `db_files.py:3297`; `static/js/views/health.js`, `files.js` | Humans | D! | Top hotspot is `<engine>` (8,850 rows); scan data 141 days old with no warning (UX review) |

### 2.4 Annotations and guidance

| Capability | Owner | Where | Consumers | A/D | State |
|---|---|---|---|---|---|
| File/line annotations with git provenance and anchor drift (`current`, `line_drifted`, `content_changed_anchor_found`, `stale`, `file_missing`); intents: explanation, warning, breadcrumb, hypothesis, decision, handoff, gotcha | **Filigree** | `db_annotations.py` (1,280 lines): `_run_git` `:188`, provenance `:201`, `_compute_annotation_anchor_state` `:373`, `carry_forward_annotation` `:1103`, close-out warnings on issue close `:1229`; `types/core.py:82-86` | Agents (13 tools), the close path | A (note), **D!** (anchor) | 7 rows fleet-wide; 1 logged call |
| Guidance sheets (entity-anchored, scope-ranked) | Loomweave | `crates/loomweave-storage/src/guidance.rs:160-198`; `propose_guidance` writes a **Filigree observation** over `filigree-mcp` stdio, and `promote_guidance` reads it back (`crates/loomweave-mcp/src/lib.rs:2373-2602`) | Agents | A | 0 sheets in Loomweave's own repo; no relationship to Filigree annotations |

### 2.5 Identity and associations

| Capability | Owner | Where | Consumers | A/D | State |
|---|---|---|---|---|---|
| SEI mint/resolve/lineage (`loomweave:eid:` + blake3(locator ‖ mint_run_id)) | Loomweave | `crates/loomweave-storage/src/sei.rs:35-60`; HTTP `/api/v1/identity/*` | Filigree, Wardline; Legis, Warpline, Plainweave (archived) | A | Not deterministic across a from-scratch re-index (hub gap map NOTE-2). Today's memo proposes deleting "SEI signing/HMAC" and the federation HTTP API |
| Issue ↔ entity association (opaque id, `content_hash_at_attach`) | Filigree | `db_entity_associations.py:188`; classic routes `dashboard_routes/entities.py` | Loomweave `entity_issue_list`; Legis and Warpline (archived); **Plainweave re-implemented its own copy** | A (link), D (hash) | 10 rows fleet-wide; 1 MCP call |
| Locator → SEI backfill | Filigree | `sei_backfill.py` (673 lines) | Operator, once | D | Owner-gated one-off |
| Drift of governed bindings against live Loomweave hashes | Filigree | `governance.py:201` `_evaluate_current_drift` → `registry.py:1611` | The close gate | D | Only when `LEGIS_URL` is set |
| Issues for an entity (reverse join) | Loomweave reads Filigree | `crates/loomweave-mcp/src/tools/graph.rs:612-780` → `GET /api/entity-associations?entity_id=`, plus `GET /api/weft/issues/{id}` | Agents | D (read-through) | Fails open with `result_kind:"unavailable"`. **Live this session:** issues `no_matches`, and the Wardline section `unavailable` (Filigree HTTP 401) |
| Wardline findings joined onto entities ("Flow B") | Loomweave reads **Filigree's copy** of Wardline output | `crates/loomweave-federation/src/filigree.rs:745-781`; `crates/loomweave-mcp/src/wardline_reconcile.rs` (byte-equal `metadata.wardline.qualname`) | `entity_issue_list`, orientation pack, `entity_wardline_list` | D of D! | `WLN-L3-LOW-RESOLUTION` rows have a **null qualname**, so ~90% of the Filigree volume can never bind |
| Wardline taint-fact store (opaque blobs) | Loomweave hosts, Wardline writes | `/api/wardline/taint-facts*` (ADR-036); `wardline/src/wardline/loomweave/client.py:275-430` | `entity_wardline_get` | A (Wardline content) | 1,628 rows written in one burst on 2026-07-12, never since |

### 2.6 Git provenance, change impact, governance

| Capability | Owner | Where | Consumers | A/D | State |
|---|---|---|---|---|---|
| Git branches, commits, PRs, CI checks, rename feed; graded enforcement; override-rate gate; sign-off ledger | Legis (archived) | Legis `api/app.py`, `mcp.py`, five DBs under `.weft/legis/` | Filigree close gate, Loomweave rename feed, agents | A (verdicts, checks); D (git) | Archived |
| Staged/committed rename detection for SEI lineage | Loomweave | `crates/loomweave-cli/src/sei_git.rs` (shells `git diff --cached -M`; the Legis feed is optional) | Loomweave matcher | D (from git) | Live |
| Index freshness and diff since last analyze | Loomweave | `crates/loomweave-mcp/src/index_diff.rs` | Agents, hooks | D | Live; the best freshness model in the suite |
| Recent change, high churn | Loomweave tools, **Warpline data** | `crates/loomweave-mcp/src/catalogue/shortcuts.rs:899-990`; `loomweave-storage/src/query.rs:1303-1306` ("does not populate `git_churn_count` in v1.0") | Agents | D (read-through) | **Dead.** The live call returns `reason: warpline-disabled` |
| Change events, timeline, churn, blast radius, reverify worklist | Warpline (archived) | Own Python AST extraction (`git.py:186-200`); dated *copies* of Loomweave edges; BFS blast radius (`propagation.py:43-75`) | Filigree, Loomweave, Legis | A: `change_events`, `co_change_pairs`; D: edge snapshots | Archived |
| Reverify worklist → Filigree work | Filigree | `warpline_consumer.py:106` | Agents | A (the issues) | 0 calls |
| Closure gate (a governed close fails closed when Legis is unreachable or 404) | Filigree + Legis | `governance.py:362` `evaluate_closure_gate`; `legis_client.py:139` (5 s urllib); also on the cascade path (`finding_issue_cascade.py:92-106`) | Every close surface | A (Legis verdict) | **Latent trap:** Legis is archived, so with `LEGIS_URL` set a governed issue cannot close |

### 2.7 Platform plumbing

| Capability | Owner | Where | Note |
|---|---|---|---|
| Multi-project daemon and project registry | Filigree | `server.py:131-258` (`~/.config/filigree/server.json`) | The brief named `registry.py`. That module is the *file-identity* backend, not project discovery. |
| Federation bearer token (3-tier, auto-minted) | Filigree; Loomweave and Wardline as clients | `federation_token.py`, `dashboard_auth.py`; Loomweave `filigree.rs:401-418`; Wardline `filigree/config.py:11-16, 79-103` | Three products each resolve the token their own way. The env-vs-file mismatch (HTTP review F14) is the likely cause of the live 401. |
| Endpoint discovery | Each product | `.weft/*/ephemeral.port`; `server.json`; Wardline `config.py:413-528` | Wardline `doctor` still keys on `"ethereal"` (`install/doctor.py:790-831`). Filigree 3.3 writes `"ephemeral"`. |
| HTTP generations (`classic`, `weft`) | Filigree | `generations/`, ADR-002 | `loom` was retired without its own ADR (HTTP review F3). |
| Dashboard | Filigree | `dashboard.py` + `dashboard_routes/` (~6k lines) + ~7.9k lines of JS | The UX review recommends a supervision console; the Files view is a static-analysis browser (an anti-persona). |
| Contract vocabulary (SEI standard, `weft-reason`, seam index, glossary) | Hub (archived) | `/mnt/data/archive/weft/{sei-standard.md, contracts/, glossary.md}` | No live home. Wardline and Filigree code comments cite dangling `~/weft/...` paths (Wardline `filigree_emit.py:248`). |

### 2.8 Where Filigree's code goes (`wc -l`, 66,693 Python lines + 7,875 JS)

| Cluster | Lines | Share | B2 disposition |
|---|---|---|---|
| Work state: issues, workflow, planning, events, templates, and their MCP/CLI/HTTP | ~19,400 | 29% | Keep and rebuild (with the MCP/HTTP critiques' fixes) |
| Findings, files, scanners: store, ingest, runner, and their MCP/CLI/HTTP | ~13,200 | 20% | **Remove**; replace with references, promote and state updates (~1.5–2.5k) |
| Federation glue: Loomweave registry client, SEI backfill, associations, governance/Legis, Warpline consumer, token/auth, generations, server mode | ~6,900 | 10% | Keep server mode, token and generations. **Remove** the registry client, backfill, governance and Warpline consumer (~3.6k) |
| Observations | ~2,700 | 4% | Keep |
| Annotations | ~2,200 | 3% | **Remove** |
| Remainder: install, doctor, hooks, migrations, meta, types, dashboard shell | ~22,300 | 33% | Keep; shrinks with the surface |

---

## 3. Overlaps, gaps and misallocations

Each placement below is re-derived from the job in §1, not from the comments that defend today's placement.

### M-1. The work tracker stores analyzer telemetry as defects (misallocation and wire defect)

- **The emitter sends everything.** Wardline emits every `Kind`, including `metric` and `fact` (`core/filigree_emit.py:103-133`). It maps `INFO` to `low` and `NONE` to `info` (`core/finding.py:265-271`), even though the code itself says NONE means "facts / metrics carry no defect severity" (`:65`).
- **Filigree cannot tell the difference.** `scan_findings` has no `kind` column, and its severity CHECK admits defect severities only. A resolver metric and a low-severity defect are structurally identical. `<engine>` becomes a row in `file_records`.
- **Filigree's own tracker** (read-only copy, 2026-10-07):

| Rule | Rows | What it is |
|---|---|---|
| `WLN-L3-LOW-RESOLUTION` on `<engine>` | 8,850 | A per-function "N% unresolved calls" metric with a null qualname |
| `WLN-ENGINE-UNKNOWN-IMPORT` | 1,100 | Engine fact |
| Other `WLN-ENGINE-*` | 5 | Engine facts and metrics |
| `PY-WL-101` (a real taint rule) | 2 | Defect (both fixed) |
| agent / codex / claude / mcp-review scratch | 16 | Probes and early LLM-scanner output |

- **Wardline already knows this is noise.** It calls the LOW-RESOLUTION stream "INFO-severity … noise, exactly the severity an agent filters out" (`core/resolution_posture.py:8-9`). The same file explains why there are no defects: a codebase with no declared trust boundaries "produces ZERO defects no matter what it does". So for the owner's own repos, the Wardline → Filigree pipeline carries essentially no work.
- **Fingerprint churn makes it worse.** The engine fingerprint is `sha256(rule_id, message)` (`scanner/diagnostics.py:102`). For LOW-RESOLUTION the message embeds the ratio, e.g. `"…has {pct}% unresolved calls ({unres}/{total_calls})"` (`scanner/taint/propagation.py:557-563`). Every call-count change therefore mints a new row.

**Placement.** Run telemetry is a property of the analysis run. It belongs in the producer's run report. A tracker has no business holding it, and the fix is not to "add `kind` to Filigree". The seam was specified as "findings → Filigree" with no notion of a *work candidate*, and that spec is the defect. This is cross-product wire work, not a deferred P3.

### M-2. Four notions of "this finding is accepted", none synchronised

1. **Wardline:** baseline, waiver and judged files in `.weft/wardline/`. This is the only one the CI gate reads (`run.py:603-626`).
2. **Filigree:** `status` (`false_positive`, `acknowledged`) beside a copy of Wardline's verdict in `metadata.wardline.suppression_state`. Contract F7 declares the read defaults "surface-asymmetric — by design" (`docs/federation/contracts.md:631-650`).
3. **Loomweave:** its own `status` (`acknowledged`, `suppressed`, `promoted_to_issue`) plus `filigree_issue_id` (migration 0001). The Filigree → Loomweave feedback loop is deferred as NG-17.
4. **Legis (archived):** graded enforcement cells over Wardline findings.

Nothing flows back from Filigree:
- There is no Wardline code that reads Filigree status (grep over `wardline/src` for `false_positive`, dismissals and the findings read routes).
- `wardline rekey` re-emits under the new scheme and lets the old fingerprints sweep as unseen. It says "there is no remap endpoint", so Filigree statuses and issue links on the old fingerprints are lost (`core/rekey.py:835-880`).

The consequences: a finding dismissed in Filigree still trips `wardline scan --fail-on ERROR` in CI, and a finding waived in Wardline shows as open work wherever a surface forgets the suppression filter.

**Placement.** Acceptance is a decision about code. It must be versioned with the code, reviewed in the PR and read by the gate. Wardline's files already do all three. Every triage outcome maps to exactly one of two existing homes:
- "Not a real problem" → producer suppression (a waiver with reason and expiry, or a baseline).
- "Real, needs work" → a Filigree issue.

No third (or fourth) lifecycle store is needed. The "asymmetric by design" comment documents the split-brain; it does not resolve it.

### M-3. A stock with no outflow (Shifting the Burden)

The findings table is a stock with a large inflow:
- About 6,000 engine rows are upserted per scan (6,144 `<engine>` rows have `last_seen_at` 2026-09-02).
- Fingerprint churn adds more (M-1).

Its outflows all fail for this population:
1. **The absent-fingerprint sweep runs per *scanned path*.** `<engine>` is never a scanned path, so about 2,700 engine rows from June can never become `unseen_in_latest`. The sweep is also switched off whenever an incomplete-analysis rule fires, and on delta scans (`filigree_emit.py:122-128`; `cli/scan.py:471-484`).
2. **`clean_stale_findings`** (`db_files.py:2317`, ADR-015) only drains rows that are already `unseen_in_latest`.
3. **Triage needs attention, and noise destroys attention.** The dashboard's top hotspot is `<engine>`. `list_findings` was called 17 times ever and `finding_promote` never.

The symptomatic fix ("mirror everything into the tracker and triage it there") displaced the fundamental one: the producer emits only work candidates and owns acceptance in the repo. Feature growth then continued on the symptomatic branch (dossier, promote-and-attach, clean-stale, suppression filters, reconciliation debt), and none of it was gated on an outcome reading (product critique P-3).

The leverage point is a **rule** (who may hold the stock), not a parameter. Retention windows will not drain it.

### M-4. The tracker maintains derived code facts: files and annotation anchors

- **File registry.** Filigree mints native file ids for every path. ADR-014's displacement to Loomweave identity is unused in all 15 projects. Every project therefore carries a shadow identity that does not follow renames, which ADR-014's own context calls "two unrelated identities for the same code".
- **Annotations.** They shell out to git (`db_annotations.py:188`) and run an anchor-drift state machine (`:373`) that re-implements what SEI plus a content hash give a code index. They also add close-out warnings to issue close (`:1229`). There are 7 rows fleet-wide.
- **File timeline** (`db_files.py:3409`) is Filigree's own finding and association events. It inherits M-1's noise and is not code history.

**Placement.** The annotation intents (`types/core.py:82`) split cleanly:
- *Code knowledge* (explanation, warning, gotcha) belongs with the code: comments, repo docs, or Loomweave guidance if Loomweave survives.
- *Work knowledge* (handoff, hypothesis, decision, breadcrumb) belongs on the issue as comments and observations.

Neither half needs a line-anchored, git-provenanced store inside the tracker.

This matches the LLM-experience review's proposal (R4) to collapse observation and annotation into a single **note** with `ttl` and `critical`/`must_consider` flags. Under B2 such a note may carry `path` or `symbol` evidence references (S-1) with a snapshot, but it carries no anchor-drift machinery. That review's LX-09 (critical annotations surface only at `issue_close`, after the work is done) is a further argument for delivering notes on `work_start` and `issue_get` rather than through a separate file-anchored store.

### M-5. A scan orchestrator inside the tracker

Filigree spawns `codex` and `claude` bug-hunt processes from a TOML registry with prompt packs, and ingests their output (§2.2). It has 4 runs ever and 0 tool calls. Wardline separately has an LLM judge (`core/judge_run.py`). The vision says "scanning is Wardline … it composes, it does not annex".

**Disposition.** Kill it in 4.0. An LLM bug hunter is a producer: if one is wanted, it emits observations or promote calls.

### M-6. Loomweave: a sound core, hollow reach-ins, and no consumer

- **Hollow tools.** `entity_recent_change_list` and `entity_high_churn_list` proxy to Warpline only (`catalogue/shortcuts.rs:899-990`); live they return `warpline-disabled`. `entity_todo_list` is a tag lookup that no plugin populates (live: `unsupported`).
- **Flow B joins a copy of a copy.** Loomweave reads Wardline findings *from Filigree's mirror* (`filigree.rs:745-781`), joins them on a qualname that is null for the bulk rows, and crosses an auth seam that failed live (HTTP 401).
- **It has a third finding lifecycle store** (M-2), with Filigree emission off by default.
- **Adoption and reliability (today's memo, non-normative):**
  - "Loomweave has **no active consumer**" (memo:29). elspeth retired it on 2026-09-25; aurora removed it on 2026-10-05.
  - Share of lookups against `rg`: ≈0.7–0.8% while working, 0.18% while broken.
  - Two silent multi-week outages, including 42 analyze runs in September with none completing.
  - About a third of the source serves archived consumers.
  - The memo recommends a lean core: plugins, storage, ~6 queries, a CLI, and an edit-time hook. It would delete or feature-gate "the federation HTTP API, SEI signing/HMAC, guidance, Wardline taint, LLM summaries, worktree indexes and most catalogue tools" (memo:186-188).

**Reading.** Loomweave's *question* is right, and elspeth's probe lanes showed correct answers (callers 100% recall, `inherits_from` 147/147). It is the wrong place to *land* other products' facts:
- It is the least adopted and least reliable member.
- Its owner-side memo is shrinking it, not growing it.
- Its value reaches agents best by **push** (edit-time hook), not as yet another query server.

The Loomweave half of the owner's question therefore has this answer: keep the map, delete the reach-ins, and do not make Loomweave a hub.

### M-7. Governance and identity built for members that do not exist

- **Legis.** A governed close fails closed when Legis is unreachable (`governance.py` DECISION 2, `:16-24`), including on the cascade path. With Legis archived, setting `LEGIS_URL` wedges closes. This is an availability trap, not a security issue, and it is cheap to defuse in 3.x.
- **Tabard.** It is a 19-line name reservation. The real need is connection-bound agent identity (MCP critique F10): every stdio agent on the host currently has the same `verified_actor`.

### M-8. Capabilities implemented more than once across the suite

| Capability | Implementations | One home |
|---|---|---|
| Git rename/commit reading | Loomweave `sei_git.rs`, Legis `git/surface.py`, Warpline `git.py` | Loomweave (only it needs renames, for SEI lineage); agents use `git` directly |
| Python entity extraction | Loomweave, Warpline | Loomweave |
| Issue ↔ code link store | Filigree `entity_associations`, Plainweave's own copy | Filigree evidence references |
| Actor registry | Filigree, Plainweave `actors`, Legis operator keys, Warpline authors | A per-product connection identity plus one naming convention |
| Verification evidence | Legis checks DB, Warpline `verification_events`, Plainweave `verification_evidence` | Filigree close-evidence references (what was asserted) |
| LLM code triage | Filigree scanners, Wardline judge | Wardline judge |
| Finding schema | Wardline `Finding`, Filigree `scan_findings`, Loomweave `findings` | One producer-neutral **finding record** spec (S-8); stored only at producers |
| Federation token resolution | Filigree, Loomweave, Wardline (three chains) | One discovery/auth convention (S-6) |

### M-9. Gaps (nobody owns them)

1. **"The producer asserts this evidence is gone at commit X."** Today Filigree *infers* "fixed" from absence, only on full scans with complete analysis (`db_files.py:1794`). An explicit, state-based producer → tracker update is missing (S-3).
2. **"What work touches this file or symbol?"** Today this is answered by Loomweave reading Filigree. It should be answerable by Filigree alone from the references it holds (S-4), with no peer needed.
3. **A home for contracts.** The SEI standard, `weft-reason`, the seam index and the glossary sit in an archived repo.
4. **Agent identity at connection time.** No product binds "who is calling" at launch.

---

## 4. Archived members: disposition

| Member | What was real | Disposition | Re-home of the durable idea | Rationale |
|---|---|---|---|---|
| **Weft hub** (contracts, glossary, SEI standard, doctrine) | Docs only (73 KB conventions, 37 KB doctrine, a seam index, the `weft-reason` vocabulary) | **Do not revive the hub repo.** Replace it with a **`weft-contracts` package**: versioned markdown + JSON Schema + golden vectors, no runtime, no PM content | S-1…S-9 below; the 11-class `weft-reason` vocabulary; the SEI standard *only if* Loomweave keeps SEI | Doctrine §6 (no runtime, no store) was right. The hub drifted because it held PM narrative and restated member facts. Contracts need a neutral home that consumers vendor goldens from. |
| **Legis** (git/CI governance; ~20k src lines, v1.4.0) | Real: closure gate, sign-off ledger, graded cells, override-rate gate, git/CI recording | **Drop as a product** | (a) "close requires evidence" becomes a **Filigree workflow-pack policy**: local, no network, required fields such as evidence ref / commit / reason per type. Where a real gate is wanted, use the business-process review's provider-agnostic model (`human` or `separation_of_duties` gates with a visible `in_review` queue), with `external:<provider>` reserved. (b) "Govern a finding" becomes Wardline's waiver (reason, expiry, optional approver), reviewed in the PR. (c) Branch/commit/PR context: agents use `git`/`gh` directly. (d) Remove `governance.py`, `legis_client.py` and the governed-binding columns in 4.0. **In 3.x, make `LEGIS_URL` set-but-unreachable warn instead of wedge, or document unsetting it.** | Its only live consumer path in a remaining product is a fail-closed trap. Its value assumed a fleet with CI governance needs that the owner's current fleet does not show. |
| **Warpline** (temporal change impact; ~11.6k src lines, v1.3.0) | Real, but its extraction duplicated Loomweave and its edges were dated copies | **Drop as a product** | (a) The reverify worklist → Filigree becomes the generic **promote seam** (S-2) with `reference` = SEI/path: "file work for these references, dedupe by reference". (b) Churn and recent change: delete Loomweave's proxy tools; agents use `git log`. If Loomweave's lean core survives and churn proves useful, Loomweave computes it from its own per-commit runs. (c) Blast radius = Loomweave callers + `git diff`, if Loomweave survives. | The authoritative data it owned (`change_events`, co-change) is re-derivable from git. The rest was mirrored. Keeping it means a fourth server for an advisory signal with 0 Filigree-side uses. |
| **Tabard** (actor identity) | 19 lines; spec only | **Drop** | A **connection-bound actor convention** in `weft-contracts` (S-6): `--agent-id` at MCP launch, a header on HTTP, the per-call `actor` becomes an override, and mutations without an identity are refused. Each product implements it locally. | The need (distinguish agents) is real; the crypto certification product was never needed for deconfliction. |
| **Plainweave** (requirements ↔ code intent; ~14.5k lines) | Real, but its own `entity_associations` and `actors` duplicated Filigree | **Stays archived** | If revived, it binds work through Filigree evidence references (a `requirement` reference kind) rather than a private copy | Out of the owner's live set. |
| **Lacuna** (demo specimen) | Specimen + tour harness (~7.7k lines incl. tests) | **Keep as a test corpus**, not a product | The cross-product **conformance and e2e fixture** for S-2/S-3/S-4 (Wardline+Filigree pair, Loomweave-absent) | Cheap, already exercises every seam, and "degrades honestly" when a tool is missing. |

---

## 5. Boundary options

**The two discriminating questions:**
1. **Where does the accept/suppress verdict live?**
2. **What key does the tracker hold?**

**Agent-UX yardstick.** The agent wants few tools and one place to ask *per question*. Today the agent sees 118 (Filigree) + 48 (Loomweave) + 18 (Wardline) = **184 tools**, with findings answerable in three places and files in two.

### Option A: today's allocation, cleaned up

- **Shape.** Filigree keeps the findings store, file registry, annotations and scanners. The fixes:
  - Add `kind` to the wire and the store, and accept only `defect` into the work-facing view.
  - Fix the severity map.
  - Sweep `<engine>` and stabilise engine fingerprints in Wardline.
  - Add a Filigree → Wardline write-back of `false_positive` as a waiver, giving one verdict.
  - Make `registry_backend=loomweave` the default when Loomweave is present.
- **Seams.** `scan-results` ingest (bulk mirror); a new verdict write-back (Filigree → Wardline files, which crosses a repo-write boundary); Flow B stays.
- **Peer down.**
  - Wardline down: no new findings; the mirror goes stale silently (no freshness model).
  - Filigree down: the scan continues and findings are lost for the tracker until the next scan.
  - Loomweave down: the registry fails closed under `loomweave` (today's ADR-014 behaviour).
- **Agent UX.** ~76 Filigree tools (MCP critique's consolidated catalog) + 48 + 18 ≈ 142. Findings still answerable in two places.
- **Solo install.** A Filigree-only user still sees Files/Findings/Annotations/Scanners surfaces that are empty without a producer.
- **Effort.** Largest Filigree rebuild (~13k findings/files lines re-implemented), plus a new write-back seam that makes Filigree write into the repo. Wardline: S. Loomweave: none.
- **Verdict.** Fixes the symptoms and keeps the misallocation. The write-back puts the tracker in the business of authoring repo files. **Rejected.**

### Option B1: Filigree is pure work state; Loomweave becomes the code-facts hub (SEI-keyed)

- **Shape.**
  - Filigree holds SEI-keyed associations only.
  - Loomweave ingests Wardline findings at index time and anchors them to entities.
  - Loomweave serves every finding query, absorbs annotations into guidance, and is the file registry.
- **Seams.** Wardline artifact → Loomweave ingest; Loomweave/Wardline → Filigree promote; Filigree stores SEI.
- **Peer down.** Loomweave down → no findings view at all, and SEI-keyed references in Filigree cannot be displayed meaningfully. **A Filigree-only install has no key to use**, because SEI requires Loomweave.
- **Agent UX.** "One place to ask about code" on paper (~52 + ~55 + 18 ≈ 125 tools). In practice it depends on agents adopting Loomweave, which the memo shows they do not (0.18–0.8%).
- **Effort.** Filigree −20k. Loomweave +6–10k Rust (cross-producer ingest, verdict reflection, registry authority, guidance migration), against its own lean-core memo. Wardline M.
- **Verdict.** Right in principle (code facts at the code owner) but bets the suite on its least-adopted, least-reliable member, and breaks Filigree's solo mode. **Rejected.**

### Option B2: Filigree is pure work state; producers own their facts; opaque typed references (RECOMMENDED)

- **Shape.**
  - Filigree stores **evidence references** on issues: `{kind ∈ path | symbol | finding | commit | sei | url, producer?, value, scheme?, snapshot{title, rule_id, severity, message, path, line, at_commit}, state, state_at}`. Filigree never interprets `value`.
  - Wardline owns its findings, telemetry and verdicts in the repo, and serves its own finding queries (`scan`, `scan_file_findings`, `findings`).
  - Loomweave owns its own findings and map.
  - A producer **promotes** a work candidate (S-2) and posts **state updates** (S-3). Filigree answers "what work touches X" from its own references (S-4).
  - No product aggregates other products' facts.
- **Seams.** S-1…S-9 (§7). The bulk mirror, Flow B, the file registry, clean-stale and the Warpline ingest are retired.
- **Peer down.**
  - Filigree down: promote fails soft and the finding stays in Wardline's artifact. The next scan re-promotes idempotently (keyed on producer + scheme + fingerprint).
  - Wardline down: references show their snapshot with `state_at` age, and `state: unknown` beyond a freshness window.
  - Loomweave down: `sei` references show their snapshot (path and symbol at attach). Nothing else is affected.
- **Agent UX.**
  - Filigree ~52 tools: the MCP critique's 76, minus 27 finding/file/annotation/scan/scanner/Warpline/association tools, plus ~3 reference tools.
  - Wardline 18; Loomweave ~6–10 (lean core).
  - **≈ 80 tools in total, down from 184.** Each question has exactly one owner:
    - "What should I do / what am I doing / what touches this file?" → Filigree.
    - "Is this safe / does it pass the gate / why is it flagged?" → Wardline.
    - "What calls this / where is it defined?" → Loomweave or `rg`.
  - The one remaining cross-product want, "context for the symbol I am editing", is met by **push** (an edit-time hook composing Wardline defects + callers + open work on the path) rather than by a new query surface. The Loomweave memo independently recommends this delivery model.
- **Solo install.**
  - Filigree alone: a complete agent tracker; `path`/`symbol`/`commit` references need no peer.
  - Wardline alone: unchanged and complete.
  - Wardline + Filigree: promote + state updates, with no Loomweave mediator. This finally retires the old A-1 pipeline-coupling asterisk.
- **Effort (relative; assumes the 4.0 ground-up rebuild already budgets the work-state core).**
  - Filigree: do **not** rebuild ~19k lines (findings/files/scanners, annotations, governance, Warpline, registry client, backfill); build ~1.5–2.5k (references, promote, state update, reverse lookup, migration export).
  - Wardline: S–M, ~1–2k. Promote-with-payload, a state-update emitter, the bulk emit removed, telemetry kept in the run report, stable engine fingerprints.
  - Loomweave: net deletion. Remove the Flow B, churn proxy, Filigree emit and SARIF → Filigree code.
  - **Lowest total of all options.**
- **Verdict.** Recommended (§6).

### Option C: a separate findings/evidence service

- **Shape.** A new member owns the finding lifecycle across producers (dedupe, triage, verdicts, evidence for closes). Filigree and the producers both integrate with it.
- **Peer down.** The service down → no triage anywhere, and producers must buffer.
- **Agent UX.** A fourth server and ~20–30 more tools. That is the opposite of "few tools".
- **Effort.** A new product (~8–15k lines) plus three integrations.
- **Verdict.** Clean authority, but no demand to pay for it: about 8 findings were ever linked to work, fleet-wide. Wardline's repo files already *are* the verdict store. **Rejected now.** Revisit if a second real producer with real defect volume appears and cross-producer dedupe becomes a measured need.

### Option D: merge Filigree and Loomweave into one "agent workbench"

- **Shape.** One server and one store for work plus code map, giving "one place to ask" literally.
- **Peer down.** Not applicable inside the merged product; Wardline as before.
- **Agent UX.** One server (~60–70 tools) plus Wardline. The best literal fit to "one place".
- **Solo install.** A tracker user must install a Rust indexer with a 500k-entity cap and hour-long analyze runs. elspeth's and aurora's removal pattern (Loomweave dropped, Filigree kept in aurora) predicts that external users would reject the bundle.
- **Effort.** XL: two languages, two persistence models (transactional work state versus a rebuildable index), and two release cadences.
- **Verdict.** **Rejected.** It couples the adopted product to the unadopted one.

### Option E: one MCP front door (gateway) over separate products

- **Shape.** A thin proxy server exposes a curated subset of all three products' tools under one connection.
- **Peer down.** The gateway becomes the shared runtime doctrine §6 forbids, and a single point of failure.
- **Agent UX.** One connection, but the tool count is unchanged unless curated. It hides, rather than removes, duplicate answers.
- **Effort.** M, plus a new runtime to operate.
- **Verdict.** **Not now.** B2 removes the duplicates that a gateway would only hide. Revisit only if measured multi-server friction remains after B2.

### Comparison

| | A cleanup | B1 Loomweave hub | **B2 producer-owned + references** | C evidence service | D merge | E gateway |
|---|---|---|---|---|---|---|
| Verdict lives | Two places + write-back | Producer (reflected in Loomweave) | **Producer (repo)** | New service | Merged store | Unchanged |
| Tracker key | File id + fingerprint | SEI | **Opaque typed ref** | Service id | Internal | Unchanged |
| Filigree solo | Cluttered | Broken (no SEI) | **Complete** | Complete | Heavy | Complete |
| Wardline+Filigree pair | Yes | Via Loomweave | **Direct** | Via service | Direct | Via gateway |
| Depends on Loomweave surviving | No | **Yes** | No | No | **Yes** | No |
| Agent tools (all servers) | ~142 | ~125 | **~80** | ~110 | ~85 | 184 (hidden) |
| Rebuild effort | L | L | **S–M net** | L | XL | M |
| Reversibility | Easy | Hard | Moderate | Hard | One-way | Easy |

---

## 6. Recommendation and ADR draft

**Recommended boundary, one sentence per product:**

- **Filigree 4.0** is the agent work-state authority: issues, workflow, claims/leases, dependencies, plans, events, observations and close evidence. It links work to code **only** through opaque, typed evidence references with snapshots. It stores no code facts and calls no peer on its core path.
- **Wardline** owns its findings end to end: production, run telemetry, and the accept/suppress verdict in the repo. It serves its own finding queries and hands Filigree only defects that need work, via promote and state updates.
- **Loomweave** is the code map (entities, edges, freshness; SEI only if its lean core keeps it), delivered by push as well as query. It owns its own structural findings, makes no reach-ins to peers, and is never a required dependency.

### Decision drivers referenced by the ADR

- **OWN-1:** agent-first; few tools and one place to ask per question (owner, 2026-10-07).
- **OWN-2:** rebuild everything; no keeping old surface "to save work".
- **OWN-3:** seams are kept, with fixed specs and rewrites where justified.
- **OWN-4:** external users may install a single product.
- **EVD-1:** 95% of Filigree MCP calls are work state; findings, files, annotations and associations together are under 1%.
- **EVD-2:** 9,955 of 9,973 stored findings are telemetry; 3 are linked to work.
- **EVD-3:** Loomweave has no active consumer and two silent outages (memo).
- **EVD-4:** four unsynchronised acceptance stores (M-2).
- **[COST]:** rebuild effort and run cost, in lines of code not rebuilt or newly built.

---

### ADR-F4-001: Filigree 4.0 holds work state and evidence references only; code facts stay with their producers

- **Status:** Proposed (owner gate: this deprecates used features and deletes data; see "Owner gates")
- **Date decided:** pending owner ruling (drafted 2026-10-07)
- **Decision makers:** product owner (John); Filigree, Wardline and Loomweave maintainers (agent fleet, actor `claude-filigree`)
- **Reversibility:** Moderate (1–6 engineer-weeks). Findings could be re-mirrored later through a new ingest generation, but the 3.x findings data is exported, not carried.
- **Blast radius on rollback:**
  - Filigree's MCP/HTTP/CLI surfaces.
  - Wardline's emitter.
  - Loomweave's Flow B and `entity_issue_list`.
  - The dashboard Files view.
  - Any external script calling `/api/weft/scan-results` or `/api/weft/findings`.
- **Next review date:** 2027-04-07, or sooner if a reversal trigger fires.
- **Hard expiry:** N/A
- **Recorded post-hoc:** No

#### Context

Filigree 3.3 holds a findings warehouse, a file registry, line-anchored annotations, an LLM scanner runner and a fail-closed Legis gate. These are about 30% of its code and under 1% of its MCP use. The warehouse is 99.8% analyzer telemetry. Its lifecycle cannot be kept correct by Filigree (M-1, M-3) and is not synchronised with the producer's verdict store, which is the one CI reads (M-2).

The hub doctrine that assigned "finding lifecycle" to Filigree assumed a live federation that has since been archived. Loomweave, the natural code-facts home, has no active consumer (EVD-3). The 4.0 rebuild is the cheapest moment to re-cut: the owner has already budgeted a breaking major.

#### Decision drivers

- **DRIVER-1 [OWN-1]:** each question has one owner; total agent-visible tools fall from 184 to about 80.
- **DRIVER-2 [OWN-4]:** each product is complete alone, and the Wardline+Filigree pair works without a mediator.
- **DRIVER-3 [EVD-2, EVD-4]:** a fact lives with the party able to keep it correct; one acceptance verdict.
- **DRIVER-4 [EVD-3]:** the boundary must not depend on Loomweave surviving its kill-date experiment.
- **DRIVER-5 [OWN-3]:** existing seams are rewritten as explicit, small contracts (S-1…S-9).
- **DRIVER-6 [COST]:** minimise net rebuild lines and permanent contract tax.

#### Alternatives considered

The full trade-offs are in §5. Summary:

- **Option A (cleanup).** Fails DRIVER-3 (two verdict stores plus a repo write-back) and DRIVER-1. Cost: L (Filigree findings rebuild). Reversibility: Easy. *Rejected.*
- **Option B1 (Loomweave hub).** Fails DRIVER-2 (Filigree solo has no key) and DRIVER-4. Cost: L in Loomweave, against its memo. Reversibility: Hard. *Rejected.*
- **Option B2 (producer-owned + references).** Meets all six drivers. Cost: S–M net (≈19k Filigree lines not rebuilt; ≈1.5–2.5k built; Wardline ≈1–2k; Loomweave net deletion). Reversibility: Moderate. **Chosen.**
- **Option C (evidence service).** Fails DRIVER-1 (a fourth server) and DRIVER-6. Cost: L. *Rejected now.*
- **Option D (merge Filigree and Loomweave).** Fails DRIVER-2 and DRIVER-4. Cost: XL. One-way. *Rejected.*
- **Option E (gateway).** Fails doctrine §6 (no shared runtime). It does not remove duplicate answers. Cost: M. *Deferred.*

#### Decision

**We will make Filigree 4.0 a work-state authority whose only link to code is an opaque, typed, snapshotted evidence reference, and leave every code fact (findings, telemetry, verdicts, file identity, code annotations) with its producer, because that is the only cut that is solo-complete, removes duplicate answers, and does not depend on Loomweave's survival.**

The decision covers: Filigree's schema, MCP, CLI and HTTP surfaces; Wardline's Filigree emitter; Loomweave's Filigree and Warpline reach-ins; the contracts in §7.

It does not cover: Loomweave's internal lean-core decision (its own memo and kill date); the MCP/HTTP envelope fixes (sibling reviews); the dashboard redesign (UX review), except that the Files/health view is removed.

#### Consequences

**Positive**
- About 19k lines of Filigree are not rebuilt. The catalog drops by ~27 tools (DRIVER-6, DRIVER-1).
- One acceptance verdict, read by the CI gate (DRIVER-3).
- A Filigree-only install is a complete tracker. The Wardline+Filigree pair works without Loomweave (DRIVER-2).
- The findings stock disappears instead of being tuned (M-3's rule-level leverage).
- The Legis fail-closed trap and the hollow Warpline reach-ins are removed.

**Negative (accepted trade-offs)**
- No cross-producer "all findings for this file" view. An agent asks Wardline (and Loomweave) separately. This is mitigated by the edit-time hook and by Filigree's reverse lookup for *promoted* work.
- The dashboard loses the code-health view. The human reviews findings in Wardline's surfaces and in PR-reviewed waiver files. This **conflicts with the UX review's "Findings triage" surface** (§9).
- Producers must carry a promote client and a state-update emitter. Wardline already has the promote client (`core/filigree_issue.py`).
- 3.x findings data (≈25k rows fleet-wide) is exported to an archive file, not migrated. This is **owner-gated data deletion**.
- Snapshots go stale by design. A reference shows "as attached" and `state_at`, not the live truth.

**Neutral / to monitor**
- Whether agents actually promote defects once noise is gone. Measure promotes per 100 defect findings.
- Whether SEI survives Loomweave's lean core. B2 is indifferent; `sei` becomes one optional reference kind.

#### Rollback / exit criteria

**Revisit when any of these occurs:**
- A second producer with real defect volume (≥100 promotable defects per month across the fleet) needs cross-producer dedupe → reconsider Option C.
- Measured multi-server friction: agents fail an S-2/S-4 round-trip in more than 5% of attempts over a 4-week window → reconsider Option E.
- Loomweave passes its kill-date experiment *and* gains a consumer that needs findings on entities → reconsider a B1-style read view (Loomweave reading Wardline's artifact, never Filigree's).

**How we would unwind.** Re-introduce a bulk findings ingest as a *new* HTTP generation in Filigree, fed only `kind=defect`, alongside references. Effort: 2–4 engineer-weeks, because the reference model remains as is.

#### Owner gates (escalate-first under the Filigree authority grant)

1. Deprecating used features: findings ingest (Wardline emits to it in 4 repos), the `finding_*`/`file_*`/`annotation_*`/`scan*` tools, and the Files view.
2. Data deletion: exporting and dropping `scan_findings`, `file_records`, `annotations` and `scan_runs`.
3. The doctrine change "finding lifecycle lives in Filigree" (hub doctrine §2/§6; Wardline `core/finding.py:4-7`).
4. Any public release.

#### Links

- Related sibling reviews: `product-critique.md` (P-3 kill/keep, P-5 federation premise), `mcp-contract-critique.md` (catalog proposal), `federation-http-api-review.md` (successor generation; F2, F9, F12), `dashboard-ux-theory.md` (Files view).
- Supersedes in part: Filigree ADR-007 (`report_finding`), ADR-014 (registry backend), ADR-015 (findings retention), ADR-017 (SEI backfill machinery), ADR-029 (association opacity: generalised into evidence references); hub doctrine §2/§6 (finding lifecycle).
- Threats: N/A, not a security-affecting decision. Federation auth and identity are covered by S-6 under the deconfliction posture.

#### Review log

- 2026-10-07: drafted by the solution architect (refresh panel); awaiting owner ruling.

---

### Migration sketch (brownfield; stages gate on observable criteria, not dates)

| Stage | What ships | Observable success | Rollback trigger | Rollback |
|---|---|---|---|---|
| **0. Stop the bleeding** (3.x patch + Wardline patch, additive) | Wardline: emit only `kind=defect` to Filigree (flag, default on); stable engine fingerprints. Filigree: refuse or ignore `path="<engine>"`; `LEGIS_URL` unreachable → warn, don't wedge; a one-off script marks existing telemetry rows `fixed` with reason "telemetry, not work" (**owner gate**). | New Filigree rows per scan ≈ number of defects; the `<engine>` count stops growing | Any defect finding missing from Filigree that Wardline emitted | Turn the Wardline flag off |
| **1. Additive seams in the last 3.x minor** | S-1 references, S-2 promote-with-payload, S-3 state update, S-4 reverse lookup, under a new HTTP generation alongside `weft`. Wardline promotes defects via S-2 when the capability is advertised. Deprecation `_meta` on the finding/file/annotation/scan tools. | Every Wardline-promoted defect in the 15 fleet projects lands as an issue with a `finding` reference, idempotent on rerun; Lacuna e2e green with Loomweave absent | Duplicate issues on re-promote; S-3 updates lost | Keep the old ingest; disable promote-by-payload |
| **2. 4.0 cut** | Remove the findings/files/annotations/scanners/governance/Warpline/registry code. The migration converts linked findings, file associations and entity associations into references on their issues; everything else is exported to `archive/findings-3x.jsonl` (**owner gate**). Tombstones carry `renamed_to` / `migration` hints. | Migration parity: every issue that had a linked finding, file or entity has an equivalent reference | Parity mismatch | Refuse the 4.0 migration; stay on 3.x |
| **3. Loomweave cleanup** (independent of the Filigree timeline) | Delete Flow B, the churn/recent-change proxies, the Filigree emit and SARIF → Filigree; `entity_issue_list` uses S-4 (by SEI or path) if kept | Loomweave builds and serves with Filigree absent and configured | — | — |

---

## 7. Seams that need new or rewritten specs

All of these live in `weft-contracts` (markdown + JSON Schema + golden vectors). For each, the **owner** writes the contract and the consumers vendor its goldens. Every seam must state the three idempotency fields (upstream delivery, consumer dedup with out-of-window behaviour, handler idempotency), the honesty envelope, and behaviour when the peer is down.

| ID | Seam | Owner → consumer(s) | Status | Must specify | Peer-down behaviour |
|---|---|---|---|---|---|
| **S-1** | **Evidence reference** (the work ↔ code link) | Filigree → everyone | **New** (generalises ADR-029 associations, file associations, `claim_commit`/`close_commit` as references) | Kinds (`path`, `symbol`, `finding`, `commit`, `sei`, `url`, extensible); `value` opaque; `producer`; `scheme`; snapshot fields and byte caps; `state ∈ {present, gone, suppressed, unknown}` + `state_at` + `state_commit`; per-type workflow policy hooks (e.g. "close requires ≥1 evidence ref of kind commit") | Filigree core never resolves. Resolution is optional client-side enrichment |
| **S-2** | **Promote** (work candidate → issue) | Filigree ← Wardline, Loomweave, agents, any producer | **Rewrite** (replaces `scan-results` bulk ingest, promote-by-fingerprint, `warpline_worklist_ingest`) | Payload = producer-neutral finding record (S-8) + priority/labels; **idempotency key** = `(producer, scheme, fingerprint)`, returning the existing open issue; `kind=defect` only; batch envelope (`succeeded`/`unchanged`/`failed` per MCP critique F8); size caps; project scoping | Producer fails soft and retries on the next scan; the key prevents duplicates |
| **S-3** | **Evidence state update** | Producer → Filigree | **New** (replaces the absent-fingerprint sweep and the close-on-fix cascade inference) | State-based, not event-based (re-sending the current state is idempotent); `at_commit`; `completeness` (a partial scan may only assert `present`, never `gone`); Filigree **records** it and surfaces "evidence gone" in attention; auto-close only via an explicit workflow policy (e.g. `resolution=resolved_by_scan`, per the business-process review §5.4). **Two open questions the spec must answer:** (1) how the producer learns which references to report on, either by an S-4 lookup by `(kind=finding, producer)` before reporting or by keeping a local promoted-set; (2) the **fingerprint-scheme bump rule** (`wardline rekey`): the producer must post an old→new remap or re-promote under the new scheme, or B2 inherits today's orphaning (`core/rekey.py:838`, "no remap endpoint") | Lost updates self-heal on the next full scan; `unknown` after a freshness window |
| **S-4** | **Work-by-reference lookup** | Filigree → Loomweave, Wardline dossier, hooks | **Rewrite** (replaces classic `/api/entity-associations?entity_id=`; HTTP review F9) | Query by `(kind, value)`, with prefix match for paths; bounded and cursor-paged; project-scoped and fail-closed on ambiguity; returns slim issue rows + reference snapshot | Callers report `unavailable` with a `weft-reason` |
| **S-5** | **Change feed** | Filigree → consumers | **Harden** (HTTP review F16) | One cursor over all entity kinds including references; tombstones; ordering and VACUUM stability | — |
| **S-6** | **Discovery, auth, actor identity** | `weft-contracts` convention; every product implements it | **Rewrite** (three token chains today; HTTP review F14; Wardline `doctor` mode skew) | `.weft/<member>/endpoint` + `token`; one resolution order; `/_capabilities` with contract versions; **connection-bound actor** (`--agent-id`; HTTP header), per-call `actor` as override only; mutations with no identity refused | A missing token is a distinct reason class, never an ambiguous 401 |
| **S-7** | **Result honesty + error envelope** | `weft-contracts` | **Rewrite** (MCP critique F2; HTTP review F4) | `{error, code, retryable, cause_kind, hint}`; MCP `isError`; the 11-class `weft-reason` vocabulary on every partial/empty cross-product result | — |
| **S-8** | **Finding record** (producer-neutral) | `weft-contracts` (Wardline primary author) | **New** (three schemas today) | `kind`, severity (one scale, no mapping to "low" for telemetry), `fingerprint {scheme, value}` with a stability rule (no volatile values in fingerprint inputs), `location`, `symbol`, optional `sei`, `rule_id`, `message`, `suppression_state` | — |
| **S-9** | **SEI** (conditional) | Loomweave → consumers | **Keep only if Loomweave's lean core keeps SEI** | The existing locked standard, plus determinism across a from-scratch rebuild (gap map NOTE-2) | Consumers degrade to `path`/`symbol` references |

**Seams to retire, with tombstones:**
- `POST /api/weft/scan-results` and `/api/v1/scan-results`
- `POST /api/weft/findings/clean-stale`
- `GET /api/weft/findings` and `/api/weft/files` (Flow B)
- `/api/v1/files*` registry delegation (ADR-014)
- The Legis closure gate and sign-off binding
- `warpline_worklist_ingest`
- Loomweave → Warpline churn
- The Wardline → Loomweave taint-fact store (retire unless Loomweave's lean core keeps it *and* a consumer exists: 1,628 rows written once, on 2026-07-12)
- SARIF → Filigree translation

**Keep as is:** Loomweave guidance proposals via Filigree **observations**. This is a correct use of work-state, and it is the pattern any producer can use for "suggestion" findings that are not defects.

---

## 8. Confidence, Risk, Information Gaps, Caveats

### Confidence Assessment

**Overall: Moderate-High on the facts and the diagnosis; Moderate on the recommendation's adoption assumptions.**

| Finding | Confidence | Basis |
|---|---|---|
| 9,955 of 9,973 Filigree findings are Wardline telemetry; 3 linked | High | Read-only SQL on a scratch copy of `.weft/filigree/filigree.db`; independently re-counted by the Wardline sub-investigation |
| The telemetry emission path (all kinds, INFO→low, engine fingerprint includes the ratio) | High | `filigree_emit.py:103-133`, `finding.py:265-271`, `diagnostics.py:102`, `propagation.py:557-563`, read directly |
| Four unsynchronised acceptance stores; no read-back from Filigree | High (code) / Moderate (absence) | Schemas read directly; the absence of read-back rests on grep, and a negative is never fully proven |
| Loomweave churn tools are Warpline-only and dead | High | `shortcuts.rs:900-962`, `query.rs:1303-1306`; live call returned `warpline-disabled` |
| Loomweave has no active consumer and low adoption | Moderate | Today's untracked, non-normative memo; transcript-count methodology is crude, as the memo itself says |
| MCP usage concentration (95% work state, <1% code-facts surfaces) | Moderate | 15 logs; MCP only; CLI and sibling HTTP not logged; one operator |
| Legis fail-closed trap | High | `governance.py:12-24`, `:362`; only bites when `LEGIS_URL` is set (no evidence it is set anywhere live) |
| B2 tool count ≈80 | Moderate | Arithmetic on the MCP critique's 76-tool proposal and the Loomweave memo's lean core; not designed tool-by-tool |
| Effort estimates | Low-Moderate | Lines-of-code proxies, not a plan |

### Risk Assessment

**Implementation risk: Medium. Reversibility: Moderate.**

| Risk (architectural) | Likelihood | Impact | Trigger | Mitigation |
|---|---|---|---|---|
| RSK-1 *Integration fragility.* S-2/S-3 become the only Wardline → Filigree path; a contract slip silently drops work candidates (confident-empty) | Medium | High | Promotes per scan = 0 while Wardline reports defects > 0 | The S-2 response lists per-item results; Lacuna e2e with Loomweave absent; Wardline prints "N defects, M promoted" |
| RSK-2 *Data consistency.* Snapshots read as live truth; agents act on a stale `state` | Medium | Medium | `state_at` older than the freshness window on an open issue | Render age; `unknown` after the window; S-3 completeness rule |
| RSK-3 *Contract evolution.* `weft-contracts` drifts as the hub did | Medium | Medium | A seam behaviour that no golden pins | Contracts-only scope (no PM, no member restatement); every seam has goldens vendored by consumers; drift lane per seam |
| RSK-4 *Adoption.* The human loses the dashboard code-health view and stops reviewing findings anywhere | Medium | Medium | Wardline waiver count flat while defects grow | Edit-time hook; a Wardline summary in the PR template; owner decision on the UX review conflict (§9) |
| RSK-5 *Migration window.* 3.x and 4.0 coexist across 15 projects at schema versions 8–29 | High | Medium | Projects stuck on 3.x after 4.0 | Stage 1 ships the new seams additively in 3.x, so producers migrate before the cut |
| RSK-6 *Scope.* The re-cut is read as "kill federation" and seams are dropped instead of rewritten | Low | High | A seam removed with no S-n replacement | §7 maps every retired seam to its replacement or a recorded drop |

### Information Gaps

1. **The owner's ruling on Loomweave's memo** (rebuild versus recover versus kill date), and whether SEI survives. B2 was chosen to be indifferent to this, but S-9 depends on it.
2. **HTTP and CLI usage.** Sibling HTTP calls (Wardline's emit and promote, Loomweave's reads) and CLI verbs are not logged. Wardline's emit volume is inferred from DB rows, not request logs.
3. **External users.** There is no PyPI or download data. elspeth replaced Filigree with GitHub Issues for an organisational release, which is the competitor an external user will compare against.
4. **Whether any project sets `LEGIS_URL`.** This decides whether M-7's trap is live anywhere.
5. **Defect volume on a codebase with declared trust boundaries.** The owner's repos have almost none (Wardline's own posture note), so the promote rate under B2 is unmeasured. Lacuna would give a first reading.
6. **The two late sibling reviews** (`llm-agentic-experience.md`, `business-process-review.md`) were read after drafting, and only for boundary-relevant content (findings, annotations, governed close, reverify, tool count). Their full findings were not cross-checked against this report.

### Caveats and Required Follow-ups

**Before relying on this analysis:**
- The owner must rule on the four gates in the ADR. Kills of used features and data deletion are escalate-first.
- The B2 boundary must be re-checked against the UX review. Its "Findings triage" surface is the main sibling disagreement: under B2, findings triage happens in Wardline (CLI/MCP and PR-reviewed waiver files), and Filigree's inbox shows *promoted* work and evidence-state changes only.
- Stage 0 must be run first, because it is reversible and measures what B2 assumes: defects per scan, and promotes per defect.

**Assumptions:**
- "Agent-first" means agents are the primary consumer, and the human review of code acceptance happens in PRs.
- The 4.0 rebuild already budgets the work-state core, so B2's effort is the *delta*.
- Archived members stay archived. If Legis or Warpline return, they bind via S-1/S-2/S-4 rather than private stores.

**Limitations:**
- No live HTTP probing of the daemon on :8749.
- No load or replay tests.
- The Wardline MCP server was not exercised (it was down this session).
- Hub specs were read in part. `federation-map.md`, `federation-topology.md`, `contracts-index.md`, `doctrine.md` §1–7 and `members/*` were read in full. `glossary.md`, `sei-standard.md` and `conflict-register.md` were read at heading level only, and `contracts/*.json` was not opened.
- Loomweave and Wardline line counts and line numbers come from sub-investigations; I re-verified the load-bearing ones (fingerprints, severity map, Loomweave findings schema and emit default, churn proxy, Wardline posture note), not every row.

**Recommended next steps:**
1. Owner ruling on ADR-F4-001 gates and on Loomweave's memo.
2. Ship Stage 0 (Wardline defect-only emit; Filigree `<engine>` refusal; Legis warn-not-wedge).
3. Draft S-1, S-2, S-3 and S-8 first. They carry B2. Then S-6 and S-7 jointly with the MCP and HTTP critiques' envelope work.
4. Re-run the usage census 4 weeks after Stage 1, including promote counts.

---

## 9. Where this disagrees with sibling reviews

| Sibling | Their position | This review | Proposed resolution |
|---|---|---|---|
| `dashboard-ux-theory.md` | Promote "Findings triage" to a top-level surface (persona D) | Findings triage leaves Filigree; the dashboard shows promoted work + evidence-state attention | Owner decision. If a human findings inbox is wanted, it reads Wardline's artifacts (a producer view), never a Filigree mirror |
| `mcp-contract-critique.md` | 76-tool catalog keeps `finding_*` (5), `file_*` (6), `annotation_*` (6), `scan*` (6) | Remove those families; add ~3 reference tools | Their envelope, idempotency and bounding rules apply unchanged to the smaller catalog |
| `federation-http-api-review.md` | Successor generation carries entity associations and worklist ingest as first-class resources (F9, F12) | Agree on a successor generation; its resources are references (S-1), promote (S-2), state (S-3) and reverse lookup (S-4) | Same mechanism, different resource set |
| `product-critique.md` | Q2: is the federation still the strategy? Q5: kill table | Answers Q2 for the live trio: keep three seams, re-specified; drop the archived members' seams. Supplies the kill rows for findings/files/annotations/scanners/governance | Consistent |
| `business-process-review.md` | Same diagnosis (BP-04: "a stock with no outflow … telemetry is stored as triage inventory"), but an **Option A fix**: keep findings in Filigree with `kind`, a per-source disposition policy (telemetry auto-acknowledged), and retention run inside ingest. Annotations "keep as-is". Governed close kept as a **provider-agnostic policy gate** (`separation_of_duties`, `human`, `external:<provider>`). Reverify worklist kept as a producer-agnostic contract. | The disposition policy is Filigree learning to ignore what it should never receive. Under B2, telemetry never crosses the seam and the verdict stays with the producer, so the policy is unnecessary. Their promote → `bug` in `triage` and cascade close `resolution=resolved_by_scan` map directly onto S-2 + S-3 + workflow policy. Their governed-close model is **adopted** as Filigree-local workflow policy (`human`, `separation_of_duties`); `external:<provider>` stays a reserved slot until a provider exists. Their reverify contract is **adopted** as S-2 with `kind=sei|path` references and "one open item per reference" dedupe. On annotations: 7 rows and 1 call do not justify a 1,280-line anchor-drift store (M-4). | Owner decision between A and B2 for findings. If A is chosen, their disposition policy is the minimum viable form; Stage 0 here is needed under either option. |
| `llm-agentic-experience.md` | LX-01: the session banner calls 9,955 telemetry rows "actionable", and its MCP hint triggers a ~38K-token `finding_list` call. R9 keeps findings in Filigree with a federation-neutral `signal_class` (`defect`/`telemetry`) set at ingest. R4: collapse observation + annotation into a **note**; default profile of 40–50 tools. | LX-01 is the agent-side cost of M-1 and the strongest single argument for not mirroring telemetry. `signal_class` is S-8's `kind`, but enforced at the **producer** (only defects are promoted), not classified by the consumer. The note collapse is **adopted** (M-4). B2's ~52-tool Filigree catalog reaches their 40–50 target once their further twin-collapses apply. | Mostly consistent. The remaining difference (classify at ingest versus do not ingest) is the same A-versus-B2 owner decision. |

---

## Summary (machine-readable)

```json
{
  "overall_confidence": "Moderate",
  "implementation_risk": "Medium",
  "reversibility": "Moderate",
  "recommended_option": "B2 - Filigree work-state + opaque typed evidence references; producers own findings, telemetry and verdicts; no aggregator",
  "top_findings": [
    {"claim": "9,955 of 9,973 Filigree findings are Wardline engine telemetry; 3 linked to issues", "confidence": "High", "evidence": "scratch copy of .weft/filigree/filigree.db; wardline core/filigree_emit.py:103-133, core/finding.py:265-271"},
    {"claim": "Four unsynchronised 'finding accepted' stores (Wardline files, Filigree status, Loomweave status, Legis cells)", "confidence": "High", "evidence": "wardline core/paths.py:77-86; filigree scan_findings; loomweave migrations/0001:104-135"},
    {"claim": "Filigree maintains derived code facts it cannot refresh (file registry, annotation anchors, scanner runner); ~20k lines", "confidence": "High", "evidence": "db_files.py:415, db_annotations.py:188,373, scanners.py; 0/15 projects on registry_backend=loomweave"},
    {"claim": "Loomweave churn/recent-change are dead Warpline proxies; its Filigree findings join returned 401 live", "confidence": "High", "evidence": "loomweave catalogue/shortcuts.rs:900-962; live MCP calls 2026-10-07"},
    {"claim": "Legis gate fails closed on governed closes with Legis archived", "confidence": "High", "evidence": "filigree governance.py:12-24,362"}
  ],
  "archived_dispositions": {
    "weft_hub": "replace with runtime-free weft-contracts package",
    "legis": "drop; close-requires-evidence as Filigree workflow policy; waivers in Wardline",
    "warpline": "drop; worklist becomes generic promote (S-2); churn via git",
    "tabard": "drop; connection-bound actor convention (S-6)",
    "plainweave": "stays archived",
    "lacuna": "keep as conformance/e2e corpus"
  },
  "seams_to_spec": ["S-1 evidence reference", "S-2 promote", "S-3 evidence state update", "S-4 work-by-reference lookup", "S-5 change feed", "S-6 discovery/auth/actor", "S-7 envelope+honesty", "S-8 finding record", "S-9 SEI (conditional)"],
  "blocking_gaps": ["owner ruling on Loomweave memo / SEI survival", "HTTP+CLI usage not logged", "owner gates: feature deprecation + data deletion"],
  "recommended_next_steps": ["owner ruling on ADR-F4-001 gates", "Stage 0: defect-only emit, <engine> refusal, Legis warn-not-wedge", "draft S-1/S-2/S-3/S-8", "re-run usage census after Stage 1"]
}
```
