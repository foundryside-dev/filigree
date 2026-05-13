# MCP Senior-User Review Master Ship Checklist

Date: 2026-05-13

This consolidates the senior-user review documents into one ship checklist.
It is a review-corpus triage artifact, not a fresh implementation audit of
every behavior. Status notes use the later review passes and a live Filigree
tracker snapshot taken with `uv run filigree session-context` and
`uv run filigree list --json --label-prefix=source: --limit=500`.

## Source index

| Tag | Source document |
|---|---|
| A | `docs/plans/completed/2026-05-06-mcp-senior-user-review.md` |
| B | `docs/plans/completed/2026-05-06-mcp-senior-user-review-b.md` |
| C | `docs/plans/completed/2026-05-06-mcp-senior-user-review-c.md` |
| D | `docs/plans/2026-05-06-mcp-senior-user-review-d.md` |
| E | `docs/plans/2026-05-09-mcp-senior-user-review-e.md` |
| F | `docs/plans/2026-05-09-mcp-senior-user-review-f.md` |
| G | `docs/plans/2026-05-09-mcp-senior-user-review-g.md` |
| H | `docs/plans/2026-05-12-mcp-senior-user-review-h.md` |

## Ship-blocking checklist

These are the consolidated findings after the ADR-003 through ADR-011 decision
pass. The product decisions are now locked; unchecked items represent remaining
implementation, documentation, tracker, or verification work needed to make the
product match those decisions.

### P1 - must implement or explicitly defer

- [x] **Implement strict unknown MCP parameter rejection.**
  Source: G1.
  Decision: [ADR-006](../architecture/decisions/ADR-006-mcp-unknown-parameter-validation.md)
  chooses strict `VALIDATION` errors for unknown MCP parameters.
  Problem: tools accept plausible but unsupported parameters such as
  `get_ready(priority_min=...)`, `export_jsonl(label=...)`, and
  `update_issue(add_labels=...)`, then silently ignore them.
  Locked outcome: unknown MCP parameters are invalid; no silent ignore and no
  soft-warning compromise.
  Ship criterion: every MCP tool rejects unknown parameters with `VALIDATION`,
  naming the unknown parameter and target tool.
  Resolution: `call_tool()` now validates arguments against the registered
  tool schema before handler dispatch, returning `VALIDATION` with the target
  tool and unknown key names. Regression coverage pins single-key,
  multi-key, and unknown-tool precedence behavior.

- [x] **Implement schema-mismatch hard stop and binary diagnostics.**
  Source: E5, F1, H18, plus earlier A17/C7.
  Tracker: `filigree-092758141c` is closed.
  Decision: [ADR-004](../architecture/decisions/ADR-004-schema-mismatch-policy.md)
  treats true `SCHEMA_MISMATCH` as a hard write-safety boundary.
  Problem: docs say `SCHEMA_MISMATCH` should make most tools return an error,
  but later reviews found normal writes succeeding while `get_mcp_status`
  reported mismatch. H also identified a local venv versus uv-tool dashboard
  mismatch that can look like MCP breakage.
  Locked outcome: true `SCHEMA_MISMATCH` gates normal writes; compatible drift
  must use a different advisory state.
  Ship criterion: write tools fail closed under true mismatch, `get_mcp_status`
  remains available, compatible drift is not called `SCHEMA_MISMATCH`, and
  session-start diagnostics identify the binary/schema pair producing warnings.
  Resolution: degraded-mode startup now leaves only `get_mcp_status` callable;
  all other MCP tool calls return structured `SCHEMA_MISMATCH`. The status
  payload includes `runtime` diagnostics for the executing Python binary,
  resolved binary path, entrypoint, module file, package root, venv root, and
  install context, with warm-degraded regression coverage in
  `tests/test_schema_mismatch.py`.

- [x] **Provide first-class observation triage and session cleanup filters.**
  Source: B19, D10, H2, H4.
  Tracker: `filigree-b0af8a661b` is closed.
  Problem: `list_observations` lacks actor, age, priority, source, and sort
  filters; agents cannot link or merge observations into existing issues; stale
  observations accumulate across review sessions.
  Ship criterion: observation triage supports actor/session filtering,
  linking/duplicate dispositions, batch dismissal or promotion by filter, and
  preservation of observation evidence.
  Resolution: schema v13 adds durable `observation_links` evidence snapshots;
  `list_observations` filters by actor, file, source issue, priority, age, sort,
  and direction; MCP/CLI expose `link_observation`, `batch_link_observations`,
  and `promote_observations_to_issue`. First-class session/run IDs remain
  deferred by ADR-011, so session cleanup is represented through actor/source
  issue filters and explicit batch triage.

- [x] **Make `report_finding` side effects explicit, traceable, and slim.**
  Source: D2, E1, F3, G5, H3, H12.
  Decision: [ADR-007](../architecture/decisions/ADR-007-report-finding-semantics.md)
  defines `report_finding` as a manual single-finding write by default, with
  paired observations explicit rather than hidden.
  Related tracker: `filigree-42e0aa3c89` is closed, but G/H still flag product
  and response-shape friction.
  Problem: `report_finding` auto-creates observations; older passes found them
  unlinked, later passes say cleanup is wired but the behavior is still hidden
  and the response exposes batch-style counters for a single write. H also notes
  no `actor` parameter for manually reported findings.
  Locked outcome: `report_finding` is a manual single-finding write by default;
  paired observations are explicit, not hidden.
  Ship criterion: add actor attribution, make paired observation creation
  opt-in or otherwise explicitly requested, keep linked observation cleanup
  transactional, and return a slim finding result plus optional
  `observation_id`.
  Resolution: `report_finding` now writes only a finding by default. Paired
  observations require `create_observation=true` in MCP or
  `--create-observation` in CLI, actor attribution is preserved for opted-in
  observations, and the default response remains slim.

- [x] **Define the end-of-session cleanup story for mixed-type scratch work.**
  Source: E3, F2/F6, H1, H4.
  Tracker: `filigree-958da5d6b5` is closed.
  Decision: [ADR-005](../architecture/decisions/ADR-005-workflow-enforcement-and-cleanup-paths.md)
  splits normal workflow-respecting close paths from explicit cleanup/archive
  paths.
  Conflict: D/E/F wanted `close_issue` and `batch_close` to respect workflow
  transitions because they bypassed templates. H found the corrected behavior
  makes routine scratch cleanup fail unless agents use `force=true`.
  Locked outcome: normal close/status paths respect workflow templates; cleanup
  and archive are explicit operational lanes.
  Ship criterion: keep workflow enforcement on normal close paths, and add or
  document cleanup/archive primitives with clear scope, previews or metadata for
  broad changes, and actor/session/label filters so agents do not sweep each
  other's artifacts.
  Resolution: `docs/mcp.md` and `docs/cli.md` now include one ordered
  end-of-session cleanup recipe covering live claim release, observation
  promotion/link/dismissal, finding triage, temporary file-record deletion,
  scratch issue archive, and compaction. The recipe requires a session-unique
  label, dry-run previews for claim release, default refusal before forced file
  deletion, and label-scope confirmation before archive.

- [ ] **Finish stale-claim and handoff discovery.**
  Source: A1/A4, B2/B12, D3, E4, G3, H4/H15.
  Tracker: claim leases were implemented in `filigree-76d27e95c2`, but G/H
  leave follow-up friction.
  Problem: historical assigned ready items were fixed, but stale/done claims can
  still leak into stale-claim discovery, released or orphaned work needs a clear
  handoff pool, and there is no "release everything I am holding" affordance.
  Ship criterion: stale-claim tools default to non-done work, handoff/orphan
  work is discoverable, and session cleanup can safely release current actor
  claims without knowing every issue ID.

### P2 - significant friction to resolve for a polished ship

- [x] **Make claim-aware writes hard to misuse.**
  Source: D1, E2, G2, H "works well" notes.
  Decision: [ADR-008](../architecture/decisions/ADR-008-claim-aware-write-defaults.md)
  makes actor-scoped claim safety the default for writes to held issues.
  Prior problem: `expected_assignee` landed and worked, but G found the default
  remained opt-in; writes to held work still succeeded unless the agent knew the
  flag.
  Locked outcome: when `actor` is present and the issue is held, the default
  expected holder is `actor`; editing someone else's held work requires an
  explicit override.
  Ship criterion: apply the actor-as-default precondition consistently to
  claim-aware write tools, return `CONFLICT` on mismatch, document override
  semantics, and keep error messages naming observed and expected holders.
  Resolution: issue update/close, comments, labels, and batch write paths now
  default the expected holder to `actor`/comment `author` when the target issue
  is held. Explicit `expected_assignee` remains the coordinator override, and
  conflict responses name both observed and expected holders.

- [x] **Fix live-work search and catch-up filters.**
  Source: D6, D11, E7/E8, G4, H4.
  Tracker: `filigree-0f3794c1c3` is closed.
  Problem: `search_issues` tokenizes away agent session prefixes and returns
  archived/done results without status filters; `get_changes` can be a heartbeat
  firehose and does not have sufficiently expressive filters for catch-up.
  Ship criterion: search supports live-work filters and predictable handling of
  bracket/hyphen actor prefixes; changes support multi-value actor/type/category
  filters or sensible heartbeat separation.
  Resolution: search already supports bracket/hyphen literal fallback and
  `status_category`. CLI `changes`/`get-changes` now match MCP catch-up filters
  for actor, issue, label, event type, same-timestamp cursor, and heartbeat
  separation; heartbeat events are excluded by default and restored with
  `--include-heartbeats`.

- [x] **Keep MCP self-discovery and docs generated from the live registry.**
  Source: D/E/F/G schema drift findings, H11, G10.
  Tracker: `filigree-b48cd07e68` is open P2.
  Problem: `get_schema.entity_id_prefixes.*.accepted_by_tools`, docs, tool
  counts, and docstrings have repeatedly drifted from live tools.
  Ship criterion: generated docs/schema come from the tool registry, and tests
  pin key counts and accepted-by-tool mappings.
  Resolution: `get_schema.entity_id_prefixes.*.accepted_by_tools` is now
  derived from the live MCP tool registry input schemas, and docs/tool-count
  drift is pinned by a registry-backed test against `docs/mcp.md`.

- [ ] **Finish ID and relationship naming consistency.**
  Source: A2/A15, B4/B17, C4, D9/D14, E15, G6/G10.
  Problem: prior major `id`/`issue_id` holes were fixed, but G still reports
  `parent_id` versus `parent_issue_id` and several dependency parameter naming
  families.
  Ship criterion: issue relationships use one public vocabulary
  (`parent_issue_id`, `from_issue_id`, `to_issue_id`, etc.) with temporary
  aliases and explicit deprecation notes where needed.

- [ ] **Normalize common response envelopes without losing useful slim paths.**
  Source: A14, B7, C3, D12/D14/D15, E14/E19/E21/E22, F4, G11, H5/H8/H9/H13/H14/H17.
  Decision: [ADR-009](../architecture/decisions/ADR-009-response-shape-philosophy.md)
  chooses predictable envelopes, slim defaults, and `response_detail` for full
  records where useful.
  Problem: writes and list-like tools still vary: full issue versus ack-only,
  bare arrays for transitions, success versus empty shape for `start_next_work`,
  verbose `add_comment`/`list_issues`/`get_plan`, duplicate stats aliases.
  Locked outcome: public living surfaces use predictable envelopes,
  slim-by-default mutation results, and `response_detail` where full records are
  useful.
  Ship criterion: wrap list-shaped tools consistently, keep batch envelopes
  canonical, add `response_detail=slim|full` where needed, migrate empty result
  shapes into the same envelope families, and document compatibility aliases.

- [ ] **Clarify workflow-template semantics and soft enforcement.**
  Source: C1/C2/C6/C8/C9, F5, H6, H10.
  Problem: reviews found confusing `start_work` target selection, missing
  `data_warnings`, duplicate warning events, reopen target questions, template
  defaults that might be display hints rather than applied defaults, and
  ambiguous transition field names.
  Ship criterion: template defaults, warning channels, reopen behavior, and
  transition readiness fields have a single documented meaning and regression
  coverage.

- [ ] **Resolve archive and done-status model consistently.**
  Source: E10, G3, G4, H1, H18.
  Decision: [ADR-010](../architecture/decisions/ADR-010-archived-status-model.md)
  defines archived work as outside active workflow and never ready/open in
  public behavior.
  Tracker: `filigree-aec52efb9b` is open P2.
  Problem: archived items have appeared as open/ready in record hydration while
  query surfaces special-case them; cleanup and stale-claim tools need a clear
  done/archived category model.
  Locked outcome: archived work is outside active workflow and must never
  hydrate or query as ready/open work.
  Ship criterion: archived records are excluded from active ready/blocked/search
  and stale-claim discovery by default, record hydration never reports them as
  open/ready, and stats/filtering expose archived state consistently.

- [ ] **Improve plan-editing and plan-read ergonomics.**
  Source: A6/A7, B8, D16/D18, E20/E21, G6/G7/G9, H9.
  Problem: earlier passes wanted file-backed plan creation, plan-native edits,
  subtree labeling, and whole-tree cleanup; later passes found cross-phase deps
  can survive moves silently, dry-run compaction has no preview, and plan reads
  lack slim modes.
  Ship criterion: plan operations expose safe high-level edits, warnings for
  surprising dependency carry-forward, predictable dependency ID syntax, and
  slim/full response modes.

- [ ] **Clarify close, dismiss, and reason semantics across issue and finding lifecycles.**
  Source: F2/F6, H10/H16, E16/E17.
  Problem: `undo_last` close semantics were fixed later, but close/dismiss
  reasons still live in different fields, `dismiss_finding` defaults to
  `false_positive`, and dismissed findings have no archival path.
  Ship criterion: reason storage and dismissal status defaults are documented
  and consistent enough for history consumers; finding archival or expiry is
  defined.

- [ ] **Hydrate blockers and context where agents need one-call triage.**
  Source: H7 plus earlier ready-context findings.
  Problem: `get_blocked` returns blocker IDs without titles/statuses, requiring
  N follow-up calls.
  Ship criterion: add `include_blockers=true` or an equivalent slim context mode.

- [ ] **Resolve the requirement-type documentation mismatch.**
  Source: H11.
  Problem: docs/docstrings mention `type='requirement'`, but `list_types` did
  not expose that type in H's run.
  Ship criterion: enable the requirements pack by default, or update docs and
  tool descriptions to say it is optional.

- [ ] **Thread actor identity through file/finding write events.**
  Source: H3 and current tracker cleanup.
  Tracker: `filigree-564438a17e` is open P2.
  Problem: manual findings and file/finding write events need durable actor
  attribution.
  Ship criterion: file/finding writes carry actor identity consistently in
  operational history events and public records where relevant.

- [ ] **Validate annotation handoff operations against real links.**
  Source: G8.
  Problem: `carry_forward_annotation` can acknowledge a source target that was
  not actually linked, creating misleading handoff history.
  Ship criterion: carry-forward validates an active link to `from_target_id` or
  fails with an actionable `VALIDATION` error.

### P3 - polish, but should be triaged before release cut

- [ ] Add actor/session filters to `archive_closed` or document session-unique
  labels for cleanup. Source: F7, E18.
- [ ] Add `get_summary` JSON or make markdown summary clearly human-only.
  Source: D12.
- [ ] Preview stale observations in `get_summary`. Source: F8.
- [ ] Document canonical `get_stats` fields and compatibility aliases according
  to ADR-009; deprecate legacy aliases only with an explicit migration path.
  Source: H13.
- [ ] Echo comment text or a structured comment in `add_comment` responses if
  the response remains slim. Source: H14.
- [ ] Decide whether `release_claim(if_held=true)` should conflict or no-op
  when another actor holds the claim. Source: H15.
- [ ] Add `get_stale_claims(expires_within_hours=...)` if proactive
  heartbeating is a supported workflow. Source: E23.
- [ ] Add optional issue events to file timelines if file-centered history is a
  first-class workflow. Source: A16/B18.
- [x] Closed scratch/file records do not need immutable-history treatment.
  ADR-003 establishes operational durability, and ADR-005 allows explicit
  cleanup/archive lanes. Remaining work belongs under the cleanup/archive
  implementation items above. Source: A18/B16.

## Decision outcomes locked

These review conflicts no longer need maintainer arbitration unless new
evidence contradicts the ADRs.

1. **Record immutability versus janitorial needs.**
   Outcome: records are durable for operational utility, not audit-proof
   evidence. Cleanup, archive, correction, and compaction are valid product
   features when scoped honestly.
   Decision: [ADR-003](../architecture/decisions/ADR-003-operational-durability-not-audit-proofing.md).

2. **Schema mismatch: hard stop or advisory drift.**
   Outcome: true `SCHEMA_MISMATCH` gates normal writes. Compatible drift must
   use a different advisory state and list safe capabilities.
   Decision: [ADR-004](../architecture/decisions/ADR-004-schema-mismatch-policy.md).

3. **Workflow enforcement versus cleanup convenience.**
   Outcome: normal lifecycle operations respect workflow templates. Cleanup and
   archive are separate explicit lanes with clear scope.
   Decision: [ADR-005](../architecture/decisions/ADR-005-workflow-enforcement-and-cleanup-paths.md).

4. **Unknown MCP parameter handling.**
   Outcome: unknown MCP parameters are rejected with `VALIDATION`; they are not
   silently ignored and not merely warned.
   Decision: [ADR-006](../architecture/decisions/ADR-006-mcp-unknown-parameter-validation.md).

5. **`report_finding` as scanner ingest versus agent note.**
   Outcome: `report_finding` is a manual single-finding write by default.
   Paired observation creation is explicit and slimly reported.
   Decision: [ADR-007](../architecture/decisions/ADR-007-report-finding-semantics.md).

6. **Claim coordination defaults.**
   Outcome: actor-scoped claim safety is the default for writes to held issues;
   cross-claim mutation requires explicit override.
   Decision: [ADR-008](../architecture/decisions/ADR-008-claim-aware-write-defaults.md).

7. **Response normalization versus payload size.**
   Outcome: predictable envelopes and slim defaults win; full records are
   requested through `response_detail` where useful.
   Decision: [ADR-009](../architecture/decisions/ADR-009-response-shape-philosophy.md).

8. **Archived status model.**
   Outcome: archived work is outside active workflow and must never appear as
   ready/open in public behavior.
   Decision: [ADR-010](../architecture/decisions/ADR-010-archived-status-model.md).

9. **Agent session/run model.**
   Outcome: first-class session/run records are deferred beyond the 2.0 ship
   bar. Actor strings, claims, comments, observations, findings, and events are
   the 2.0 coordination model.
   Decision: [ADR-011](../architecture/decisions/ADR-011-agent-sessions-deferred-beyond-2-0.md).

## Reported fixed or mostly resolved by later passes

Keep these here to avoid re-filing old findings unless fresh verification shows
regression.

- [x] Assigned issues no longer appear in ready queues. Source: A1/A4/B12;
  tracker `filigree-9f3e99c84c` closed.
- [x] Major `issue_id` rename gaps and stale `promote_observation` snapshot
  were fixed or merged into canonical tickets. Source: A2/A3/B1/B4/C4;
  tracker `filigree-d240a738db`, `filigree-38750e20cd` closed.
- [x] MCP session-start alias and plan tool parity were largely addressed.
  Source: A5/A6/A7; tracker `filigree-a664709859`,
  `filigree-a3bfd42460`, `filigree-a958cd4e26` closed.
- [x] Scanner risk metadata and batch remove-label were addressed. Source:
  A9/A10; tracker `filigree-2fbb61fe08`, `filigree-d92e9e0c22` closed.
- [x] `get_critical_path` empty result now includes an explanatory note.
  Source: A13/B20/E "works well"; tracker `filigree-5546e76254` closed.
- [x] Priority-like labels now have an explicit reject/reserved policy.
  Source: B14/C10; tracker `filigree-fb7b45c056` closed.
- [x] `get_template` and `get_type_info` overlap was clarified as aliasing.
  Source: C5; tracker `filigree-b9354a21f5` closed.
- [x] File language inference was added for registered source paths. Source:
  C13; tracker `filigree-b9f56e9e84` closed.
- [x] `release_claim` gained an idempotent `if_held` mode and wip release now
  rejoins open-category work by default, though H15 still questions the
  held-by-other semantics. Source: C12/D3/E4/G "works well"/H15; tracker
  `filigree-181c3fbb36` closed.
- [x] `undo_last` close composite behavior, no-actor heartbeat default,
  close workflow enforcement, `get_blocked` wip inclusion, and linked
  `source_finding_id` cleanup were reported fixed by G/H. Source: G "what works
  well", H "what works well".

## Current live tracker coverage snapshot

Open issues already covering parts of this checklist:

- `filigree-b0af8a661b` - structured observation triage, P1.
- `filigree-b48cd07e68` - generated MCP self-discovery and docs, P2.
- `filigree-aec52efb9b` - archival-status model implementation, P2
  (policy locked by ADR-010).
- `filigree-564438a17e` - actor identity through file/finding write events, P2.

Deferred by ADR rather than 2.0-blocking:

- `filigree-c2009921cf` - agent session/run checkpoints, deferred beyond 2.0
  by ADR-011.

Recent release:v2.0 issues from observation cleanup that are adjacent but not
core senior-user review consolidation:

- `filigree-660e79b93d` - corrupt `issues.fields` JSON guard.
- `filigree-c5dd08e240` - `register_file` duplicate path race.
- `filigree-7feef2cf67` - dashboard `min_findings` SQLite bounds.
- `filigree-c31fe51a34` - finding CLI mutations refresh context; currently
  `fixing` and assigned to `codex`.

## Suggested next triage pass

1. Convert every unchecked P1/P2 item that is not already covered into a
   Filigree issue under an MCP 2.0 ship-readiness epic.
2. For each existing issue, add this checklist path and source tags to the
   issue description or comments so implementation can trace back to the
   review evidence.
3. Before release, run a live MCP+CLI parity smoke that exercises the final
   checklist, not just the unit suite.

Decision update: the major conflict decisions are now recorded in ADR-003
through ADR-011. Remaining unchecked checklist entries should be implemented,
converted into tracker issues, or explicitly deferred against those ADRs.
