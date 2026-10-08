# Filigree refresh review: the agent's experience (LLM specialist lane)

**Date:** 2026-10-07
**Reviewer:** Claude (LLM specialist on the refresh panel). Actor identity: `claude-filigree`.
**Repo state:** `release/3.3.0` @ `59053a3` (3.3.0 released 2026-09-02). Working tree clean before and after (only sibling reviewers' untracked output present).
**Target:** the next major, which the owner has now named **4.0**. Rewrites are allowed, back-compat shims are not wanted, and the audience is the owner's fleet plus external agent users.
**Lane:** how an LLM agent *perceives, orients in, and recovers within* Filigree. The MCP *contract* (schemas, envelopes, idempotency, pagination, parity) belongs to the sibling `mcp-contract-critique.md`. Where a finding overlaps, I cite the sibling finding and add only the agent-experience evidence.

**Method**

- Ran `filigree session-context` against the live tracker and decomposed every line against the database (read-only `sqlite3 ... mode=ro`).
- Dumped the served MCP catalog (all 118 tools) in-process. Measured byte weight per tool and tier, and audited descriptions and parameter docs.
- Ran three live ToolSearch queries in this session, phrased the way an agent would ask, to test tool selection.
- Probed the write and error paths in-process through `mcp_server.call_tool` against a throwaway `filigree init` project at `scratchpad/llm-probe/`. The real tracker received no writes.
- Probed SCHEMA_MISMATCH on a *copy* of that probe DB.
- Read the instruction surfaces: `data/instructions.md`, the project `CLAUDE.md`, the skill and all five references plus the example, the MCP prompt text, `workflow_guide_get`, and `docs/agent-integration.md`.
- Tallied real tool usage from this repo's MCP log (`.weft/filigree/filigree.log`: 532 calls, 2026-06-07 → 2026-10-07).
- Read the May prior art: the agent-systems review, master checklist, gap analysis, the April interface review, reviews D–H, and PRD-0001. Checked tracker status of epic `filigree-ed2ccaf10d` and its children, X-4/X-5/X-6, and related issues.

**Token estimates:** no tokenizer was available. JSON/prose bytes ÷ 3.6 ≈ tokens, about ±20%. Every token figure below uses that method.

---

## Executive summary

Filigree's *always-on* agent surface is lean and mostly well-built:

- The managed CLAUDE.md block is 17 lines.
- The SessionStart snapshot is about 700 tokens.
- In Claude Code the 118 tools are deferred behind ToolSearch, so session-start cost is about 1.2K tokens of names.
- Several recovery affordances are good: `INVALID_TRANSITION` carries `valid_transitions`/`next_action`/`missing_fields`, `CONFLICT` carries `observed`/`expected`, and `work_ready` and `issue_search` return slim rows.

The problems are about **what the agent is pointed at, and when signals reach it**. They are not about raw instruction length.

1. **Orientation points agents at the wrong things.**
   - The session-start banner's most prominent line reports 9,956 "actionable" analyzer findings. 9,955 of them are Wardline engine telemetry. The one "defect-signal" is a leftover `smoke-test-rule` row from a prior review.
   - The banner's MCP hint (`finding_list`, no filter) produces a ~137 KB (~38K token) response.
   - "READY TO WORK" ignores the `startable` flag the wire already computes. 8 of the 15 items shown here are containers or unapproved features, including 3 of the top 4.
   - "IN PROGRESS (resume these)" lists every agent's work with no assignee.
2. **Core reads are unbounded in the way agents actually call them.** The log shows `list_issues` called 40 times, usually with `no_limit=true`. On this tracker that is ~91 KB (~25K tokens) per call because full descriptions ride along. `issue_list` has no slim mode (H-F17, still open).
3. **Coordination signals arrive after the decision, or not at all.**
   - A `critical` + `must_consider` annotation surfaces only as a warning on `issue_close`, after the work is done. It does not appear on `issue_get` or `work_start`.
   - The planned broadcast board delivers only at SessionStart, with a 30-minute window. That misses its own canonical case: mid-session deconfliction.
   - The skill teaches agents to free a peer's claim with plain `release`. A live probe shows `work_release(actor=B, expected_assignee=B)` strips A's unexpired lease, even though the parameter's own description says it "only release[s] when the current assignee matches".
4. **Tool selection is unaided.** The `[tier: …]` suffix is invisible to the ranker, and no agent surface mentions tiers. Live ToolSearch results:
   - "what should I work on next" ranked niche `work_claim_next` first.
   - `work_claim_next`'s description instructs the claim-then-`issue_update` two-step that the managed instructions forbid.
   - "claim an issue and start working" ranked `work_start` 5th, behind three annotation/entity tools.
5. **Recovery text names tools that no longer exist.**
   - The unknown-parameter error names the pre-3.0 tool (`Unknown parameter(s) for get_ready` when the agent called `work_ready`).
   - Annotation close-warnings suggest `resolve_annotation` and three other removed names.

**Highest-leverage 4.0 change: actor-aware orientation on a launch-bound identity (R1).**

- **Launch-bound identity:** `filigree-mcp --actor <id>`, written by `filigree install` and shared with the CLI. Per-call `actor` disappears from 62 schemas.
- **Actor-aware snapshot:** session-context becomes "who you are / your claims / startable for you / others active / attention items".

On its own this closes or defuses LX-03, LX-05, LX-09, LX-10 and LX-11. It also shrinks the catalog and removes the one convention external agents cannot guess. R2 is a close second: bounded, slim-by-default reads in one sweep.

---

## Findings

Severity is by **agent blast radius**, meaning how badly the issue degrades the agent's work loop:

- **High:** the agent acts on the wrong thing, loses another agent's work, or loses a large slice of its context window.
- **Medium:** wasted calls or wrong-but-recoverable choices.
- **Low:** friction or polish.

### LX-01. The session-start analyzer banner is almost pure noise, and its MCP hint triggers a ~38K-token call

- **Tag:** STILL-OPEN. Residual of closed `filigree-4d489560e0` (FIL-1), whose literal title complaint is still true: telemetry is still counted inside "actionable". Also STILL-OPEN `filigree-56cb5c93f3` (strict `--kind defect` misses kind-less rows), `filigree-8f6a1599fb` (X-4 vocabulary) and `filigree-b789da2a1e` (X-6 `finding_list` bloat).
- **Severity:** High.
- **Evidence:**
  - Live hook output: `ANALYZER FINDINGS: 9956 not yet bridged to the tracker (9956 actionable: 1 defect-signal, 9955 telemetry/info; 0 baselined/suppressed) — review with filigree finding list --kind defect … (MCP: finding_list / finding_promote)`. Produced at `src/filigree/hooks.py:163-173`.
  - DB breakdown of non-terminal findings: 8,850 `metric` WLN-L3-LOW-RESOLUTION, 1,100 `fact` WLN-ENGINE-UNKNOWN-IMPORT, and a handful of other engine facts.
  - The single "defect-signal" is `filigree-sf-cd7e5fa33f`: `smoke-test-rule`, scan_source `agent`, message "F3 smoke test finding". It is counted defect-side only because it has no `kind` (`db_files.py:3150-3156`).
  - Following the CLI advice (`filigree finding list --kind defect --json`) returns **2 rows, both `status: fixed`**. It misses the one open signal (kind-less) and shows terminal rows, because `finding list` has no non-terminal default.
  - Following the MCP advice (`finding_list` with no arguments) returns 100 rows of WLN-ENGINE-UNKNOWN-IMPORT: **136,923 bytes, about 38K tokens** in one call. Measured via the CLI, which shares `list_findings_global` with the MCP handler (`mcp_tools/files.py:736`).
- **Recommendation:**
  - The banner should lead with the defect-side count only. Show telemetry as a bare parenthetical with no command, and omit the line entirely when `actionable_defect == 0`.
  - The MCP aside must carry the same filter as the CLI advice: `finding_list(kind="defect", status="open")`.
  - `finding_list` should default to non-terminal statuses.
  - Land X-6 (slim, bounded default).
  - For 4.0, see R9: classify findings at ingest into a federation-neutral `signal_class` (`defect`/`telemetry`), so the banner does not depend on `metadata.wardline.kind`.
- **Confidence:** High (every number reproduced). **Risk of acting on it:** Low.

### LX-02. "READY TO WORK" advertises unstartable items

- **Tag:** STILL-OPEN. Residual of closed `filigree-406e6b7ee0`. That issue's description explicitly named "snapshot/get_summary advertise … as ready". The fix added `startable`/`next_action` to `work_ready` JSON only.
- **Severity:** High. Agents act on the top of this list, and the log confirms they rarely call `work_ready` (2 calls) or `work_start_next` (0 calls).
- **Evidence:**
  - On the live tracker, 43 items are ready and 12 are `startable:false` (28%). Of the 15 rendered, 8 are unstartable (53%): phases, a milestone, epics and `proposed` features. Three of the top four slots are unstartable.
  - The hook renders neither `startable` nor `next_action` (`hooks.py:125-135`), and `summary.py:159-173` has the same gap.
  - Probe: after creating the skill's own `examples/sprint-plan.json`, the snapshot read `READY TO WORK (8 tasks with no blockers)`. 6 of the 8 were unstartable.
  - A fresh `filigree init` seeds a `[release] "Future"` singleton (status `planning`; created at `core.py:2453-2483`) that appears as ready work in every new project.
  - The top P1 items (`filigree-3b49babe31`, `filigree-a5d21610fd`, `filigree-b21d7a9f17`, `filigree-d72069ae5d`) were last touched 2026-09-01 and belong to the already-released 3.3.0. The snapshot shows no age, so the agent cannot tell the queue is stale.
  - `next_action` vocabulary is mixed: `"complete child issues"` is an instruction, while `"approved"` is a bare status name.
- **Recommendation:**
  - Render a "STARTABLE NOW" list (leaf, startable) and put the startable count in the header.
  - Collapse containers and awaiting-approval items into one count line.
  - Append age (`· 36d`) to each line.
  - Make `next_action` an imperative ("transition to approved (human gate)").
  - Stop seeding a ready "Future" release, or exclude containers from ready.
- **Confidence:** High. **Risk:** Low.

### LX-03. "IN PROGRESS (resume these)" is actor-blind

- **Tag:** NEW.
- **Severity:** Medium–High in multi-agent use, Low for a solo agent.
- **Evidence:**
  - `hooks.py:104-110` lists every wip issue under "resume these" with no assignee shown and no filter. Probe: agent-a's claimed task appeared for any session. In contrast, STALE CLAIMS does show `-> assignee` (`hooks.py:118`).
  - `skills/.../team-coordination.md:201` claims the hook automatically answers "What was I working on?" (`list --status=in_progress --assignee <name>`). It does not, and it cannot without knowing the agent's identity (see LX-11).
- **Recommendation:** Show assignee and lease expiry now. In 4.0, split the section into "YOUR CLAIMS" and "OTHERS ACTIVE: do not touch" (R1).
- **Confidence:** High. **Risk:** Low.

### LX-04. Core list reads and write echoes are unbounded the way agents actually use them

- **Tag:** STILL-OPEN. H-F17 (`list_issues` slim), H-F5 (`add_comment` full echo) and H-F9 (`create_plan` slim). The master-checklist row "Normalize common response envelopes… [x]" closed via `plan_get`/batch only. The sibling covers the contract side in `mcp-contract-critique.md` Finding 6. This finding adds measured agent behaviour.
- **Severity:** High.
- **Evidence:**
  - The log shows 40 `list_issues` calls, mostly with `no_limit: true` (e.g. `{status_category:"open", no_limit:true}`).
  - Measured on the live tracker:
    - The 49 open issues come to 90,875 bytes (~25K tokens), averaging 1.6 KB per row.
    - The default 50-row page is 115,903 bytes (~32K tokens).
    - Description length averages 762 characters, max 2,228.
  - By contrast, `issue_search` returns 5-field rows and `work_ready` returns 7-field rows.
  - `issue_list`, `issue_search`, `issue_get`, `finding_list` and `change_list` have no `response_detail`. Only 17 tools do, mostly batch and annotation tools.
  - Other measured payloads:
    - `comment_add` returns the full issue on every comment (~1.6 KB here).
    - `plan_create` returned 10,527 bytes for the 7-step skill example.
    - `change_list` returns 54,842 bytes for 98 events (~15K tokens).
- **Recommendation:** R2.
  - Slim projection by default (id, title, status, category, priority, type, assignee, updated_at, startable).
  - Descriptions only via `issue_get` or `response_detail=full`.
  - Default limit of 25 with a prominent `has_more`/`next_offset`.
  - Writes return a slim ack plus `changed_fields`.
- **Confidence:** High. **Risk:** Low. In 4.0, no shim is needed.

### LX-05. The skill teaches peer-claim stripping, and `work_release`'s precondition parameter is silently ignored

- **Tag:** NEW (agent-facing angle). The contract defect (release is unconditional) is the sibling's Finding 3. The May H review's "what works well" list assumed `release_claim` honoured `expected_assignee`.
- **Severity:** High in multi-agent use.
- **Evidence:**
  - Probe: agent-a `work_start`ed a task (lease expiring +48h). Then `work_release {issue_id, actor:"agent-b", expected_assignee:"agent-b"}` succeeded, cleared the assignee and reverted `in_progress → open`. A third agent can now `work_start` it, which means double work.
  - Code: `db_issues.py:1669-1691` uses `expected_assignee` only when `if_held=True`. The served parameter description says "Only release when the current assignee matches this value".
  - The skill's Stale Claims recipe (`team-coordination.md:169-177`) is `filigree list --status=in_progress --assignee <missing-agent>` followed by `filigree release <issue-id>`. It does not check for a live lease.
  - The skill pack never mentions `work_stale_list`, `work_reclaim`, `work_heartbeat`, `work_release_mine` or leases (grep: 0 hits across `SKILL.md` and all references). `docs/agent-integration.md`, a human doc, does describe them.
- **Recommendation:**
  - Skill: Stale Claims becomes `work_stale_list`, then `work_reclaim(expected_assignee=…, reason=…)`.
  - Add a 3-line lease section: heartbeat only for work longer than 48h, and `work_release_mine` at session end.
  - 4.0 contract: release is holder-checked by default (sibling F3), so the documented `expected_assignee` semantics become true.
- **Confidence:** High. **Risk:** Low.

### LX-06. Tier markers do not help tool selection, and live ToolSearch misranks the daily drivers

- **Tag:** STILL-OPEN against closed `filigree-e49e6469de`. Its stated acceptance ("an agent's first search returns the daily-driver ~25") is unmet. The sibling's Finding 27 covers the metadata channel.
- **Severity:** Medium–High.
- **Evidence:** live ToolSearch in this session, top 5 results:

  | Query | Result |
  |---|---|
  | "filigree claim an issue and start working on it" | `finding_promote_and_attach_entity`, `issue_annotation_list`, `workflow_transition_list`, `entity_association_add`, then `work_start` (5th) |
  | "filigree what should I work on next" | `work_claim_next` (niche) 1st, `work_start_next` 2nd, `work_ready` absent |
  | "filigree jot down something odd I noticed in a file without derailing" | `observation_create` absent; file-centric tools dominate |

- The tier exists only as a ` [tier: X]` suffix (`mcp_server.py:459-465`). `tiers.py:3-6` claims it helps ToolSearch. No agent-facing surface mentions tiers (grep across `instructions.md`, the skill and `CLAUDE.md`: 0).
- Tier versus real usage, from the local log (532 calls):
  - Core-tier tools are barely used: `work_ready` 2, `work_blocked` 1, `work_start_next` 0.
  - Non-core tools are used more: `comment_list` 26 (common), `workflow_transition_list` 10 (common), `observation_create` 9 (niche).
  - The current core-12 covers 84.6% of calls. Swapping those three for the three above gives 92.5%. This answers the product-critique question of whether the 12-tool core is sufficient: nearly, but it is the wrong 12.
- **Recommendation:** R5.
  - Put discriminators in the *indexed* text, for example: "Reserve-only, no status change. For normal work use `work_start_next`."
  - Re-tier by observed usage.
  - Move the tier into `_meta`.
  - For hosts that eager-load, offer a profile (R4).
- **Confidence:** High for the misranking (observed). Moderate on ranker internals, which are opaque. **Risk:** Low.

### LX-07. Claim-only tool descriptions instruct the two-step the managed rules forbid

- **Tag:** STILL-OPEN. The 2026-04-18 review's Pain point 2 led to `start_work` being added, but the claim descriptions were never re-aimed.
- **Severity:** Medium–High. LX-06 shows `work_claim_next` is what ToolSearch serves first.
- **Evidence:**
  - `work_claim`: "Does NOT change status — use issue_update to advance through workflow after claiming." `work_claim_next` says the same.
  - `data/instructions.md:13-15` says: "Never chain a claim with a separate status update; that two-step form races other agents."
  - The skill does frame claim as niche (`commands.md:60-70`), but the tool text is what the model sees at selection time.
- **Recommendation:** In 4.0, remove claim-only verbs from the default catalog, or fold them into `work_start(transition=false)`. Until then, open both descriptions with "Reserve-only (coordinator use). To pick up work use work_start / work_start_next."
- **Confidence:** High. **Risk:** Low.

### LX-08. Recovery text names tools that are not on the wire

- **Tag:** REGRESSED against the ADR-016 cutover (`63beb7c`). The runtime-prose guard `tests/mcp/test_no_old_names_in_runtime_prose.py` matches only directive-verb plus name patterns, so list literals and f-string interpolation slip through. The NOT_FOUND-without-`renamed_to` part is the sibling's Finding 9.
- **Severity:** Medium.
- **Evidence:**
  - Probe: `work_ready {bogus:1}` returned `"Unknown parameter(s) for get_ready: bogus"`, and `issue_get {id:…}` returned `"Unknown parameter(s) for get_issue: id"` (`mcp_server.py:539` interpolates the canonical old name).
  - Probe: `issue_close` on an issue with a critical `must_consider` annotation returned `annotation_warnings[].suggested_actions: ["resolve_annotation","supersede_annotation","promote_annotation","carry_forward_annotation"]` (`db_annotations.py:1266-1271`). All four are rejected by `call_tool` as `Unknown tool` because they are `RENAME_MAP` keys (`mcp_server.py:891-898`).
  - Separately, `issue_get {id:…}` offers no "did you mean `issue_id`".
- **Recommendation:**
  - In 4.0, re-key handlers, tiers and argument maps to the served names. This is the end-state the sibling also proposes. With no shims wanted, the internal-old-name indirection should go entirely.
  - Emit served names in all prose.
  - Add a did-you-mean for near-miss parameters (`id` → `issue_id`).
  - Extend the guard to string literals that equal any retired name.
- **Confidence:** High. **Risk:** Low.

### LX-09. Critical `must_consider` annotations reach the agent only after the work is done

- **Tag:** NEW. The design delivered by `filigree-360ac7fc4c` chose closeout warnings. The "attention routing" goal implies delivery before the work starts.
- **Severity:** Medium.
- **Evidence:**
  - Probe: created `annotation_create(critical=true, intent="warning", links=[{issue, must_consider}])`.
  - `issue_get`: no annotation field. The only flags are `include_files` and `include_transitions`.
  - `work_start`: nothing.
  - `issue_close`: succeeded and attached `annotation_warnings`.
  - The only pre-work path is `annotation_attention_list` (niche tier), which nothing tells the agent to call.
- **Recommendation:**
  - Add a slim `attention: [{annotation_id, note, file_path}]` to `work_start`, `work_start_next` and `issue_get` whenever active critical `must_consider` links exist.
  - Count them in the snapshot.
  - Keep the close-time warning as the backstop. This is the general R3 channel.
- **Confidence:** High. **Risk:** Low.

### LX-10. The broadcast-board design (PRD-0001, T4) misses the decision moment

- **Tag:** NEW, against PRD-0001 and `filigree-c5a365a9be` (T4) / `filigree-0d0e64292e` (T2).
- **Severity:** Medium. The feature is not implemented yet, so the fix is cheap now. `product-critique.md` §6.4 separately questions whether to build it at all.
- **Evidence:**
  - The PRD's canonical case is mid-session ("I was editing `<module>` and another agent is active; I should back off").
  - T4 delivers only through the SessionStart hook, with a 30-minute window. An agent already working never sees a later post.
  - At SessionStart the agent has claimed nothing, so the PRD's relevance rule ("same ticket + different body") cannot be computed when the message is delivered.
  - T4's proposed text ("N — another agent broadcast a message!") delivers a count, not content, so it forces another call.
  - The identity model rests on Tabard's Body key, and Tabard is now archived (owner direction).
  - The success metric counts *posts*, not *receipts*.
  - Existing delivery seams go unused:
    - every `work_start`/`issue_update` response;
    - the already-installed `PreToolUse` hook on `mcp__filigree__.*` (`.claude/settings.json`, used today only for `ensure-dashboard`).
- **Recommendation:** If the bet proceeds:
  - Deliver unread, relevant broadcasts as a `notices` block on the responses agents already make (R3), filtered by the actor's claimed issue or touched file.
  - Keep SessionStart as the backstop and show content, not a count.
  - Measure "delivered to a different actor before a conflicting claim".
  - Rebase the identity on Filigree's own launch-bound actor (R1).
- **Confidence:** Moderate–High. Exactly what PreToolUse or PostToolUse hooks can inject into model context varies by host and is listed as a gap. **Risk:** Low.

### LX-11. No launch-bound identity, and the symptoms surface as misleading errors and fragmented attribution

- **Tag:** STILL-OPEN for the model (`filigree-c2009921cf` proposed, `filigree-81d3971467` approved; sibling Finding 10 covers the contract). NEW for the agent-facing symptoms below.
- **Severity:** Medium.
- **Evidence:**
  - **Misleading conflict.** An actorless `issue_update` or `comment_add` on a held issue returns `"Issue is claimed by a different assignee"` with `details.expected:"mcp"`. That happens even when the caller *is* the holder and only forgot `actor`, and the remedy (pass `actor`) is never stated.
  - **False instruction.** The project CLAUDE.md Weft block tells agents to pass identity via "the MCP launch-bound `--agent-id`". `filigree-mcp` accepts only `--project` (`mcp_server.py:1472-1473`).
  - **No guidance at all.** `data/instructions.md` never says which name to use.
  - **Fragmentation.**
    - `events` holds 54 distinct actors, 18 of them `claude*` variants (`claude`, `Claude`, `claude-opus-4-7`, `claude-opus-4.7`, `claude-opus48`, `claude-filigree`, …).
    - In 2026-05, 1,927 of 5,674 events (34%) were the anonymous defaults `cli`/`mcp`.
    - In the logged window (2026-06-07 → 2026-10-07, while this repo carried the Weft identity block), 257 of 257 MCP mutations carried identity. Variants persist anyway (`claude-filigree-codex-gs7-warpline-worklist`, `c18-filigree-agent`).
  - **Features that key on this string:** `work_release_mine(actor)`, `observation_list(actor)`, stale-claim attribution, ADR-008 holder defaults, and the broadcast board's distinct-actor metric.
  - 62 tools carry `actor`/`assignee`/`author` parameters, about 4.9 KB (~1.4K tokens) of repeated schema.
- **Recommendation:** R1. Bind identity at launch (`filigree-mcp --actor`, `FILIGREE_ACTOR`, written by `filigree install` and echoed in the managed block). Keep per-call identity only as a coordinator override. Until then, the error text should read: "No actor supplied (defaulted to 'mcp'); holder is 'agent-a'. Pass actor=<your identity>." Also fix the `--agent-id` claim in the Weft identity block for Filigree.
- **Confidence:** High. **Risk:** Medium. Identity is a cross-surface change; mitigation is in R1.

### LX-12. `work_start_next` says "No ready issues" when ready-but-unstartable issues exist

- **Tag:** NEW. The `{status:"empty"}` shape is locked by ADR-009 and not re-raised here. This finding is about the *reason text*.
- **Severity:** Medium–Low.
- **Evidence:** Probe: two ready items (a `proposed` feature and the seeded `planning` release). `work_start_next` returned `{"status":"empty","reason":"No ready issues matching filters"}`, while the server log said `all 2 candidate(s) failed to claim` (`db_issues.py:2352`, `mcp_tools/issues.py:1869`).
- **Recommendation:** Return a reason such as "2 ready, 0 startable: <id> (feature: proposed → needs approval); pass advance=true or ask a human", plus `skipped_unstartable: [...]`.
- **Confidence:** High. **Risk:** Low.

### LX-13. Three overlapping "I noticed something about a file" tools with no disambiguation

- **Tag:** NEW. The sibling's Finding 14 lists twin tools but not this conceptual overlap.
- **Severity:** Medium–Low.
- **Evidence:**
  - The tools are `observation_create` ("something you noticed in passing"), `finding_report` ("Report a single code finding (bug, smell, security issue) discovered by the agent"), `annotation_create` ("shared project annotation anchored to a file") and `comment_add`.
  - `observations.md` gives "A code smell in a neighbouring file" as a model observation, which is exactly `finding_report`'s use case.
  - None of the three descriptions names the others, and `observations.md` never mentions annotations or `finding_report`.
  - ToolSearch for the natural phrasing did not surface `observation_create` (LX-06).
- **Recommendation:**
  - Near term: a one-line decision rule in each description, mirrored in the skill:
    - noticed in passing and uncertain → observation;
    - durable context a future agent must read → annotation;
    - definite defect → issue (or `finding_report` only for analyzer-style output).
  - 4.0: shrink the concept count (R4).
- **Confidence:** Moderate–High. **Risk:** Low.

### LX-14. Agent-facing prose has drifted from behaviour

- **Tag:** STILL-OPEN for the `filigree-b48cd07e68` class: its fix pinned tool counts and `accepted_by_tools` only, not behavioural notes. REGRESSED for the release-semantics line, where the opposite drift was fixed in May.
- **Severity:** Medium.
- **Evidence:**

  | Location | Drift |
  |---|---|
  | `team-coordination.md:3,11,48` | Still framed as "filigree 2.0" |
  | `team-coordination.md:182` and `docs/agent-integration.md:87` | CONFLICT documented as `details: {current_assignee}`. Live shape is `{issue_id, observed, expected}`, so an agent parsing `current_assignee` gets nothing |
  | `team-coordination.md:201` | The false hook claim (LX-03) |
  | `docs/agent-integration.md` | `work_release(issue_id=…)  # Clear assignee without changing status`. Live default reverts wip→open |
  | MCP prompt `filigree-workflow` (`mcp_server.py:719-750`) | "Filigree data lives in `.filigree/`" and "editing `.filigree/templates/`". The store moved to `.weft/filigree/` in 3.0 |
  | `error-codes.md:67-69` | "no local `.filigree.conf`". The anchor is confless now |
  | `error-codes.md:43-49` | Legis closure-gate guidance, and Legis is archived |
  | `commands.md:113-120` | Points MCP agents at the HTTP route `GET /api/files/_schema` |
  | `commands.md:136` | "What should I work on? `filigree ready`, pick highest priority" contradicts `SKILL.md`'s "Ready ≠ startable" box |
  | `observations.md` triage | Never mentions `observation_link`, the N→1 merge, or `actor=` filtering, all shipped by `filigree-b0af8a661b` |

- **Recommendation:** R6. Use one generated agent guide, and pin behavioural notes with fixture tests that assert the documented envelope shapes against live probes.
- **Confidence:** High. **Risk:** Low.

### LX-15. Five parallel copies of "how to use Filigree" prose

- **Tag:** NEW.
- **Severity:** Low–Medium.
- **Evidence:**
  - The copies:
    - `data/instructions.md`
    - `SKILL.md` and its five references
    - the MCP prompt `_WORKFLOW_TEXT_STATIC`
    - `workflow_guide_get` (tips plus a full catalog)
    - `docs/agent-integration.md`
  - They disagree on the first step. The prompt says read `filigree://context`, the instructions say run `filigree session-context`, and the skill says session-context then `ready`.
  - `instructions.md:4` tells the agent to run `filigree session-context` even though the SessionStart hook already injected it, which is a duplicate ~700 tokens if followed literally.
  - `workflow_guide_get(pack="core")` returns 6,895 bytes, of which about 4.5 KB is the full 118-tool `tool_catalog`, re-sent for every pack queried.
- **Recommendation:** R6. Generate all five from one source, gate the catalog behind a flag, and change `instructions.md` to "the SessionStart hook prints the snapshot; call `session_context_get` only to refresh".
- **Confidence:** High. **Risk:** Low.

### LX-16. Schema prose hygiene: undocumented parameters, internal provenance tokens, and a rename that lost meaning

- **Tag:** NEW. The rename sub-item is REGRESSED (ADR-016).
- **Severity:** Low.
- **Evidence:**
  - 88 of 469 parameters have no description. They span 18 tools: all 13 annotation tools plus `plan_create`, `plan_step_add`, `file_list`, `file_timeline_get` and `finding_list`.
  - Only 21 of 118 descriptions name a sibling tool, so there is little "use X instead" guidance.
  - 16 internal-provenance tokens appear in served prose. Examples: `summary_get` "(filigree-cb980eee0d, P3.12.)", `issue_get.include_files` "…since Phase C3", and ADR-029 throughout.
  - Archived-component jargon sits on core tools: the `commit` parameters on `work_start` and `issue_close` say "(warpline seam) … so warpline can correlate". These are tokens the agent pays for and cannot act on.
  - `promote_observations_to_issue` (whose plural signalled N→1) was renamed `observation_promote_to_issue`, which reads as the 1:1 promote.
- **Recommendation:**
  - Make a description on every parameter a CI gate.
  - Strip ticket, ADR and phase IDs from served text.
  - Describe seam parameters by their agent value ("commit anchor; optional").
  - The 4.0 consolidation (R4) removes the trio.
- **Confidence:** High. **Risk:** Low.

### LX-17. SCHEMA_MISMATCH remedy ignores the install context and omits "do not retry"

- **Tag:** STILL-OPEN, residual of H-F18. The runtime diagnostics landed only in `mcp_status_get`.
- **Severity:** Low.
- **Evidence:**
  - Probe on a DB copy bumped to v30: every tool returned "To fix: upgrade filigree (`uv tool upgrade filigree` …)".
  - Meanwhile `mcp_status_get.runtime.install_context` was `"venv"`, where that command upgrades the wrong install. This repo itself runs a `.venv` CLI and `~/.local/bin/filigree-mcp`.
  - The text never says "surface to the user; do not retry". Only agents carrying the managed block know that.
  - Source: `install_support/version_marker.py:10-17`.
- **Recommendation:** Compose the remedy from the runtime block, add "Do not retry; surface to the user; details in mcp_status_get", and put the versions in `details`.
- **Confidence:** High. **Risk:** Low.

### LX-18. Recovery hints are static where structured data already knows the next step

- **Tag:** NEW. Generic `SAFE_MESSAGE` prose with structured details is locked (`filigree-d25e75cebf`) and not re-raised; a computed `hint` keeps that lock intact.
- **Severity:** Low.
- **Evidence:**
  - Every INVALID_TRANSITION carries `hint: "Use workflow_transition_list to see allowed state changes"`, even when `valid_transitions` and `missing_fields` are already inline.
  - For `verifying → closed`, the error says the transition "is not allowed", but the real remedy is "pass `fields={fix_verification: …}`".
  - `work_start` on a triage bug returns `next_action:"confirmed"`, while the tool's own description offers `advance=true`, which the error does not mention.
  - `workflow_status_explain(bug, triage)` lists `requires_fields: []`, but `workflow_transition_list` shows `missing_fields:["severity"]` for the same hop. This is H-F6 (requires vs missing) and is still confusing even though the docs now explain it.
- **Recommendation:** Compute `hint` from the structure:
  - `missing_fields` → "retry with fields={…}";
  - `next_action` → "or pass advance=true";
  - a done target unreachable → "status='wont_fix'".
  - Rename `missing_fields` to `blocking_fields` on transitions.
- **Confidence:** High. **Risk:** Low.

### LX-19. Orientation papercuts, and a stale critical path

- **Tag:** NEW. Includes an N-4 residual: `filigree-4e64621f70` added "(MCP: …)" asides, but the truncation lines lack them.
- **Severity:** Low.
- **Evidence:**
  - `hooks.py:120,134`: "(truncated, run 'filigree ready' …)" has no MCP equivalent.
  - CRITICAL PATH shows two `pending` dogfood steps (`filigree-6fd5b4db6b → filigree-1f9e9330fe`), untouched since 2026-09-01, under a milestone that already shipped. This answers the product-critique question: yes, session-context should stop surfacing a stalled path. Age-gate it, for example show it only if an item changed within 14 days, otherwise print one line ("critical path stalled 36d").
  - `session_context_get`'s description does not say the hook already delivered the same text.
- **Recommendation:** Fold into the R1 snapshot redesign.
- **Confidence:** High. **Risk:** Low.

### What works, and must not be refactored away

- **The SessionStart contract:**
  - small, at ~700 tokens;
  - sanitised titles (`_sanitize_context_title`);
  - honest-empty sections;
  - never fails the hook;
  - prints the remedy inside the snapshot on registry failure.
- **The managed block** is 17 lines with two "rules `--help` won't tell you". Progressive disclosure to the skill and references is the right shape.
- **INVALID_TRANSITION** carries `current_status`, `to_state`, `valid_transitions`, `next_action` and `missing_fields`. An agent can plan a retry from the payload alone. In the probe, the hard gate `verifying→closed` was fixed in one retry using `missing_fields`.
- **CONFLICT** carries structured `observed`/`expected`. `work_start` with `advance=true` walks soft hops and reports field gaps as `data_warnings` rather than blocking.
- **Slim shapes:** `work_ready` (with `startable`/`next_action`), `issue_search` rows, and `observation_list` are the right defaults to copy.
- **Ready ≠ startable** is taught well in `SKILL.md:47-54`. The snapshot just needs to match it.
- **The observation scope rule** in `observations.md` ("Would I have noticed this even if I weren't working on this task?") is good prompt design.

### Locked decisions observed but not re-raised

- `{status:"empty"}` envelope (ADR-009 note).
- Generic MCP `SAFE_MESSAGE` (`filigree-d25e75cebf`).
- `if_held=true` returning CONFLICT when the claim is held by someone else (H-F15).
- ADR-011 session deferral.

The 4.0 recommendations below change the substrate under some of these. Each is noted where it applies.

---

## Refresh recommendations (4.0), ranked by leverage

These incorporate the owner's direction for 4.0:

- 4.0 rebuilds every Weft component.
- Only Loomweave and Wardline are live. Hub, Legis, Warpline, Lacuna, Tabard and Plainweave are archived, but their seams stay and their specs get fixed.
- Rewrites are allowed and back-compat shims are not wanted.
- The audience is agent-first: the owner's fleet plus external agents, so onboarding and conceptual size matter.
- The catalog is **118 tools, not ~150**.

### R1. Actor-aware orientation on a launch-bound identity (highest leverage)

**Identity**

- `filigree-mcp --actor <id>`, plus `FILIGREE_ACTOR` for the CLI, written by `filigree install` and echoed in the managed block ("You are `<id>`").
- Per-call `actor`/`assignee`/`author` leave the default schemas (62 tools, ~1.4K tokens). An explicit override remains only on coordinator verbs (`work_reclaim`, `work_release` for a peer).
- Mutations with no resolvable identity are refused with a remedy, instead of defaulting to `"mcp"`.
- The identity model is Filigree-owned. The Tabard Body-key dependency is gone because Tabard is archived. Keep a `verified_principal` seam field for a future verifier (`filigree-81d3971467`).

**Session-context becomes a bounded "what should *I* do" brief, about 40 lines:**

1. who you are;
2. YOUR CLAIMS, with lease expiry;
3. STARTABLE NOW, with age and parent;
4. OTHERS ACTIVE: do not touch;
5. ATTENTION: critical annotations on your claims;
6. one-line counts for containers, blocked, stale claims, observations, and defect-only analyzer signal;
7. an age-gated critical path.

**Closes:** LX-02, LX-03, LX-11, LX-19, and the identity half of LX-10. It enables holder-checked release by default (LX-05) and scoping by `actor=me` everywhere. It also makes ADR-011's session model largely unnecessary for orientation: per-run checkpoints can stay deferred.

### R2. Bounded, slim-by-default reads in one sweep

- **Scope:** `issue_list`, `finding_list` (X-6), `change_list`, `file_list`, `observation_list`, plus mutation echoes (`comment_add`, `plan_create`, `issue_update`).
- **Defaults:** slim rows, a default limit of 25, a prominent cursor, and description or metadata only via `issue_get` or `response_detail=full`.
- **No shim:** 4.0 changes the default outright.
- **Steering:** session-context and every hint should steer only to bounded calls.
- **Closes:** LX-04 and the payload half of LX-01.

### R3. One decision-time delivery channel

- Add a small `notices` block on the responses agents already make: `work_start`, `work_start_next`, `issue_get`, `issue_update`, `issue_close`.
- Contents: critical `must_consider` annotations, peer activity on the same issue or file, unread broadcasts, and stale-claim or lease warnings for your own claims.
- SessionStart is the backstop, not the channel.
- **Closes:** LX-09 and LX-10. Any broadcast board, if retained, rides this channel.

### R4. A smaller conceptual surface and tool catalog

**Tool consolidation** (aligns with the sibling's catalog proposal):

- re-key everything to served names and delete `RENAME_MAP`-as-identity (LX-08);
- delete `type_get`;
- fold claim-only verbs into `work_start(transition=false)` or drop them (LX-07);
- collapse the observation promote trio, `finding_promote` and `…_and_attach_entity`, the label/comment/close batch twins (accept arrays), and the four annotation list tools.

**Concept consolidation** (needs an owner call; moderate confidence):

- Agents meet five ways to write text about code: issue, comment, observation, annotation, finding.
- Collapse observation and annotation into a single **note** with `ttl` and `critical`/`must_consider` flags.
- Keep findings as machine-ingested analyzer output.

**Evidence and target:**

- Usage: 29 distinct tools appear in this repo's 532 logged calls. Product-critique's fleet logs show 66 of 118 ever called, with the top 20 at 95%.
- **Target: a default profile of about 40–50 tools**, with an `admin`/`federation` profile behind a launch flag for eager-loading hosts.
- The full catalog is about 91 KB (~25K tokens) eager-loaded. The 21-tool typical-loop set is about 23 KB (~6.3K tokens).
- **Closes:** LX-13 and LX-16 (rename trio), and makes LX-06 tractable.

### R5. Selection aids where the ranker looks

- Every description opens with "Use when … / not for … (use X)".
- Re-tier by observed usage: core = `issue_get`, `issue_close`, `comment_add`, `issue_update`, `issue_search`, `issue_list`, `comment_list`, `issue_create`, `work_start`, `workflow_transition_list`, `observation_create`, `session_context_get`, plus `work_ready`.
- Move the tier into `_meta`.
- Strip ticket, ADR, phase and archived-component jargon from served prose.
- Require a description on every parameter (CI gate).
- **Closes:** LX-06, LX-07 and LX-16.

### R6. One generated agent guide

- **One source:** `instructions.md`, the skill and its references, the MCP prompt, `workflow_guide_get` and `docs/agent-integration.md` are all generated from a single source plus the live registry.
- **Pinned shapes:** behavioural notes (CONFLICT shape, release semantics, transition fields) are pinned by fixture tests that call the real handlers.
- **4.0 skill rewrite:**
  - lease lifecycle (stale → reclaim, heartbeat when work exceeds the lease, release-mine at session end);
  - observation link and merge;
  - a note/finding/issue decision table;
  - no "2.0" framing;
  - no Legis closure-gate text while Legis is archived (keep it in the seam spec instead).
- **Closes:** LX-14 and LX-15.

### R7. Computed recovery text

- `hint` is derived from `missing_fields`, `next_action` and reachable done states (LX-18).
- Actorless errors name the fix (LX-11).
- Unknown parameters get a did-you-mean.
- Any future renamed tool returns `renamed_to`. This is recovery text, not a shim.
- SCHEMA_MISMATCH composes its remedy from install context and says "do not retry" (LX-17).
- `work_start_next` explains what it skipped (LX-12).

### R8. Onboarding for external agents

- `filigree init` seeds nothing that appears as ready work, not even the "Future" release.
- The first-run snapshot prints a five-line "how to work here" that includes the bound identity.
- The managed block states the identity and the session loop: start → work → close with reason → release-mine.
- Goal: an external agent in an unfamiliar repo gets to "startable work claimed correctly" in two calls.

### R9. Federation seams for live components, specced so they read well for agents

- **Wardline:** the snapshot and `finding_list` defaults must assume telemetry-dominated volume (98% engine facts in this DB).
  - Spec a federation-neutral `signal_class` (`defect`/`telemetry`) assigned at ingest, so third-party scanners classify too. This resolves the strict-kind versus kind-less ambiguity in `filigree-56cb5c93f3`.
  - The banner keys on `signal_class=defect AND non-terminal AND unbridged`.
- **Loomweave:** keep `entity_id`/`entity_symbol` on `issue_create`, but describe them by agent value first ("bind this issue to a code entity so future agents find it from the code"), with no ADR numbers.
- **Archived seams (Warpline `commit`, `warpline_worklist_ingest`, Legis closure gate):**
  - keep them in the seam spec;
  - move them out of default-profile prose and the skill;
  - expose them only when a consumer is configured, so agents do not pay tokens for components that are not running.

---

## Confidence Assessment

**Overall confidence:** High for the observed behaviour. Moderate for the prescriptions, which depend on owner product calls.

| Finding | Confidence | Basis |
|---|---|---|
| LX-01 banner noise and 38K-token hint | High | Live hook output, read-only DB breakdown, CLI/MCP shared serializer (`mcp_tools/files.py:736`) |
| LX-02 unstartable "ready" | High | Live `ready --json` startable counts; probe with skill example plan; `hooks.py:125-135` |
| LX-03 actor-blind IN PROGRESS | High | `hooks.py:104-110`; probe |
| LX-04 unbounded core reads | High | Measured payloads on live tracker; log shows `no_limit:true` usage |
| LX-05 peer release taught and param ignored | High | Probe; `db_issues.py:1669-1691`; skill grep |
| LX-06 ToolSearch misranking | High (observed) / Moderate (cause) | Three live ToolSearch results; ranker internals unknown |
| LX-07 claim descriptions teach the two-step | High | Served descriptions vs `instructions.md:13-15` |
| LX-08 old names in recovery text | High | Probes; `mcp_server.py:539`; `db_annotations.py:1266-1271` |
| LX-09 annotations post-hoc | High | Probe sequence create → get → start → close |
| LX-10 broadcast delivery gap | Moderate–High | PRD and T4 text; host hook-injection capabilities not verified |
| LX-11 identity symptoms | High | Probe; `mcp_server.py:1472-1473`; events and log tallies |
| LX-12 misleading empty reason | High | Probe; code |
| LX-13 overlapping note tools | Moderate–High | Descriptions; skill text; one ToolSearch query |
| LX-14 prose drift | High | Line-cited diffs against probes |
| LX-15 duplicate guides | High | File reads; `workflow_guide_get` payload |
| LX-16 schema hygiene | High | Catalog audit script |
| LX-17 SCHEMA_MISMATCH remedy | High | Probe on DB copy |
| LX-18 static hints | High | Probes |
| LX-19 papercuts and stale critical path | High | Hook output; issue ages |
| R4 concept merge (observation + annotation → note) | Moderate | Product judgement; needs owner call |
| R1 as the highest-leverage change | Moderate–High | Count of findings it closes; sibling F10 independently converges |

## Risk Assessment

**Implementation risk:** Medium. R1 and R4 are structural, the rest is Low.
**Reversibility:** Moderate. The snapshot, descriptions and docs are easy to reverse. Identity binding and catalog consolidation are breaking, which suits a 4.0 boundary with no shims.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Launch-bound identity breaks multi-agent setups that share one MCP server process (e.g. HTTP/streamable mode serving several agents) | High | Medium | Bind per connection: stdio via launch arg, HTTP via header or token claim (as the sibling proposes). Keep explicit coordinator override verbs. Test the dual-agent-one-server case |
| A slim-by-default `issue_list` hides descriptions agents relied on | Medium | Medium | `issue_get` stays full. `response_detail=full` stays available. Snapshot rows include `parent_title` for context |
| `notices` on mutation responses becomes the new noise channel | Medium | Medium | Strict relevance filter (own claims, same issue or file). Cap at 3 items. Measure ignore rate. Keep the PRD's relevance-gating language |
| Re-tiering or consolidation by usage drops a tool the fleet's federation path needs | Medium | Low–Medium | Usage logs cover MCP only. HTTP consumers (Loomweave, Wardline) must be checked before any kill, per product-critique §6.2 |
| A concept merge (note) disrupts existing observation/annotation data | Medium | Low | A one-time data migration is acceptable in 4.0. Only 7 annotation rows exist fleet-wide (product-critique) |
| Banner changes hide a real kind-less third-party defect | Low | Low | `signal_class` assigned at ingest, with "unknown → defect" (keeps the current safe-side rule) |

## Information Gaps

1. [ ] **No tokenizer.** All token figures are bytes ÷ 3.6 (±20%). A real count against the target model would firm up R2/R4 budgets.
2. [ ] **ToolSearch ranker is a black box.** Three queries show misranking but not why. A 20-query golden set scored against expected tools would make LX-06 measurable and give R5 a regression test.
3. [ ] **Host behaviour for eager versus deferred catalogs** (Codex, Cursor, generic MCP clients) was not verified. The ~25K-token eager cost applies only where hosts load all tools.
4. [ ] **Hook-injection capabilities per host.** Whether PreToolUse or PostToolUse output reaches model context affects how LX-10/R3 are delivered.
5. [ ] **Wardline MCP was down this session.** Finding ingest and the `kind` vocabulary were read from stored rows, not exercised end-to-end.
6. [ ] **Loomweave was re-indexing.** No call-graph queries were used. Evidence is from direct reads and probes.
7. [ ] **Usage logs cover this repo's MCP only** (532 calls). CLI usage is unlogged. Fleet-wide numbers come from the sibling `product-critique.md`.
8. [ ] **No writes against the real tracker, by design.** Multi-agent races were probed in-process, single-threaded, against a scratch DB. Real concurrent multi-session behaviour was not exercised.

## Caveats and Required Follow-ups

### Before relying on this analysis

- Re-run the three ToolSearch queries, ideally a 20-query golden set, after any description change. Ranking claims are empirical and host-specific.
- Confirm with the owner whether the broadcast board proceeds (`product-critique.md` §6.4 questions it) before investing in LX-10's delivery design.
- Coordinate R1 with the sibling's Finding 10 and the R4 consolidation with the sibling's catalog proposal, so 4.0 makes one identity design and one catalog decision.

### Assumptions

- The owner's 4.0 direction (no shims; only Loomweave and Wardline live) holds for the agent-facing surface.
- This repo's tracker and log are representative of agent behaviour. It is the heaviest dogfooded instance, and its analyzer volume is Wardline-driven.

### Limitations

- Does not assess HTTP/dashboard surfaces (siblings' lanes) or MCP transport mechanics.
- Severity ratings judge agent work-loop impact, not security. Identity and release findings are framed as coordination and availability problems, per the owner's guidance.

### Recommended next steps

1. Quick wins on 3.x, if any further minor ships:
   - LX-01 banner and MCP aside;
   - LX-02 startable rendering;
   - LX-05 skill Stale Claims recipe;
   - LX-07 claim description lead line;
   - LX-08 served names in error text and `suggested_actions`;
   - LX-14 CONFLICT shape and path fixes.
   All are text or rendering changes.
2. A 4.0 ADR for R1 (identity plus snapshot) and R2 (bounded reads), written before catalog consolidation, since both reshape every schema.
3. An agent-experience eval harness (golden ToolSearch queries, a scripted "cold start → claim → close" transcript, and token budgets per call) as the 4.0 acceptance gate.

---

## Summary (machine-readable)

```json
{
  "reviewer": "llm-specialist (agent experience lane)",
  "date": "2026-10-07",
  "repo_ref": "release/3.3.0@59053a3",
  "overall_confidence": "High",
  "implementation_risk": "Medium",
  "reversibility": "Moderate",
  "catalog": {"served_tools": 118, "approx_bytes": 90898, "approx_tokens_eager": 25000, "tiers": {"core": 12, "common": 35, "niche": 71}, "typical_loop_21_tools_tokens": 6300, "claude_code_deferred_names_tokens": 1200},
  "top_findings": [
    {"id": "LX-01", "tag": "STILL-OPEN", "refs": ["filigree-4d489560e0", "filigree-56cb5c93f3", "filigree-8f6a1599fb", "filigree-b789da2a1e"], "severity": "High", "claim": "Session-start analyzer banner is 9955/9956 telemetry; the sole defect-signal is a smoke-test leftover; MCP hint (unfiltered finding_list) yields ~137KB/~38K tokens", "confidence": "High", "evidence": "src/filigree/hooks.py:163-173; db_files.py:3135-3183; live hook output"},
    {"id": "LX-02", "tag": "STILL-OPEN", "refs": ["filigree-406e6b7ee0"], "severity": "High", "claim": "READY TO WORK ignores startable: 8/15 shown are containers or unapproved; fresh init seeds a ready 'Future' release; no item age", "confidence": "High", "evidence": "src/filigree/hooks.py:125-135; summary.py:159-173; ready --json"},
    {"id": "LX-03", "tag": "NEW", "severity": "Medium-High", "claim": "IN PROGRESS (resume these) lists every agent's WIP without assignee", "confidence": "High", "evidence": "src/filigree/hooks.py:104-110"},
    {"id": "LX-04", "tag": "STILL-OPEN", "refs": ["H-F17", "H-F5", "H-F9", "sibling F6"], "severity": "High", "claim": "issue_list returns full records; agents call it with no_limit; ~91KB/~25K tokens per call here; comment_add/plan_create echo full issues", "confidence": "High", "evidence": "live payload measurements; .weft/filigree/filigree.log"},
    {"id": "LX-05", "tag": "NEW", "refs": ["sibling F3"], "severity": "High", "claim": "Skill teaches plain release of a peer's claim; work_release ignores expected_assignee unless if_held and strips live leases", "confidence": "High", "evidence": "skills/filigree-workflow/references/team-coordination.md:169-177; db_issues.py:1669-1691; probe"},
    {"id": "LX-06", "tag": "STILL-OPEN", "refs": ["filigree-e49e6469de"], "severity": "Medium-High", "claim": "Tier suffix inert; live ToolSearch ranks work_claim_next first for 'what next' and work_start 5th for 'claim and start'; core tier mismatches usage (84.6% vs 92.5% achievable)", "confidence": "High", "evidence": "ToolSearch probes; mcp_tools/tiers.py; MCP log"},
    {"id": "LX-07", "tag": "STILL-OPEN", "refs": ["2026-04-18 review pain point 2"], "severity": "Medium-High", "claim": "work_claim/work_claim_next descriptions instruct the claim-then-issue_update two-step the managed rules forbid", "confidence": "High", "evidence": "served descriptions; data/instructions.md:13-15"},
    {"id": "LX-08", "tag": "REGRESSED", "refs": ["ADR-016 / 63beb7c", "sibling F9"], "severity": "Medium", "claim": "Error and warning text names removed tools (Unknown parameter(s) for get_ready; suggested_actions resolve_annotation etc.)", "confidence": "High", "evidence": "mcp_server.py:539; db_annotations.py:1266-1271"},
    {"id": "LX-09", "tag": "NEW", "refs": ["filigree-360ac7fc4c"], "severity": "Medium", "claim": "critical must_consider annotations surface only at issue_close, not at issue_get/work_start", "confidence": "High", "evidence": "probe"},
    {"id": "LX-10", "tag": "NEW", "refs": ["PRD-0001", "filigree-c5a365a9be"], "severity": "Medium", "claim": "Broadcast board delivers only at SessionStart with a 30-min window; misses mid-session deconfliction; identity rests on archived Tabard", "confidence": "Moderate", "evidence": "docs/product/prd-0001-agent-broadcast-board.md"},
    {"id": "LX-11", "tag": "STILL-OPEN", "refs": ["filigree-c2009921cf", "filigree-81d3971467", "sibling F10"], "severity": "Medium", "claim": "No launch-bound identity: actorless CONFLICT says expected 'mcp'; CLAUDE.md claims a --agent-id filigree-mcp lacks; 54 actors / 18 claude variants", "confidence": "High", "evidence": "mcp_server.py:1472-1473; events table; probe"},
    {"id": "LX-12", "tag": "NEW", "severity": "Medium-Low", "claim": "work_start_next says 'No ready issues' when ready-but-unstartable exist", "confidence": "High", "evidence": "mcp_tools/issues.py:1869; probe"},
    {"id": "LX-13", "tag": "NEW", "severity": "Medium-Low", "claim": "observation_create / finding_report / annotation_create overlap with no disambiguation", "confidence": "Moderate", "evidence": "served descriptions; references/observations.md"},
    {"id": "LX-14", "tag": "STILL-OPEN", "refs": ["filigree-b48cd07e68"], "severity": "Medium", "claim": "Skill/docs/prompt drift: CONFLICT details shape, release semantics (REGRESSED), .filigree/ paths, 2.0 framing, HTTP schema route, missing lease/link guidance", "confidence": "High", "evidence": "team-coordination.md:182; docs/agent-integration.md:87; mcp_server.py:719-750"},
    {"id": "LX-15", "tag": "NEW", "severity": "Low-Medium", "claim": "Five parallel copies of usage prose disagree; workflow_guide_get re-sends full catalog", "confidence": "High", "evidence": "file reads; probe payload 6895B"},
    {"id": "LX-16", "tag": "NEW", "severity": "Low", "claim": "88/469 params undocumented; 16 internal ticket/ADR/phase tokens and archived-seam jargon in served prose; rename lost N->1 meaning (REGRESSED)", "confidence": "High", "evidence": "catalog audit"},
    {"id": "LX-17", "tag": "STILL-OPEN", "refs": ["H-F18"], "severity": "Low", "claim": "SCHEMA_MISMATCH remedy ignores install_context and omits do-not-retry", "confidence": "High", "evidence": "install_support/version_marker.py:10-17; probe"},
    {"id": "LX-18", "tag": "NEW", "severity": "Low", "claim": "Static hints despite structured next steps (fields=, advance=true)", "confidence": "High", "evidence": "probes"},
    {"id": "LX-19", "tag": "NEW", "severity": "Low", "claim": "Truncation lines lack MCP aside; stalled critical path shown without age-gating", "confidence": "High", "evidence": "hooks.py:120,134; issue ages"}
  ],
  "refresh_recommendations_ranked": [
    "R1 actor-aware orientation on launch-bound identity (highest leverage)",
    "R2 bounded slim-by-default reads in one sweep",
    "R3 decision-time notices channel on existing responses",
    "R4 smaller conceptual surface + ~40-50 tool default profile; re-key to served names; no shims",
    "R5 selection aids in indexed description text; re-tier by usage",
    "R6 one generated agent guide with behavioural fixture tests",
    "R7 computed recovery text",
    "R8 onboarding for external agents",
    "R9 federation seams: signal_class at ingest for Wardline; agent-first Loomweave prose; archived seams out of default prose"
  ],
  "blocking_gaps": ["no tokenizer", "ToolSearch ranker opaque", "eager-vs-deferred host behaviour unverified", "Wardline MCP down"],
  "recommended_next_steps": ["3.x text/render quick wins (LX-01,02,05,07,08,14)", "4.0 ADR for R1+R2 before catalog consolidation", "agent-experience eval harness as 4.0 acceptance gate"]
}
```
