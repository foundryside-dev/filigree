# API Review: Filigree federation HTTP surface (`/api/weft/*`, living aliases, auth, sibling seams)

- Reviewer: api-reviewer (actor `claude-filigree`), 2026-10-07
- Repo state: `release/3.3.0` @ `59053a3` (clean), Loomweave index fresh (run `d88d095a`, commit matches HEAD)
- Mode: read-only. The only live calls were GETs against the ephemeral dashboard on :8834 (health, `/api/weft/{ready,issues,blocked,changes}`, `/api/loom/issues`). The daemon on :8749 was not up. Wardline MCP was down.
- Tag key: **NEW** = no existing issue found. **STILL-OPEN** = tracked by an open issue (cited). **REGRESSED** = previously fixed, now broken. No REGRESSED findings were identified.

## Summary

| Category | Score | Issues |
|---|---|---|
| REST / resource modeling | WARN | 4 (F9, F10, F12, F13) |
| Versioning / generations | FAIL | 3 (F3, F10, F15) |
| Security (re-derived as functional) | WARN | 2 (F13, F14) |
| Error handling / envelope | WARN | 3 (F4, F8, F11) |
| Consistency / pagination / bounding | WARN | 4 (F5, F6, F7, F16) |
| Idempotency / partial batch | FAIL | 3 (F2, F6, F12) |
| Availability / production readiness | FAIL | 4 (F1, F7, F11, F14) |
| Documentation / contract drift | WARN | 4 (F3, F9, F15, F17) |

Strengths worth keeping:
- Real shape adapters per generation under `src/filigree/generations/`.
- A closed `ErrorCode` enum with an exhaustive status mapper (`src/filigree/types/api.py:861`).
- 29 pinned weft fixtures under `tests/fixtures/contracts/weft/`.
- A registry-driven sibling byte-drift check (`tests/federation/test_sibling_drift.py`) with a scheduled armed lane.
- Governed closes fail closed, with a per-issue versus batch short-circuit distinction (`governance.py`).
- The scan-ingest write window is correctly isolated: private connection, `to_thread`, `BEGIN IMMEDIATE` plus busy-retry (`db_files.py:1940-1990`).
- `X-Filigree-Project` echo, plus fail-closed on ambiguous federation writes in server mode (`dashboard.py:960-1012`).
- 400/401/404 bodies on the weft surface use the shared flat envelope.

## Headline

**The refresh decision is driven by ADR-002 §3: "additive within weft" versus "needs a new generation".**
- Findings F5, F8, F9 (and F6 if cursoring is adopted) cannot be fixed without changing shapes that siblings already pin. They need a successor generation.
- F1, F2, F7, F12 (the `failed[]` part), F14 and F17 can ship now, additively, in weft.
- F3 (the unrecorded retirement of `loom`) must be closed with an ADR **before** a successor generation is announced. Otherwise "frozen" is not a credible promise to Wardline, Legis, Loomweave or Warpline.
- A major version number alone does not imply a new generation (ADR-002 §3).

## Critical / High issues

| ID | Tag | Sev | Location | Issue | Risk | Fix |
|---|---|---|---|---|---|---|
| F1 | NEW | **High (availability)** | `dashboard_routes/issues.py:623, 1476` (single close), `:91-130` `_gate_batch_failures` / `_gate_status_change_failures`; `legis_client.py:52` (5 s timeout); `dashboard.py:1272` (single uvicorn worker) | Governed closes call `governance.evaluate_closure_gate` **synchronously inside `async def` handlers**. That means blocking `urllib` to Legis (5 s) plus the Loomweave drift resolver (retry sleeps, `registry.py:1112-1137`). The batch path does not thread `legis_known_down` (only `finding_issue_cascade.py:150` does). | With Legis down or slow, one HTTP batch-close of N governed issues stalls the whole daemon for about 5N s. Scan ingest, `/api/health` and every sibling stall with it. | Move the gate off the loop with `FiligreeDB.borrow_for_worker_thread` and `to_thread`, or a gate pre-pass on a worker thread that only does the network call. Thread `legis_known_down` through `_gate_*_failures`. Design tension: the module header (`issues.py:1-12`) pins writes to the loop connection (CONTRACT-E). The fix is to run only the network probe off-loop and keep DB reads on-loop. |
| F2 | NEW | **High** | `generations/weft/adapters.py:355-360` (`failed=[]` hardcoded, comment "until per-finding ingest failure tracking lands"); `db_files.py:1273-1301`; `files.py:640-675` | Partial-batch semantics are not machine-readable. Commit `20bc918` makes an over-cap path drop its findings, but the only signal is a free-text `warnings[]` string, under HTTP 200. The `failed[]` channel exists in the envelope and is never populated. `succeeded` means "newly created finding ids" only. A fully replayed batch returns `succeeded: []`, `failed: []`. | Wardline cannot tell which findings were dropped or why. An agent retry re-sends everything and gets the same silent drop. The `BatchResponse` contract is half-implemented. | **Additive in weft.** Populate `failed[]` with `{id: <path|fingerprint>, error, code: VALIDATION/BODY_TOO_LARGE}` for skipped paths. Add `stats.findings_skipped`. Keep `warnings[]`. Document `succeeded` semantics. |
| F3 | NEW (policy breach) + STILL-OPEN `filigree-bc80a673e1` (P4, wording only) | **High (cross-product contract)** | `docs/MIGRATION-3.0.md:302-318`; `docs/architecture/decisions/ADR-002` §3 and §8; live `GET /api/loom/issues` returns `404 {"error":"Not Found","code":"NOT_FOUND"}` | The `loom` generation was retired in 3.0.0 by a hard rename to `weft`, with **no alias** and no 12-month deprecation, and with no retirement ADR. ADR-002 §3/§8 say a generation is frozen for life and retired only by ADR plus 12 months' notice. ADR-002 itself still says `loom` throughout. `docs/federation/contracts.md` says weft was "Introduced in 2.0". The 404 is indistinguishable from "resource missing". | The "frozen generation" promise is already broken once, so siblings have no reason to trust the next freeze. The tracking issue is P4 and is about doc wording, not the policy breach. Under the owner's rule on cross-product wire work, this is not a P3/P4 deferral. | Before the refresh: write a retroactive ADR (supersedes ADR-002 §4 naming and §8). Have `/api/loom/*` return a structured `410`/`301` with `details.successor`. Do not mint the successor name until that ADR exists. Re-scope `filigree-bc80a673e1` upward. |
| F4 | NEW | **High (agentic consumer)** | `dashboard_routes/issues.py:79-88` `_closure_gate_block`; `types/api.py:457-486, 861-891`; `dashboard.py:828-841` | The closed `ErrorCode` enum conflates retryable and terminal outcomes. `CONFLICT` 409 is used both for "Legis says no" (terminal) and "Legis unavailable" (retry later). `PERMISSION` is 401 from the middleware but 403 from the mapper, and `_status_to_errorcode` folds both back. `INTERNAL` is 502 at the gate but 500 in the mapper. `SCHEMA_MISMATCH` is returned as 409 by the app handler, while the mapper says 503. The scan 413 is coded `VALIDATION`, but contracts.md documents `VALIDATION`→400. There is no `Retry-After` and no `retryable` flag anywhere (`grep` found none). | Agents branch on `code`, as the enum docstring says. A Legis outage reads as policy denial, and an upstream Loomweave failure reads as "your request is invalid" (F11). | Needs a new generation to fix cleanly. Add `details.retryable` / `details.cause_kind` to every envelope, and split `CONFLICT` into `BLOCKED_BY_POLICY` and `DEPENDENCY_UNAVAILABLE`. Until then, populate `details.cause_kind` additively (the registry envelope already has it) and make the mapper table the single source for status. |
| F5 | NEW | **High (wire-breaking to fix)** | `issues.py:1077-1096` (`/ready`, `/blocked`: docstrings admit "unbounded today"); `issues.py:1203-1215` (`/issues/{id}/files`); live `/api/weft/ready` returned 68 KB with `has_more` hard-coded false | Several `ListResponse` endpoints ignore `limit`/`offset` and always claim `has_more: false`. Others allow `limit` up to 10,000 (`common.py:36`), and `/search` silently clamps to 1000 while the others return 400 on out-of-range. | Unbounded payloads grow with the tracker. A silent clamp versus a 400 is an inconsistent contract. A safe default `limit` would change the shape for existing pinners. | Successor generation: enforce `limit` everywhere with a documented cap (e.g. 200) and cursor tokens. Interim additive step: accept optional `limit`/`offset` on `/ready` and `/blocked` while the default stays unbounded, and mark the endpoints `unbounded` in contracts.md. |

## Medium issues

| ID | Tag | Sev | Location | Issue | Recommendation |
|---|---|---|---|---|---|
| F6 | NEW | Medium | `db_files.py:1939` (`findings_count + ?`), `db_files.py:1478` (`seen_count + 1`); no `Idempotency-Key` in any weft write | Scan-ingest replay is mostly idempotent for findings (fingerprint or file/rule/line dedup) but not for counters. A retried batch double-counts `scan_runs.findings_count` and bumps `seen_count`. Post-commit cascades (reopen/close issues) run in separate transactions: a crash between them leaves finding state and issue state diverged, covered only by reconciliation debt. `POST /api/weft/issues` and comment creation have no idempotency key, so a retry duplicates. | Additive: accept an optional `Idempotency-Key` or `batch_id` header on weft writes. Make `findings_count` count distinct findings. Document replay semantics per endpoint in contracts.md. |
| F7 | NEW | Medium (availability) | `dashboard_routes/common.py:112-122` (`request.json()`); `files.py:51-53, 283-330` | No request-body byte cap. The 1000-findings and text-length checks run after the full body is parsed. `scanned_paths` is allowed up to 100,000 entries. No uvicorn `limit_concurrency` or request timeout (`dashboard.py:1272`). The Loomweave resolve path has per-chunk deadlines but no overall request deadline. | Add a body-size cap (e.g. 8 MiB) before parse, returning 413 with the standard envelope. Add an overall ingest deadline. Availability, not a security finding. |
| F8 | NEW | Medium | `dashboard.py:860-883` and `types/api.py:861` | `_http_exception_to_envelope` logs a warning and coerces any unmapped status (405, 413, 422 variants) to `VALIDATION`. Framework 404/405 bodies carry no `details`. The 404 for a retired path gives no migration hint. | Add a generation-aware 404 handler (F3) and map 405/413 explicitly. |
| F9 | NEW | Medium (contract placement) | `dashboard.py:714-724` (entities only has `create_classic_router`); `dashboard_routes/entities.py:122-190`; `dashboard_auth.py:32-33` | Federation-critical resources live only on the frozen classic generation. `POST /api/issue/{id}/entity-associations` carries Legis sign-off `signature`/`signoff_seq`. `GET /api/entity-associations?entity_id=` is Loomweave's `issues_for` and Warpline's reverse lookup (ADR-029). They are not in `/api/weft/*`, so they have no weft pinning fixture (the only golden is `tests/fixtures/contracts/entity-associations-response.json`, at the contracts root). They sit outside the token gate (`is_weft_scoped_path`) and outside the server-mode fail-closed ambiguous-write guard (`dashboard.py:996-1007`). An unscoped write lands on the default project and is only echoed via `X-Filigree-Project` (`filigree-b62d865dad`, closed as an echo-only fix). The in-code docstring justifies the placement as "transport-open, deconfliction is the boundary", which is a security argument. Re-derived from ADR-002 §2/§7, it is a modeling gap: federation capability is supposed to land in weft. | Successor generation: `/api/<gen>/issues/{id}/entity-associations` and `/api/<gen>/entity-associations`, with project-scoped fail-closed writes. Keep the classic routes as frozen aliases. |
| F10 | NEW | Medium | `docs/federation/contracts.md` (Living-surface table); `dashboard_auth.py:32-33` | Living-surface policy is ad hoc. Only `scan-results` and `observations` have aliases, and gating depends on a hand-maintained allow-list (`LIVING_FEDERATION_ALIASES`, whose docstring says "add new aliases here"). The decision table has several "deferred (alias-eligible)" rows with no revisit date, and C3 deferred aliases "until at least Phase D", which landed long ago. Resource naming is split: singular `/issue/{id}` (classic) versus plural `/issues/{id}` (weft), and weft `/findings`, `/files/{id}/findings` and `/issues/{id}/files` mix flat and nested shapes. | In the refresh, either drop the living surface (ADR-002 says it is non-stability anyway) or generate the gate list from the router. Add a test that fails when a weft-owned route is outside the gate. |
| F11 | NEW | Medium | `files.py:243-248, 655-663`; `db_files.py:1273-1293`; `registry_errors.py` | Scan ingest maps registry failures inconsistently. `RegistryUnavailableError` becomes `REGISTRY_UNAVAILABLE` 503 with only `str(e)`, while the app-level handler (`dashboard.py:842-858`) builds the richer envelope with `cause_kind` and hint. A generic `RegistryResolutionError` (including batch `other_errors`, which come from Loomweave 5xx or 4xx errors) becomes `VALIDATION` 400. Under `registry_backend=loomweave` with `allow_local_fallback=false` (the default), scan ingest fails closed whenever Loomweave is down, so Wardline findings are lost unless the producer retries. | Route both exceptions through `registry_startup_error_response`. Add `Retry-After` on 503. Document the fail-closed coupling in contracts.md as a deliberate opt-in (ADR-002 §7 says weft must work without peers, and that holds only for `registry_backend=local`). |
| F12 | NEW | Medium | `warpline_consumer.py` (ingest loop), `mcp_tools/federation.py`; no route in `dashboard_routes/` or on `codex/gs7-warpline-worklist` | Warpline worklist ingest is **MCP-only**. ADR-002 §5 says "callers who need pinned stability use HTTP". The weft contract fixtures cover no worklist endpoint. The ingest is SEI-keyed, so a retry is safe (`linked`), but it is not atomic across items: a storage error on item 3 returns a single `IO` envelope and loses the report for items 1-2, which were already filed. The check-then-create is not in one transaction, so two concurrent ingests can double-file. There is no item cap, and non-mapping items are silently dropped from `total`. | Expose `POST /api/<gen>/worklists:ingest` with a per-item result and `failed[]`. Make filing atomic, or return partial results on error. Cap items. |
| F13 | NEW | Medium (functional) | `dashboard_auth.py:36-57`; `docs/federation/contracts.md` §Authentication lines ~35-62 | The token gate covers `/api/weft/*`, two living aliases and `/mcp`, while the same issue data is readable and writable unauthenticated on the classic surface (`/api/issues`, `/api/issue/{id}/close`, …). The gate is therefore a contract marker ("this surface is for siblings"), not an access boundary. That is acceptable under the deconfliction posture, but consumers cannot infer which surface is the contract from a 401. Re-derived as functional: a sibling that hits a classic route works either way and silently escapes pinning. | Document this plainly as "gate = generation marker". Optionally have the classic routes return a `Deprecation`/`Link` header pointing to the weft equivalent. |
| F14 | NEW (observed on this host) | Medium (availability) | `federation_token.py` (`mint_token_file`, returns an existing file untouched); `dashboard.py:141` (boot mint, never `rotate=True`); `cli_commands/admin.py:989` (rotate is manual) | **Observed:** on this host, the token in `.weft/filigree/federation_token` returns `401` against the :8834 daemon, while the `WEFT_FEDERATION_TOKEN` env value returns `200`. Boot never reconciles an env pin against an existing file. A same-host sibling that follows the documented tier-2 default (read the file) is locked out until someone runs `doctor`/rotate and restarts. The token is also resolved once at `create_app`, so rotation is not live. The `/api/health` `auth.token_env` shows `WEFT_FEDERATION_TOKEN` but not the source tier or mismatch. | At boot, when an env token exists and differs from the file, either realign the file or warn loudly with the sibling impact. Expose `auth.source` and a `file_matches_active` boolean on `/api/health`. Pairs with `filigree-806dc04161`. |
| F15 | NEW | Medium (docs/onboarding) | `docs/federation/contracts.md` §Authentication vs `ADR-018` amendment (PR #52 B1) vs live `/api/health` | contracts.md still says auth is off by default and `/api/v1/scan-results` is **not** gated. The code and the ADR-018 amendment say the token is auto-minted (on by default) and both `/api/v1/*` aliases are gated. `/api/health` confirms all five protected paths. A sibling author following contracts.md will POST to the wrong path or skip the header. | Fix contracts.md. Add a docs-contract test that diffs `auth_scope` against the documented list. |

## Low issues

| ID | Tag | Sev | Location | Issue | Recommendation |
|---|---|---|---|---|---|
| F16 | NEW | Low | `analytics.py:575-640` (`/changes`) | The feed requires `since` and cannot start from "now", but it does cursor correctly (`next_since` plus `next_event_id`). Those extra keys are not part of the documented `ListResponse`, and `offset` is rejected. Offset pagination elsewhere runs over mutable data, and the `/issues` default order is not stable (first live row was an old closed P0). | Document `/changes` as the only cursor-paged feed. Give every list a documented `order_by` and cursor in the successor generation. |
| F17 | NEW | Low (drift risk) | `tests/federation/_oracle.py:174-260` `DRIFT_REGISTRY`; `tests/_fakes/legis_http.py`; `tests/test_warpline_consumer.py` | Sibling contract coverage is uneven. Pinned: capabilities, loomweave scan-results, SEI oracle, entity-associations, weft issue detail, three Wardline goldens, Legis sign-off binding. **Not pinned:** Legis `GET /filigree/issues/{id}/closure-gate` (tested only against a hand-written fake), and the Warpline worklist (no vendored golden or drift entry on HEAD). The Warpline goldens exist on the unlanded branch `codex/gs7-warpline-worklist`, which has diverged heavily from main (about 1.4k insertions and 3.4k deletions vs main) and cannot be merged as-is. | Add the closure-gate wire golden with a Legis-owned drift entry. Land the Warpline goldens by rebasing, not merging. Treat both as refresh-blocking (see the recommendations). Related: `filigree-1544621b0a` (STILL-OPEN P2). |
| F18 | STILL-OPEN `filigree-806dc04161` | Low | `dashboard.py:1024-1036` | `/api/health` is shallow: version, mode and an auth block, with no DB, registry or Legis probe, no `ready` versus `live` split, and no generation or capability discovery for `/api/weft` (ADR-002 §8.3 promised a tool telling consumers which generation they use). `/api/files/_schema` is the only capability probe and it is classic and files-only. | Add `/api/weft/_capabilities` with `generation`, `successor`, `limits` and `auth.source`. |
| F19 | STILL-OPEN `filigree-81d3971467` | Low | `dashboard.py:156-175` | HTTP `actor` is self-asserted and `verified_actor` is NULL. Surfaced honestly in `/api/health`. Not escalated, per the deconfliction posture. | No action beyond keeping the health note. |
| F20 | STILL-OPEN `filigree-1b7102e0b6` (P4, proposed) | Low | `dashboard.py` (`docs_url=None, redoc_url=None`) | No generated OpenAPI per generation, so there is no machine-readable contract for agent consumers. The fixtures are examples, not a schema. | Promote this to the refresh scope. It would also remove the manual doc drift in F15. |
| F21 | NEW | Low | `governance.py:398` | `LEGIS_URL` unset means governance is OFF even for issues that carry a Legis `signature`. A governed issue then closes unchecked. This is by design ("invisible until wanted"), but it is a silent configuration fail-open. | Return `UNAVAILABLE` for a signed binding when `LEGIS_URL` is unset, or at least log it and add a warning to the close response. Worth an owner decision. |

## Assessment by requested area

**Resource modeling and URI consistency.**
- Weft is plural and issue_id-based, with unified envelopes. The adapters keep handlers shared.
- The inconsistencies are F9 and F10: federation resources on classic, singular versus plural paths, and an ad hoc living surface.
- `/api/p/{key}/weft/...` plus `?project=` scoping is sound. Unscoped weft writes fail closed.

**Versioning and generation strategy.** See F3 and the recommendations. The named-generation design is good. Its lifecycle policy was not followed at the first real test.

**Error envelope versus MCP envelope.**
- Both use `ErrorResponse {error, code, details?}`. `TransitionError` adds `hint` and `valid_transitions` in both.
- There is no structural divergence between HTTP and MCP. The divergence is semantic (F4, F8, F11): status and code pairings are inconsistent, and nothing signals retryability.

**Pagination and bounding.** F5, F7, F16.

**Idempotency and partial batch.**
- Scan ingest: finding upsert is idempotent. Counters, the cascade and the partial-drop signal are not (F2, F6).
- Warpline: SEI-keyed and retry-safe, but not atomic (F12).
- Batch close and update: per-item `failed[]` is properly populated. Gate failures are reported per item, which is good.

**Fail-open versus fail-closed.**

| Seam | Behaviour | Verdict |
|---|---|---|
| Legis down, governed close | Closed (409 `CONFLICT`) | Correct, but not distinguishable from policy denial (F4), and it blocks the loop (F1) |
| Legis not configured | Open | By design (F21) |
| Legis non-affirming 2xx | Closed per issue | Correct |
| Loomweave down, drift probe | Open (freshness UNKNOWN, enrich-only) | Correct |
| Loomweave down, scan ingest, `registry_backend=loomweave` | Closed (503) | Opt-in coupling (F11) |
| BODY_TOO_LARGE single path | Open, the row is dropped | Correct intent, wrong signal (F2) |
| Federation token resolution error | Auth off, loud warning | Documented (F14) |

**Contract-drift risk.** F3, F15, F17, F20. The vendored-golden approach is the strongest part of the design. Two live wire seams have no golden.

**Production readiness.** F1, F7, F14, F18. There is a single worker, no body cap, no overall request deadline, a shallow health endpoint, and no rate or concurrency limits. All of that is acceptable for loopback deconfliction, except F1, which turns a peer outage into a full stall.

## Major-refresh API recommendations

Ordering follows the ADR-002 §3 test.

**A. Do first (governance, no code).**
1. Write the retroactive `loom` retirement ADR (F3). It should record the 3.0.0 hard break as a one-off exception and bind the successor to §8 (ADR plus 12 months' notice, an alias, and a `410` with `successor` details). Re-scope `filigree-bc80a673e1` from P4 doc wording to a refresh gate.
2. Decide whether the major bump carries a new generation. §3 says a bump does not imply one. If any of F4, F5 or F9 is in scope, it does.
3. Choose the successor name thematically (§4). This review does not pick one.

**B. Ship additively in weft now (no new generation).**
- F1: take the Legis and Loomweave network calls off the event loop, and thread `legis_known_down`.
- F2: populate `failed[]` and add `stats.findings_skipped` on scan results.
- F6: optional idempotency key, and distinct-finding counting.
- F7: body-size cap and an overall ingest deadline.
- F11: unify registry error rendering, with `details.cause_kind` and `Retry-After`.
- F14: boot-time env-vs-file token reconciliation, and an `auth.source` field on health.
- F15: fix contracts.md and add a docs-contract test.
- F18: `/api/weft/_capabilities`.
- F17: the closure-gate golden and the Warpline goldens (rebase the branch), with drift entries owned by the producing sibling.

**C. Successor generation (wire-breaking).**
- F4: split the `CONFLICT` and `PERMISSION` semantics, add `retryable` and `cause_kind` to the envelope, and make one mapper the source of truth for status.
- F5: mandatory bounded pagination on every list, with cursor tokens and documented ordering.
- F9: entity-associations, the reverse lookup and worklist ingest (F12) as first-class, project-scoped, gated resources.
- F10: drop the living surface, or generate the gate list from the router.
- F20: generate OpenAPI per generation.
- Keep `weft` frozen and running alongside it, with the policy in §8.

**D. Consider, owner decision.** F21: governed close with `LEGIS_URL` unset.

## Confidence Assessment

**Overall: Moderate-High.**

| Finding | Confidence | Basis |
|---|---|---|
| F1 | High | Code read at `issues.py:623, 1476, 91-130`, the CONTRACT-E header, `legis_client.py:52`, `dashboard.py:1272`. Not load-tested. |
| F2 | High | `adapters.py:355-360`, `db_files.py:1273-1301`. |
| F3 | High | The migration doc, ADR-002 §3/§8 and a live 404 on `/api/loom/issues` all agree. |
| F4 | High (mapping), Moderate (impact on sibling clients) | `types/api.py`, `dashboard.py`, `issues.py:79-88`. Sibling retry behaviour was not inspected. |
| F5 | High | Live `/ready` returned 68 KB with `has_more` false. The docstrings admit it. |
| F6, F7 | Moderate | Code read. No replay or large-body test run (that would be a mutating call). |
| F8, F10, F13, F16, F21 | Moderate | Static read. |
| F9, F12 | High (placement), Moderate (impact) | Routes confirmed absent from weft, and the codex branch confirmed to add no HTTP route. |
| F11 | Moderate | Static read of exception mapping. |
| F14 | Moderate | Observed on this host only. Cause is inferred from `mint_token_file` semantics and the boot call sites. |
| F15, F17, F18, F20 | High | Direct doc, code and test comparison. |

## Risk Assessment

- Highest blast radius: F1. A single sibling outage becomes a daemon-wide stall.
- Highest trust cost: F3. It affects every sibling's willingness to pin to a new freeze.
- Cross-product P0 candidates, which should not sit as P3/P4 (per the owner's rule): F3 (the retirement ADR), F17 (the Legis closure-gate and Warpline wire pins, because a drift here breaks governance gating of closes or the reverify loop silently), and F9/F12 if Warpline or Legis is already depending on the classic or MCP paths. No hidden P0 beyond these was found.
- Security-shaped items (F13, F14, F19, F21) were re-derived as functional or availability findings. None is rated above Medium.

## Information Gaps

- `docs/plans/2026-05-17-weft-uri-spec.md` was **not read** (the URI spec is currently reserved and not live per MIGRATION-3.0 §3h). URI consistency was assessed from route code only.
- Sibling repos (Wardline, Legis, Loomweave, Warpline) were not read. Their actual retry behaviour, which paths they call, and whether Warpline reaches the worklist tool through `/mcp` or stdio are unknown.
- Wardline MCP was down this session, so no `wardline scan` was run (read-only review, no code changed).
- The :8749 daemon was not up, so server-mode behaviour (`/api/p/{key}`, per-project tokens) was assessed from code only.
- The GET probes were limited to a handful of read routes. No load, replay, large-body or concurrency test was run.
- ADR-002 and the work-package plans were read for policy intent. Other ADRs (009, 012, 014) were sampled, not audited.
- The contract fixtures under `tests/fixtures/contracts/weft/` were listed, not diffed against live responses.

## Caveats & Required Follow-ups

- Line numbers refer to `release/3.3.0` @ `59053a3`. Re-verify before filing.
- **Loomweave index artifact:** the route list carries duplicate `api_loom_*` rows with `sei: null` at the same lines as the `api_weft_*` rows. This is stale-index residue from the rename, not a Filigree defect, but it means Loomweave's entity identity did not carry across the rename.
- The `codex/gs7-warpline-worklist` diff against main is dominated by unrelated deletions. Landing it must be a rebase and cherry-pick of the Warpline goldens, not a merge.
- Prior-art search used `issue_search` keywords only. A missing hit is not proof that no issue exists.
- Suggested next steps, in order:
  1. Owner decision on the generation question (A2).
  2. File issues for F1, F2, F17 (P1/P2) and the F3 ADR (P1, cross-product).
  3. Verify F14 on the live daemon (a read-only `filigree doctor` check).
  4. Run a replay test of one scan batch against a scratch project to confirm F6.

## Machine-readable summary

```json
{
  "review": "filigree-federation-http-api",
  "date": "2026-10-07",
  "repo_ref": "release/3.3.0@59053a3",
  "overall_confidence": "moderate-high",
  "refresh_headline": "Mint a successor generation only for wire-breaking fixes (F4,F5,F9); ship additive fixes in weft now (F1,F2,F6,F7,F11,F14,F15,F17,F18); write the retroactive loom-retirement ADR first (F3).",
  "findings": [
    {"id":"F1","tag":"NEW","severity":"high","area":"availability","breaking":false,"loc":"src/filigree/dashboard_routes/issues.py:623,1476,91-130; src/filigree/legis_client.py:52"},
    {"id":"F2","tag":"NEW","severity":"high","area":"partial-batch","breaking":false,"loc":"src/filigree/generations/weft/adapters.py:355-360; src/filigree/db_files.py:1273-1301"},
    {"id":"F3","tag":"NEW+STILL-OPEN","issue":"filigree-bc80a673e1","severity":"high","area":"versioning","breaking":false,"loc":"docs/MIGRATION-3.0.md:302-318; ADR-002 s3,s8"},
    {"id":"F4","tag":"NEW","severity":"high","area":"error-envelope","breaking":true,"loc":"src/filigree/types/api.py:457-486,861-891; issues.py:79-88"},
    {"id":"F5","tag":"NEW","severity":"high","area":"pagination","breaking":true,"loc":"src/filigree/dashboard_routes/issues.py:1077-1096"},
    {"id":"F6","tag":"NEW","severity":"medium","area":"idempotency","breaking":false,"loc":"src/filigree/db_files.py:1478,1939"},
    {"id":"F7","tag":"NEW","severity":"medium","area":"bounding","breaking":false,"loc":"src/filigree/dashboard_routes/common.py:112-122; files.py:51-53"},
    {"id":"F8","tag":"NEW","severity":"medium","area":"error-envelope","breaking":false,"loc":"src/filigree/dashboard.py:860-883"},
    {"id":"F9","tag":"NEW","severity":"medium","area":"resource-modeling","breaking":true,"loc":"src/filigree/dashboard.py:714-724; src/filigree/dashboard_routes/entities.py:122-190"},
    {"id":"F10","tag":"NEW","severity":"medium","area":"versioning","breaking":true,"loc":"src/filigree/dashboard_auth.py:32-33; docs/federation/contracts.md"},
    {"id":"F11","tag":"NEW","severity":"medium","area":"fail-open-closed","breaking":false,"loc":"src/filigree/dashboard_routes/files.py:243-248,655-663"},
    {"id":"F12","tag":"NEW","severity":"medium","area":"idempotency","breaking":false,"loc":"src/filigree/warpline_consumer.py; src/filigree/mcp_tools/federation.py"},
    {"id":"F13","tag":"NEW","severity":"medium","area":"auth-functional","breaking":false,"loc":"src/filigree/dashboard_auth.py:36-57"},
    {"id":"F14","tag":"NEW","severity":"medium","area":"availability","breaking":false,"loc":"src/filigree/federation_token.py; src/filigree/dashboard.py:141"},
    {"id":"F15","tag":"NEW","severity":"medium","area":"docs-drift","breaking":false,"loc":"docs/federation/contracts.md Authentication; ADR-018 amendment"},
    {"id":"F16","tag":"NEW","severity":"low","area":"pagination","breaking":false,"loc":"src/filigree/dashboard_routes/analytics.py:575-640"},
    {"id":"F17","tag":"NEW+STILL-OPEN","issue":"filigree-1544621b0a","severity":"low","area":"contract-drift","breaking":false,"loc":"tests/federation/_oracle.py:174-260"},
    {"id":"F18","tag":"STILL-OPEN","issue":"filigree-806dc04161","severity":"low","area":"health","breaking":false,"loc":"src/filigree/dashboard.py:1024-1036"},
    {"id":"F19","tag":"STILL-OPEN","issue":"filigree-81d3971467","severity":"low","area":"identity","breaking":false,"loc":"src/filigree/dashboard.py:156-175"},
    {"id":"F20","tag":"STILL-OPEN","issue":"filigree-1b7102e0b6","severity":"low","area":"docs","breaking":false,"loc":"src/filigree/dashboard.py"},
    {"id":"F21","tag":"NEW","severity":"low","area":"fail-open","breaking":false,"loc":"src/filigree/governance.py:398"}
  ],
  "regressed": [],
  "cross_product_p0_candidates": ["F3","F17","F9","F12"],
  "information_gaps": ["weft-uri-spec not read","sibling repos not read","wardline mcp down","daemon :8749 not up","no load/replay tests"]
}
```
