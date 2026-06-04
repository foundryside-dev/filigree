# Filigree Read-Only Codebase Audit

Date: 2026-06-04

Scope: `/home/john/filigree`

Mode: Strict read-only review. No tests, services, migrations, or mutating tracker actions were run. The only write made by the coordinator is this requested markdown artifact.

Subagents: seven specialized review agents were dispatched using read-only prompts that explicitly set `enable_write_tools=false` and `enable_mcp_tools=false` as operating constraints, disabled MCP usage, and prohibited file edits. The subagent API available in this session did not expose literal boolean parameters with those names, so those constraints were enforced in each agent prompt. Each returned report stated that no MCP tools or writes were used.

Subagent roster:

- Architecture Critic: `019e8f84-015c-7b80-bdc2-0ceafa0e3bbb`
- Systems Thinker: `019e8f84-0202-7440-9fbb-ec962967c108`
- Python Engineer: `019e8f84-029a-7572-b42f-e9733063e542`
- Quality Engineer: `019e8f84-0319-7382-99d5-03018b3e804e`
- Security Architect: `019e8f84-0432-7043-9f9a-d18b0ba4ca0e`
- Static Tools Analyst: `019e8f84-05eb-7e20-a90d-b37c9eb1a2db`
- MCP & CLI Specialist: `019e8f89-2eec-7932-b380-197f9eba4afb`

Note on the requested static-analysis scope: `scanner/ast_primitives.py`, `scanner/rules/`, and rules `PY-WL-101` through `PY-WL-111` were not present in this repository. The Static Tools Analyst therefore audited the scanner/finding ingestion and static-analysis-adjacent surfaces that do exist.

## Executive Summary

No confirmed Critical findings were identified.

The highest-risk issues are concentrated around trust boundaries and state transitions:

- Clarion bearer tokens can be sent to a project-configured arbitrary URL.
- A token-protected scan-result ingestion path has an unprotected classic alias.
- MCP context resource reads bypass degraded-startup error gates.
- Some actor-bearing MCP/CLI paths bypass shared actor sanitization.
- Scanner state can be marked completed despite failures, or remain pending forever.
- Planning dependency writes can race into cycles, and critical-path reporting hides existing cycles.
- The dashboard app factory relies on mutable module globals, creating cross-project state bleed risk.

## Critical

No confirmed Critical findings.

## High

### H-01: Project-Controlled Clarion Base URL Can Exfiltrate Bearer Tokens

Locations:

- [/home/john/filigree/src/filigree/registry.py:534-544](/home/john/filigree/src/filigree/registry.py:534)
- [/home/john/filigree/src/filigree/core.py:1015-1033](/home/john/filigree/src/filigree/core.py:1015)
- [/home/john/filigree/src/filigree/core.py:1081-1095](/home/john/filigree/src/filigree/core.py:1081)
- [/home/john/filigree/src/filigree/registry.py:368-371](/home/john/filigree/src/filigree/registry.py:368)
- [/home/john/filigree/src/filigree/registry.py:382-395](/home/john/filigree/src/filigree/registry.py:382)

Evidence: `normalize_clarion_base_url` accepts any `http` or `https` URL with a host. `FiligreeDB` reads the project config, resolves a Clarion token from an environment variable, builds `ClarionRegistry`, and immediately performs a capability probe. `_clarion_headers` attaches `Authorization: Bearer ...` to that request.

Impact: An untrusted repository can configure `registry_backend=clarion` and point `clarion.base_url` at an attacker-controlled host. If the user has `CLARION_LOOM_TOKEN` or the configured token env var set, Filigree sends the token to that host. The same path is also an SSRF surface to arbitrary HTTP(S) origins.

Remediation:

- Treat Clarion origin as trusted user-local configuration, not repository-controlled data.
- Default to loopback-only Clarion URLs unless a non-repo allowlist explicitly permits another origin.
- Require HTTPS for non-loopback hosts.
- Disable redirects for token-bearing probes, or revalidate every redirect target before forwarding authorization headers.
- Bind tokens to expected origins and add regression tests for attacker URL, redirect URL, and loopback URL cases.

### H-02: Token-Protected Scan Ingestion Is Bypassable Through Classic V1 Route

Locations:

- [/home/john/filigree/src/filigree/dashboard_auth.py:28-46](/home/john/filigree/src/filigree/dashboard_auth.py:28)
- [/home/john/filigree/src/filigree/dashboard_auth.py:91-96](/home/john/filigree/src/filigree/dashboard_auth.py:91)
- [/home/john/filigree/src/filigree/dashboard.py:584-616](/home/john/filigree/src/filigree/dashboard.py:584)
- [/home/john/filigree/src/filigree/dashboard_routes/files.py:485-508](/home/john/filigree/src/filigree/dashboard_routes/files.py:485)
- [/home/john/filigree/src/filigree/dashboard_routes/files.py:552-577](/home/john/filigree/src/filigree/dashboard_routes/files.py:552)

Evidence: `FILIGREE_API_TOKEN` middleware only gates paths classified by `is_loom_scoped_path`, including `/api/loom/*` and `/api/scan-results`. Classic `POST /api/v1/scan-results` reaches the same ingestion behavior but is outside that protected path set.

Impact: A local untrusted client can bypass configured bearer-token protection by posting scan results to `/api/v1/scan-results`, injecting findings and optionally creating observations.

Remediation:

- Gate scan ingestion by semantic capability rather than route generation.
- Require the same token on `/api/v1/scan-results`, `/api/scan-results`, and `/api/loom/...` scan-result aliases.
- Add auth parity tests that enumerate every scan-result route when `FILIGREE_API_TOKEN` is set.

### H-03: MCP Context Resource Can Crash Outside Structured Error Handling

Locations:

- [/home/john/filigree/src/filigree/mcp_server.py:504-521](/home/john/filigree/src/filigree/mcp_server.py:504)
- [/home/john/filigree/src/filigree/mcp_server.py:705-725](/home/john/filigree/src/filigree/mcp_server.py:705)

Evidence: `list_resources` always advertises `filigree://context`; `read_context` directly calls `generate_summary(_get_db())`. `call_tool` has explicit gates for schema mismatch, registry startup error, and DB open error, but the resource path does not.

Impact: MCP clients following the prompt to read context first can receive an unstructured protocol exception during degraded startup, while tools return structured `ErrorResponse` envelopes.

Remediation:

- Gate `read_context` through the same degraded-startup checks as `call_tool`.
- Either hide the resource when unavailable or return a stable diagnostic resource with the same error code semantics as tools.
- Add resource-read tests for schema mismatch, registry mismatch, and uninitialized database startup.

### H-04: MCP `release_my_claims` Bypasses Shared Actor Sanitization

Locations:

- [/home/john/filigree/src/filigree/mcp_tools/issues.py:1221-1256](/home/john/filigree/src/filigree/mcp_tools/issues.py:1221)
- [/home/john/filigree/src/filigree/db_issues.py:1691-1740](/home/john/filigree/src/filigree/db_issues.py:1691)
- [/home/john/filigree/src/filigree/validation.py:14-33](/home/john/filigree/src/filigree/validation.py:14)
- [/home/john/filigree/src/filigree/cli_commands/issues.py:1002-1046](/home/john/filigree/src/filigree/cli_commands/issues.py:1002)

Evidence: The MCP handler only requires `actor` to be a non-empty string after `.strip()`. `release_my_claims` repeats that minimal check. Shared `sanitize_actor` rejects control/format characters and actors over 128 characters.

Impact: MCP can accept newline/control-character or overlong actor identities that other issue tools reject. Because this operation releases claims and records audit events, invalid actors can corrupt attribution and line-oriented audit/log consumers.

Remediation:

- Replace the local strip-only actor handling with `_validate_actor(raw_actor)` in `_handle_release_my_claims`.
- Enforce `sanitize_actor` inside `db_issues.release_my_claims` as defense in depth.
- Add MCP tests for blank, control-character, format-character, and overlong actor values.

### H-05: Scanner Failures Can Still Mark Scan Runs Completed

Locations:

- [/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:640-686](/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:640)
- [/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:860-900](/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:860)

Evidence: `_analyse_files` records executor/report/API failures but still sends a final empty `complete_scan_run=True` POST whenever `scan_run_id` is set. `run_scanner_pipeline` returns a nonzero exit code only after that final completion request.

Impact: A reserved scan run can show `completed` even when the scanner failed, missed reports, or dropped finding POSTs. Status polling and cooldown logic then operate on a false success.

Remediation:

- Send completion only when executor failures and API failures are zero.
- Add a failure-status callback/API path for scanner-side failures.
- Add tests for scanner executor failure, failed finding POST, and report parse failure to ensure the scan run does not become `completed`.

### H-06: Stale Pending Scan Reservations Can Permanently Block Future Scans

Locations:

- [/home/john/filigree/src/filigree/db_scans.py:79-135](/home/john/filigree/src/filigree/db_scans.py:79)
- [/home/john/filigree/src/filigree/db_scans.py:228-301](/home/john/filigree/src/filigree/db_scans.py:228)
- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:839-908](/home/john/filigree/src/filigree/mcp_tools/scanners.py:839)
- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:1104-1214](/home/john/filigree/src/filigree/mcp_tools/scanners.py:1104)

Evidence: `reserve_scan_run` inserts `pending` rows with `pid=NULL`. `check_scan_cooldown` treats `pending` and `running` rows as singleton locks regardless of age. `get_scan_status` auto-fails only `running` rows with a PID.

Impact: A crash after reservation but before spawn/backfill leaves an unreconciled pending row that blocks future scans for the scanner/file pair.

Remediation:

- Add a TTL/reconciliation path for stale `pending` reservations.
- Surface stale reservations in status output and provide a repair command.
- Add a regression test for crash-after-reserve or spawn-before-backfill failure.

### H-07: Concurrent Dependency Writes Can Create Planning Cycles

Locations:

- [/home/john/filigree/src/filigree/db_planning.py:219-280](/home/john/filigree/src/filigree/db_planning.py:219)
- [/home/john/filigree/src/filigree/db_planning.py:680-720](/home/john/filigree/src/filigree/db_planning.py:680)
- [/home/john/filigree/src/filigree/db_planning.py:742-780](/home/john/filigree/src/filigree/db_planning.py:742)

Evidence: Dependency add/retarget paths check for cycles before insert or delete-plus-insert. The check and write are not serialized in one immediate transaction.

Impact: Two concurrent writers can both pass `_would_create_cycle` and insert opposing edges. The resulting cycle can deadlock readiness and corrupt planning invariants.

Remediation:

- Wrap cycle check plus mutation in a retrying `BEGIN IMMEDIATE` transaction.
- Recheck the graph under the write lock immediately before insert.
- Add concurrent tests for opposing A->B and B->A insertions.
- Consider a recursive trigger or post-write SCC integrity check as defense in depth.

### H-08: Critical Path Reporting Silently Hides Cyclic Planning Graphs

Locations:

- [/home/john/filigree/src/filigree/db_planning.py:415-483](/home/john/filigree/src/filigree/db_planning.py:415)

Evidence: `get_critical_path` uses a Kahn-style topological traversal but never verifies that every node was processed. If the graph is cyclic, the queue can empty while cyclic nodes remain unprocessed, and the function can return an empty or misleading path.

Impact: Once a cycle exists, the command that should help operators diagnose the planning graph can instead hide the cycle.

Remediation:

- Track processed node count and compare it to graph node count.
- If unprocessed nodes remain, return or raise a typed integrity error with the cycle/SCC members.
- Add tests for all-cyclic and mixed cyclic/acyclic graphs.

### H-09: Dashboard App Factory Relies On Mutable Module-Global Runtime State

Locations:

- [/home/john/filigree/src/filigree/dashboard.py:73-100](/home/john/filigree/src/filigree/dashboard.py:73)
- [/home/john/filigree/src/filigree/dashboard.py:389-425](/home/john/filigree/src/filigree/dashboard.py:389)
- [/home/john/filigree/src/filigree/dashboard.py:809-823](/home/john/filigree/src/filigree/dashboard.py:809)

Evidence: `_db`, `_config`, `_allow_http_force_close`, `_current_project_key`, and `_project_store` are module globals. `_get_db()` changes behavior based on these globals, and `main()` explicitly clears them because a later run can otherwise serve the wrong database.

Impact: Multiple dashboard apps in one process, tests, or embedded deployments can bleed state across projects or modes. This can route requests to the wrong tracker database.

Remediation:

- Introduce a `DashboardState` object stored on `app.state`.
- Pass database/project-store dependencies into router factories instead of resolving module globals.
- Remove module-global request resolution.
- Add tests creating two apps in one process and proving request isolation.

## Medium

### M-01: Dashboard-Mounted MCP HTTP Endpoint Is Not Protected By Bearer Token

Locations:

- [/home/john/filigree/src/filigree/dashboard.py:584-616](/home/john/filigree/src/filigree/dashboard.py:584)
- [/home/john/filigree/src/filigree/dashboard.py:724-753](/home/john/filigree/src/filigree/dashboard.py:724)
- [/home/john/filigree/src/filigree/mcp_server.py:806-930](/home/john/filigree/src/filigree/mcp_server.py:806)
- [/home/john/filigree/src/filigree/dashboard.py:902-902](/home/john/filigree/src/filigree/dashboard.py:902)

Evidence: Dashboard auth middleware only enforces `is_loom_scoped_path`, which is `/api/...` scoped. The dashboard mounts `/mcp` separately and forwards it to the MCP HTTP session manager. The server binds to `127.0.0.1`, reducing remote exposure but not protecting against local untrusted clients.

Impact: Any local process able to reach the dashboard port can invoke the HTTP MCP surface, including write-capable tracker tools, without the configured API token.

Remediation:

- Require bearer auth on `/mcp` when `FILIGREE_API_TOKEN` is set, or add a separate MCP HTTP token.
- Consider disabling dashboard-mounted MCP by default unless explicitly enabled.
- Add tests for `/mcp` with and without token configuration.

### M-02: Project-Local Scanner TOML Can Execute Arbitrary Processes When Triggered

Locations:

- [/home/john/filigree/src/filigree/scanners.py:165-184](/home/john/filigree/src/filigree/scanners.py:165)
- [/home/john/filigree/src/filigree/scanners.py:190-252](/home/john/filigree/src/filigree/scanners.py:190)
- [/home/john/filigree/src/filigree/scanner_runtime.py:32-91](/home/john/filigree/src/filigree/scanner_runtime.py:32)
- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:770-884](/home/john/filigree/src/filigree/mcp_tools/scanners.py:770)
- [/home/john/filigree/src/filigree/cli_commands/scanners.py:372-453](/home/john/filigree/src/filigree/cli_commands/scanners.py:372)

Evidence: Scanner configs are loaded from `.filigree/scanners/*.toml`; `command` and `args` become process argv. Metadata supports `requires_approval`, but trigger paths validate command existence and call `subprocess.Popen`.

Impact: In an untrusted repository, a malicious scanner definition can point to a repo-local executable or arbitrary PATH command. This is not shell injection, but it is repo-configured code execution once a scanner is triggered.

Remediation:

- Enforce approval metadata before spawning.
- Default to bundled/managed scanners only.
- Require a trusted-scanner allowlist stored outside the repository.
- Reject repo-relative executables unless an explicit trust flag is passed.

### M-03: Scan-Result Ingestion Lacks Body, Count, And String-Size Limits

Locations:

- [/home/john/filigree/src/filigree/dashboard_routes/common.py:113-122](/home/john/filigree/src/filigree/dashboard_routes/common.py:113)
- [/home/john/filigree/src/filigree/dashboard_routes/files.py:171-209](/home/john/filigree/src/filigree/dashboard_routes/files.py:171)
- [/home/john/filigree/src/filigree/db_files.py:838-909](/home/john/filigree/src/filigree/db_files.py:838)
- [/home/john/filigree/src/filigree/db_files.py:1074-1125](/home/john/filigree/src/filigree/db_files.py:1074)
- [/home/john/filigree/src/filigree/registry.py:744-746](/home/john/filigree/src/filigree/registry.py:744)

Evidence: HTTP routes parse the whole JSON body with `request.json()`. Finding validation checks types and project-relative paths but does not cap finding count, string lengths, metadata size/depth, or path count. Clarion batch resolution chunks all unique paths rather than rejecting oversized requests.

Impact: A local caller can force large memory allocations, many validations, database writes, observation creation, and potentially many Clarion batch calls.

Remediation:

- Add request body-size limits.
- Add maximum findings per request.
- Add maximum string lengths for message, suggestion, rule ID, file path, metadata keys/values, and evidence fields.
- Add maximum metadata depth/size.
- Require a reserved scan run or per-run callback secret for ingestion.

### M-04: Scan Ingestion Mutates Findings And Observations Without Refreshing Agent Context

Locations:

- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:663-767](/home/john/filigree/src/filigree/mcp_tools/scanners.py:663)
- [/home/john/filigree/src/filigree/cli_commands/scanners.py:1003-1208](/home/john/filigree/src/filigree/cli_commands/scanners.py:1003)
- [/home/john/filigree/src/filigree/dashboard_routes/files.py:485-507](/home/john/filigree/src/filigree/dashboard_routes/files.py:485)
- [/home/john/filigree/src/filigree/dashboard_routes/files.py:552-579](/home/john/filigree/src/filigree/dashboard_routes/files.py:552)
- [/home/john/filigree/src/filigree/db_files.py:1235-1278](/home/john/filigree/src/filigree/db_files.py:1235)
- [/home/john/filigree/src/filigree/summary.py:300-315](/home/john/filigree/src/filigree/summary.py:300)

Evidence: Scanner/report and HTTP scan-result paths call `process_scan_results` and return. Paired observations can be created inside ingestion. Other observation MCP paths explicitly refresh summary, but scan ingestion does not consistently do so.

Impact: Scanner-created observations and finding-driven issue cascades can exist in the DB while `context.md` remains stale, causing agents to miss pending work at session start.

Remediation:

- Centralize summary invalidation/refresh after tracker mutations.
- Refresh summary after successful scan ingestion on MCP, CLI, and HTTP surfaces.
- Add tests for `report_finding --create-observation` and `/scan-results` summary freshness.

### M-05: Finding-To-Issue Cascades Are Post-Commit And Best-Effort

Locations:

- [/home/john/filigree/src/filigree/db_files.py:1352-1465](/home/john/filigree/src/filigree/db_files.py:1352)
- [/home/john/filigree/src/filigree/db_files.py:1897-1919](/home/john/filigree/src/filigree/db_files.py:1897)
- [/home/john/filigree/src/filigree/db_files.py:1972-1998](/home/john/filigree/src/filigree/db_files.py:1972)

Evidence: Scan ingest commits before reopening linked issues; stale-finding cleanup commits before closing linked issues. Cascade failures are appended as warnings rather than failing the original mutation or persisting reconciliation debt.

Impact: A finding can be open while its linked issue remains closed, or fixed while its issue remains open. If warnings are ignored, operators see contradictory global state.

Remediation:

- Persist cascade failures as durable reconciliation items.
- Surface reconciliation debt in summary, dashboard, and stats.
- Add retry/repair tooling.
- Add fault-injection tests for close/reopen cascade failures.

### M-06: Observation Promotion Splits Issue Creation From Cleanup And Enrichment

Locations:

- [/home/john/filigree/src/filigree/db_observations.py:747-835](/home/john/filigree/src/filigree/db_observations.py:747)
- [/home/john/filigree/src/filigree/db_observations.py:875-990](/home/john/filigree/src/filigree/db_observations.py:875)
- [/home/john/filigree/src/filigree/db_observations.py:1029-1202](/home/john/filigree/src/filigree/db_observations.py:1029)

Evidence: Observation promotion commits the issue, then separately links/deletes observations and adds audit/labels/file associations as best-effort follow-up.

Impact: A failed cleanup leaves both a tracked issue and a live observation, creating duplicate triage signals. Failed enrichment can hide the issue's observation origin or file context.

Remediation:

- Make cleanup/enrichment failures durable and queryable.
- Refresh the returned issue after enrichment.
- Add fault-injection tests for delete, link, label, and file-association failures.

### M-07: Status Reads Can Mutate Scan State

Locations:

- [/home/john/filigree/src/filigree/db_scans.py:263-301](/home/john/filigree/src/filigree/db_scans.py:263)
- [/home/john/filigree/src/filigree/db_scans.py:164-227](/home/john/filigree/src/filigree/db_scans.py:164)

Evidence: `get_scan_status` performs live PID checks and calls `update_scan_run_status(..., "failed")` when a running process is dead.

Impact: Monitoring or polling changes system state. Operators and MCP hosts may not expect a read/status path to perform reconciliation writes.

Remediation:

- Split pure read from reconciliation, for example `get_scan_status(reconcile=False)` plus explicit `reconcile_scan_status`.
- Record explicit reconciliation events.
- Mark MCP/CLI status tools correctly if reconciliation remains enabled.

### M-08: Summary Rendering Can Grow With Unbounded WIP And Stale Sections

Locations:

- [/home/john/filigree/src/filigree/summary.py:70-80](/home/john/filigree/src/filigree/summary.py:70)
- [/home/john/filigree/src/filigree/summary.py:171-213](/home/john/filigree/src/filigree/summary.py:171)

Evidence: `in_progress` loads up to 10,000 issues; In Progress and Stale sections iterate all matching items while Ready and Blocked are capped.

Impact: Large projects can make every summary refresh expensive and produce a context file too large for quick agent orientation.

Remediation:

- Cap rendered WIP/stale sections.
- Include totals and query hints for omitted items.
- Add a summary size-budget test.

### M-09: MCP `observation_list` Is Marked Read-Only But Sweeps Expired Rows

Locations:

- [/home/john/filigree/src/filigree/mcp_server.py:415-429](/home/john/filigree/src/filigree/mcp_server.py:415)
- [/home/john/filigree/src/filigree/mcp_server.py:441-449](/home/john/filigree/src/filigree/mcp_server.py:441)
- [/home/john/filigree/src/filigree/mcp_tools/observations.py:403-433](/home/john/filigree/src/filigree/mcp_tools/observations.py:403)
- [/home/john/filigree/src/filigree/db_observations.py:440-526](/home/john/filigree/src/filigree/db_observations.py:440)
- [/home/john/filigree/src/filigree/db_observations.py:602-615](/home/john/filigree/src/filigree/db_observations.py:602)

Evidence: MCP read-only inference marks `list_*` tools read-only. `list_observations` calls tracker `list_observations`, which sweeps expired observations. A separate stats path documents `sweep=False`, confirming that sweep behavior is not required for all reads.

Impact: MCP hosts can invoke a mutating cleanup path under read-only assumptions.

Remediation:

- Make MCP `list_observations` use a no-sweep list path and filter expired rows in memory, or remove `readOnlyHint` for this tool.
- Add tests asserting read-only tool annotations only for non-mutating handlers.

### M-10: MCP `report_finding` Severity Type Can Crash Before Error Envelope

Locations:

- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:663-681](/home/john/filigree/src/filigree/mcp_tools/scanners.py:663)
- [/home/john/filigree/tests/api/test_scanner_tools.py:493-506](/home/john/filigree/tests/api/test_scanner_tools.py:493)
- [/home/john/filigree/tests/cli/test_scanners_commands.py:1748-1772](/home/john/filigree/tests/cli/test_scanners_commands.py:1748)

Evidence: The handler checks `severity not in VALID_SEVERITIES` without first ensuring severity is a string. An unhashable JSON array/object can raise `TypeError` before a structured MCP validation response. Tests cover invalid string severity, and CLI has a regression for this class of issue, but MCP does not.

Impact: Bad MCP input can produce an unstructured exception instead of a typed validation error.

Remediation:

- Check `isinstance(severity, str)` before membership testing.
- Add MCP regression tests for `severity=[]` and `severity={}`.

### M-11: `report_finding` Actor Attribution Bypasses Shared Validation

Locations:

- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:663-715](/home/john/filigree/src/filigree/mcp_tools/scanners.py:663)
- [/home/john/filigree/src/filigree/cli_commands/scanners.py:975-1002](/home/john/filigree/src/filigree/cli_commands/scanners.py:975)
- [/home/john/filigree/src/filigree/cli_commands/scanners.py:1122-1132](/home/john/filigree/src/filigree/cli_commands/scanners.py:1122)
- [/home/john/filigree/src/filigree/db_files.py:1242-1264](/home/john/filigree/src/filigree/db_files.py:1242)
- [/home/john/filigree/src/filigree/db_observations.py:222-256](/home/john/filigree/src/filigree/db_observations.py:222)

Evidence: MCP and CLI `report_finding` strip an optional actor and pass it as `observation_actor`. `process_scan_results` uses that value when creating observations. `create_observation` does not apply shared `sanitize_actor`. The CLI command also ignores the global `--actor` flow for this paired observation path.

Impact: Scan findings can create observations with actors that other observation and issue tools would reject, weakening audit consistency.

Remediation:

- Validate provided actors with `sanitize_actor` or MCP `_validate_actor`.
- In CLI, use `@click.pass_context`, default local actor to `ctx.obj["actor"]`, and sanitize before passing to ingestion.
- Add CLI and MCP tests for invalid actors with `create_observation`.

### M-12: HTTP MCP Schema Mismatch Uses The Wrong HTTP Status

Locations:

- [/home/john/filigree/src/filigree/mcp_server.py:852-859](/home/john/filigree/src/filigree/mcp_server.py:852)
- [/home/john/filigree/src/filigree/types/api.py:743-755](/home/john/filigree/src/filigree/types/api.py:743)

Evidence: The HTTP MCP app returns `409` for `SchemaVersionMismatchError`, while central `errorcode_to_http_status` maps `ErrorCode.SCHEMA_MISMATCH` to `503`.

Impact: HTTP MCP/dashboard clients receive inconsistent transport semantics for the same typed error and may retry or report the wrong remediation.

Remediation:

- Return `errorcode_to_http_status(ErrorCode.SCHEMA_MISMATCH)` in the MCP HTTP schema mismatch branch.
- Add a startup test asserting both JSON error code and HTTP status.

### M-13: Several CLI Validators Bypass JSON Error Envelopes

Locations:

- [/home/john/filigree/src/filigree/cli_commands/meta.py:312-323](/home/john/filigree/src/filigree/cli_commands/meta.py:312)
- [/home/john/filigree/src/filigree/cli_commands/meta.py:403-417](/home/john/filigree/src/filigree/cli_commands/meta.py:403)
- [/home/john/filigree/src/filigree/cli_commands/meta.py:493-510](/home/john/filigree/src/filigree/cli_commands/meta.py:493)
- [/home/john/filigree/src/filigree/cli_commands/files.py:931-951](/home/john/filigree/src/filigree/cli_commands/files.py:931)
- [/home/john/filigree/src/filigree/cli_commands/files.py:1046-1055](/home/john/filigree/src/filigree/cli_commands/files.py:1046)
- [/home/john/filigree/src/filigree/cli_commands/files.py:1106-1116](/home/john/filigree/src/filigree/cli_commands/files.py:1106)
- [/home/john/filigree/src/filigree/cli_commands/files.py:1176-1186](/home/john/filigree/src/filigree/cli_commands/files.py:1176)
- [/home/john/filigree/src/filigree/cli_commands/files.py:1224-1239](/home/john/filigree/src/filigree/cli_commands/files.py:1224)

Evidence: These commands use Click `IntRange` and `Choice` validation at parse time. Parse-time failures occur before command bodies can emit structured JSON `{error, code}` responses, unlike MCP handlers that validate inside handlers.

Impact: Automation using `filigree ... --json` cannot reliably parse validation failures across CLI surfaces, and CLI/MCP behavior diverges for comparable invalid arguments.

Remediation:

- Move validation for JSON-capable commands into command bodies, or add a top-level Click error adapter that emits structured JSON whenever `--json` was requested.
- Add parity tests for invalid enum/range values across CLI and MCP.

### M-14: Invalid Line Normalization Can Merge Distinct Findings

Locations:

- [/home/john/filigree/src/filigree/db_files.py:919-960](/home/john/filigree/src/filigree/db_files.py:919)
- [/home/john/filigree/src/filigree/db_files.py:1170-1195](/home/john/filigree/src/filigree/db_files.py:1170)
- [/home/john/filigree/tests/core/test_files.py:1360-1390](/home/john/filigree/tests/core/test_files.py:1360)

Evidence: `_normalize_line_attribution_for_existing_files` clears out-of-range `line_start`/`line_end`. Legacy dedup uses a key with `coalesce(line_start, -1)`. Existing tests assert clearing behavior but not collisions between multiple bad-line findings.

Impact: Distinct findings with invalid line attribution can collapse onto the same dedup key.

Remediation:

- Reject out-of-range line attribution instead of clearing it, or require a stable fingerprint when clearing.
- Include message/evidence hash in dedup for unknown-line findings.
- Add tests for two findings on the same file/rule with different invalid lines.

### M-15: Markdown Finding Parser Can Drop Valid Findings And Misattribute Lines

Locations:

- [/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:248-314](/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:248)
- [/home/john/filigree/tests/util/test_scan_utils.py:940-990](/home/john/filigree/tests/util/test_scan_utils.py:940)

Evidence: `parse_findings` skips a section if it contains the substring `No concrete bug found` anywhere. Line extraction uses the first `:(\d+)` in evidence, so ports, timestamps, URLs, or unrelated citations can become `line_start`.

Impact: Valid findings can be dropped or attached to the wrong line, then normalized/merged later.

Remediation:

- Treat the no-finding sentinel as an exact section marker, not a substring anywhere in evidence.
- Parse explicit file path/line citation grammar, preferably matching the reported `file_path`.
- Prefer JSON/SARIF scanner output where possible.

### M-16: Parent-Cycle Traversal Can Hang On Pre-Existing Parent Loops

Locations:

- [/home/john/filigree/src/filigree/db_issues.py:398-415](/home/john/filigree/src/filigree/db_issues.py:398)

Evidence: `_would_create_parent_cycle` walks parent links without tracking visited ancestors.

Impact: If the database already contains a parent loop, subsequent parent-cycle checks can loop indefinitely.

Remediation:

- Track visited issue IDs during traversal.
- Treat revisiting an ancestor as an existing integrity error.
- Add a repair/report path for parent loops.

### M-17: Scanner TOML Loader Crashes On Invalid UTF-8

Locations:

- [/home/john/filigree/src/filigree/scanners.py:203-209](/home/john/filigree/src/filigree/scanners.py:203)

Evidence: `_parse_toml` catches `OSError` around `path.read_text(encoding="utf-8")`, but not `UnicodeDecodeError`.

Impact: One malformed scanner file can break scanner listing/loading instead of being skipped like other malformed TOML.

Remediation:

- Catch `UnicodeDecodeError` with `OSError`.
- Append a scanner load error and return `None`.
- Add a malformed UTF-8 scanner fixture test.

### M-18: `FileRecord` Runtime Validation Accepts Invalid Registry Backends

Locations:

- [/home/john/filigree/src/filigree/models.py:148-162](/home/john/filigree/src/filigree/models.py:148)

Evidence: `FileRecord.__post_init__` checks only the `local`/empty-hash correlation. A row with `registry_backend="bogus"` and non-empty `content_hash` passes despite the `RegistryBackend` literal contract.

Impact: Corrupt DB rows or bad migrations can leak invalid API data.

Remediation:

- Validate `registry_backend in get_args(RegistryBackend)`.
- Add tests for invalid backend with and without content hash.

### M-19: Config JSON Is Cast To `ProjectConfig` Without Shape Validation

Locations:

- [/home/john/filigree/src/filigree/core.py:556-576](/home/john/filigree/src/filigree/core.py:556)

Evidence: `read_config` assigns raw JSON to `ProjectConfig` with a type ignore, then validates only registry settings. `enabled_packs`, `prefix`, and `version` types are not fully checked before later use.

Impact: A malformed `.filigree/config.json` can crash startup or silently alter enabled pack behavior.

Remediation:

- Reuse `read_conf`-style validation for `prefix`, `version`, and `enabled_packs`.
- Return defaults or raise a clear typed/structured error consistently.
- Add malformed-config tests.

### M-20: API Success Responses Can Crash Scanner Ingestion

Locations:

- [/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:350-394](/home/john/filigree/src/filigree/scanner_scripts/scan_utils.py:350)

Evidence: `post_to_api` assumes every 2xx response is a JSON object and then calls `body.get(...)`. Decode errors, Unicode errors, or JSON arrays are not handled.

Impact: A proxy, dashboard bug, or HTML success response can crash the scanner process instead of returning controlled failure details.

Remediation:

- Catch JSON/Unicode/type errors.
- Require `isinstance(body, dict)` before accessing fields.
- Return `(False, detail)` for malformed success responses.

### M-21: Clarion Capability Probe Uses Different HTTP Policy Than Runtime Resolution

Locations:

- [/home/john/filigree/src/filigree/registry.py:392-396](/home/john/filigree/src/filigree/registry.py:392)
- [/home/john/filigree/src/filigree/registry.py:613-613](/home/john/filigree/src/filigree/registry.py:613)

Evidence: Capability probing uses `urllib.request.urlopen`, while runtime Clarion resolution uses `httpx.Client(trust_env=False, follow_redirects=True)`.

Impact: Startup probe behavior can differ from file resolution under proxy, environment, and redirect settings. This also complicates token-origin controls.

Remediation:

- Use the same `httpx` client policy for capability probes and runtime resolution.
- Explicitly set proxy and redirect policy for token-bearing requests.
- Add tests for proxy env and redirect behavior if feasible.

### M-22: MCP Tool Modules Depend On Private `mcp_server` Globals

Locations:

- [/home/john/filigree/src/filigree/mcp_server.py:63-77](/home/john/filigree/src/filigree/mcp_server.py:63)
- [/home/john/filigree/src/filigree/mcp_tools/issues.py:930-960](/home/john/filigree/src/filigree/mcp_tools/issues.py:930)
- [/home/john/filigree/src/filigree/mcp_tools/files.py:734-765](/home/john/filigree/src/filigree/mcp_tools/files.py:734)

Evidence: Tool handlers import `_get_db` and `_refresh_summary` from `filigree.mcp_server` inside functions, tying domain tool modules to one transport/composition module.

Impact: Reuse, isolated testing, and alternate MCP/HTTP wiring are fragile.

Remediation:

- Define a `ToolContext` or handler factory in `mcp_tools.common`.
- Have `mcp_server` inject DB, path resolver, logger, and summary-refresh behavior.
- Remove private imports from tool modules.

### M-23: DB Mixins Are Coupled Through A Broad MRO Contract

Locations:

- [/home/john/filigree/src/filigree/db_base.py:260-509](/home/john/filigree/src/filigree/db_base.py:260)
- [/home/john/filigree/src/filigree/db_files.py:1352-1597](/home/john/filigree/src/filigree/db_files.py:1352)
- [/home/john/filigree/src/filigree/db_files.py:1829-1907](/home/john/filigree/src/filigree/db_files.py:1829)

Evidence: `DBMixinProtocol` declares cross-domain methods for issues, workflow, meta, files, observations, scans, and planning. `FilesMixin` handles scan ingestion, registry resolution, finding lifecycle, observation promotion, and issue close/reopen cascades.

Impact: Package boundaries are porous; changes in one domain can require coordinated updates across many mixins and protocol declarations.

Remediation:

- Split orchestration from persistence.
- Keep repositories narrow and move scan ingestion plus finding-to-issue cascade into explicit service classes.
- Add service-level tests around cross-domain workflows.

### M-24: MCP Runtime Validation Is Split Across Schemas, Casts, And Handlers

Locations:

- [/home/john/filigree/src/filigree/types/inputs.py:5-14](/home/john/filigree/src/filigree/types/inputs.py:5)
- [/home/john/filigree/src/filigree/mcp_tools/common.py:29-36](/home/john/filigree/src/filigree/mcp_tools/common.py:29)
- [/home/john/filigree/src/filigree/mcp_server.py:483-499](/home/john/filigree/src/filigree/mcp_server.py:483)
- [/home/john/filigree/src/filigree/mcp_tools/issues.py:930-960](/home/john/filigree/src/filigree/mcp_tools/issues.py:930)

Evidence: TypedDicts mirror JSON Schema, `_parse_args()` mostly casts, and dispatch only rejects non-object or unknown arguments before handlers do individual checks.

Impact: Correct behavior relies on external SDK validation and per-handler checks staying aligned; direct/internal calls can fail inconsistently, as shown by the `report_finding` severity type issue.

Remediation:

- Add one dispatch-level JSON Schema or generated model validator before handler invocation.
- Pass validated/coerced inputs to handlers.
- Add tests for malformed types on representative tools.

### M-25: Coverage Floors Omit Several High-Risk Surfaces

Locations:

- [/home/john/filigree/.github/workflows/ci.yml:45-61](/home/john/filigree/.github/workflows/ci.yml:45)
- [/home/john/filigree/pyproject.toml:171-186](/home/john/filigree/pyproject.toml:171)
- [/home/john/filigree/scripts/check_coverage_floors.py:15-23](/home/john/filigree/scripts/check_coverage_floors.py:15)

Evidence: CI enforces total 85% coverage and a short list of file-specific floors. Several high-risk surfaces in this report, including dashboard auth, registry/Clarion token handling, MCP server startup/resource handling, scanner runtime, and scan-result ingestion, do not have explicit file floors.

Impact: Regression in thin but security-critical surfaces can be hidden by stronger coverage elsewhere.

Remediation:

- Add floors for `dashboard_auth.py`, `dashboard_routes/files.py`, `mcp_server.py`, `registry.py`, `scanner_runtime.py`, and `scanner_scripts/scan_utils.py`.
- Pair floors with behavior-focused tests for the High/Medium issues in this report.

### M-26: Live Clarion Integration Coverage Is Optional In CI

Locations:

- [/home/john/filigree/.github/workflows/ci.yml:45-61](/home/john/filigree/.github/workflows/ci.yml:45)
- [/home/john/filigree/tests/integration/test_clarion_phase_d_e2e.py:53-93](/home/john/filigree/tests/integration/test_clarion_phase_d_e2e.py:53)
- [/home/john/filigree/tests/federation/test_sei_oracle_live_clarion.py:52-122](/home/john/filigree/tests/federation/test_sei_oracle_live_clarion.py:52)
- [/home/john/filigree/src/filigree/registry.py:382-470](/home/john/filigree/src/filigree/registry.py:382)
- [/home/john/filigree/src/filigree/registry.py:615-780](/home/john/filigree/src/filigree/registry.py:615)

Evidence: CI runs standard pytest but does not show a required live Clarion service stage. The live Clarion tests are present but environment-dependent.

Impact: Cross-product registry drift, auth semantics, and capability-probe behavior can ship without being exercised against a real Clarion endpoint.

Remediation:

- Add a required mocked-contract suite for Clarion auth/capabilities and path resolution.
- If live Clarion remains optional, add a scheduled or gated live job with clear failure triage.
- Include redirect and token-origin tests from H-01.

### M-27: Contract Parity Helper Checks Only The First List Item

Locations:

- [/home/john/filigree/tests/util/test_generation_parity.py:66-101](/home/john/filigree/tests/util/test_generation_parity.py:66)
- [/home/john/filigree/tests/util/test_generation_parity.py:107-129](/home/john/filigree/tests/util/test_generation_parity.py:107)

Evidence: The parity helper validates representative list contents by inspecting only the first item.

Impact: Generated API/adapter contracts can drift for later list elements without the parity test catching it.

Remediation:

- Validate every list item, or at least sample first, middle, and last with explicit length checks.
- Add a negative test where the second item differs from the expected shape.

### M-28: Scanner Subprocess Behavior Is Heavily Mocked

Locations:

- [/home/john/filigree/src/filigree/scanner_runtime.py:31-93](/home/john/filigree/src/filigree/scanner_runtime.py:31)
- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:770-908](/home/john/filigree/src/filigree/mcp_tools/scanners.py:770)
- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:1104-1214](/home/john/filigree/src/filigree/mcp_tools/scanners.py:1104)

Evidence: The high-risk behavior here is process spawn, reservation, PID/log backfill, and completion callback ordering. Subagent review found mocked coverage but no dynamic crash-after-reserve or failure-callback test.

Impact: Races like H-05 and H-06 are easy to miss if tests do not execute realistic subprocess lifecycle edges.

Remediation:

- Add integration tests with a tiny controlled scanner process.
- Exercise spawn failure, crash before callback, callback failure, PID death, and stale pending repair.

## Low

### L-01: MCP Tool Annotations Understate Destructive And Admin Behavior

Locations:

- [/home/john/filigree/src/filigree/mcp_server.py:415-450](/home/john/filigree/src/filigree/mcp_server.py:415)
- [/home/john/filigree/src/filigree/mcp_tools/meta.py:340-419](/home/john/filigree/src/filigree/mcp_tools/meta.py:340)
- [/home/john/filigree/src/filigree/mcp_tools/scanners.py:295-320](/home/john/filigree/src/filigree/mcp_tools/scanners.py:295)
- [/home/john/filigree/src/filigree/mcp_tools/issues.py:717-803](/home/john/filigree/src/filigree/mcp_tools/issues.py:717)

Evidence: `_DESTRUCTIVE_TOOLS` only includes `delete_issue` and `delete_file_record`. Other exposed tools can import tracker state, archive closed issues, compact events, undo admin actions, restart dashboard processes, disable scanners, or batch-close items.

Impact: MCP hosts that use `ToolAnnotations` for safety prompts may treat high-impact admin operations as ordinary mutating tools.

Remediation:

- Expand annotation classification for import, compact, archive, undo, restart, disable, and batch mutation tools.
- Add a static assertion requiring every admin/batch mutation tool to have an explicit annotation decision.

### L-02: Workflow Seeding Has A Runtime Circular Dependency On `core.py`

Locations:

- [/home/john/filigree/src/filigree/core.py:47-47](/home/john/filigree/src/filigree/core.py:47)
- [/home/john/filigree/src/filigree/core.py:827-857](/home/john/filigree/src/filigree/core.py:827)
- [/home/john/filigree/src/filigree/db_workflow.py:90-127](/home/john/filigree/src/filigree/db_workflow.py:90)

Evidence: `core.py` imports `WorkflowMixin`; `WorkflowMixin._seed_templates()` runtime-imports `_seed_builtin_packs` from `core.py`.

Impact: The workflow layer reaches back into the composition root, making import timing and initialization refactors brittle.

Remediation:

- Move `_seed_builtin_packs` into a neutral module such as `template_bootstrap.py` or `db_templates.py`.
- Import that module from both `core.py` and `db_workflow.py`.

### L-03: Plan Payload Validation Is Duplicated Across CLI, MCP, And DB Layers

Locations:

- [/home/john/filigree/src/filigree/cli_commands/planning.py:397-473](/home/john/filigree/src/filigree/cli_commands/planning.py:397)
- [/home/john/filigree/src/filigree/mcp_tools/planning.py:532-614](/home/john/filigree/src/filigree/mcp_tools/planning.py:532)
- [/home/john/filigree/src/filigree/db_planning.py:795-836](/home/john/filigree/src/filigree/db_planning.py:795)

Evidence: Similar milestone/phase/step validation and error messages are implemented separately before both surfaces call `db.create_plan()`.

Impact: Future plan fields or validation rules can drift by surface.

Remediation:

- Extract shared plan payload parsing/coercion returning normalized inputs plus surface-neutral validation errors.

### L-04: Observation Line Numbering Permits Line 0 While Scanner Findings Are 1-Based

Locations:

- [/home/john/filigree/src/filigree/db_observations.py:255-270](/home/john/filigree/src/filigree/db_observations.py:255)
- [/home/john/filigree/src/filigree/db_files.py:856-872](/home/john/filigree/src/filigree/db_files.py:856)
- [/home/john/filigree/src/filigree/db_schema.py:261-266](/home/john/filigree/src/filigree/db_schema.py:261)

Evidence: Observations accept `line >= 0`; scanner findings reject line numbers below 1.

Impact: Manual observations can point to impossible source line 0, creating anchor drift between observation and scanner flows.

Remediation:

- Normalize observation line 0 to `None` or require `>= 1`.
- Migrate existing line-0 observations to `NULL`.
- Align MCP and CLI validation.

### L-05: Project-Local Workflow Template Overrides Can Silently Weaken Policy

Locations:

- [/home/john/filigree/src/filigree/templates.py:823-834](/home/john/filigree/src/filigree/templates.py:823)
- [/home/john/filigree/src/filigree/templates.py:1118-1124](/home/john/filigree/src/filigree/templates.py:1118)
- [/home/john/filigree/src/filigree/templates.py:1193-1208](/home/john/filigree/src/filigree/templates.py:1193)

Evidence: Project-local `.filigree/templates/*.json` load after built-ins and `_register_type(tpl)` overwrites existing type definitions.

Impact: A project-local template can weaken workflow controls such as required close fields or transition policy. This may be intended extensibility, but policy changes are not prominent.

Remediation:

- Require explicit opt-in for overriding built-in types.
- Surface a warning in CLI/dashboard when a built-in type is overridden.
- Keep security-critical invariants in code rather than template-only policy.

### L-06: Bundled Scanner TOML Writer Does Not Escape Strings

Locations:

- [/home/john/filigree/src/filigree/bundled_scanners.py:23-35](/home/john/filigree/src/filigree/bundled_scanners.py:23)

Evidence: TOML is built with direct interpolation of names, descriptions, commands, args, and file types.

Impact: A future bundled scanner containing quotes, backslashes, or control characters can generate invalid TOML.

Remediation:

- Use a TOML writer helper or a dedicated TOML string-quoting function.
- Add a fixture with quotes/backslashes/control characters.

### L-07: XSS Guard Tests Are Brittle String-Snippet Checks

Locations:

- [/home/john/filigree/tests/static/test_xss_guards.py:14-43](/home/john/filigree/tests/static/test_xss_guards.py:14)
- [/home/john/filigree/src/filigree/static/js/views/detail.js:96-170](/home/john/filigree/src/filigree/static/js/views/detail.js:96)
- [/home/john/filigree/src/filigree/static/js/views/files.js:224-230](/home/john/filigree/src/filigree/static/js/views/files.js:224)
- [/home/john/filigree/src/filigree/static/js/app.js:487-511](/home/john/filigree/src/filigree/static/js/app.js:487)

Evidence: The tests assert specific source strings such as `escHtml(...)` rather than executing rendering with adversarial input.

Impact: A refactor can preserve the string but break runtime escaping, or remove the string while remaining safe.

Remediation:

- Add runtime DOM/rendering tests with malicious issue titles, statuses, file IDs, and transition names.
- Keep static string tests only as supplemental guardrails.

### L-08: CLI Tests Mutate Process-Wide Current Working Directory

Locations:

- [/home/john/filigree/tests/cli/conftest.py:16-24](/home/john/filigree/tests/cli/conftest.py:16)

Evidence: CLI fixtures change process-wide cwd for tests.

Impact: Parallelization or fixture reuse can create order-sensitive test failures.

Remediation:

- Prefer isolated runner cwd parameters where possible.
- If global cwd mutation remains, mark affected tests and keep them serialized.

### L-09: Loom Slim Issue Type Documentation Has Drifted

Locations:

- [/home/john/filigree/src/filigree/types/api.py:54-65](/home/john/filigree/src/filigree/types/api.py:54)
- [/home/john/filigree/src/filigree/generations/loom/types.py:30-39](/home/john/filigree/src/filigree/generations/loom/types.py:30)
- [/home/john/filigree/src/filigree/generations/loom/adapters.py:44-59](/home/john/filigree/src/filigree/generations/loom/adapters.py:44)

Evidence: `SlimIssueLoom` documentation says its key difference from `SlimIssue` is `issue_id`, but `SlimIssue` already uses `issue_id`.

Impact: Low-level documentation/type duplication can mislead future adapter changes.

Remediation:

- Reuse/alias the shared `SlimIssue` if the shape is identical.
- Otherwise update the comment to describe the actual distinction.

## Suggested Remediation Order

1. Fix trust-boundary bypasses first: H-01, H-02, M-01, M-03.
2. Fix MCP/CLI validation and degraded-startup behavior: H-03, H-04, M-10, M-11, M-12, M-13.
3. Fix scanner lifecycle correctness: H-05, H-06, M-20, M-28.
4. Fix planning graph integrity: H-07, H-08, M-16.
5. Address cross-domain consistency and architecture risks: H-09, M-04, M-05, M-06, M-22, M-23, M-24.
6. Harden tests and coverage around the above: M-25, M-26, M-27, L-07, L-08.

## Verification Notes

- This was a static, read-only audit. No tests were run because test execution can write caches, databases, temporary files, coverage data, or logs.
- Local coordinator checks used file reads only (`rg`, `sed`) plus the requested artifact write.
- Several concurrency findings are static race analyses and should be confirmed with fault-injection or parallel-writer tests during remediation.
- Exact line ranges are from the repository snapshot inspected on 2026-06-04 and may drift after edits.
