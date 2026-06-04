# Filigree Read-Only Codebase Audit - 2026-06-04

Scope: `/home/john/filigree`

This is a synthesis of five specialized read-only audits over the current tree:

- Architecture Critic: package boundaries, coupling, cohesion, design fragility.
- Systems Thinker: propagation paths, feedback loops, hidden side effects, failure modes.
- Python Engineer: Python implementation details, typing surfaces, exception handling, parser/scanner idioms.
- Quality Engineer: tests, CI structure, coverage gates, maintainability.
- Security Architect: trust boundaries, untrusted input, auth, parsing, MCP, Clarion, scanner/LLM-facing flows.

The request listed five roles and later referred to seven agents; this audit used the five listed roles. Each subagent prompt specified `enable_write_tools=false` and `enable_mcp_tools=false`, forbade write tools and MCP tools, and included the requested instruction not to use escaped double quotes in tool arguments. The audit did not run test, build, format, or migration commands; it used source inspection only. The only write performed for this task is this report artifact.

## Severity Summary

Critical: none confirmed.

High:

- F-001: Clarion bearer tokens can be sent to non-loopback origins when code bypasses or skips the capability probe.
- F-002: The living `/api/observations` alias is mounted but omitted from the bearer-token auth predicate.

Medium:

- F-003: Bundled scanner helpers cannot authenticate to the gated scanner callback endpoint.
- F-004: Dashboard inline JavaScript handlers mix JS-string escaping with HTML-attribute context.
- F-005: MCP schema validation ignores JSON Schema `required` properties.
- F-006: Concurrent dependency removal can record false undoable removal events.
- F-007: Scanner-created observations can detach from canonical file identity.
- F-008: HTTP observation listing mutates state by sweeping expired observations.
- F-009: The Python pytest job depends on Node but does not provision it.
- F-010: Live Clarion drift detection is manual-only in CI.

Low:

- F-011: Dashboard project-cache locking serializes potentially slow DB initialization.
- F-012: MCP tool modules import transport-server runtime globals.
- F-013: Scanner reporting paths have divergent CLI/MCP policy and normalization behavior.
- F-014: Coverage and XSS guardrails are present but brittle for the most security-sensitive surfaces.
- F-015: The opt-in bearer token protects only federation paths, which is easy to misread operationally.

## Critical Findings

No Critical findings were confirmed during this read-only audit.

## High Findings

### F-001 - Clarion bearer token origin guard is not enforced at construction or request time

Locations:

- [registry.py](/home/john/filigree/src/filigree/registry.py:359) lines 359-365: `_validate_clarion_token_origin()` rejects token-bearing requests to non-loopback origins.
- [registry.py](/home/john/filigree/src/filigree/registry.py:382) lines 382-397: `probe_clarion_capabilities()` calls the guard before the capability request.
- [registry.py](/home/john/filigree/src/filigree/registry.py:569) lines 569-594: `ClarionRegistry.__post_init__()` normalizes the base URL and creates an HTTP client, but does not call the guard.
- [registry.py](/home/john/filigree/src/filigree/registry.py:614) line 614, [registry.py](/home/john/filigree/src/filigree/registry.py:753) line 753, and [registry.py](/home/john/filigree/src/filigree/registry.py:931) line 931: request paths attach `_clarion_headers(auth_token=self.auth_token, ...)`.
- [registry.py](/home/john/filigree/src/filigree/registry.py:594) line 594: the persistent client follows redirects.

Problem:

The code has a clear security invariant: Clarion bearer tokens should only be sent to loopback Clarion origins by default. That invariant is enforced only by the capability probe path. Direct `ClarionRegistry` construction, future probe-skipping flows, and any request path reached after construction can still attach `Authorization: Bearer ...` to a normalized `http(s)` base URL that is not loopback. Because the persistent client follows redirects, a token-bearing request also needs redirect-origin handling.

Impact:

A token configured for local Clarion can be sent to an arbitrary configured remote origin, or potentially to a redirect target, if a code path constructs or uses `ClarionRegistry` without a successful guarded probe. This is a credential-exfiltration class defect.

Remediation:

1. Enforce `_validate_clarion_token_origin()` in `ClarionRegistry.__post_init__()` after `normalize_clarion_base_url()` and before creating the client.
2. Re-check the concrete request URL immediately before each request that attaches auth headers.
3. Disable redirects for token-bearing Clarion requests, or allow redirects only when every hop remains on an approved loopback origin.
4. Add regression tests for:
   - direct `ClarionRegistry("https://example.invalid", auth_token="secret")`;
   - a `FiligreeDB` construction path where the capability probe is skipped;
   - redirect behavior from loopback to non-loopback.

### F-002 - Living `/api/observations` alias bypasses bearer-token auth

Locations:

- [dashboard_auth.py](/home/john/filigree/src/filigree/dashboard_auth.py:28) lines 28-29: living/classic federation aliases include `scan-results` but not `observations`.
- [dashboard_auth.py](/home/john/filigree/src/filigree/dashboard_auth.py:32) lines 32-51: `is_loom_scoped_path()` gates only `/api/loom/*`, configured aliases, and `/mcp`.
- [dashboard_routes/analytics.py](/home/john/filigree/src/filigree/dashboard_routes/analytics.py:692) lines 692-695: `POST /api/loom/observations` is the protected loom observation write path.
- [dashboard_routes/analytics.py](/home/john/filigree/src/filigree/dashboard_routes/analytics.py:700) lines 700-723: `POST /api/observations` is mounted as the living equivalent.
- [dashboard.py](/home/john/filigree/src/filigree/dashboard.py:515) lines 515-516: both files and analytics living-surface routers are mounted.
- [tests/api/test_loom_auth.py](/home/john/filigree/tests/api/test_loom_auth.py:63) lines 63-77: the alias drift guard imports only `dashboard_routes.files.create_living_surface_router()`, so analytics aliases are not checked.

Problem:

When `FILIGREE_API_TOKEN` is configured, the token middleware gates `/api/loom/observations`, but the living alias `/api/observations` is not included in `LIVING_FEDERATION_ALIASES`. The application mounts the analytics living router, and its `POST /observations` handler delegates to the same observation-creation logic.

Impact:

A deployment that believes federation writes are protected by `FILIGREE_API_TOKEN` still exposes a write-capable living alias for observation ingestion without the token. This weakens the explicit auth boundary and lets unauthenticated callers add observation records on the living surface.

Remediation:

1. Add `observations` to the protected living federation aliases, or derive protected aliases from all mounted living routers rather than maintaining a hand-written set.
2. Extend the alias drift test to inspect both `files.create_living_surface_router()` and `analytics.create_living_surface_router()`.
3. Add integration tests proving unauthenticated `POST /api/observations` returns 401 when `FILIGREE_API_TOKEN` is set, and authenticated calls succeed.

## Medium Findings

### F-003 - Bundled scanner callbacks do not send bearer tokens to the gated scan-results endpoint

Locations:

- [bundled_scanners.py](/home/john/filigree/src/filigree/bundled_scanners.py:38) lines 38-80: bundled Codex and Claude scanners are launched with `--api-url`.
- [scan_utils.py](/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:346) lines 346-387: `post_to_api()` posts to `/api/scan-results` with only `Content-Type`.
- [dashboard_auth.py](/home/john/filigree/src/filigree/dashboard_auth.py:28) lines 28-51: `/api/scan-results` is one of the gated federation aliases.
- [tests/util/test_scan_utils.py](/home/john/filigree/tests/util/test_scan_utils.py:1118) lines 1118-1190: callback tests assert URL and content-type behavior, but do not cover auth headers.

Problem:

The scanner callback endpoint is intentionally gated when `FILIGREE_API_TOKEN` is configured, but the packaged scanner posting helper has no token parameter and sends no `Authorization` header.

Impact:

Secured deployments can silently break managed scanner ingestion: scans may run, then fail to report findings with 401 responses. That is a security/quality feedback-loop failure: the system becomes more secure at the HTTP boundary while losing scanner telemetry.

Remediation:

1. Add scanner-side token support, preferably explicit and testable: `--api-token-env`, `--api-token`, or a propagated environment convention documented next to `FILIGREE_API_TOKEN`.
2. Have `post_to_api()` attach `Authorization: Bearer <token>` when configured.
3. Update bundled scanner command templates to pass or expose the token source.
4. Add tests for both tokenless and token-bearing scanner callback requests.

### F-004 - Inline dashboard handlers use JS-string escaping in HTML-attribute context

Locations:

- [ui.js](/home/john/filigree/src/filigree/static/js/ui.js:46) lines 46-64: `escHtml()` and `escJsSingle()` are separate context-specific helpers.
- [ui.js](/home/john/filigree/src/filigree/static/js/ui.js:163) lines 163-170: `issueIdChip()` inserts `escJsSingle(id)` into double-quoted inline event attributes.
- [detail.js](/home/john/filigree/src/filigree/static/js/views/detail.js:112) lines 112-119: dependency UI builds inline handlers with JS-escaped issue IDs.
- [kanban.js](/home/john/filigree/src/filigree/static/js/views/kanban.js:388) lines 388-418: cards mix `data-id="${escHtml(issue.id)}"` with inline `onclick="openDetail('${safeIssueId}')"`.
- [types/core.py](/home/john/filigree/src/filigree/types/core.py:20) lines 20-25: `make_issue_id()` enforces non-empty strings, but not an ID grammar.
- [core.py](/home/john/filigree/src/filigree/core.py:568) lines 568-571 and [core.py](/home/john/filigree/src/filigree/core.py:1494) line 1494: issue IDs are generated from a configurable prefix without a conservative character grammar.
- [db_meta.py](/home/john/filigree/src/filigree/db_meta.py:434) lines 434-441: imported IDs are prefix-checked but not normalized to a safe DOM/attribute grammar.

Problem:

`escJsSingle()` escapes single quotes, backslashes, line breaks, and angle brackets for a JavaScript single-quoted string. It does not escape every character relevant to a surrounding double-quoted HTML attribute. Several templates place `safeId` directly inside inline `onclick`/`onkeydown` attributes. If an issue ID or configured/imported prefix can contain a double quote or other attribute-breaking character, the handler attribute can be malformed and potentially injectable.

Impact:

This is a stored local-dashboard XSS risk reachable through issue IDs or imported/project-prefix-controlled IDs. The dashboard is local-first, but this still crosses a trust boundary because project data and imported artifacts can be untrusted.

Remediation:

1. Prefer removing inline event handlers. Render inert DOM with escaped text/data attributes, then bind listeners with JavaScript.
2. If inline handlers remain, compose escaping for both contexts: HTML-attribute escaping outside plus JS-string escaping inside.
3. Add a conservative issue ID and prefix grammar for generated/imported IDs.
4. Add DOM/runtime tests with IDs containing `"`, `'`, `<`, `>`, `&`, and whitespace.

### F-005 - MCP schema validation ignores JSON Schema `required` properties

Locations:

- [mcp_server.py](/home/john/filigree/src/filigree/mcp_server.py:537) lines 537-592: `_validate_schema_value()` checks type, bounds, properties, and `additionalProperties`, but does not enforce `required`.
- [mcp_server.py](/home/john/filigree/src/filigree/mcp_server.py:595) lines 595-609: `_schema_validation_error()` delegates to the incomplete validator.
- [mcp_server.py](/home/john/filigree/src/filigree/mcp_server.py:905) lines 905-926: handler dispatch returns schema errors, otherwise lets handler exceptions propagate.
- [mcp_tools/scanners.py](/home/john/filigree/src/filigree/mcp_tools/scanners.py:437) lines 437-447: `preview_scan` declares required inputs in its schema.
- [mcp_tools/scanners.py](/home/john/filigree/src/filigree/mcp_tools/scanners.py:1481) lines 1481-1490: `_handle_preview_scan()` indexes `args["scanner"]` and `args["file_path"]`.
- [types/inputs.py](/home/john/filigree/src/filigree/types/inputs.py:755) lines 755-758: `PreviewScanArgs` is a required-field `TypedDict`.

Problem:

Schema metadata says fields are required, and handler code assumes they exist, but the generic MCP validator never checks the `required` array. Missing fields therefore pass schema validation and fail later as raw handler exceptions.

Impact:

Clients receive less precise errors, handlers can leak implementation exceptions, and MCP behavior diverges from the advertised tool contract. This also makes future handlers fragile if they rely on required-schema enforcement.

Remediation:

1. In `_validate_schema_value()`, when `value` is an object and `schema["required"]` is a list of strings, reject any missing required key before validating present properties.
2. Add tests for at least `preview_scan` with missing `scanner`, missing `file_path`, and both present.
3. Consider defensive `.get()` checks in handlers where missing values could cause confusing downstream errors.

### F-006 - Concurrent dependency removal can record false undoable removal events

Locations:

- [db_planning.py](/home/john/filigree/src/filigree/db_planning.py:219) lines 219-224: `add_dependency()` begins an immediate transaction.
- [db_planning.py](/home/john/filigree/src/filigree/db_planning.py:285) lines 285-304: `remove_dependency()` reads, deletes, records an event, and commits without `_begin_immediate()`.
- [db_events.py](/home/john/filigree/src/filigree/db_events.py:443) lines 443-474: undo of `dependency_removed` re-inserts with `INSERT OR IGNORE`.

Problem:

`remove_dependency()` performs a read-then-delete-then-event sequence without taking the same early write lock used by `add_dependency()`. If two connections race, both can observe the dependency before either commit. One deletes the row; the other can still proceed to record a removal event after its delete affects zero rows.

Impact:

The event log can claim a dependency was removed by a command that did not actually remove it. Undoing that false event can restore a dependency edge that the second caller never owned. This corrupts the planning dependency history under concurrent use.

Remediation:

1. Call `_begin_immediate(self.conn, "remove_dependency")` before existence checks and the select/delete/event sequence.
2. Check `cursor.rowcount` after the delete and record `dependency_removed` only when exactly one row was deleted.
3. Add a two-connection concurrency regression test that races two removals and asserts only one event is recorded.

### F-007 - Scanner-created observations can detach from canonical file identity

Locations:

- [db_files.py](/home/john/filigree/src/filigree/db_files.py:953) lines 953-1052: `_upsert_file_record()` stores the registry-normalized canonical path and file ID.
- [db_files.py](/home/john/filigree/src/filigree/db_files.py:1232) lines 1232-1255: scanner-created observations pass `file_path=path` rather than the canonical stored path or file ID.
- [db_observations.py](/home/john/filigree/src/filigree/db_observations.py:308) lines 308-318: observation creation links `file_id` by exact `file_records.path = file_path` lookup.

Problem:

File ingestion resolves and stores a canonical registry path. Observation creation then uses the original scanner path and relies on exact path lookup to attach a `file_id`. If registry canonicalization changes the path, the observation can be created without a `file_id` even though the finding has one.

Impact:

Scanner findings and their promoted observations can diverge in file identity. Filters, file-centric timelines, and downstream triage can miss related observations.

Remediation:

1. Pass the known canonical stored path or trusted `file_id` into `create_observation()` for scanner-created observations.
2. Prefer linking by `file_id` where the caller already has it, with `file_path` as display metadata.
3. Add a regression test using a registry backend that canonicalizes `./path.py` to `path.py` and asserts the observation links to the same file record as the finding.

### F-008 - HTTP observation listing mutates state by sweeping expired observations

Locations:

- [db_observations.py](/home/john/filigree/src/filigree/db_observations.py:448) lines 448-466: `list_observations()` defaults `sweep=True`.
- [db_observations.py](/home/john/filigree/src/filigree/db_observations.py:536) line 536: listing can call `_sweep_expired_observations()`.
- [dashboard_routes/analytics.py](/home/john/filigree/src/filigree/dashboard_routes/analytics.py:665) lines 665-690: HTTP `GET /api/loom/observations` calls `db.list_observations(...)` without `sweep=False`.
- [mcp_tools/observations.py](/home/john/filigree/src/filigree/mcp_tools/observations.py:403) lines 403-424: MCP list observations explicitly passes `sweep=False`.

Problem:

The MCP surface treats list observations as read-only, but the HTTP loom list route can delete/transition expired observations as a side effect of a GET request.

Impact:

Read requests can mutate audit state, produce unexpected event churn, and contend with writers. It also creates behavior drift between MCP and HTTP list surfaces for the same conceptual operation.

Remediation:

1. Pass `sweep=False` in `api_loom_list_observations()`.
2. Move expiration sweeping to an explicit maintenance/write endpoint or a clearly named background maintenance action.
3. Add an HTTP regression test mirroring the MCP read-only observation-list behavior.

### F-009 - Python pytest CI depends on Node but does not provision it in the test job

Locations:

- [ci.yml](/home/john/filigree/.github/workflows/ci.yml:47) lines 47-53: the separate frontend job provisions Node.
- [ci.yml](/home/john/filigree/.github/workflows/ci.yml:58) lines 58-68: the Python `test` job runs `uv run pytest` but does not set up Node.
- [test_dashboard_activity_state.py](/home/john/filigree/tests/static/test_dashboard_activity_state.py:18) lines 18-26: static pytest tests shell out to `node --input-type=module`.
- [test_dashboard_files_overview.py](/home/john/filigree/tests/static/test_dashboard_files_overview.py:13) lines 13-21: another pytest test uses Node.

Problem:

The pytest suite contains Node-backed tests, but the Python test job relies on whatever Node happens to exist on the runner image. The frontend job installs Node, but that setup does not carry into the Python test job.

Impact:

CI can become image-dependent and fail unexpectedly when runner images change. Locally, contributors may also see pytest failures that are not explained by the Python dependency setup.

Remediation:

1. Add `actions/setup-node` to the Python test job, or move the Node-backed static tests into the frontend job.
2. If keeping them in pytest, document Node as a test prerequisite in development docs.
3. Add a small guard that reports a clear skip/error if Node is unavailable, depending on whether these tests are required in CI.

### F-010 - Live Clarion drift detection is manual-only in CI

Locations:

- [ci.yml](/home/john/filigree/.github/workflows/ci.yml:96) lines 96-112: the live Clarion job runs only for manual workflow dispatch with `require_live_clarion`.
- [test_clarion_phase_d_e2e.py](/home/john/filigree/tests/integration/test_clarion_phase_d_e2e.py:57) lines 57-67: the live test skips unless `FILIGREE_REQUIRE_LIVE_CLARION=1`.
- [test_sei_oracle_live_clarion.py](/home/john/filigree/tests/federation/test_sei_oracle_live_clarion.py:52) lines 52-54: the live SEI oracle test also skips unless required.

Problem:

Clarion is a cross-product trust boundary and registry source, but the live integration lane is opt-in manual-only. Normal PR CI exercises conformance and mocked paths, not a live Clarion deployment.

Impact:

Wire-level Clarion drift can merge if maintainers do not remember to run the manual job. This is not a direct code vulnerability, but it is a release-governance gap around a security-sensitive integration.

Remediation:

1. Add a scheduled live Clarion CI job against a pinned staging Clarion instance.
2. Keep the manual `require_live_clarion` option, but require scheduled success before release branches or tags.
3. Emit a release checklist warning if the live job has not passed since the relevant Filigree/Clarion contract changes.

## Low Findings

### F-011 - Dashboard project-cache locking serializes slow DB initialization

Locations:

- [dashboard.py](/home/john/filigree/src/filigree/dashboard.py:209) lines 209-233: `ProjectStore` owns a single process-wide lock.
- [dashboard.py](/home/john/filigree/src/filigree/dashboard.py:294) lines 294-317: `get_db()` performs project lookup/cache work while holding that lock.

Problem:

The project cache uses one lock for all projects. If DB open, migration, registry initialization, or eviction cleanup becomes slow under the lock, unrelated project requests can queue behind it.

Impact:

This is an availability and latency risk for multi-project dashboard/server-mode use. It is not currently shown to be exploitable, but it is a fragile concurrency boundary.

Remediation:

1. Split the critical section so slow DB opens occur outside the global map lock.
2. Use per-project locks or a placeholder/future entry to prevent duplicate opens while allowing other projects to proceed.
3. Add a concurrency test that proves a slow open for project A does not block a fast cached read for project B.

### F-012 - MCP tool modules import transport-server runtime globals

Locations:

- [mcp_server.py](/home/john/filigree/src/filigree/mcp_server.py:105) lines 105-123: server runtime globals and request-local DB helpers live in the transport module.
- [mcp_tools/workflow.py](/home/john/filigree/src/filigree/mcp_tools/workflow.py:222) lines 222-249: workflow tools import `_get_db`, `_all_tools`, and other runtime globals from `mcp_server`.
- [mcp_server.py](/home/john/filigree/src/filigree/mcp_server.py:916) lines 916-938: the transport dispatch path serializes tool execution around server-held locks.

Problem:

Domain tool handlers reach back into the transport module for active DB state, tool registries, and status helpers. That couples tool logic to the server runtime and makes alternate transports or unit-level reuse harder.

Impact:

The coupling is architectural rather than an immediate bug. It increases the chance that transport changes alter tool behavior, and it makes it harder to test handlers in isolation.

Remediation:

1. Introduce a small MCP runtime/context object that exposes `db`, `filigree_dir`, safe-path helpers, and tool metadata.
2. Inject that context into handlers instead of importing `mcp_server` globals.
3. Keep transport-specific concerns, locks, and lifespan state in `mcp_server.py`.

### F-013 - Scanner reporting paths have divergent CLI/MCP policy and normalization behavior

Locations:

- [mcp_tools/scanners.py](/home/john/filigree/src/filigree/mcp_tools/scanners.py:737) lines 737-834: MCP `report_finding` has its own parse, registry, warning, and observation-linking flow.
- [cli_commands/scanners.py](/home/john/filigree/src/filigree/cli_commands/scanners.py:1068) lines 1068-1256: CLI `report-finding` mirrors but does not share all behavior with MCP.
- [db_files.py](/home/john/filigree/src/filigree/db_files.py:1232) lines 1232-1255: observation creation is ultimately delegated through DB logic with path/file identity choices.

Problem:

The CLI and MCP scanner reporting surfaces duplicate policy and normalization steps around findings, registry resolution, warning propagation, and observation linking.

Impact:

Behavior can drift by surface. A scanner finding reported through MCP can receive different validation, warnings, or observation linkage than a finding reported through CLI.

Remediation:

1. Extract shared scanner-report orchestration into a service function that accepts a narrow context and returns a structured result.
2. Have CLI and MCP adapters handle only argument parsing and response formatting.
3. Add parity tests that feed the same finding through both adapters and compare the normalized DB record and observation linkage.

### F-014 - Coverage and XSS guardrails are present but brittle for sensitive surfaces

Locations:

- [check_coverage_floors.py](/home/john/filigree/scripts/check_coverage_floors.py:15) lines 15-29: security-sensitive modules have explicit but low file-specific floors in some cases.
- [ci.yml](/home/john/filigree/.github/workflows/ci.yml:67) lines 67-68: CI runs total coverage and the floor script.
- [test_xss_guards.py](/home/john/filigree/tests/static/test_xss_guards.py:40) lines 40-43: XSS tests assert source substrings rather than executing DOM behavior.

Problem:

The quality gates are useful, but some floors for auth/MCP/security surfaces allow substantial untested space, and XSS tests check source text instead of runtime escaping behavior.

Impact:

Security regressions can pass if they do not move the exact source strings or if they occur in uncovered branches. This amplifies F-004 rather than standing alone as a direct vulnerability.

Remediation:

1. Raise floors for auth, registry, MCP validation, and scanner auth surfaces after adding targeted tests.
2. Replace source-string XSS checks with runtime DOM tests that render representative data and assert inert attributes/listeners.
3. Add specific regression tests for the issues in F-001, F-002, F-004, and F-005.

### F-015 - `FILIGREE_API_TOKEN` protects only federation paths, which is easy to misread

Locations:

- [dashboard_auth.py](/home/john/filigree/src/filigree/dashboard_auth.py:1) lines 1-13: module docstring says the token gates the loom federation surface while leaving the classic surface and dashboard UI open.
- [dashboard_auth.py](/home/john/filigree/src/filigree/dashboard_auth.py:94) lines 94-103: middleware bypasses non-loom paths.
- [dashboard.py](/home/john/filigree/src/filigree/dashboard.py:646) lines 646-662: `FILIGREE_API_TOKEN` installs the federation auth middleware when non-empty.

Problem:

The code intentionally leaves classic dashboard routes open, but the environment variable name is broad enough that operators can infer it protects the whole API.

Impact:

This is an operator-confusion risk, especially when the same process exposes local dashboard, classic API, living aliases, MCP, and federation endpoints. It is lower severity because the source docstring is explicit and ADR-012 treats loopback as the default trust boundary.

Remediation:

1. Rename or supplement the variable with a more specific alias such as `FILIGREE_FEDERATION_API_TOKEN`.
2. Surface auth scope in `/api/health` or startup logs: federation auth enabled, dashboard/classic auth not enabled.
3. Document route classes in one operator-facing table: unauthenticated local/dashboard, gated federation, gated MCP.

## Cross-Cutting Remediation Plan

Recommended order:

1. Fix auth-boundary defects first: F-001 and F-002.
2. Add scanner token propagation: F-003.
3. Remove inline event handlers or harden escaping: F-004.
4. Implement MCP `required` validation: F-005.
5. Repair state-history races and hidden read-side mutations: F-006 and F-008.
6. Normalize scanner observation identity: F-007.
7. Harden CI and governance guardrails: F-009, F-010, F-014.
8. Address structural risks as follow-up refactors: F-011, F-012, F-013, F-015.

Suggested targeted tests before closing the high/medium items:

- Unit tests for `ClarionRegistry` construction with token-bearing non-loopback URLs and redirects.
- API tests for `POST /api/observations` with and without `FILIGREE_API_TOKEN`.
- Scanner helper tests asserting `Authorization` is attached when configured.
- Dashboard runtime DOM tests for issue IDs containing quote and angle-bracket characters.
- MCP schema tests for missing required fields.
- SQLite two-connection dependency-removal race tests.
- HTTP observation-list tests proving GET does not sweep expired observations.
- CI job update proving Node-backed pytest tests run under an explicitly installed Node version.

## Audit Limitations

This was a strict read-only source audit. No tests, type checks, build commands, dashboard server, MCP server, scanner commands, or live Clarion calls were executed. The findings above are based on source review plus local line-range verification against the current tree.
