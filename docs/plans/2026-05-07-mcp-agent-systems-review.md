# Filigree MCP Agent-Systems Effectiveness Review

**Date:** 2026-05-07 local time (live MCP events recorded 2026-05-06 UTC)
**Reviewer:** Codex, acting as a senior representative for agentic systems using the MCP
**Parent issue:** `filigree-ed2ccaf10d`
**Method:** Replayed the documented startup workflow through live MCP (`session_context`, `get_summary`, `get_ready`, `get_schema`) and then exercised active work, observation capture, finding reporting, handoff, event catch-up, file timelines, and cleanup against the working `.filigree` database. I also checked the prior 2026-05-06 senior-user findings and current source/docs.

## Executive Summary

The individual MCP tools are much better than the prior senior-user review found. The earlier paper cuts around `session_context`, ready queue trust, ID field consistency, batch remove labels, plan editing, file timeline issue events, scanner risk metadata, and change cursors have mostly been fixed and closed in Filigree. The live surface now feels like a credible tracker interface for a foreground coding agent.

The next layer is not more CRUD polish. The missing pieces are "agent operating system" primitives: leases, heartbeats, durable session intent, structured observation triage, and generated self-discovery. The observation system is the clearest success signal. It exists because agents repeatedly noticed unrelated problems and needed a zero-derailment way to preserve them. That same product instinct should be generalized: the MCP should make it cheap for agents to notice, defer, checkpoint, hand off, recover, and later convert side knowledge into tracked work.

## What Is Working Well Now

- `session_context` exists as a first-class MCP tool and matches the startup instruction shape.
- `get_ready` now excludes assigned work. The stale P1 `filigree-1c7b2776a5` no longer appears as claimable ready work because it is assigned to `claude-debug`.
- `get_issue(include_transitions=true)` is still the best single-issue working context: status, fields, readiness, and transition blockers are visible together.
- `claim_issue` now supports released in-flight handoff. In the scratch run, `release_claim` left `filigree-4576602301` in `in_progress` with no assignee, and `claim_issue(..., assignee="agent-bravo")` claimed it atomically.
- `get_changes` now has useful filters and returns `next_since`.
- `list_scanners` now exposes risk/egress metadata: `execution_mode`, `may_send_contents`, `requires_dashboard`, `requires_approval`, and `risk_summary`.
- `get_file_timeline(include_issue_events=true)` now merges associated issue events and uses entity-specific IDs.
- Plan-native tools now exist (`create_plan_from_file`, `add_plan_step`, `retarget_plan_dependency`, `move_plan_step`, `label_plan_tree`, `label_subtree`).

## New Findings

### P1 - Claim Leases And Stale Reclaim

**Issue:** `filigree-76d27e95c2`

Ready is now honest, but abandoned ownership can disappear from the startup path. Live evidence: `filigree-1c7b2776a5` is a P1 open bug assigned to `claude-debug`, last updated 2026-04-18, with a comment saying the fix was staged but not committed. It is no longer in `get_ready`, which is correct, but `session_context` does not surface it as stale or reclaimable work.

For agentic systems, assignment needs lease semantics, not just a string field. Add claim metadata such as `claimed_at`, `last_heartbeat_at`, and optionally `claim_expires_at`; tools such as `heartbeat_work`, `get_stale_claims`, and `reclaim_issue(expected_assignee, reason)`; and a `release_claim(reason=...)` path that captures why a handoff happened.

### P1 - Observation Triage Needs Link/Merge/Cluster

**Issue:** `filigree-b0af8a661b`

Observations are successful enough that they now need scale mechanics. The live queue has 15 pending observations, including sibling reports against the same file, synthetic review leftovers, and observations that overlap existing issues. Today an agent can promote to a new issue or dismiss, but cannot link an observation to an existing issue, mark it duplicate-of an issue, merge several observations into one issue, or batch-triage with structured disposition metadata.

Add tools like `link_observation(observation_id, issue_id, disposition, reason)`, `batch_link_observations`, `cluster_observations` or `suggest_observation_groups`, filters by priority/actor/source issue/age, and a "promote many into one issue" flow that preserves all source observation IDs.

### P2 - Agent Sessions Need Durable Checkpoints

**Issue:** `filigree-c2009921cf`

The initial workflow is more than "read context." It includes a user assignment, constraints, current workspace, and a reason this session exists. Filigree currently records actor strings, events, comments, observations, and issues, but not the session/run envelope that ties them together. `get_changes(actor=...)` cannot answer "what did this run intend, touch, observe, verify, and leave unfinished?"

Consider `start_agent_session`, `checkpoint_session`, `finish_agent_session`, and `get_session_changes`. A session should link claimed issues, observations, findings, comments, touched files, verification commands, blockers, and handoff summary.

### P2 - `report_finding` Silently Creates An Unlinked Observation

**Issue:** `filigree-42e0aa3c89`

Live evidence: `report_finding(...)` returned `finding_id="filigree-sf-e13aa808fd"` but did not mention observation creation. Immediately after, `list_observations(file_id="filigree-f-42b1a8c90a")` showed a new `[agent] ...` observation (`filigree-obs-e958a165a9`) for the same message, with no visible `finding_id` link and empty `source_issue_id`. Source confirms `src/filigree/mcp_tools/scanners.py:301-306` calls `process_scan_results(..., create_observations=True)`.

The dual-write may be intentional, but it must be explicit and correlated. Either return the created `observation_id` and link it to the finding, or make observation creation opt-in/opt-out. Otherwise findings and observations become overlapping queues with unclear cleanup semantics.

### P2 - MCP Self-Discovery And Docs Drift From The Live Registry

**Issue:** `filigree-b48cd07e68`

Live/self-doc evidence:

- `get_schema` omits `batch_promote_observations` from observation ID consumers even though the tool exists.
- `docs/mcp.md:3` says 71 tools, while `docs/agent-integration.md:7` says 73, and live source registers more.
- `docs/agent-integration.md:52` says schema mismatch returns `NOT_INITIALIZED`; the current contract and `get_mcp_status` use `SCHEMA_MISMATCH`.
- `docs/agent-integration.md:93` says `release_claim` releases back to open; live behavior leaves status in `in_progress` and clears assignee.

Agents treat tool metadata and bundled docs as executable truth. Generate MCP reference material and ID-consumer schema from the live `Tool` registry and typed input models, then add docs tests for tool count and key behavioral notes.

### P1 - Shared File Annotations With Provenance

**Issue:** `filigree-360ac7fc4c`

Follow-up design: [2026-05-07-mcp-shared-file-annotations-design.md](2026-05-07-mcp-shared-file-annotations-design.md).

Annotations are shared file notes, distinct from observations. They preserve durable context such as "future agents working this epic need to know why this file section matters." They should capture file anchors, commit/diff/checksum provenance, the agent's current context, typed links to epics/issues/findings/observations/sessions, and a boolean `critical` flag for attention routing.

## Product Principle

The observation system worked because it matched a real agent behavior: "I saw something important, but fixing it now would derail the task." Future MCP improvements should look for the same pattern. Where agents currently use comments, labels, actor strings, local memory, or plain prose to preserve state, Filigree should decide whether the pattern deserves a first-class primitive.

Strong candidates:

- noticing side work -> observations, linked/clustered triage;
- owning work -> leases, heartbeats, stale reclaim;
- doing a run -> session/checkpoint records;
- handing off -> structured transfer with current hypothesis, touched files, and verification state;
- learning from review -> generated self-docs and schema, not handwritten drift.

## Scratch Cleanup

Created and closed scratch issue `filigree-4576602301`. Dismissed scratch observations `filigree-obs-84f0dbbd45` and `filigree-obs-e958a165a9`. Dismissed scratch finding `filigree-sf-e13aa808fd`. The five actionable findings above remain open under parent epic `filigree-ed2ccaf10d`.
