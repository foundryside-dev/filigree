# Filigree MCP Contract Critique (next-major refresh)

Auditor: mcp-server-critic (actor `claude-filigree`), 2026-10-07
Subject: Filigree 3.3.0 MCP server, `release/3.3.0` (118 tools, 1 resource, 1 prompt)
Lane: MCP contract only (catalog shape, schemas/envelopes, idempotency/concurrency, pagination, CLI parity, transport, Consistency Gate). Agent-experience content (session-context prose, skills, description-as-prompt, catalog token cost) belongs to the sibling LLM-specialist review and is not duplicated here.

Intent context: DECLARED (issue tracker for AI coding agents, work-state authority in the Weft federation; multi-agent claim/handoff workflows). Intent-mismatch findings are therefore unconditional unless a finding says otherwise.

Method and evidence base:
- Read prior art (04-18 review, 05-06 consistency plan, 05-13 master checklist, 05-14 gap analysis, 06-02 namespacing plan, `docs/mcp.md`) and checked tracker status of epic `filigree-ed2ccaf10d`, X-4 `filigree-8f6a1599fb`, X-5 `filigree-af55859975`, X-6 `filigree-b789da2a1e`, `filigree-4c73f6cf22` (all still open/proposed as of 2026-10-07; X-5 is `proposed`).
- Static: `mcp_server.py`, `mcp_runtime.py`, `mcp_tools/{common,tiers,rename,issues,meta}.py`, `types/api.py`, `dashboard.py`/`dashboard_auth.py` (auth scope).
- Live: dumped the served catalog (`_all_tools`, 118 tools) and drove the real `filigree-mcp` stdio server with the MCP client SDK against a throwaway project at `.../scratchpad/mcp-probe/` (no write to the real tracker; real tracker touched read-only: five `issue_get`, three `issue_search`, one `issue_list limit=3`). Every "Evidence" below marked PROBE is a verbatim observation from that stdio session.
- Side-effect disclosure: my first `uv run` recreated `/home/john/filigree/.venv` (gitignored; it reported "Removed virtual environment ... Creating virtual environment"). No tracked file changed. The untracked `.playwright-mcp/` directory in `git status` is not mine.

Tag legend: NEW, STILL-OPEN (issue/checklist cited), REGRESSED (earlier fix cited).

---

## Executive summary

The surface is better disciplined than most (namespaced `<entity>_<verb>` names, strict unknown-parameter rejection, schema-mismatch fail-closed with a degraded-mode diagnostic, claim-aware write defaults for most writes, a shared `ErrorCode` enum used by MCP/CLI/dashboard, CLI error envelopes that match MCP). The weaknesses are concentrated in three places the 2.x checklists declared "Done" but never tested adversarially:

1. **The protocol-level error contract is split in two and neither half is complete.** Domain errors are returned with `isError=false`; SDK schema-validation errors come back as `isError=true` plain text with no `code`. A client or retry layer keyed on `isError` sees every domain failure as success.
2. **Retry-safety is not a property of the surface.** `admin_undo_last` walks back one more event per call; `work_start_next` / `work_claim_next` claim a *second* issue on retry and strand the first for 48 hours; `issue_create`, `comment_add`, `plan_create` duplicate; `work_release` and `issue_close` error on the second identical call; `issue_batch_close` reports one id as both succeeded and failed in one response. Which behaviour you get is per-tool and undeclared (no `idempotentHint`, no idempotency key, no state-already-reached signal).
3. **Bounding is a convention, not a rule.** Four regimes (cap-50 + `no_limit` bypass; default-100/max-10000; limit with no max; no limit parameter at all) plus an `issue_list` that returns full issue bodies (217 KB for one default page of 50 issues with 3 KB descriptions) while `issue_search` returns 144-byte slim rows.

Separately, the catalog carries 118 tools where roughly 76 would carry the same capability: nine read-only reference tools should be resources (the server declares exactly one resource), `type_get` is documented as a "compatibility alias" of `template_get`, 14 tools are single/batch twins, and eight admin/maintenance tools sit in every agent's default catalog. See the catalog proposal (118 -> 76 default + 7 opt-in admin).

Counts: 28 findings. blocker 1, major 16, minor 9, nit 2 (see summary block).

---

## Findings

Ordered blocker -> major -> minor -> nit. Evidence labelled PROBE = observed live on stdio; CODE = file reference; DOC = prior-art or tracker reference.

### Finding 1: undo-walks-back-and-has-no-event-guard
Severity:      blocker
Tag:           NEW (related closed: `filigree-a849860f2e` "undo_last gets stuck", `filigree-f38d4e2874` "Concurrent undo_last double-undo"; the walk-back is the fix for the first, and the second only closed the same-event race)
Location:      tool `admin_undo_last` (canonical `undo_last`)
Defect class:  retry-amplification; idempotency undeclared; claim-aware-write bypass
Evidence:      PROBE. Issue created, `issue_update priority=1`, `issue_update title="E-renamed"`, `work_claim assignee=alice`. Then `admin_undo_last {issue_id, actor:"bob"}` -> `undone:true, event_type:"claimed"` (bob un-claimed alice). Same call again (actor alice) -> `event_type:"title_changed"`. Again -> `event_type:"priority_changed"`. Fourth -> `{"undone":false,"reason":"No reversible events to undo"}`. Final `issue_get` shows title and priority both reverted. Input schema is `{issue_id, actor}` only.
Confidence:    HIGH for behaviour (deterministic, reproduced). MEDIUM for real-world frequency (the retry trigger, a lost response or host timeout, was inferred, not observed).
Qualification: unconditional.
Why it matters: "undo" is the one tool agents call *because something went wrong*, so it is the tool most likely to be retried under a timeout. Each retry silently reverts a different, older change. There is no `expected_event_id`, so the caller cannot say "undo event 27 only". Separately, `bob` reverted `alice`'s claim: `issue_update` by a non-holder gets `CONFLICT` (ADR-008), undo does not. The 05-14 row "Claim-aware writes hard to misuse: Done" omitted undo and release (Finding 3).
Remediation:   Require (or strongly default) `expected_event_id` (the id returned in the previous response / event list) and return `CONFLICT` with the current head event when it differs. Apply the same holder check as `issue_update` (derive expected holder from `actor`). Rename `issue_undo`. Return `{undone:false}` as `ErrorCode.NOT_FOUND`-class or at least keep the shape stable.

### Finding 2: domain-errors-isError-false-and-two-error-shapes
Severity:      major
Tag:           NEW (the 04-18 review recommendation #4 "single envelope + closed error enum" delivered the enum and shape but never the transport flag; no `isError` anywhere in `src/`)
Location:      global (`mcp_server.call_tool`, `mcp_tools/common._text`)
Defect class:  error-envelope shape; Consistency Gate "unrecoverable error envelope"
Evidence:      PROBE. `issue_get {"issue_id":"nope"}` -> `isError=False`, body `{"error":"Issue not found: nope","code":"NOT_FOUND"}`. `issue_create {"title":"a","priority":9}` -> `isError=True`, body `Input validation error: 9 is greater than the maximum of 4` (not JSON, no `code`). `finding_batch_update {status:"bogus"}` -> `isError=True`, `'bogus' is not one of ['acknowledged', ...]`. CODE: `grep -rn isError src/` returns nothing; `_text()` always returns `list[TextContent]`.
Confidence:    HIGH.
Qualification: unconditional.
Why it matters: Two error dialects chosen by *which layer rejected the call* (the SDK's jsonschema pass vs the handler). A host or federation client that gates retry/telemetry on `isError` treats all 16 `ErrorCode` failures as successes; a client parsing JSON for `code` crashes on the SDK dialect. Also session_context_get and summary_get return non-JSON text, so "parse the first content item as JSON" is not a universal contract.
Remediation:   Set `isError=true` for every non-success envelope (return `CallToolResult` or raise the SDK error path with the JSON body). Catch the SDK validation failure and re-emit it as `{error, code:"VALIDATION", details:{path, constraint}}`. Add `retryable: bool` and a uniform `hint`/`next_action` to the envelope (today `hint` is top-level for `INVALID_TRANSITION`, nested at `details.hint` for `scan_trigger` NOT_FOUND, absent for `CONFLICT`). Declare the envelope as `outputSchema` (Finding 21).

### Finding 3: work-release-unconditional-and-non-idempotent
Severity:      major
Tag:           NEW (ADR-008 / 05-14 row "Claim-aware writes hard to misuse: Done" does not cover `work_release`)
Location:      tool `work_release` (canonical `release_claim`), parameters `actor`, `if_held`, `expected_assignee`
Defect class:  concurrency contract undefined; retry non-idempotent; deconfliction gap
Evidence:      PROBE. alice `work_start` -> `in_progress`, assignee alice. `work_release {actor:"bob"}` -> success, issue now `status:"open"`, `assignee:""` (alice's claim cleared and her wip status reverted). alice then calls `work_release {actor:"alice"}` -> `{"error":"Cannot release ...: no assignee set","code":"CONFLICT"}`; a third time, same. With `if_held:true` the same state returns the issue as success. CODE: `_handle_release_claim` defaults `actor` to the literal `"mcp"` and passes `expected_assignee` only if supplied; contrast `work_heartbeat` which "treats actor as the expected current holder".
Confidence:    HIGH.
Qualification: unconditional (functional/availability: two agents end up working one issue; not a security claim).
Why it matters: (a) any agent can strip another agent's claim and silently revert its status, after which `work_ready` re-offers the issue (double work); (b) the *default* mode is the retry-unsafe one, and the retry-safe mode is an opt-in flag the agent must already know about.
Remediation:   Make release holder-checked by default (like heartbeat); a coordinator overrides with explicit `expected_assignee` or use `work_reclaim`. Make "already released" a success with `already_released:true`. Fold `if_held` away.

### Finding 4: claim-next-and-start-next-strand-claims-on-retry
Severity:      major
Tag:           NEW
Location:      tools `work_start_next`, `work_claim_next` (and `work_claim` lease default)
Defect class:  retry-amplification
Evidence:      PROBE. `work_start_next {assignee:"zed"}` -> claimed `probe-578949b749` (in_progress). Identical call again -> claimed a *different* issue `probe-015915fced`. Lease: `claim_expires_at` = claim time + 48 h on every claim (all `work_claim` responses). No idempotency token, no "you already hold wip work" signal.
Confidence:    HIGH for behaviour; MEDIUM for impact (depends on how often a lost response occurs).
Qualification: unconditional.
Why it matters: A retry after a lost response leaves the first issue claimed and in progress for up to 48 h with nobody working it, invisible to `work_ready`. Cleanup requires `work_stale_list` (only returns *expired* leases by default) or `work_release_mine`.
Remediation:   Add `client_request_id` (echoed in the claim event) so a repeat returns the original result; or return `already_holding` with the holder's current wip issue(s) unless `allow_multiple:true`. Shorten the default lease (heartbeat already exists) or surface `claim_expires_at` prominently.

### Finding 5: create-class-tools-have-no-idempotency-key
Severity:      major
Tag:           NEW
Location:      `issue_create`, `comment_add`, `comment_batch_add`, `plan_create`, `plan_step_add`, `finding_promote`, `annotation_create`
Defect class:  retry-amplification
Evidence:      PROBE. `comment_add {text:"hi", actor:"alice"}` twice -> `comment_id` 1 then 2, identical text. `plan_create` with the same body twice -> two identical milestone trees (second response differs only in timestamps/ids). Contrast `observation_create` (deduped: second identical call left one row in `observation_list`) and `finding_report` (upsert, `seen_count` 1 -> 2), and the closed `filigree-c68a96c9fd` "merged observation promotion retries can create duplicate issues" which fixed one tool in this class only.
Confidence:    HIGH.
Qualification: unconditional. Dedup precedent proves the team already treats this as a defect where noticed.
Remediation:   Uniform optional `client_request_id` on every create-class tool, stored on the event, with "same id + same args -> return original; same id + different args -> CONFLICT". Declare `idempotentHint` per tool in annotations (Finding 11).

### Finding 6: issue-list-returns-full-bodies
Severity:      major
Tag:           NEW (same class as X-6 `filigree-b789da2a1e`; 05-14 row "Common response envelopes and slim paths: Done" covered plan/ready, not `issue_list`)
Location:      tool `issue_list` (and `issue_get`, `issue_create`, every mutator that returns `issue_to_public`)
Defect class:  return-shape-that-blows-budget
Evidence:      PROBE. 60 issues with 3,000-char description + 1,000-char notes: `issue_list {}` (default page, 50 items) = 217,109 bytes; `no_limit:true` = 290,486 bytes for 68 items. `issue_search` on the same data = 7,200 bytes (rows `{issue_id,title,status,priority,type}`). `work_ready` rows are slim plus `startable`. Real tracker (`issue_list limit=3`): a phase row carries 18 child ids; the milestone carries `description` (~900 chars). Every row also carries both `parent_id` and `parent_issue_id`, 7 null claim/commit fields, `data_warnings:[]`, `fields`, `blocks`, `blocked_by`, `children`, `labels`. `issue_list` has no `response_detail` parameter (17 other tools do).
Confidence:    HIGH.
Qualification: unconditional; magnitude scales with description length, which the schema does not bound.
Remediation:   Slim projection by default for every list tool (identity, title, status, priority, type, assignee, `updated_at`), `response_detail=full` opt-in, the same enum name everywhere. Truncate `description`/`notes` with `truncated:true` plus a byte count. Drop `parent_id` (Finding 19).

### Finding 7: pagination-and-bounding-is-four-regimes
Severity:      major
Tag:           STILL-OPEN (X-6 `filigree-b789da2a1e` for `finding_list`; 04-18 review "Smaller friction: `_MAX_LIST_RESULTS = 50` silently caps"); non-finding tools NEW
Location:      all list tools
Defect class:  return-shape-that-blows-budget; inconsistent contract
Evidence:      Schema dump (`limit`/`offset`/`no_limit`):
  - Regime A, cap 50 with `no_limit` bypass (10,000,000 effective cap): `issue_list`, `issue_search`, `observation_list`. `limit:500` silently clamps to 50 (PROBE: `limit500 items 50 True`) with no warning.
  - Regime B, default 100 / `maximum 10000`, no bypass: `file_list`, `finding_list`, `annotation_list`, `file_annotation_list`, `issue_annotation_list`, `annotation_attention_list` (X-6).
  - Regime C, default 50-100 and no maximum: `change_list`, `issue_event_list`, `reconciliation_debt_list`, `file_timeline_get` (max 10000).
  - Regime D, no limit parameter at all: `work_ready`, `work_blocked`, `work_stale_list`, `comment_list`, `entity_association_list`, `entity_association_list_by_entity`, `type_list`, `pack_list`, `scanner_*`, `prompt_pack_list`. PROBE: `work_ready` returned 10,981 bytes for 65 rows with `has_more:false` always (vestigial field).
  - `offset` paging only; no cursor, so concurrent writes duplicate/skip rows between pages. `change_list` is the only tool with a real cursor (`next_event_id`, `next_since`).
  - CLI `list --limit` default is 100; MCP default is 50 (parity drift, Finding 20).
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   One bounding rule, enforced in one helper: default 25-50 slim rows, hard server maximum (e.g. 200) with no bypass, `has_more` + opaque `cursor` on every list tool (keyset on `(sort_key, id)`), `truncated` flag + total count. Delete `no_limit`. Reject `limit` above max with `VALIDATION` rather than clamping silently. X-6 becomes "finding_list adopts the rule".

### Finding 8: batch-contract-is-per-tool-and-self-contradictory
Severity:      major
Tag:           NEW (05-14 "Common response envelopes: Done" and 04-18 rec #4 claim a single batch envelope; shape differs in practice)
Location:      `issue_batch_update`, `issue_batch_close`, `label_batch_*`, `comment_batch_add`, `observation_batch_*`, `finding_batch_update`
Defect class:  partial-failure semantics undeclared; retry non-idempotent
Evidence:      PROBE.
  - Same id twice in one call: `issue_batch_close [E,E,F]` -> `succeeded:[E,F]` and `failed:[E already closed]`: E is reported as both succeeded and failed.
  - Re-running that exact batch -> `succeeded:[]` and three INVALID_TRANSITION failures although the desired end state holds. A caller cannot tell "my first call worked" from "this never worked".
  - One foreign-prefix id (`nope-123`, `zzzz-abc`) rejects the *whole* batch: `{"error":"Issue ID does not belong to this project","code":"VALIDATION"}` with nothing applied; an unknown id without a dash (`nope`) or with the right prefix (`probe-0000000000`) is a per-item `NOT_FOUND`. The failure granularity depends on the id's spelling.
  - `issue_batch_update {issue_ids:[X]}` with no field to change -> `succeeded:[X]` (silent no-op success).
  - `finding_batch_update` with all ids unknown -> `{"error":"All 2 finding update(s) failed","code":"VALIDATION"}`: per-item `failed[]` dropped, wrong code, and it is not an `{succeeded,failed}` envelope. Issue batch with all failing returns the normal envelope.
  - `succeeded` item type varies: slim issue objects (`issue_batch_*`), bare id strings (`label_batch_add`), observation/finding records elsewhere. Not atomic, and not declared as non-atomic.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   One `BatchResponse` for every batch tool: `{succeeded:[{id,...}], unchanged:[{id,reason}], failed:[{id,code,error}], summary:{requested,applied}}`. Dedupe ids first (reject or collapse, never double-process). Treat already-in-target-state as `unchanged`. All id-shape problems become per-item `failed`. Empty change set -> `VALIDATION`. Declare "non-atomic, in order" in the description or offer `atomic:true`.

### Finding 9: removed-tool-NOT_FOUND-has-no-renamed-to-and-errors-leak-old-names
Severity:      major
Tag:           REGRESSED (ADR-016 / 06-02 plan §5.1 and `tests/mcp/test_no_old_names_*`: "every downstream guard, arg check, and log line operates on one name"; the intent was that the agent never sees the old name)
Location:      `mcp_server.call_tool`, `_unknown_argument_error`, `rename.RENAME_MAP`
Defect class:  error recovery; schema-drift-without-bump
Evidence:      PROBE. `issue_get {issue_id:"x", bogus:1}` -> `Unknown parameter(s) for get_issue: bogus` (the agent called `issue_get`; `get_issue` is a name it has never been shown). `get_issue {...}` -> `{"error":"Unknown tool: get_issue","code":"NOT_FOUND"}` with no `renamed_to`, even though `RENAME_MAP["get_issue"]` is in hand at that exact line. CODE: `call_tool` rebinds `name = canonical` before `_unknown_argument_error(name, ...)`; `tier_for`, `_tool_argument_names`, `_all_handlers`, logging all key on the old names.
Confidence:    HIGH.
Qualification: unconditional.
Why it matters: The 3.0 break was clean on the wire but gave every stale skill, cached prompt and federation consumer an error with no recovery path, in the one place the server can name the successor for free.
Remediation:   Return `{error, code:"NOT_FOUND", details:{renamed_to:"issue_get", removed_in:"3.0.0", migration:"docs/MIGRATION-3.0.md"}}` for any `RENAME_MAP` key. Report the served name in all validation/log text. In the next major, re-key `_all_handlers`/`TIER_MAP`/argument maps to the served names and demote `RENAME_MAP` to a data-only tombstone table (Catalog proposal, "Migration shims").

### Finding 10: unspecified-identity-actor-self-asserted-and-defaults-to-shared-mcp
Severity:      major
Tag:           STILL-OPEN (`filigree-c2009921cf` session/run identity, `proposed`; code cites `filigree-81d3971467` for the HTTP actor-verification deferral)
Location:      `actor` parameter on 61 of 118 tools; default `"mcp"`
Defect class:  parameter-the-agent-cannot-fill (identity should be a connection property); deconfliction collapse
Evidence:      PROBE. `issue_update {priority:1}` (no actor) against an issue claimed by bob -> `CONFLICT ... details:{observed:"bob", expected:"mcp"}`; two agents that both omit `actor` are both `"mcp"` and pass each other's holder checks (`work_claim {actor:"mcp"}` then `issue_update` with no actor succeeded). Claim verbs alone require an identity (`Provide 'assignee' or 'actor'`). CODE: `dashboard.py` auth scope states HTTP writes carry unverified actors; stdio stamps `verified_author` (OS user `john`), which is identical for every agent on the host.
Confidence:    HIGH on behaviour, MEDIUM on how often agents omit it.
Qualification: framed as functional deconfliction (per the owner's rule), not security: the claim-aware default only deconflicts agents that name themselves distinctly.
Remediation:   Bind identity at connection time (stdio `--agent-id` launch arg, as Legis already does; `Mcp-Agent-Id` header or federation token claim over HTTP), make per-call `actor` an optional *override* removed from 61 schemas (also cuts catalog size), and refuse mutating calls with no resolvable identity instead of defaulting to `"mcp"`.

### Finding 11: tool-annotations-incomplete-and-prefix-derived
Severity:      major
Tag:           NEW
Location:      `mcp_server._apply_tier_metadata`, `_READ_ONLY_PREFIXES`, `_DESTRUCTIVE_TOOLS`
Defect class:  capability declaration; Consistency Gate "idempotency undeclared"
Evidence:      PROBE/CODE. Annotation census on the 118 served tools: 69 have none, 47 `readOnlyHint:true` only, 2 `destructiveHint:true` only (`issue_delete`, `file_delete`). No tool sets `idempotentHint`, `openWorldHint` or `title`. Read-only is inferred from the canonical *old-name prefix* (`get_`, `list_`, `search_`, `explain_`, `preview_`) so a read tool named otherwise (`session_context`, which does a side effect, `ensure_dashboard_running`) is skipped, and a future `get_*` that writes would be mislabelled read-only. By MCP spec an unannotated non-read-only tool defaults to `destructiveHint:true`, so hosts will treat `label_add` and `admin_compact_events` identically, and will prompt on both or neither. `admin_compact_events` (drops history), `admin_import_jsonl merge`, `admin_archive_closed`, `issue_batch_close force`, `finding_dismiss` are irreversible-ish and unflagged.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Declare annotations per tool in the registration table (not by prefix), including `idempotentHint` (true for `label_add`, `dependency_add/remove`, `work_heartbeat`, `finding_report`; false for the create class until Finding 5 lands), `destructiveHint` for compact/import/archive/force paths, `openWorldHint:true` for scan triggers and registry-backed tools. Add a test that every tool has all four hints explicitly set.

### Finding 12: silent-success-on-bad-filters
Severity:      major
Tag:           NEW (05-13 P1 "strict unknown MCP parameter rejection: Done" covered unknown *keys*, not bad *values*)
Location:      `issue_list`, `issue_search`, other list tools
Defect class:  silent misuse; error-as-empty-success
Evidence:      PROBE. `issue_list {type:"bogus"}` -> `{"items":[],"has_more":false}`; `{status:"nonsense"}` -> same; `{priority_min:3, priority_max:1}` -> same empty list; `{status_category:"wip", status:"open"}` -> returns the `open` issues (one filter silently wins). Contrast `issue_create {type:"nonsense"}` -> `VALIDATION` with the valid type list, so the information to reject exists. `limit:500` silently clamped (Finding 7).
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Validate `type` and `status` against the registered templates, reject `min > max`, reject conflicting `status`/`status_category` with `VALIDATION` and the valid values in `details`.

### Finding 13: primitive-selection-reference-data-as-tools-and-triple-pulse
Severity:      major
Tag:           NEW (06-02 plan chose tiers over consolidation; 04-18 never examined primitives)
Location:      `template_get`, `type_get`, `type_list`, `pack_list`, `schema_get`, `workflow_status_list`, `workflow_status_explain`, `workflow_guide_get`, `label_taxonomy_get`, `prompt_pack_list`, `scanner_available_list`, `mcp_status_get`; `session_context_get`, `summary_get`, resource `filigree://context`
Defect class:  tool-that-should-be-a-resource; primitive misuse
Evidence:      CODE/PROBE. Server declares 1 resource (`filigree://context`), 0 resource templates, 1 prompt. Roughly 10 argument-light, near-static read-only tools return model-readable reference text (e.g. `type_list` returned all 11 types with states; `workflow_guide_get {pack:"core"}` 6,895 bytes). `session_context_get`, `summary_get` and the `filigree://context` resource are three routes to the same "project pulse" content (`summary_get(format=json)` additionally embeds `stats_get`). `list_tools` description for `type_get` literally says "compatibility alias for template_get; returns the same canonical full workflow definition."
Confidence:    HIGH for the duplication; MEDIUM for the resource recommendation (depends on hosts: several agent hosts do not let the model read resources autonomously, which is why Claude Code users reach for tools).
Qualification: conditional on host resource support; hence the proposal keeps one fallback `reference_get` tool.
Remediation:   Publish reference data as resources/resource templates (`filigree://types`, `filigree://types/{type}`, `filigree://packs`, `filigree://schema`, `filigree://workflow/guide/{pack}`, `filigree://labels/taxonomy`, `filigree://scanners`, `filigree://prompt-packs`) and keep a single `reference_get(topic, key?)` tool for resource-blind hosts. Delete `type_get`. Collapse pulse to `summary_get` + the resource; keep `session_context_get` only if the sibling LLM review keeps it.

### Finding 14: overlapping-and-twin-tools
Severity:      major
Tag:           STILL-OPEN (`filigree-18bd3b8c98` "Toolkit DX", cited in `filigree-4c73f6cf22` as the owner of "a larger surface consolidation"); 04-18 smell "tool-as-CRUD-mirror" never run
Location:      see Catalog proposal
Defect class:  overlapping-tools; tool-as-CRUD-mirror
Evidence:      CODE (`RENAME_MAP`, schema dump). Confirmed overlaps:
  - `type_get` == `template_get` (documented alias).
  - `issue_subtree_label(parent_id,label)` vs `plan_label_tree(milestone_id,label)`: a milestone is an issue; same operation, differently named id parameters (`parent_id`, `milestone_id`).
  - `observation_promote` (1:1), `observation_promote_to_issue` (N:1 merge), `observation_batch_promote` (N:N): one verb, three tools; `observation_dismiss`/`_batch_dismiss`, `observation_link`/`_batch_link` twins.
  - `finding_promote` (has `attach_entity` flag) vs `finding_promote_and_attach_entity` (separate tool for the same effect).
  - `scan_trigger` vs `scan_trigger_batch`; `scanner_list` vs `scanner_available_list`.
  - `annotation_list`, `file_annotation_list`, `issue_annotation_list`, `annotation_attention_list`: four list tools over one table, differing by a filter.
  - `work_claim` vs `work_start`, `work_claim_next` vs `work_start_next`: claim-only variants are tiered `niche` while the transition variants are `core`.
  - `plan_create` vs `plan_create_from_file`; `label_add`/`label_batch_add` and `comment_add`/`comment_batch_add`.
  - `issue_file_list` / `file_list(issue_id)`; `entity_association_list` vs `entity_association_list_by_entity`.
Confidence:    HIGH (names/schemas), MEDIUM on which merges owners will accept.
Qualification: unconditional.
Remediation:   Per the catalog proposal.

### Finding 15: blocking-sync-handlers-on-the-event-loop
Severity:      major
Tag:           NEW
Location:      all `async def _handle_*`; `_handle_restart_dashboard` (`time.sleep` loops); streamable-HTTP daemon (:8749)
Defect class:  transport reliability; availability
Evidence:      CODE. `grep -c "to_thread\|run_in_executor" src/filigree/mcp_tools/*.py src/filigree/mcp_server.py` = 0 for every file. Handlers are `async def` but call synchronous `sqlite3`, file I/O and, in `admin_restart_dashboard`, up to 3 s of `time.sleep(0.1)` plus `os.kill`. `call_tool` also serialises each DB behind one `asyncio.Lock` (redundant for sync handlers, harmful for the daemon since the lock is held across the whole handler).
Confidence:    MEDIUM: structure is certain; real stall depends on workload (large `admin_import_jsonl`, `admin_export_jsonl`, `file_register` on big trees). No daemon trace supplied.
Qualification: matters in server mode (one process serving dashboard and every registered project's `/mcp`); negligible for stdio.
Remediation:   Run handlers via `anyio.to_thread.run_sync`, keep per-DB serialisation there, make restart non-blocking, and make `admin_restart_dashboard` unavailable over HTTP (restarting the process that hosts the call).

### Finding 16: admin-and-filesystem-path-tools-in-the-default-agent-catalog
Severity:      major
Tag:           NEW
Location:      `admin_archive_closed`, `admin_compact_events`, `db_checkpoint`, `admin_export_jsonl`, `admin_import_jsonl`, `admin_restart_dashboard`, `admin_reload_templates`, `plan_create_from_file`, `file_register`
Defect class:  capability declaration; tool-surface scope
Evidence:      CODE/schema. Eight maintenance tools plus path-taking tools are in the same flat catalog as `issue_get`; `admin_export_jsonl {output_path}` and `admin_import_jsonl {input_path, merge}` take server-host paths, which in daemon mode are paths on the daemon's machine, not the agent's. `tier: niche` is a description suffix, not an access boundary.
Confidence:    HIGH.
Qualification: functional framing: an agent can irreversibly `compact_events` or `import_jsonl merge` by guess; over HTTP the path semantics differ from stdio.
Remediation:   Launch profiles (`--profile agent|admin`, default `agent`) or a second server entry for admin; remove path arguments from the agent profile (return content or a resource link instead).

### Finding 17: no-output-schemas-and-text-only-results
Severity:      major
Tag:           NEW
Location:      global (118/118 tools have no `outputSchema`; `structuredContent` always null)
Defect class:  Consistency Gate "unstated return shape"; schema-drift-without-bump
Evidence:      PROBE. `res.structuredContent is not None` is `False` for every call; tool dump `outputSchema` count = 0. Return shapes live only in `types/api.py` TypedDicts and `docs/mcp.md`. Pretty-printed JSON (`indent=2`) adds ~17% bytes (620 vs 515 compact for one issue).
Confidence:    HIGH.
Qualification: unconditional; most important for federation peers (Clarion, Wardline, Shuttle, Loomweave) which parse these bodies.
Remediation:   Emit `structuredContent` plus `outputSchema` generated from the existing TypedDicts (one for success per tool, one shared error envelope), keep a compact JSON text mirror, and gate shape changes on the tool-list `listChanged`/contract version (Finding 18).

### Finding 18: no-contract-version-serverinfo-and-instructions-empty
Severity:      minor
Tag:           NEW
Location:      `Server("filigree")`, initialize result
Defect class:  capability declaration; schema versioning
Evidence:      PROBE. `initialize` -> `serverInfo: name='filigree' version='1.28.1'` (the MCP SDK's own version, not 3.3.0), `instructions` empty, capabilities `tools.listChanged:false, resources.subscribe:false, prompts.listChanged:false`. `get_mcp_status` exposes schema versions but there is no tool-contract version a consumer can pin or compare. HTTP is `stateless=True`, so there is no per-session negotiation to compensate.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Pass `version=filigree.__version__` and a short `instructions` string at server construction; add `_meta.contract_version` to the tool list (the rename plan's `deprecated_tool_name_calls` idea generalised) and bump it on any breaking schema or shape change.

### Finding 19: parent-id-alias-and-payload-shape-drift
Severity:      minor
Tag:           REGRESSED (05-14 row "ID and relationship naming consistency: Done": public payload emits both `parent_id` and `parent_issue_id`)
Location:      `admin_undo_last` response; every issue payload; `issue_subtree_label(parent_id)`, `plan_dependency_retarget(old_depends_on_id,new_depends_on_id)`, `plan_step_add(phase_id)`
Defect class:  schema drift; naming grammar
Evidence:      PROBE. `admin_undo_last` success body has `"parent_id":null` and no `parent_issue_id` (the same issue via `issue_get` has both). Every other payload carries both fields (a permanent duplicate pair). Leftover non-`issue_id` spellings: `parent_id`, `milestone_id`, `phase_id`, `step_id`, `old_depends_on_id`, `new_depends_on_id`, `target_id`.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Next major: drop `parent_id`, emit only `parent_issue_id`; route undo through the same projector; rename the stragglers to `*_issue_id`.

### Finding 20: cli-mcp-parity-gaps
Severity:      minor
Tag:           STILL-OPEN (`filigree-4c73f6cf22` for comment grammar; 04-18 pain point 5 "CLI has capability holes" partly closed)
Location:      `src/filigree/cli_commands/`
Defect class:  parity
Evidence:      `filigree --help` and group helps. CLI-only: `finding clean-stale`, `file migrate-registry`, `doctor`, `server *`. MCP-only (no CLI command found): `annotation_update/supersede/promote/link/unlink/attention_list`, `issue_subtree_label`/`plan_label_tree`, `entity_association_*`, `warpline_worklist_ingest`, `finding_promote_and_attach_entity`, `plan_step_move`/`plan_dependency_retarget` (no `plan` subcommands listed), `work_release`-family equivalents exist. Naming: CLI still verb-noun (`add-comment`, `get-comments`, `batch-add-comment`), MCP noun-verb (`comment_add`, `comment_list`); issue says aliases still missing. Defaults: CLI `list --limit` 100 vs MCP 50; default actor `cli` vs `mcp`. Positive: error JSON is identical between surfaces (`filigree show nope --json` -> `{"error":"Not found: nope","code":"NOT_FOUND"}`, exit 1).
Confidence:    MEDIUM-HIGH (command listing verified, deeper flags not exhaustively compared).
Qualification: matters because CLAUDE.md names the CLI as the fallback when MCP is down.
Remediation:   Generate the CLI from the same registration table as MCP (one source for noun-verb names, params, defaults); keep the verb-noun spellings as hidden aliases, which closes `filigree-4c73f6cf22` by construction.

### Finding 21: no-additionalProperties-and-duplicate-validators
Severity:      minor
Tag:           NEW (05-13 P1 strict-unknown-params: Done, mechanism is the finding)
Location:      `mcp_server._unknown_argument_error`, `_validate_schema_value`
Defect class:  schema discipline
Evidence:      Schema dump: 0 of 118 input schemas set `additionalProperties:false`, so strictness is a hand-written side table. `_validate_schema_value` re-implements a subset of JSON Schema (no `enum`, `items`, `minItems`, `anyOf`, `pattern`) after the SDK has already validated; PROBE shows the SDK rejects first (`Input validation error: ...`), so the in-house validator is mostly unreachable. 88 parameters have no `description`.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Set `additionalProperties:false` in the schemas and delete the side table and the hand-rolled validator; let one validator own the contract and wrap its error (Finding 2).

### Finding 22: stale-claim-fields-survive-close-and-reopen
Severity:      minor
Tag:           NEW
Location:      `issue_close`, `issue_reopen`
Defect class:  state-machine consistency
Evidence:      PROBE. After `issue_close actor:bob`, the closed payload still shows `assignee:"bob"`, `claim_expires_at` 2 days out. `issue_reopen actor:bob` returns `status:"open"` with `assignee:"bob"` and the original `claimed_at`: the reopened issue is "held" by bob for the rest of the old lease without a claim call.
Confidence:    HIGH on behaviour; MEDIUM that it is unintended.
Qualification: may be intentional ("reopen returns to previous holder"); if so it should be documented and heartbeat-gated.
Remediation:   Clear claim fields on close; reopen leaves the issue unassigned or requires an explicit `claim:true`.

### Finding 23: success-shaped-sentinels
Severity:      minor
Tag:           NEW
Location:      `work_claim_next`, `work_start_next` (`{status:"empty"}`), `admin_undo_last` (`{undone:false}`), `issue_close` already-closed (INVALID_TRANSITION), `work_release` already-released (CONFLICT)
Defect class:  error-envelope consistency
Evidence:      CODE `ClaimNextEmptyResponse(status="empty", reason=...)`; PROBE undo `{"undone":false,"reason":"..."}`. "empty queue" is a success-shaped body whose `status` key collides with the issue `status` field; "nothing to undo" is another shape; "already in target state" is an error (Findings 3, 8).
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Pick one: `{result:"no_op", reason}` for benign no-ops across tools, `NOT_FOUND`-class code only for genuinely absent targets.

### Finding 24: label-vocabulary-unvalidated
Severity:      minor
Tag:           NEW
Location:      `label_add`, `label_batch_add`, `issue_create labels`
Defect class:  parameter validation
Evidence:      PROBE. `label_add {label:"BAD LABEL!!"}` -> `label_result:"added"`, stored as `["BAD LABEL!!","x"]`. Search docs say "label vocabulary is kebab-case" and `label_taxonomy_get` defines namespaces.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Validate against a documented pattern (`^[a-z0-9][a-z0-9:_-]*$`), return `VALIDATION` with the pattern.

### Finding 25: resource-and-http-degraded-mode-asymmetry
Severity:      minor
Tag:           NEW
Location:      `read_context` resource; `create_mcp_app`
Defect class:  transport reliability
Evidence:      CODE. In degraded mode `read_context` returns `json.dumps(ErrorResponse)` as the body of a `text/markdown` resource (in-band error, no protocol error). Over streamable HTTP the same `SCHEMA_MISMATCH` is an HTTP status (`errorcode_to_http_status`) before JSON-RPC exists, while stdio gets an in-band tool result. Positive: `/mcp` is token-gated (`protected_paths ["/mcp","/mcp/*"]`, 401 `WWW-Authenticate: Bearer`, constant-time compare), and `mcp_status_get`/`list_tools` stay live in degraded mode.
Confidence:    MEDIUM (not exercised against the live daemon).
Qualification: unconditional; low frequency.
Remediation:   Raise a protocol error for degraded resource reads; document the HTTP-vs-stdio difference in `docs/mcp.md` and in the contract version.

### Finding 26: no-golden-conversation-or-retry-replay-tests
Severity:      minor
Tag:           NEW
Location:      `tests/mcp/`
Defect class:  Consistency Gate "missing golden-conversation test"
Evidence:      `tests/mcp/` has 30+ files (`test_concurrency.py`, `test_error_code_coverage.py`, `test_rename_map.py`, `test_tool_tiers.py`) but no file that replays a recorded multi-call conversation or asserts same-call-twice behaviour per side-effecting tool. Findings 1, 3, 4, 5, 8 are exactly the cases such a test would pin. (Listing only; I did not read the test bodies.)
Confidence:    MEDIUM.
Qualification: unconditional on the listing.
Remediation:   Add a table-driven "call twice" test over every mutating tool declaring `expected_second_call: noop|dup|error` and compare to `idempotentHint`.

### Finding 27: tier-marker-and-metadata-in-description-prose
Severity:      nit
Tag:           NEW
Location:      `_apply_tier_metadata`
Defect class:  metadata in the wrong channel (the content/prompt aspects are the sibling lane)
Evidence:      Every description ends in ` [tier: core|common|niche]`, appended at import and by suffix test; the tier is not in `_meta`/annotations where a host could use it.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   Emit tier in `_meta` (and use it for profiles, Finding 16).

### Finding 28: naming-grammar-exceptions
Severity:      nit
Tag:           STILL-OPEN (06-02 plan §9 open item 5: `dependency_critical_path` vs `..._get`)
Location:      served tool names
Defect class:  naming grammar
Evidence:      Served names deviate from `<entity>_<verb>`: `dependency_critical_path` (no verb), `work_ready`/`work_blocked` (adjective, not verb, vs `work_stale_list`), `issue_subtree_label` vs `plan_label_tree` (object-before-verb vs verb-last), `file_annotation_list`/`issue_annotation_list` (entity=file/issue, not annotation, vs `annotation_list`), `entity_association_list_by_entity`, `change_list` (returns events), `db_checkpoint` and `admin_*` mixed prefixes for the same tier of tool, `warpline_worklist_ingest` (entity=consumer, not the thing ingested), `finding_promote_and_attach_entity` (conjunction), `scan_*` plus `scanner_*`, `observation_promote_to_issue` (prepositional), `*_get` for singletons vs bare `schema_get`/`stats_get`/`summary_get` project aggregates.
Confidence:    HIGH.
Qualification: unconditional.
Remediation:   The catalog proposal removes most of these by merging; for survivors apply `<entity>_<verb>` strictly.

---

## Architect-vs-Critic Disagreements

No design rationale was supplied in this task; the "architect" positions below are the decisions recorded in the repo docs and ADRs.

### Disagreement 1: "No master-checklist implementation gap remains"
Architect position: 05-14 gap analysis closes every P1/P2/P3 row as Done, including "Claim-aware writes hard to misuse" and "Common response envelopes and slim paths".
Critic position: Findings 1, 3, 6, 8, 19 show the guarantees stop at the rows' stated scope: undo and release bypass the holder check; `issue_list` is not slim; batch envelopes differ per tool; `issue_undo` payload dropped `parent_issue_id`.
Proposed resolution: Reopen the two rows (claim-aware writes, slim paths) with the specific tools named; add the call-twice test (Finding 26) as the closing evidence.

### Disagreement 2: canonical-old-name internal identity (ADR-016 / 06-02 plan §5.1)
Architect position: Keep the old name as the internal canonical identity forever: it avoids re-keying `TIER_MAP`, argument maps and the three `get_mcp_status` guards.
Critic position: That choice was correct for the additive phase; after Phase 2 it is a standing leak (Finding 9) and doubles every registration key. The guards can key on a constant, the tags on the registration record.
Proposed resolution: In the next major, register tools once with their served names plus per-tool metadata (annotations, tier, profile), delete `NEW_TO_OLD`, and keep `RENAME_MAP` only as a tombstone table feeding `renamed_to` hints.

### Disagreement 3: tiering instead of consolidation (06-02 plan §1, approach (a))
Architect position: Tier tags plus a curated index solve discoverability without changing the wire.
Critic position: Tier is a description suffix, not a boundary, so all 118 schemas ship in every session (about 85.6 K characters of names/descriptions/schemas by my count; token-cost framing belongs to the sibling review). Consolidation and profiles reduce both the catalog and the failure surface.
Proposed resolution: Adopt the catalog proposal below at the major boundary, where breaking changes are already budgeted.

### Disagreement 4: "documented" batch semantics ("per-issue error handling", `docs/mcp.md`)
Architect position: Batch tools are documented as per-item.
Critic position: Per-item is not what they do for foreign-prefix ids, duplicates, empty change sets or all-fail finding batches (Finding 8). Re-derived from the contract: a response that lists one id in both `succeeded` and `failed` is not a coherent per-item contract regardless of the comment.
Proposed resolution: Specify the `BatchResponse` in Finding 8 and test it.

---

## Major-refresh catalog proposal

Headline: **118 tools -> 76 in the default agent profile (-36%), plus 7 maintenance tools in an opt-in `admin` profile (83 total); 10 reference tools move to resources with one fallback tool.** All names stay `<entity>_<verb>`. All mutating tools gain `client_request_id` (optional), explicit annotations, `isError` semantics, `outputSchema`; all list tools share one bounded slim-by-default contract; `actor` leaves the per-call schema (Finding 10).

Legend: KEEP, RENAME, MERGE (into the named survivor), KILL, RESOURCE (publish as resource; fallback via `reference_get`), PROFILE (admin only).

| Current served name | Action | Next-major name | Notes / migration shim |
|---|---|---|---|
| issue_get | KEEP | issue_get | `include_files` removed (use `file_list(issue_id)`), slim default |
| issue_list | KEEP | issue_list | Slim default, `response_detail`, validated filters, cursor, no `no_limit` |
| issue_search | KEEP | issue_search | Same bounding rule |
| issue_create | KEEP | issue_create | + `client_request_id` |
| issue_update | KEEP | issue_update | |
| issue_close | KEEP | issue_close | already-closed -> `no_op` |
| issue_reopen | KEEP | issue_reopen | clears claim (Finding 22) |
| issue_delete | KEEP | issue_delete | destructive hint |
| issue_validate | KEEP | issue_validate | |
| issue_batch_update | KEEP | issue_batch_update | unified `BatchResponse` |
| issue_batch_close | KEEP | issue_batch_close | unified `BatchResponse` |
| issue_event_list | KEEP | issue_event_list | bounded |
| issue_subtree_label | KEEP | issue_subtree_label | survivor of the pair |
| plan_label_tree | MERGE | issue_subtree_label | tombstone `renamed_to` |
| admin_undo_last | RENAME | issue_undo | `expected_event_id` required (Finding 1) |
| issue_file_list | MERGE | file_list(issue_id) | |
| issue_annotation_list | MERGE | annotation_list(issue_id) | |
| work_ready | KEEP | work_ready | bounded, cursor |
| work_blocked | KEEP | work_blocked | bounded |
| work_start | KEEP | work_start | the one claim-and-transition verb for a known issue |
| work_start_next | RENAME | work_next | `start:true` default; `start:false` = claim only |
| work_claim | MERGE | work_start(advance=false) | claim-only is a flag, not a tool |
| work_claim_next | MERGE | work_next(start=false) | |
| work_release | KEEP | work_release | holder-checked default (Finding 3) |
| work_release_mine | KEEP | work_release_mine | |
| work_reclaim | KEEP | work_reclaim | |
| work_heartbeat | KEEP | work_heartbeat | |
| work_stale_list | KEEP | work_stale_list | |
| dependency_add / dependency_remove | KEEP | dependency_add / dependency_remove | |
| dependency_critical_path | RENAME | dependency_critical_path_get | closes 06-02 open item 5 |
| plan_dependency_retarget | MERGE | dependency_retarget | generalised |
| plan_create | KEEP | plan_create | + `client_request_id` |
| plan_create_from_file | MERGE | plan_create(source) | path form only in admin profile |
| plan_get | KEEP | plan_get | |
| plan_step_add / plan_step_move | KEEP | plan_step_add / plan_step_move | |
| label_add / label_remove | MERGE | label_add / label_remove (take `issue_ids[]`) | validate label pattern |
| label_batch_add / label_batch_remove | MERGE | label_add / label_remove | |
| label_list | KEEP | label_list (`detail=taxonomy`) | |
| label_taxonomy_get | MERGE | label_list(detail=taxonomy) | |
| comment_add | KEEP | comment_add | + `client_request_id` |
| comment_batch_add | KILL | none | loop client-side; no known caller |
| comment_list | KEEP | comment_list | bounded |
| file_list | KEEP | file_list | `issue_id` filter |
| file_get | KEEP | file_get | |
| file_timeline_get | KEEP | file_timeline_get | |
| file_register | KEEP | file_register | path semantics documented; stdio only |
| file_association_add | KEEP | file_association_add | |
| file_delete | KEEP | file_delete | destructive |
| file_annotation_list / annotation_list / annotation_attention_list | MERGE | annotation_list (filters `file`, `issue`, `needs_attention`) | |
| annotation_create / get | KEEP | annotation_create / annotation_get | |
| annotation_update, annotation_resolve, annotation_supersede | MERGE | annotation_update (`status`, `replacement_id`, `reason`) | |
| annotation_link, annotation_unlink | MERGE | annotation_update (`add_links`, `remove_links`) | |
| annotation_promote, annotation_carry_forward | KEEP | same | |
| observation_create / list | KEEP | same | |
| observation_dismiss + observation_batch_dismiss | MERGE | observation_dismiss(`observation_ids[]`) | |
| observation_link + observation_batch_link | MERGE | observation_link(`observation_ids[]`) | |
| observation_promote + observation_promote_to_issue + observation_batch_promote | MERGE | observation_promote(`observation_ids[]`, `mode: each|merge`) | |
| finding_list / get / report | KEEP | same | X-6: slim default; X-5: `sink`, `tier` filters |
| finding_update + finding_batch_update + finding_dismiss | MERGE | finding_update(`finding_ids[]`, `status`, `reason`) | |
| finding_promote + finding_promote_and_attach_entity | MERGE | finding_promote(`attach_entity`) | |
| entity_association_add / remove | KEEP | same | |
| entity_association_list + entity_association_list_by_entity | MERGE | entity_association_list(`issue_id` or `entity_id`) | |
| warpline_worklist_ingest | KEEP | warpline_worklist_ingest | profile `federation` candidate |
| scan_trigger + scan_trigger_batch | MERGE | scan_trigger(`file_paths[]`) | |
| scan_preview / scan_status_get | KEEP | same | |
| scanner_list + scanner_available_list | MERGE | scanner_list(`include_available`) | |
| scanner_enable / scanner_disable | KEEP | same | |
| prompt_pack_list | RESOURCE | `filigree://prompt-packs` | |
| template_get | RESOURCE | `filigree://types/{type}` | |
| type_get | KILL | none | documented alias of template_get; tombstone `renamed_to: template_get`/resource |
| type_list, pack_list, schema_get | RESOURCE | `filigree://types`, `filigree://packs`, `filigree://schema` | |
| workflow_status_list, workflow_status_explain, workflow_guide_get | RESOURCE | `filigree://workflow/statuses`, `.../explain/{type}/{status}`, `.../guide/{pack}` | |
| (new) | ADD | reference_get(topic, key?) | single fallback for hosts that cannot read resources; replaces 10 tools |
| workflow_transition_list | KEEP | workflow_transition_list | issue-specific, stays a tool |
| stats_get | MERGE | summary_get(format=json) | already embedded there |
| summary_get / metrics_get / change_list / reconciliation_debt_list | KEEP | same | change_list is the model cursor |
| session_context_get | KEEP* | session_context_get | *subject to the sibling LLM review's verdict |
| mcp_status_get | RENAME | status_get | name is the diagnostic; keep degraded-mode exemption by constant |
| admin_archive_closed, admin_compact_events, db_checkpoint, admin_export_jsonl, admin_import_jsonl, admin_restart_dashboard, admin_reload_templates | PROFILE | same names, `--profile admin` | not served in default profile; keep CLI equivalents; restart unavailable over HTTP |

Default-profile tally: issue 14 (incl. `issue_undo`), work 9, dependency 4, plan 4, label 3, comment 2, file 6, entity-association 3, warpline 1, scan 3 + scanner 3, annotation 6, observation 5, finding 5, reference 1 + workflow_transition_list 1, project/diagnostic 6 (`summary_get`, `metrics_get`, `change_list`, `reconciliation_debt_list`, `session_context_get`, `status_get`) = 76 (14+9+4+4+3+2+6+3+1+6+6+5+5+2+6).

### Migration-shim notes
1. **One tombstone table, not a live alias layer.** The 3.0 plan banned a 228-tool catalog and removed aliases; keep that. For every KILL/MERGE/RENAME row emit `NOT_FOUND` with `details:{renamed_to, removed_in, migration}` (Finding 9). A failing test asserts every removed served name has a tombstone; the table lives in the same module as `RENAME_MAP`, which becomes data-only.
2. **Announce one minor ahead.** In the last 3.x minor add `_meta:{deprecated:true, replaced_by}` and a counter of old-name calls in `status_get` (the 06-02 plan's telemetry, which `docs/MIGRATION-3.0.md` shows was implemented as `deprecated_tool_name_calls`); do not publish duplicates in `list_tools`.
3. **Parameter-level shims for merges.** Merged tools accept the old singular parameter (`observation_id`, `finding_id`, `issue_id` on label tools) for one major as an alias of the array form, returning the unified `BatchResponse` plus `warnings:[{code:"DEPRECATED_PARAM"}]`. Response shape change (single-object -> batch envelope) is the real break; document it in the migration table row by row.
4. **Contract version.** Bump `_meta.contract_version` and `serverInfo.version` together (Finding 18) so federation consumers can gate on it instead of guessing from error text.
5. **CLI.** Keep every current verb-noun CLI command as a hidden alias of the generated noun-verb command (closes `filigree-4c73f6cf22`), so the CLI-as-fallback story does not break at the same boundary.
6. **Resources first.** Ship the resource URIs in the last minor alongside the existing tools, so hosts and skills can migrate before the tools disappear.

---

## Consistency Gate sweep

| Gate item | Result |
|---|---|
| Agent-voice intent | Declared; descriptions are sibling lane |
| Idempotency declared per tool | FAIL (Findings 1, 3, 4, 5, 11; no `idempotentHint`, no key) |
| Return-shape budget stated | FAIL (Findings 6, 7) |
| Recoverable error envelope | PARTIAL (shared enum and flat shape: pass; `isError`, `retryable`, uniform hint: fail; Findings 2, 9) |
| Silent schema break | PASS for 3.0 break handling in docs, FAIL for runtime hint and contract version (Findings 9, 18) |
| Concurrency contract defined | PARTIAL (per-DB serialisation and claim leases: pass; release/undo holder semantics and event-loop blocking: fail; Findings 1, 3, 15) |
| Golden-conversation test | FAIL (Finding 26) |
| Smell catalog walked | PASS (see summary) |

Smells checked: overlapping-tools fired (14); tool-as-CRUD-mirror fired (annotation, observation, finding twin families; 14); error-as-stack-trace clear (no traces seen; internal exceptions raise to SDK, unobserved, see Info Gaps); parameter-the-agent-cannot-fill fired (identity `actor`, Finding 10; `expected_assignee` on 13 tools); return-shape-that-blows-budget fired (6, 7); retry-amplification fired (1, 3, 4, 5, 8); schema-drift-without-bump fired (9, 17, 18, 19); namespace-collision clear (the `mcp__filigree__` wrapper makes cross-server collision moot; served names do not shadow RENAME_MAP keys, verified by the existing invariant test); resource-that-should-be-a-tool clear (the one resource is a pulse that duplicates two tools but is not misclassified); tool-that-should-be-a-resource fired (13).

---

## Machine-readable summary

```yaml
review_summary:
  surface_size:
    tools: 118
    resources: 1
    prompts: 1
    error_classes_seen: 16        # ErrorCode enum members; 9 observed live
    trace_supplied: false         # live probes only; no production retry trace
  total_findings: 28
  by_severity:
    blocker: 1
    major: 16
    minor: 9
    nit: 2
  by_tag:
    NEW: 21
    STILL-OPEN: 5                 # F7 (b789da2a1e), F10 (c2009921cf), F14 (18bd3b8c98), F20 (4c73f6cf22), F28 (06-02 plan open item 5)
    REGRESSED: 2                  # F9 (ADR-016 intent), F19 (05-14 naming row)
  intent_context_declared: true
  smells_checked:
    overlapping-tools: fired
    tool-as-CRUD-mirror: fired
    error-as-stack-trace: clear
    parameter-the-agent-cannot-fill: fired
    return-shape-that-blows-budget: fired
    retry-amplification: fired
    schema-drift-without-bump: fired
    namespace-collision: clear
    resource-that-should-be-a-tool: clear
    tool-that-should-be-a-resource: fired
  disagreements_recorded: 4
  top_findings:
    - slug: undo-walks-back-and-has-no-event-guard
      severity: blocker
      tag: NEW
      location: tool admin_undo_last
    - slug: domain-errors-isError-false-and-two-error-shapes
      severity: major
      tag: NEW
      location: global call_tool / _text
    - slug: pagination-and-bounding-is-four-regimes
      severity: major
      tag: STILL-OPEN (filigree-b789da2a1e)
      location: all list tools
  recommended_remediations:
    - "Add expected_event_id and holder check to undo; make release holder-checked and idempotent by default"
    - "Set isError on every error envelope, re-emit SDK validation failures as {error,code,details}, add retryable/hint, publish outputSchema"
    - "One bounded slim-by-default list contract with cursors and no no_limit bypass; one BatchResponse; client_request_id on every create-class tool"
  catalog_proposal:
    from_tools: 118
    default_profile_tools: 76
    admin_profile_tools: 7
    moved_to_resources: 10
```

---

## Confidence Assessment

Overall confidence: Moderate-High. Behavioural findings (1, 2, 3, 4, 5, 6, 8, 9, 12, 19, 22, 24) were reproduced against the real stdio server and are High. Structural findings (7, 11, 13, 14, 17, 18, 21, 28) come from the live schema dump and code reads and are High. Findings 15, 25 and 26 are Moderate: they rest on code structure or file listings, not a daemon trace or test-body reading. The catalog proposal is a design recommendation (Moderate); the 76/7 counts are my arithmetic on the proposed table, not verified against caller telemetry. A real conversation trace with retries (host timeouts) and the `deprecated_tool_name_calls` counter from the live daemon would raise Findings 1, 4 and 9 to confirmed-blocker/major.

## Risk Assessment

Implementation risk (of making these changes): High, because the proposal is wire-breaking on the exact surface federation peers (Clarion, Wardline, Shuttle, Loomweave) consume; Reversibility: Moderate (tombstones and a contract version make rollback possible; response-shape changes are not reversible for consumers who already migrated).
Residual risk if ignored: (a) state corruption of issue history through retried undo (Finding 1), (b) double-worked and stranded claims in multi-agent runs (Findings 3, 4), (c) duplicate issues/comments/plans on retry (5), (d) context blow-ups from `issue_list` and unbounded list tools on a tracker whose descriptions are multi-KB (6, 7), (e) clients that cannot tell failure from success (2). Mitigation order: Findings 1-5 and 8 first (additive-compatible: new optional parameters and flags), then the envelope/pagination/annotation work, then the catalog merge at the major.

## Information Gaps

- No production trace: actual retry frequency, host timeout behaviour, and whether any host honours `isError` or the annotations are unknown.
- Daemon (:8749) behaviour under concurrent project load was not exercised; Findings 15 and 25 are structural.
- Handler bodies for annotations, observations, scanners and files were read only at schema level; their idempotency (other than the ones probed) is unverified, as are `scan_trigger` happy paths (no scanner installed in the probe project).
- The test bodies under `tests/mcp/` were listed, not read.
- Federation consumers' actual tool usage (which of the proposed KILL/MERGE rows have external callers) is unknown; the old-name telemetry counter would answer it.
- `warpline`/`loomweave` MCP peers were not exercised; `warpline_worklist_ingest` was assessed from its schema only.

## Caveats & Required Follow-ups

- Security-shaped observations (self-asserted actor, HTTP unverified identity) are deliberately framed as deconfliction/availability findings per the owner's rule; the token gate on `/mcp` is credited, not criticised.
- Several "intentional" behaviours (release without holder check, reopen retaining the assignee, `status:"empty"`) have supporting comments or docs; I re-derived them from the claim-aware contract in ADR-008 rather than accepting the comments. If the owner confirms a coordinator override is intended for release/undo, the remedy is an explicit `override:true` parameter, not silence.
- The probe project is a fresh `filigree init` with default packs; magnitudes in Findings 6 and 7 use synthetic 3 KB descriptions, with one real-tracker row sample for plausibility.
- Required follow-ups, in order: (1) decide Findings 1, 3, 4 (additive fixes can ship in a 3.3.x patch); (2) file tracker issues for Findings 2, 5, 8, 9, 10, 11 (none exist today; I filed nothing, per the read-only rule); (3) fold X-6 into the shared bounding rule rather than fixing `finding_list` alone; (4) ratify the catalog proposal with the Toolkit DX epic `filigree-18bd3b8c98`; (5) domain-owner review of the consumer impact for each KILL/MERGE row before the tombstone table is frozen.
