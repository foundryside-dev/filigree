# MCP Senior-User Friction Review — 2026-05-12 (review-h)

Reviewer actor: `mcp-review-h`. Method: drove every workflow against the live
`.filigree/` database in this repo, tagging artifacts `cluster:mcp-review-h`
and `mcp-review-scratch`. All scratch closed at the end of the session.

## 1. Executive summary

The MCP surface is, for the core "find a ready issue, claim it, work it, close
it" loop, **good and getting noticeably tighter**: `start_work` / `claim_next` /
`newly_unblocked` / `expected_assignee` together remove most of the
coordination friction that earlier reviews flagged. The two findings most
likely to bite a real agent are **(F1) `batch_close` cannot clean up its own
scratch without `force=true`** because the default done-state is unreachable
for half the registered types (bug, milestone, phase, step), and **(F2)
`list_observations` lacks the filters the docs themselves assume an agent
needs** (no `actor`, no priority, no age, no sort) — which makes the
session-end triage workflow the docs promote a manual fetch-all-and-pray.
Beyond those, the response-shape drift around full PublicIssue echoes
(add_comment, add_dependency, create_plan, get_plan, list_issues) is a steady
token tax that the slim-vs-full envelope work hasn't reached yet.

## 2. Per-workflow walkthrough

### Workflow A — session-start orientation

What I did: `get_mcp_status`, `session_context`, `get_ready --include_context`,
`get_blocked`, `get_stats`, `list_observations`, `list_types`.

What worked:

- `get_mcp_status` is genuinely useful as a first call — the v12/v12 confirm
  immediately disproved the alarming `Database schema v12 is newer than this
  version` line from the session-start hook (which is the *uv-tool dashboard*
  failing, not the *venv MCP server* — see F18).
- `get_ready include_context=true` returned parent title inline. That answered
  "do I even know what this work is part of?" without a second call. Strong.
- `session_context` surfaces stale claims, ready queue, and stale-observation
  count in one shot.

What hurt:

- 12 stale claims from prior review runs (b, d, e, f, g) sit on the issue,
  every one tagged with a long-dead reviewer actor (`reviewer-b`,
  `mcp-review-e`, `other-agent-h`, …). There is no agent-facing "release
  everything I'm holding" tool — see F4.
- Two ready issues with literally just the title `"T"` (created and abandoned
  by some prior agent) appear in the ready queue. Not a surface bug, but the
  surface didn't push back on the empty title at create time either.
- `get_stats` returns both `by_status` and `status_name_counts` (and
  `by_category` / `status_category_counts`) as separate top-level keys with
  identical values. The "compat" note in CLAUDE.md says both ship; in practice
  it just doubles the payload (F13).
- `list_types` does not include `requirement`, even though both CLAUDE.md and
  the live `promote_observation` docstring tell agents to use
  `type='requirement'` for formal requirements (F11).
- `valid_transitions` on an issue with missing template fields returns
  `requires_fields: []` *and* `missing_fields: ["acceptance_criteria"]`. The
  reader has to know that `requires_fields` is the schema and `missing_fields`
  is the diff; together they read like a contradiction (F6).

### Workflow B — create + start_work + comment + close

```
create_issue title="[mcp-review-h] Scratch B — core lifecycle probe"
  labels=["cluster:mcp-review-h","mcp-review-scratch"]
  → filigree-20e4fe7d8d, is_ready=true

start_work issue_id=… assignee="mcp-review-h"
  → status="in_progress", claim_expires_at=+48h, last_heartbeat_at set

add_comment issue_id=… expected_assignee="mcp-review-h" text="…"
  → comment_id=100, **PLUS full PublicIssue payload (~25 fields)**

add_comment expected_assignee="someone-else"  (wrong)
  → {"error":"Cannot operate on filigree-20e4fe7d8d: assigned to
     'mcp-review-h' (expected 'someone-else')","code":"CONFLICT"}

close_issue reason="…" expected_assignee="mcp-review-h"
  → status="closed", fields.close_reason="…"
```

What worked: the core 4-step lifecycle was effortless. `start_work` auto-resolved
the unique wip target. `expected_assignee` gave me a clean CONFLICT with a
sentence that names the issue, the observed assignee, *and* what I expected —
exactly what an agent needs to recover. `close_issue` parked the reason in
`fields.close_reason`, where it survives in the audit trail.

What hurt:

- `add_comment` returns the entire PublicIssue for a one-line write (F5). At
  scale (a chatty agent commenting on 20 issues) this adds up.
- `add_comment` response does not echo the comment text. Verifying "did the
  exact text I sent get stored?" requires a follow-up `get_comments` (F14).
- The closed issue still carries `claim_expires_at: "…+48h"` and a populated
  `assignee`. That's audit-correct, but means a naive "is this claim live?"
  predicate that only checks `assignee != ""` will mis-count closed issues as
  claimed. Minor.

### Workflow C — contention & claim semantics

```
start_work agent-one → success, status=in_progress
start_work agent-two → CONFLICT "already assigned to 'agent-one'"
claim_issue agent-two → CONFLICT (same message)
heartbeat_work actor=agent-one → ok, last_heartbeat_at refreshed,
  claim_expires_at moved forward
heartbeat_work actor=agent-two → CONFLICT "assigned to 'agent-one'
  (expected 'agent-two')"

release_claim actor=agent-two if_held=true
  → CONFLICT — but the docstring says if_held is "release-if-held cleanup"
    and "unassigned issues are a no-op". I'd expected silent no-op when
    held-by-other. (F15)

release_claim actor=agent-one (default) reason="…"
  → status reverts to "open", assignee="" — issue rejoins ready queue ✓

start_next_work assignee=mcp-review-h priority_min=3 priority_max=3 type=task
  → picked up the freed issue, status=in_progress ✓

start_next_work (queue now empty)
  → {"status":"empty","reason":"No ready issues matching filters"}
```

What worked: claim-aware writes are consistent across `add_comment`,
`update_issue`, `heartbeat_work`, `release_claim`, `batch_*`. `release_claim`
reverting wip → open is a real workflow improvement — without it, abandoned
work would orphan in wip with no assignee, invisible to `get_ready`.

What hurt:

- `release_claim if_held=true` did not act as a silent no-op when the issue is
  held by someone else; it returned CONFLICT. That contradicts the spirit of
  "if-held" (F15).
- `start_next_work` empty case returns `{status: "empty", reason: …}`, while
  the success case returns a flat PublicIssue (no `status` discriminant key in
  that shape). Agents have to branch on `result.get("status") == "empty"`
  rather than e.g. `result.get("issue_id") is None`. Inconsistent (F8).

### Workflow D — observations triage

```
observe summary="…" file_path="…" line=1 priority=3 actor="mcp-review-h"
  → filigree-obs-35d26419da, expires_at=+14d
observe (no file_path)  → file_path="" file_id=null line=null  (accepted)
observe (with file_path) → file_id auto-resolved by path ✓

promote_observation observation_id=…
  labels=["cluster:mcp-review-h","mcp-review-scratch"]
  → new issue filigree-55745dccb8 with:
       labels = [cluster:…, from-observation, mcp-review-scratch]
       description appended "Observed in: `…workflow.py`:1"
       fields.source_observation_id = "filigree-obs-35d26419da"

dismiss_observation reason="scratch — not real"
  → {"status":"dismissed","observation_id":"…"}   ← nice and slim

batch_dismiss_observations observation_ids=[…]
  → {"succeeded":["…"],"failed":[]}

list_observations file_path="workflow.py"
  → returned 1 result   (filter works)
```

What worked: promote/dismiss are tidy. `promote_observation` preserves the
back-pointer in `fields.source_observation_id` and automatically attaches the
`from-observation` label — that's a great convention because
`list --label=from-observation` is the exact pipeline-output view agents need.

What hurt:

- `list_observations` filters by `file_path` (substring) and `file_id` only.
  No `actor` filter, no priority filter, no age filter, no sort. The skill
  sheet's own "At session end, skim list_observations" pattern means agents
  routinely want "my observations this session, sorted by age" — which
  requires fetching the whole queue and filtering client-side (F2).
- Shape asymmetry: `dismiss_observation` returns 2 keys
  (`{status, observation_id}`), `promote_observation` returns the full
  PublicIssue of the created issue. Defensible — promote creates a new entity,
  dismiss only changes state — but worth noting.

### Workflow E — findings & scanners triage

```
list_scanners → 1 item: "claude-code" requires_dashboard=true requires_approval=true
list_findings status=open → 1 leftover from review-d

report_finding file_path=…review-h.md rule_id=mcp-review-h-scratch …
  → ScanFinding fields PLUS ingest stats:
     {finding_result:"created", findings_created:1, findings_updated:0,
      file_created:true, observations_created:1, observations_failed:0,
      observation_ids:[…], observation_id:…}
  ← surprise! a paired observation was auto-created. F3.

promote_finding finding_id=… labels=[…]
  → new bug issue filigree-06e89739c6:
     title="[agent] Synthetic finding to drive promote/dismiss path. …"
     fields.source_finding_id=…  fields.scan_source="agent"  fields.rule_id=…
     labels=[…, from-finding, mcp-review-scratch]
     description templated with "Scan source: …" "Rule: …" "Severity: …"
     "Finding location: `…`"
  → paired observation auto-cleaned ✓

report_finding (second) → another paired observation auto-created
dismiss_finding finding_id=… reason="…"
  → status="false_positive", metadata.dismiss_reason="…"
  → paired observation auto-cleaned ✓
```

What worked: the report → promote/dismiss lifecycle silently cleans up its
paired observation. The promote/dismiss tools form a coherent triage triangle
with observations.

What hurt:

- `report_finding` *silently* creates a paired observation. The doc string
  mentions "any observation_id created for triage" but the side-effect is not
  obvious from the tool name (F3). An agent who calls `report_finding`
  expecting a finding-only entry, then sees `observations_created: 1`, has to
  reason about ownership of that observation.
- `report_finding` has no `actor` parameter, so multi-agent attribution of
  manually-reported findings is impossible. Compare with every other write
  tool. (F3)
- The response mixes finding fields with ingest stats designed for batch
  intake (`findings_created`, `findings_updated`, `file_created`). For a
  single-finding tool, those are constants that read like noise.
- `promote_finding` title is `"[agent] " + message`. The `[agent]` prefix is
  derived from `scan_source`, not from a meaningful agent identity — so all
  manually-reported findings get the same opaque prefix. (F12)
- `dismiss_finding` stores the reason in `metadata.dismiss_reason`, while
  `close_issue` stores its reason in `fields.close_reason`. Same concept,
  different home — inconsistent (F10).
- `dismiss_finding` default `status="false_positive"` mislabels what is often
  a triage dismissal ("not worth tracking", "duplicate", "acknowledged"). The
  docstring acknowledges the issue (F16).
- The only registered scanner (`claude-code`) requires the dashboard, and the
  dashboard fails to start in this environment because of the dual filigree
  install (uv-tool v8 schema vs venv v12 schema). So an agent on this
  environment cannot actually trigger any automated scans. (F18)

### Workflow F — dependencies, batch, planning

```
create_issue ×2 (blocker + blocked)
add_dependency from=blocked to=blocker
  → flat PublicIssue + {dependency_result:"added", dependency:{…}}
get_blocked → 1 item, blocked_by=["filigree-c4d855bea9"]  ← ID only, no title

create_plan milestone+2 phases+3 steps (cross-phase dep "0.1")
  → full PublicIssue echo of milestone, every phase, every step  (~3 KB)
get_plan milestone_id=…
  → same massive shape + progress_pct

batch_update issue_ids=[blocker,blocked] priority=4
  → succeeded[] returns SlimIssue (5 fields each), failed=[]   ← nice
batch_close issue_ids=[blocker] reason="…"
  → succeeded=[blocker_slim], newly_unblocked=[blocked_slim]   ← excellent

batch_close issue_ids=[<9 mixed-type scratch items>] reason="cleanup"
  → succeeded: 2  (the plain tasks)
     failed: 7
       - filigree-06e89739c6 (bug, status=triage):
         "Transition 'triage' -> 'closed' is not allowed for type 'bug'"
         valid_transitions: [{to:confirmed}, {to:wont_fix}, {to:not_a_bug}]
       - milestone (status=planning):
         "Transition 'planning' -> 'completed' is not allowed"
         valid_transitions: [{to:active}, {to:cancelled}]
       - 2× phase (status=pending):
         "Transition 'pending' -> 'completed' is not allowed for type 'phase'"
         valid_transitions: [{to:active}, {to:skipped}]
       - 3× step (status=pending):
         "Transition 'pending' -> 'completed' is not allowed for type 'step'"
         valid_transitions: [{to:in_progress}, {to:skipped}]

batch_close … force=true → all 7 close cleanly
```

What worked: the batch tools' `succeeded` / `failed[] with valid_transitions
echo` / `newly_unblocked` envelope is *the* high-mark of this surface.
`batch_close` failing on 7 of 9 items still told me, per-item, exactly what
states would have been reachable — I could have synthesised a per-type retry
plan from the response alone.

What hurt:

- **F1 — cleanup is force-only for mixed types.** The `reason="cleanup"` path
  here is the exact "tag everything `mcp-review-scratch` and close at the end
  of session" pattern the documentation explicitly suggests. But unless the
  scratch is all of the same type *and* in a state that has a direct
  transition to a 'closed/done' state, `batch_close` defaults will fail for
  most of it, and `force=true` (which the docstring says is for "cleanup
  flows that intentionally skip the workflow") becomes the routine answer.
  The previous reviewers' 12 stale claims are downstream of exactly this
  friction — they didn't want to `force` so they left it.
- **F7 — `get_blocked` items show `blocked_by` as bare IDs**, no titles. To
  read this output meaningfully an agent has to issue N more `get_issue`
  calls. `get_ready include_context=true` solved the analogous problem for
  the ready queue; the same idea should extend.
- `add_dependency` returns full PublicIssue + dependency echo for a write
  whose meaningful output is "did the link form?". (Token cost again.)
- `create_plan` / `get_plan` are unbounded — full PublicIssue per milestone +
  every phase + every step, with no `response_detail=slim` mode like
  `batch_*` has. For a 30-step plan this is dozens of KB.
- `list_issues` returns full PublicIssue records by default with no slim
  option. The 9-row cleanup query I ran above returned 9× full records when
  9× `{id,title,type,status}` would have done.

## 3. Findings

Sorted severity then workflow.

### P1 — genuinely degrades a real agent workflow

| # | Title | Evidence | Why it matters | Suggested resolution |
|---|---|---|---|---|
| **F1** | `batch_close` cannot clean up mixed-type scratch without `force=true`; default done-status is unreachable for bug, milestone, phase, step from their *initial* states | Workflow F: 7 of 9 items failed with `INVALID_TRANSITION` ("Transition 'triage' -> 'closed' is not allowed", "'pending' -> 'completed' is not allowed", etc.) | Documentation promotes "tag your scratch and clean up at end of session"; in practice that requires `force=true`, which the docstring itself says is "only for cleanup flows that intentionally skip the workflow". Outcome: prior reviewers leak 12 stale claims rather than `force`. | Either accept a per-type `status_by_type` map, or default to the *closest* done state (bug→wont_fix, milestone→cancelled, phase→skipped, step→skipped) when no explicit status is passed and the issue is in `open` category. |
| **F2** | `list_observations` lacks the filters needed for session-end triage | Tool schema: only `file_path`, `file_id`, `limit`, `offset`. Skill doc says "skim list_observations and either dismiss or promote" at session end. | The advertised workflow ("show me my observations from this session, sorted by age") requires fetching the whole queue and filtering client-side. Already a P1 ticket exists (filigree-b0af8a661b "Add structured observation triage"); this confirms the gap end-to-end. | Add `actor`, `priority_max`, `older_than_hours`, `sort_by` (created_at/priority), plus a `mine=true` shortcut keyed off the calling actor. |
| **F3** | `report_finding` has hidden side-effects, no `actor`, and bundles batch-ingest stats into a single-write response | Workflow E: response includes `findings_created:1, findings_updated:0, file_created:true, observations_created:1, observation_ids:[…], observation_id:…`. No `actor` parameter on tool schema. | Auto-creating a paired observation makes ownership/cleanup non-obvious — promote_finding and dismiss_finding both clean it up, but an agent doesn't know that unless they read other tools' code. No actor breaks the "who reported this" audit trail for manually-reported findings. | Add `actor` parameter; document the paired-observation behavior in the docstring (or stop auto-creating it); slim the response to flat ScanFinding + `observation_id` when one is created. |
| **F4** | Stale claims and stale observations both accumulate across review sessions; no "release everything I'm holding" affordance | `session_context`: 12 stale claims across `reviewer-b`, `claude-debug`, `agent-bravo`, `mcp-review-d`, `mcp-review-e`, `mcp-review-f`, `other-agent-h`, etc. Same snapshot reports "STALE OBSERVATIONS: 9 older than 48h" — same accumulation pattern across reviewers, same root cause (F1 makes routine cleanup feel like a workflow violation, so reviewers don't do it). | Every "do a review against the live DB" workflow leaks both claims and observations. Forces leases and 14-day observation expiry to do janitorial work that an end-of-session call should do. | Add `release_my_claims(actor=…, label_filter=…)` (or extend `batch_close` with `expected_assignee` to enable atomic mass-release of held items) **plus** `batch_dismiss_observations` filtered by actor/label. Couple with F1 so end-of-session cleanup actually works in one call. |

### P2 — friction agents will hit, but workarounds exist

| # | Title | Evidence | Why it matters | Suggested resolution |
|---|---|---|---|---|
| **F5** | `add_comment` returns the full PublicIssue payload for a one-line write | Workflow B: response had ~25 issue fields plus `comment_id:100`. | Chatty agents (e.g. an autocrunch that comments on every issue it touches) pay 20× the bytes for the actual op. | Slim response: `{issue_id, comment_id, author, text, created_at}`. |
| **F6** | `valid_transitions.requires_fields` and `missing_fields` are confusing to read together | Workflow A: `{requires_fields: [], missing_fields: ["acceptance_criteria"], ready: false}` on a feature transition. | Plausibly `requires_fields` = required *for this transition* (none, hence empty) and `missing_fields` = template-required fields still absent (drives `ready: false`). That's a defensible distinction, but an agent reading the response without the docstring open will read it as a contradiction — both fields named `*_fields`, opposite values, both present. | Either drop `requires_fields` (recoverable from `get_template`), rename `missing_fields` → `blocking_fields`, or add a brief inline comment in the schema clarifying the two roles. |
| **F7** | `get_blocked` items show `blocked_by` as bare IDs without titles | Workflow F: `[{issue_id, title, status, priority, type, blocked_by: [<id>]}]` | Reading the blocked queue requires N follow-up `get_issue` calls to find out *what* is blocking. `get_ready include_context=true` solved the analogous "what's my parent?" friction; the same idea is needed here. | Add `include_blockers=true` flag that hydrates `blocked_by` into `[{issue_id, title, status}]`. |
| **F8** | `start_next_work` envelope inconsistent between success and empty | Workflow C: success = flat PublicIssue; empty = `{"status":"empty","reason":"…"}`. | Agents have to branch on a magic `status: "empty"` key vs. flat shape. Most other tools share a shape and use error codes for not-found. | Either always wrap (`{issue, …} | {issue: null, status: "empty"}`) or return `null`/`None` for empty; pick one consistent envelope. |
| **F9** | `create_plan` and `get_plan` have no slim response mode | Workflow F: 3-step plan returned ~3 KB of full PublicIssue echoes; a 30-step plan would be ~30 KB. | Plan reads are common, and most callers only need title/status/progress. | Add `response_detail=slim` (mirror `batch_*`). |
| **F10** | Close-reason storage diverges between `close_issue` and `dismiss_finding` | `close_issue` → `fields.close_reason`; `dismiss_finding` → `metadata.dismiss_reason`. | Two homes for the same concept means audit consumers have to special-case. | Land on one — either `fields.*_reason` everywhere, or a top-level `reason` mirrored back. |
| **F11** | `requirement` issue type referenced in docs but not registered in `list_types` | `list_types` returned: bug, deliverable, epic, feature, milestone, phase, release, release_item, step, task, work_package. Both CLAUDE.md and `promote_observation` docstring mention `type='requirement'`. | Following the docs gives an agent a type-not-found error. Either ship the type or update the docs/docstrings. | If it's intentional that the requirements pack is opt-in, update the docstrings to say "if requirements pack is enabled". Otherwise enable it. |
| **F12** | Promoted-finding titles get an `[agent]` prefix derived from `scan_source` | Workflow E: `"[agent] Synthetic finding to drive promote/dismiss path. Will be cleaned up."` | Every manually-reported finding gets the same opaque `[agent]` prefix; the prefix conveys nothing actionable. | Use `rule_id` as a short prefix instead, or drop the prefix entirely when scan_source is the generic `"agent"`. |

### P3 — cosmetic / minor

| # | Title | Evidence | Why it matters | Suggested resolution |
|---|---|---|---|---|
| **F13** | `get_stats` returns duplicate keys for the same data | `{by_status, status_name_counts, by_category, status_category_counts}` — pairs are identical in current responses. | Compat keys ship forever unless deprecated explicitly. | Either drop the legacy keys after a deprecation window, or note clearly in docstring that the pairs are aliases. |
| **F14** | `add_comment` doesn't echo the comment text in the response | Workflow B: response includes `comment_id:100` but not the stored text. | Agents that want to verify "exact text persisted as sent" need a follow-up `get_comments`. | Include `comment: {text, author, created_at}` in the response. |
| **F15** | `release_claim if_held=true` returns CONFLICT when claim is held by someone else | Workflow C: actor=agent-two if_held=true → `CONFLICT "assigned to 'agent-one' (expected 'agent-two')"`. | The name "if-held" reads as "release only if I hold it; silent otherwise". CONFLICT here is surprising; it makes the flag equivalent-but-stricter to default behavior rather than idempotent. | If the actor doesn't hold it, return `{status:"not_held", observed_assignee: "agent-one"}` and HTTP-success, matching the unassigned-no-op case. |
| **F16** | `dismiss_finding` default `status="false_positive"` mislabels triage dismissals | Workflow E: omitting `status` left a "this is a synthetic test finding" as `false_positive`. | Most "I don't want to track this" is actually `acknowledged` or `unseen_in_latest`, not a positive claim that the finding was wrong. | Change default to `acknowledged`; keep `false_positive` as an explicit choice. |
| **F17** | `list_issues` returns full PublicIssue records by default | Workflow F cleanup pass: 9 records × ~25 fields each, but only `id`/`title`/`type`/`status` were needed. | Listing the queue is one of the most common ops; the unbounded shape is a per-call tax. | Add `response_detail=slim` here too (mirroring `batch_*`); leave `full` available for callers who need it. |
| **F18** | Dual filigree install causes silent dashboard failure that surfaces as a confusing "schema v12 newer than v8" hook message at session start | `doctor` output: "Running from venv (…) but uv tool also installed". Session-start hooks try to launch the *uv-tool* dashboard (schema v8) against the *venv project* DB (schema v12). MCP itself is fine. | The first thing an agent sees is a "Database schema v12 is newer than this version" error from the hook, which scares them into assuming the *MCP* is broken. It isn't. | `doctor` warning is already correct; consider adding a session-start hook that explicitly reports which `filigree` binary is doing what, so the dashboard failure can't be confused for an MCP failure. |

## 4. What works well

Naming these so they don't get refactored away.

- **`start_work` / `start_next_work` atomic claim+transition** — the right
  default for picking up work. Auto-resolves the unique wip target so callers
  don't have to know the workflow.
- **`newly_unblocked` echo in `batch_close`** — saves an agent from
  re-querying the ready list after a closure.
- **`valid_transitions` echo per-failure in `batch_close` / `update_issue`** —
  agents can synthesise a retry plan from the response alone. This is the
  best error-recovery affordance on the surface.
- **CONFLICT error messages on claim-aware writes** name the issue, the
  observed assignee, *and* what was expected. Genuinely debuggable.
- **`release_claim` reverts wip→open** by default so abandoned work rejoins
  the ready queue rather than going invisible.
- **`expected_assignee` precondition is consistent** across `add_comment`,
  `update_issue`, `add_label`, `remove_label`, `batch_update`, `batch_close`,
  `heartbeat_work`, `release_claim`. One mental model for the whole surface.
- **`promote_observation` and `promote_finding` preserve back-pointers**
  (`fields.source_observation_id`, `fields.source_finding_id`) and apply the
  `from-observation` / `from-finding` labels automatically. The "where did
  this issue come from?" audit trail is intact.
- **`get_ready include_context=true`** is the model for slim-default-with-
  opt-in-context. The pattern should propagate to `get_blocked`, `list_issues`,
  and `get_plan`.
- **`report_finding` + promote/dismiss tidy up paired observations** —
  whatever the side-effect surprise (F3), the cleanup is at least
  transactionally complete.
- **`get_mcp_status` is the right shape** for a read-only first-call
  diagnostic, especially given the schema-mismatch dual-install (F18)
  environment.

## 5. Open questions for the maintainer

1. **What is the intended end-of-session cleanup story for mixed-type
   scratch?** Right now `force=true` is the practical answer, but the
   docstring says force is for "cleanup flows that intentionally skip the
   workflow" — which makes routine cleanup feel like a workflow violation.
   Should there be a `batch_archive_by_label(label)` or a `force=true`
   default for scratch labels?
2. **Is the `requirement` type meant to be enabled by default?** It's
   referenced in CLAUDE.md and `promote_observation`'s docstring but isn't
   in `list_types`. Either the docs are stale or the pack is unintentionally
   off.
3. **Should `release_claim if_held=true` silently no-op when someone else
   holds the claim**, given the "if-held" name? Today it returns CONFLICT —
   which is the same shape as `if_held=false`.
4. **What's the canonical close/dismiss reason home — `fields.X_reason` or
   `metadata.X_reason`?** Issue close lives in `fields`, finding dismiss
   lives in `metadata`. Audit consumers have to special-case.
5. **Is auto-creating a paired observation from `report_finding` intentional
   product behavior, or an internal coupling that should be hidden?** It's
   currently a hidden side-effect of a single-write tool, and shows up in
   the response as ingest-stats noise.
6. **Should `list_observations` filters be expanded now, or wait for
   filigree-b0af8a661b's structured triage work?** F2 is already covered by
   that P1 ticket; the question is whether even minor filter expansion
   (actor, age) is worth landing now as a stop-gap.

---

**Reviewer's footer.** All `cluster:mcp-review-h` scratch (issues,
observations, paired-from-finding observations, findings) closed at session
end via `batch_close ... force=true` after F1 bit. Final
`list_issues label=cluster:mcp-review-h status_category=open` returns `{}`.
