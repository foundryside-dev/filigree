# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **``get_blocked`` now includes wip-category issues that are stuck on
  another non-done issue.** Previously ``get_blocked`` filtered to
  open-category only, so an in-progress task waiting on an open
  blocker was invisible to the "what's stuck right now" surface.
  filigree-cb980eee0d, P2.8 senior-user MCP review.

- **``add_label`` reports mutual-exclusivity displacement.** When a
  ``review:`` namespace label silently displaces a sibling, the response
  now carries ``label_result="replaced"``, ``replaced_labels=[...]``,
  and a ``data_warnings`` entry naming the displaced label. Previously
  the displacement was completely silent and the response showed
  ``label_result="added"``. CLI emits a "(replaced: ...)" hint.
  ``add_label`` now returns a 3-tuple ``(added, canonical, replaced)``
  internally; the only callers were within the codebase. (filigree-cb980eee0d, P2.7)

- **``PublicIssue`` emits both ``parent_id`` and ``parent_issue_id``.**
  ``get_issue`` previously returned ``parent_id`` while ``get_ready`` and
  ``list_issues`` returned ``parent_issue_id`` — the same value with two
  names depending on the tool. Both names now appear on the full
  PublicIssue shape; agents should prefer ``parent_issue_id`` for
  consistency. ``parent_id`` is retained for backwards compatibility.
  (filigree-cb980eee0d, P2.9)

- **``get_changes`` defaults to excluding ``heartbeat`` events.**
  With 48-hour leases and aggressive heartbeating, the catch-up firehose
  was dominated by liveness pings. The MCP tool now filters out
  ``heartbeat`` events by default; pass ``include_heartbeats=true``
  (or ``type='heartbeat'`` explicitly) to see them. ``get_events_since``
  in ``db_events`` gains an ``exclude_types: list[str] | None`` param.
  (filigree-cb980eee0d, P2.11)

- **``heartbeat_work`` defaults the audit actor to the issue's
  current assignee** when the caller omits ``actor``. Previously the
  MCP handler defaulted to the literal string ``'mcp'``, so the
  rightful holder's heartbeat would CONFLICT with "expected 'mcp'"
  whenever they forgot to pass ``actor`` — silently letting their lease
  expire. (filigree-cb980eee0d, P2.5)

- **Observations created from findings link back via ``source_finding_id``;
  ``dismiss_finding`` and ``promote_finding`` cascade-dismiss the link.**
  Senior-user MCP review (run d, finding P1.2) flagged that the prior fix
  (``9af7b82``) only added the observation IDs to the
  ``report_finding`` response — the observations themselves had no
  structural backlink, so closing or promoting the finding left a
  14-day zombie observation that re-appeared in ``list_observations``
  every time. The schema now adds ``observations.source_finding_id``
  (migration v11→v12) and an index for cascade scans;
  ``report_finding`` populates the field, and ``update_finding`` (the
  underlying helper for both ``dismiss_finding`` and
  ``promote_finding``) auto-dismisses any linked observation when the
  finding reaches a terminal status (``false_positive``, ``fixed``,
  ``unseen_in_latest``) or is linked to an issue. The dismissal is
  recorded in ``dismissed_observations`` for audit. (filigree-cb980eee0d)

- **``release_claim`` auto-reverts wip→open by default.**
  Senior-user MCP review (run d, finding P1.3) flagged that releasing a
  claim while the issue was in a wip-category status (e.g.
  ``in_progress``, ``fixing``) cleared the assignee but left the status
  unchanged, so the issue became invisible to ``get_ready``,
  ``get_blocked``, ``get_summary`` — orphaned in wip with no discovery
  surface. ``release_claim`` now resolves a template-defined reverse
  target (the open-category predecessor whose forward transition targets
  the current wip status; e.g. ``in_progress→open`` for ``task``,
  ``fixing→confirmed`` for ``bug``) and transitions the status back so
  the issue rejoins ``get_ready``. Statuses with no direct open
  predecessor (e.g. ``bug.verifying``) fall back to the template's
  ``initial_state`` if it is open-category. Pass
  ``release_claim(revert_status=False)`` to opt out and keep the legacy
  "release without status change" behaviour. (filigree-cb980eee0d)

- **Optional ``expected_assignee`` precondition on write tools.**
  Senior-user MCP review (run d, finding P1.1) flagged that
  ``heartbeat_work`` / ``release_claim`` / ``reclaim_issue`` strictly
  enforce claim ownership while ``update_issue`` / ``batch_update`` /
  ``close_issue`` / ``add_comment`` / ``add_label`` / ``remove_label``
  ignored it — a non-claimant could overwrite a held issue silently.
  Each of those tools now accepts an optional ``expected_assignee``
  string; when set, the call returns ``CONFLICT`` (with the
  CONFLICT-shaped ``"assigned to '<x>' (expected '<y>')"`` message) if
  the observed assignee differs. Default behaviour with the parameter
  omitted is unchanged for backward compatibility. Batch tools apply
  the precondition per-item and surface failures via the standard
  ``failed[]`` array. (filigree-cb980eee0d)

### Changed

- **``search_issues`` description now documents FTS tokenisation
  behaviour.** Hyphens split tokens and tokens shorter than 2 chars are
  elided, so ``mcp-review-d`` returns nothing while ``mcp review``
  matches ``[mcp-review-d] Scratch task A``. (filigree-cb980eee0d, P2.6)

### Changed (BREAKING)

- **``close_issue`` now validates state transitions like ``update_issue``.**
  The senior-user MCP review (run d, ``docs/plans/2026-05-06-mcp-senior-user-review-d.md``
  finding P1.4) flagged that ``update_issue(status='closed')`` and
  ``close_issue()`` had opposite contracts for the same target state — a
  bug stuck in ``triage`` could be force-closed via ``close_issue`` even
  though the bug template only allows ``triage → wont_fix``,
  ``triage → not_a_bug``, or walking through ``confirmed → fixing →
  verifying → closed``. ``close_issue`` previously passed
  ``_skip_transition_check=True`` to the underlying update; that bypass
  is now removed. The MCP/CLI/dashboard surfaces all return the same
  ``INVALID_TRANSITION`` envelope (with ``valid_transitions`` and
  ``hint``) for unreachable target statuses. To support legitimate
  alternate-done closures, ``close_issue`` now exposes ``status``
  (already in the underlying db method, but absent from MCP/CLI/dashboard
  surfaces); pass ``close_issue(status='wont_fix')`` /
  ``filigree close <id> --status=wont_fix`` to land in a directly
  reachable done state when the default isn't reachable. The MCP tool
  description and CLI help text document this. (filigree-cb980eee0d)

### Added (since v2.0.0 baseline)

- **``response_detail``/``--detail`` opt-in on MCP and CLI batch tools.**
  Closes a documentation/implementation gap: the agent guidance
  (``src/filigree/data/instructions.md``, the project ``CLAUDE.md`` and
  ``AGENTS.md`` blocks, and the ``filigree-workflow`` skill pack)
  promised ``response_detail="full"`` on every batch tool but only the
  dashboard ``loom`` HTTP routes implemented it; MCP/CLI silently
  ignored the parameter. The flag is now wired on the six batch tools
  across all three surfaces:
  - Issues: ``batch_close``/``batch-close`` and
    ``batch_update``/``batch-update`` — slim ``BatchResponse[SlimIssue]``
    (default) → full ``BatchResponse[PublicIssue]``.
  - Meta: ``batch_add_label``/``batch-add-label`` and
    ``batch_add_comment``/``batch-add-comment`` — slim
    ``BatchResponse[str]`` (issue IDs) → full
    ``BatchResponse[PublicIssue]``.
  - Findings: ``batch_update_findings``/``batch-update-findings`` —
    slim ``BatchResponse[str]`` (finding IDs) → full
    ``BatchResponse[ScanFindingDict]``.
  - Observations: ``batch_dismiss_observations``/
    ``batch-dismiss-observations`` — slim ``BatchResponse[str]``
    (observation IDs) → full ``BatchResponse[ObservationDict]``
    (records snapshotted *before* dismissal so callers can audit what
    they just dropped). Backed by a new
    ``FiligreeDB.get_observations_by_ids`` helper.

  Bad ``response_detail`` values return ``code: VALIDATION`` (MCP) or
  exit 2 (CLI usage error). The shared ``parse_response_detail`` helper
  in ``filigree.types.api`` is the single source of truth for the
  slim/full vocabulary across MCP, CLI, and dashboard routes.
- **CLI ``get-template <type>`` verb-noun alias.** Mirrors the MCP
  ``get_template`` tool and the existing pattern (``get-type-info``,
  ``get-valid-transitions``, ``get-workflow-guide``). Supports ``--json``
  and emits the 2.0 flat error envelope (``ErrorCode.NOT_FOUND``) on
  unknown types. (filigree-6213766f9b)
- **CLI ``get-workflow-statuses`` verb-noun alias.** Closes a hole in
  the verb-noun alias convention (docs/cli.md): the MCP tool is
  ``get_workflow_statuses`` but only the short form ``workflow-statuses``
  was registered. Both forms now produce identical output; the alias
  table in ``docs/cli.md`` is updated. (filigree-ca938979f4)

### Fixed

- **Loom ``ScanIngestResponseLoom`` is now a concrete TypedDict.** It
  previously subclassed ``BatchResponse[str]`` from ``filigree.types.api``,
  but at runtime TypedDict + ``Generic`` does not preserve the ``str``
  substitution (``succeeded`` resolved to ``list[~_T]``), and parent
  ``NotRequired`` markers are stripped on a ``total=True`` subclass — which
  promoted ``newly_unblocked`` to a *required* key on the response
  type, even though scan ingestion cannot unblock issues. The shape is
  now declared inline (``succeeded: list[str]``, ``failed``, ``stats``,
  ``warnings``); a ``get_type_hints`` contract test pins it.
  (filigree-58f82a0c38)
- **Loom ``BatchCloseResponseLoom.succeeded`` widened to
  ``list[SlimIssueLoom | IssueLoom]``.** The C5 contract
  (``docs/federation/contracts.md`` §C5) and the handler in
  ``dashboard_routes/issues.py`` upgrade ``succeeded[]`` to full
  ``IssueLoom`` items when ``response_detail=full``, but the type was
  pinned slim-only. ``newly_unblocked`` stays ``SlimIssueLoom`` per the
  locked C5 rule. A ``get_type_hints`` contract test pins both.
  (filigree-2189802389)
- **``filigree install`` and ``filigree doctor --fix`` now surface
  ``ForeignDatabaseError`` instead of swallowing it as a generic missing-project
  error.** Both admin commands caught the bare ``FileNotFoundError`` raised by
  ``find_filigree_root``, so the rich ``ForeignDatabaseError`` message
  ("Refusing to latch onto another project's filigree database… ``cd <git
  boundary> && filigree init``") never reached users running these commands
  from inside an inner repo. The catch is now ``ProjectNotInitialisedError``
  (the parent class of both ``ProjectNotInitialisedError`` and
  ``ForeignDatabaseError``), and the exception's own message is emitted to
  stderr — same contract as ``cli_commands/scanners.py`` and the session-hook
  fix above. (filigree-dad647cf35)
- **``filigree install`` now exits 1 when one or more selected install steps
  failed.** The command accumulated per-step ``(name, ok, msg)`` tuples and
  printed them, but never mapped any ``ok=False`` to a non-zero exit code, so
  CI pipelines and shell scripts depending on the exit code treated partial
  failures as success. The summary now exits 1 (with a "Some install steps
  failed" stderr line and a suppressed "Next:" hint) whenever any step
  returned ``False``. ``TestInstallMode`` tests gained the same
  ``SERVER_CONFIG_DIR`` isolation that ``TestInstallModeIntegration`` already
  used, since the exit-code change exposed pre-existing prefix-collision
  pollution from the user's real ``~/.config/filigree/server.json``.
  (filigree-ca4e5d28dd)
- **``filigree metrics --days`` now rejects non-positive values via a
  ``click.IntRange(1, 3650)``.** Without the range, ``--days=-5`` reached
  ``get_flow_metrics``'s own ``ValueError`` and bubbled up as a Python
  traceback. The range mirrors sibling admin commands (``archive``,
  ``clean-stale-findings``, ``compact``) and the dashboard's own
  ``api_metrics`` clamp at ``[1, 3650]``. (filigree-d9cf9d34b1)
- **``filigree export`` now catches ``OSError``/``sqlite3.Error`` and emits a
  clean "Export failed: …" line.** The command called ``db.export_jsonl``
  unguarded, so missing parent directories, permission errors, or disk-full
  conditions surfaced as raw Python tracebacks. The matching ``import``
  command already had this contract (and a regression test); ``export``
  now mirrors it. (filigree-48613c1c55)
- **Session hooks and dashboard launcher honour ``.filigree.conf`` and
  surface ``ForeignDatabaseError``.** ``generate_session_context`` walked
  up via ``find_filigree_root`` and then opened
  ``.filigree/filigree.db`` directly, ignoring any custom ``db`` path
  declared in ``.filigree.conf``; ``ForeignDatabaseError`` (raised when
  walk-up crosses a ``.git/`` boundary) was swallowed by the catch-all
  ``FileNotFoundError`` handler, hiding the actionable remediation
  message. Both session-context and ``ensure_dashboard_running`` now
  resolve via ``find_filigree_anchor`` / ``find_filigree_root``, route DB
  init through ``FiligreeDB.from_conf`` when a conf is present, and
  surface the foreign-database message instead of exiting silently.
  (filigree-4e28325279, filigree-acbacc5b3e)
- **Dashboard URL emitted from session context now uses
  ``verify_pid_ownership``.** The pre-existing fast path in
  ``_build_context`` trusted raw ``is_pid_alive``, so a recycled PID
  owned by an unrelated process could be reported as a live dashboard.
  Session context now performs the same OS-cmdline identity check that
  the reuse path and ``filigree doctor`` already use.
  (filigree-aa38935c28)
- **Ethereal dashboard reuse no longer races concurrent starts.** The
  pre-lock probe in ``_ensure_dashboard_ethereal_mode`` unlinked
  ``ephemeral.pid`` / ``ephemeral.port`` when ownership verification
  failed, so a peer that wrote fresh metadata between our read and our
  verify could have it erased outside ``ephemeral.lock``. The probe is
  now read-only outside the lock; all destructive cleanup runs after
  the lock is held. The post-lock cleanup also terminates an owned
  dashboard whose port never came up past the startup grace window
  before respawning, so the orphan process is no longer leaked.
  (filigree-48215e8343, filigree-fd3ac0feec)
- **Skill-pack freshness check now hashes the entire skill tree.** The
  hook compared only ``SKILL.md`` between source and installed copies,
  so changes to companion files under ``examples/`` and ``references/``
  could leave installed skill packs stale indefinitely. The freshness
  check now computes a sorted ``(relative path, bytes)`` digest across
  the whole ``filigree-workflow`` directory and reinstalls when any
  file differs. (filigree-4bd71d94c3)
- **CLI `--priority*` ranges now emit the 2.0 envelope instead of Click
  usage errors.** `list`, `list-issues`, `update`, `update-issue`,
  `claim-next`, and `start-next-work` declared bounded numeric options as
  `click.IntRange(0, 4)`, which made Click reject out-of-range values with
  a `BadParameter` plain-text usage error before the command body could
  emit `{"error", "code"}` (Phase E §9 envelope contract). The
  `_range_check_priority` body-time helper that `create` already used for
  the same reason is generalised to `_range_check_int(value, name, ...)`,
  and the affected options now parse as plain `int` and validate after
  `--json` has been observed. (filigree-ff324e6a96)
- **CLI `claim-next --json` now emits the documented `ClaimNextResponse` /
  `ClaimNextEmptyResponse` shapes.** Empty replies gain `reason: "No ready
  issues matching filters"`; successful replies gain `selection_reason`
  built from the new shared `Issue.format_claim_next_reason()` helper, so
  CLI / MCP / HTTP all agree on the wire shape (the MCP handler is
  refactored to use the same helper to keep parity by construction).
  (filigree-0e8dadbfcc)
- **`/api/loom/scanners` now resolves scanner TOMLs from the project root,
  not the DB directory.** The route built ``scanners_dir = db.db_path.parent /
  "scanners"`` (`src/filigree/dashboard_routes/files.py`), which only matched
  the legacy `.filigree/filigree.db` layout. For `.filigree.conf` projects
  with a relocated `db = "data/track.db"`, the route looked at
  `<project>/data/scanners` instead of `<project>/.filigree/scanners` and
  returned an empty list — even though the CLI `list-scanners` and MCP
  `list_scanners` correctly enumerated them. Same family as filigree-da8d5aba0f
  (dashboard opens conf-declared DB path). The route now anchors to
  `db.project_root / ".filigree" / "scanners"` when `project_root` is set
  (always the case for `from_filigree_dir` and `from_conf`), and falls back
  to `db.db_path.parent / "scanners"` only for bare `FiligreeDB(...)`
  construction. Regression tests in
  `tests/test_dashboard.py::TestLoomScannersRelocatedDB`.
  (filigree-641037692a)
- **Dashboard now opens the conf-declared DB path.** Both ethereal mode
  (`main()`) and server mode (`ProjectStore.get_db`) called
  `FiligreeDB.from_filigree_dir`, which hardcodes `.filigree/filigree.db` and
  ignores the `db` field in `.filigree.conf`. Custom-DB projects (e.g.
  `db = "storage/track.db"`) were silently displayed and mutated through the
  wrong (default) database, while the CLI/MCP — which goes through
  `cli_common.py` — opened the correct path. A new
  `_open_db_for_filigree_dir` helper now uses `from_conf` when a
  `.filigree.conf` sits next to the directory and falls back to
  `from_filigree_dir` only for legacy installs (the same shape as
  `cli_common.py:_build_db` and the doctor fix in filigree-3572d3b273).
  Regression test in
  `tests/api/test_multi_project.py::TestProjectStore::test_get_db_honors_custom_db_path_from_conf`.
  (filigree-da8d5aba0f)
- **Dashboard `ProjectStore.reload()` is now atomic and serves consistent
  handles under concurrent reads.** Two race windows existed: (1) `load()`
  published `_projects` *before* `reload()` evicted stale `_dbs` under the
  lock, leaving a window where `_projects[key]` referenced the new path
  while `_dbs[key]` was the old handle (request handlers got the wrong DB);
  (2) `get_db()` had an unlocked fast path that could hand out a handle
  immediately before `reload()` popped and closed it (request handlers got
  a closed `sqlite3.Connection`). The fix factors `load()` into a
  `_compute_projects()` helper, performs an atomic state swap in `reload()`
  under one lock acquisition, removes the unlocked fast path in `get_db()`,
  and parks evicted handles on `_evicted_dbs` so they are closed only at
  `close_all()` (process shutdown) — sidestepping the close-while-in-use
  hazard without introducing a refcount/lease mechanism. New regression
  test
  `tests/api/test_multi_project.py::TestProjectStore::test_reload_atomic_under_concurrent_get_db`
  exercises a reader against a path-rotating reloader and asserts no torn
  views and no closed handles. (filigree-e43edbc067)
- **`report-finding --file` now surfaces the foreign-database diagnostic.**
  The command read and validated the `--file` payload before resolving the
  filigree project, so a `ForeignDatabaseError` (cwd inside a git repo whose
  nearest filigree anchor sits across a `.git/` boundary) was masked by the
  next available file/JSON `VALIDATION` envelope. Project resolution now
  runs at the top of the callback via `_resolve_filigree_dir_or_die`,
  matching every other command in this module. The
  `TestForeignDatabaseDiagnostic` matrix in `tests/cli/test_scanners_commands.py`
  gains a `report-finding --file <missing> --json` parametrize case.
  (filigree-08ed302942)
- **`trigger-scan-batch <scanner> --json` now returns a structured envelope
  on empty file lists.** The variadic `file_paths` argument was declared
  `required=True`, so Click rejected an empty invocation with raw usage text
  before the callback ran — making the in-callback `VALIDATION` envelope
  (and its match with the MCP `mcp_tools/scanners.py` contract) unreachable
  from the CLI. The argument is now `required=False`; the existing empty-list
  guard emits `{"error": "file_paths must be a non-empty list", "code":
  "VALIDATION"}` as intended. New regression test in
  `tests/cli/test_scanners_commands.py::TestForeignDatabaseDiagnostic::test_trigger_scan_batch_empty_filepaths_returns_validation_envelope`.
  (filigree-9b04b0aca9)
- **Graph v2: archived issues no longer leak through `include_done=false`
  and `status_categories` filters.** `archive_closed()` writes
  `status='archived'` while preserving `closed_at`, but the synthetic
  status resolves to category `open`, not `done`. The graph route
  filtered only on `status_category == 'done'`, so archived issues
  appeared in `include_done=false` results, matched
  `status_categories=open`, and inflated `blocks_open_count` (the
  `.blocks` adjacency list, unlike `.blocked_by`, isn't pre-filtered for
  archived members). A new `_graph_status_category()` helper normalizes
  archived to `done` for filter predicates, blocker/dependent counts,
  and the serialized node payload. (filigree-b6cacfce72)
- **Graph v2: shortcut edges between non-adjacent critical-path nodes
  are no longer flagged `is_critical_path`.** `db.get_critical_path()`
  returns an ordered chain (longest-path DP). The route collapsed it
  into a node-id set and marked any edge between two members critical,
  so a chain `A→B→C→D` plus shortcut `A→D` mis-flagged the shortcut
  even though it isn't part of the chain. The route now keeps the
  ordered list and builds adjacent `(source, target)` pairs;
  `_filter_graph_edges` flags only those pairs.
  (filigree-c9b08d1363)
- **`install_codex_mcp`: replace TOML-equivalent header forms in place
  instead of duplicating them.** `_upsert_toml_table` matched the
  existing `mcp_servers.filigree` block with a literal regex
  (`^\[mcp_servers\.filigree\]`), so valid equivalent forms accepted by
  `tomllib` — `[mcp_servers."filigree"]`, `[mcp_servers . filigree]`,
  `["mcp_servers".filigree]` — bypassed replacement and took the append
  branch. The resulting file then contained two semantically identical
  tables, which `tomllib` rejects under its duplicate-table rule,
  breaking subsequent `install_codex_mcp` runs with "malformed TOML;
  fix or remove it." The matcher now parses each `^\[...\]` header
  through `tomllib` and compares the normalized key path, so any
  TOML-equivalent spelling is replaced in place. (filigree-32fe96222e)
- **`_install_skill_to`: concurrent installs no longer race on a shared
  staging directory.** The old implementation reused a deterministic
  `<skill>.installing` sibling path for every invocation; two Claude Code
  sessions starting near-simultaneously (each firing the SessionStart
  stale-skill auto-refresh in `hooks.py`) could clobber each other's
  staging area, producing `FileExistsError` or leaving the target
  directory absent between rmtree and rename. Each call now stages into
  a unique `tempfile.mkdtemp` directory and the swap tolerates a peer
  winning the rename race (their staged content is identical).
  (filigree-86b73c3d6c)
- **`_atomic_write_text`: preserves destination permissions on rewrite.**
  `tempfile.mkstemp()` creates files with mode 0o600; the previous
  implementation's `os.replace()` would silently downgrade existing
  files (CLAUDE.md, AGENTS.md, .gitignore) from 0o644 to 0o600 on every
  install or update. The helper now captures the existing mode before
  the swap and chmods the temp file to match. New files respect the
  process umask. (filigree-156023f053)
- **Doctor: `.gitignore` check now uses the same gitignore-aware parser
  as `ensure_gitignore`, instead of substring matching.** Previously a
  `.gitignore` containing only a comment (`# .filigree/ ...`), only a
  negation (`!.filigree/`), or only a non-root substring
  (`src/.filigree/cache/`) would be reported as healthy even though git
  did not actually ignore the project-root `.filigree/`. The parser was
  extracted into `install_support/gitignore.py` so install and doctor
  share one implementation. (filigree-bc5d2af1ef)
- **Doctor: malformed `mcpServers.filigree` entries in `.mcp.json` are
  now flagged invalid.** A truthy non-dict value (string, bool, list)
  used to silently report "Configured in .mcp.json" because the
  per-server schema was never validated — `command` was coerced to
  `""`, both absolute-path branches were skipped, and the success
  branch caught the fall-through. Doctor now requires the entry to be
  a dict and validates the stdio (non-empty `command`, list `args`)
  and streamable-http (non-empty `url`) shapes the installer emits.
  (filigree-466bcb6279)
- **Doctor no longer subprocess-runs the hook-configured Python
  interpreter.** When the SessionStart hook in `.claude/settings.json`
  used module form (`<abs-path> -m filigree …`), `filigree doctor`
  used to execute `<abs-path> -c "import filigree"` to detect a
  venv-purged install (filigree-36539914b3). Because
  `.claude/settings.json` is project-controlled, a hostile or
  compromised repo could plant a binary at that path and get arbitrary
  code executed under anyone running `filigree doctor` on a fresh
  clone. The probe is removed; module-form hooks now get structural
  validation only. The original venv-purge case still surfaces as a
  SessionStart failure on the next session and is repaired by
  `filigree install --hooks`. (filigree-e6828dcdb1)
- **Scan ingest: `process_scan_results` rejects `suggestion=None` with
  `ValueError` instead of crashing with `TypeError`.** The validator
  in `_validate_scan_findings` (db_files.py:422) only rejected
  non-string suggestions when the value was not `None`, but the write
  path (`_upsert_finding`) called `len(suggestion)` unconditionally
  via `f.get("suggestion", "")` — and `dict.get` returns the explicit
  `None`, not the default. The HTTP routes only translate `ValueError`
  → 400, so a `null` suggestion became a 500. The validator now treats
  `None` the same as any other non-string value.
  (filigree-e74fecddd4)
- **Scan ingest: `finding.language` is validated.** The validator
  hardened every documented finding field except `language`. A
  non-string value either silently coerced to a string (SQLite type
  affinity on `int`) — violating the `FileRecordDict.language: str`
  contract — or leaked a raw `sqlite3.ProgrammingError` to the API
  consumer (on `list`/`dict`). `language` is now required to be a
  string when present; `None` is normalized to `""` for symmetry with
  the existing `f.get("language", "")` default.
  (filigree-2134b23fb9)
- **`scan_runs.findings_count` reflects findings observed by this
  run, not first-attribution membership.** `scan_findings.scan_run_id`
  is first-attribution-wins (set on insert, preserved on update by
  `_update_existing_finding`). The completion path counted via
  `SELECT COUNT(*) FROM scan_findings WHERE scan_run_id = ?`, so a
  re-scan that only re-saw existing findings reported
  `findings_count=0`. The count is now accumulated incrementally on
  the `scan_runs` row per batch (`findings_created + findings_updated`),
  which fixes both the re-scan case AND preserves the existing
  multi-batch contract where the orchestrator's final
  `complete_scan_run=True` call has empty findings.
  (filigree-f84f141e86)
- **`list_files_paginated` rejects invalid sort `direction`.** The
  rest of the query-shaping params raise `ValueError` for unknown
  values, but `direction` silently fell back to the default order:
  `direction='SIDEWAYS'` returned a default-ordered result instead of
  erroring. MCP's wrapper already validates strictly — this brings
  the core API in line, so HTTP/CLI callers get the same 400 the
  MCP surface returns. `None` still means "use default."
  (filigree-e53ce98110)
- **``setup_logging()`` always normalizes the surviving handler.** If
  another import attached a bare ``RotatingFileHandler`` for the target
  path before ``setup_logging()`` ran, the dedup loop reused that
  handler and returned early — leaving ``logger.level`` at ``NOTSET``
  (effective ``WARNING``) and the formatter unset, so MCP startup INFO
  events were silently dropped and warning/error records were written
  as plain text instead of JSONL. The function now applies
  ``_JsonFormatter`` and ``logger.setLevel(logging.INFO)``
  unconditionally on the surviving handler. Regression test pre-attaches
  an unconfigured matching handler and asserts the level/formatter and
  that an ``info`` emit lands as a single JSON line.
  (filigree-1ba7d648c6)
- **``batch-update-findings --json`` all-failed envelope no longer
  misclassifies storage failures as ``VALIDATION``.** The per-item
  loop already classifies failures as ``NOT_FOUND``/``VALIDATION``/
  ``IO``, but the all-failed branch hard-coded
  ``ErrorCode.VALIDATION`` for the envelope, breaking the closed-set
  contract callers branch on for retry policy: an ``sqlite3.Error``
  burst was reported as a client-input error. The envelope now
  derives its code from the per-item codes — ``IO`` wins (retryable);
  a single homogeneous code is preserved; only genuinely mixed
  failures fall back to ``VALIDATION``. Regression monkeypatches
  ``update_finding`` to raise ``sqlite3.OperationalError`` and asserts
  ``code == "IO"``. (filigree-c2aeba2946)
- **``add_dependency()`` no longer leaks an open SQLite write
  transaction on the duplicate path.** The idempotent ``INSERT OR
  IGNORE`` opens an implicit write transaction even when no row
  changes; the early ``return False`` skipped both ``commit()`` and
  ``rollback()``, so the lock lingered and any other connection's
  write failed with ``database is locked``. Now the duplicate path
  explicitly rolls back before returning. Regression covers
  ``conn.in_transaction`` and a second-connection write.
  (filigree-a0fc2b4ecc)
- **``get_plan()`` and ``get_release_tree()`` no longer silently
  truncate after 100 children.** Both delegated to
  ``list_issues(parent_id=...)``, which defaults to ``limit=100`` and
  always applies ``LIMIT ? OFFSET ?``; plans/releases with more than
  100 children lost the rest from the tree and from
  ``total_steps``/progress. Tree builders now use a private
  ``_list_all_children()`` that issues an unbounded child query.
  Regression covers 101 phases, 101 steps, and 101 release children.
  (filigree-07d55ee5e5)
- **``create_plan()`` rejects float and other non-int/non-str
  ``dep_ref`` values instead of silently misrouting.** Parsing
  coerced every ref through ``str(dep_ref)``, so ``0.1`` became
  ``"0.1"`` → ``phase 0 step 1`` — a real dependency on the wrong
  step instead of a ``ValueError``. Booleans and oddly-formatted
  strings (``"+0"``, ``" 1 "``) followed the same path. A new
  ``_normalize_dep_ref`` validator runs first: accepts ``int``
  (excluding ``bool``, non-negative) and ``str`` matching ``"N"`` or
  ``"P.S"`` with non-negative integer parts; rejects everything else
  with ``ValueError``. (filigree-6802ed02e0)
- **``verify_pid_ownership`` PID-file fallback no longer rejects the
  dashboard's own record when the port was stored as a separate
  metadata field.** When the OS-argv lookup is unavailable
  (constrained sandboxes without ``/proc``/``ps``/``wmic``), the
  fallback re-validated against ``cmd`` plus the appended
  ``--port <N>`` requirement — but callers (``hooks.py:420``,
  ``server.py:269``/``393``) record ``cmd="filigree dashboard"`` and
  the port in a separate ``port`` field, so the requirement could
  never match and live dashboards were misclassified as stale. The
  fallback now validates ``cmd`` against caller-supplied
  ``required_args`` only, trusting the parsed ``port`` field as
  metadata. The strict ``--port <N>`` match is retained on the
  OS-argv path, where the live argv actually carries the
  token. (filigree-403dd029c3)
- **``cleanup_stale_pid`` no longer clobbers a newer primary PID
  file on POSIX during quarantine restore.** The restore path used
  ``Path.rename(quarantine, pid_file)``, which on POSIX overwrites
  the destination — the comment claimed the rename would fail when a
  fresh primary already existed. Combined with a shared
  ``.removing`` quarantine name, two concurrent cleaners could
  collide and overwrite each other, and a newer writer's PID record
  could be replaced by an older quarantined copy. The quarantine
  filename is now unique per cleaner (suffix +
  ``os.getpid()`` + ``time.monotonic_ns()``), and the restore uses
  ``os.link``, which atomically fails with ``FileExistsError`` if
  the destination already exists; the quarantined copy is dropped in
  that case so the newer primary stands. (filigree-5aa3dc590a)
- **``read_pid_file`` no longer truncates float PIDs/ports or leaks
  ``OverflowError`` on non-finite JSON.** ``int(data["pid"])`` /
  ``int(raw_port)`` accepted ``float`` (silently truncating ``12.9``
  to ``12``), accepted ``bool`` (``True`` became PID ``1``), and
  raised ``OverflowError`` on values like ``1e999`` (parsed by
  ``json.loads`` as ``inf``) — and the outer handler caught only
  ``(ValueError, OSError)``. ``read_pid_file`` now uses a strict
  ``_coerce_pos_int`` helper that rejects ``bool``, ``float``, and
  non-digit strings, and only accepts integers in range; the outer
  handler also catches ``OverflowError`` for defense-in-depth. The
  documented contract that corrupt PID files return ``None`` now
  holds. (filigree-626c12d368)
- **Concurrent ``create_observation`` calls now return the existing live
  duplicate instead of raising ``IntegrityError``.** The dedup path was
  ``SELECT … if existing: return … else INSERT`` with no writer lock
  under ``isolation_level="DEFERRED"``, so two callers could pass the
  pre-insert SELECT, one INSERT would succeed and the other would
  surface ``UNIQUE constraint failed: index 'idx_observations_dedup'``
  through MCP as a generic database error — contradicting the
  documented contract. ``create_observation`` cannot wrap the whole
  function in ``BEGIN IMMEDIATE`` because callers invoke it with
  ``auto_commit=False`` inside their own transactions; instead, it now
  catches ``IntegrityError`` on the dedup index, rolls back, re-SELECTs
  the live duplicate, and returns it. Other ``IntegrityError`` causes
  (e.g. the still-present ``register_file`` race) propagate
  unchanged. (filigree-248ca4ff3f)
- **Concurrent ``dismiss_observation`` and
  ``batch_dismiss_observations`` no longer write duplicate audit rows.**
  Both paths read-then-deleted without serialization, so racing dismiss
  callers all wrote to ``dismissed_observations`` (no ``obs_id``
  uniqueness) and all reported success even though only one ``DELETE``
  actually removed a row. The SELECT/INSERT/DELETE sequence is now
  wrapped in ``BEGIN IMMEDIATE``. **Behavior change:** a concurrent
  second dismiss now raises ``ValueError("Observation not found")``
  instead of silently succeeding. (filigree-fd77d71a49)
- **Priority validators now reject non-int (incl. ``bool``) before any
  write, and ``update_issue`` validates regardless of equality with
  current.** ``create_issue`` and ``update_issue`` previously enforced
  only ``0 <= priority <= 4``. Float priorities (``2.5``, ``3.5``) passed
  the range check and were INSERTed before ``Issue.__post_init__``
  rejected them at hydration, leaving a row with
  ``typeof(priority)='real'`` durably committed while the caller saw a
  ``ValueError``. Bool priorities (``True``/``False``) slipped through
  both layers because Python's ``bool`` is an ``int`` subclass and
  ``0 <= True <= 4`` is ``0 <= 1 <= 4`` — silently coercing ``True`` to
  ``1``. Additionally, ``update_issue``'s short-circuit
  ``priority is not None and priority != current.priority and not
  (0 <= priority <= 4)`` skipped validation entirely when the new value
  equalled the current one by Python ``==``; because ``True == 1``,
  ``update_issue(priority=True)`` on a P1 issue was a silent no-op that
  looked successful. A new module-level ``_validate_priority_value`` is
  now invoked from ``create_issue`` and from ``update_issue`` whenever
  ``priority is not None``. (filigree-fa01508ee2)

- **``start_next_work`` no longer erases a pre-existing same-assignee
  claim on transition failure.** ``claim_issue`` is intentionally
  idempotent for the same identity (existing same-assignee claims
  re-claim successfully without changing the row), and ``claim_next``
  inherits that. ``start_next_work`` previously called
  ``_safe_release_claim`` unconditionally on any failure path —
  default-target template missing, ``canonical_working_status()`` raising,
  or the ``update_issue`` transition raising — wiping out a claim that
  ``alice`` had owned before the call. ``start_work`` already mirrors
  this scenario via ``newly_acquired_claim`` ownership tracking
  (filigree-31404d228f); ``start_next_work`` now uses the same contract
  via a new private ``_claim_next_with_prior`` helper that returns
  ``(Issue, prior_assignee)`` so the rollback closure can release only
  when this invocation acquired the claim. (filigree-fa01508ee2)

- **``SCHEMA_V1_SQL`` test fixture restored to faithful v1 shape.** The
  legacy v1-schema constant used by the migration test suite had drifted
  from what real v1 databases actually looked like: it was missing the
  ``issues.fields`` column and the seven core ``issues`` /
  ``dependencies`` indexes. ``migrate_v4_to_v5`` reads ``fields``, so
  applying the migration chain to an unpatched fixture failed with
  ``MigrationError: Migration v4 → v5 failed: no such column: fields``;
  ``tests/core/test_schema.py`` was masking this with a manual
  ``ALTER TABLE issues ADD COLUMN fields`` workaround. ``migrate_v5_to_v6``
  only adds the missing indexes inside its rebuild path, which is
  short-circuited when the parent self-FK is already correct (as it is in
  the fixture), so the indexes never reappeared. This is fixture-fidelity
  drift only — real v1 databases were created from the v1-era
  ``SCHEMA_SQL`` and already had these columns and indexes — but the
  drift hid whether migrations would self-heal a degraded database.
  ``SCHEMA_V1_SQL`` now matches the real v1 shape, the workaround
  ``ALTER TABLE`` is removed, and a new ``test_migrated_v1_matches_fresh_schema``
  asserts column- and index-level equivalence between a v1→CURRENT
  migrated database and a fresh ``SCHEMA_SQL`` to prevent recurrence.
  (filigree-c35e6fe9e9)

- **``undo_last()`` no longer restores a parent that creates a circular
  parent chain.** The ``parent_changed`` undo branch only checked that
  the previous parent still existed, bypassing the ancestor-walk cycle
  check that ``update_issue()`` enforces on the normal write path. With
  an interleaved sequence — ``C`` reparented ``A→B``, then ``A``
  reparented under ``C`` — undoing ``C``'s last reparent would set
  ``C.parent_id = A`` and form an ``A↔C`` cycle that subsequently
  truncated subtrees in ``get_release_tree``. The cycle check is now
  factored into ``IssuesMixin._would_create_parent_cycle()`` and called
  from both write paths. (filigree-0a8c3d38d7)

- **``undo_last()`` serializes the candidate-selection read against
  concurrent callers.** The SELECT-then-write candidate query used
  ``NOT EXISTS`` to skip already-undone events, but the ``undone``
  marker insert happened later — so two concurrent ``undo_last`` calls
  on separate connections could both pass the check and both write
  markers for the same target event, silently consuming one entry from
  the user's undo history. ``undo_last`` now opens a ``BEGIN IMMEDIATE``
  transaction at entry to acquire SQLite's write lock before the SELECT,
  with a ``finally`` clause that rolls back the lock on every
  ``undone=False`` early-return path. (filigree-f38d4e2874)

- **``fields_changed`` undo rejects ``NULL`` and non-object stored
  values.** The handler treated a missing ``old_value`` as ``"{}"`` and
  only validated that the value was parseable JSON — so an imported or
  hand-edited event with ``old_value=NULL``, ``"[]"``, or ``"text"``
  could make ``undo_last`` report success while clearing
  ``issues.fields`` to ``{}`` or writing a non-object JSON shape into
  the column, violating the model invariant. The handler now requires
  ``old_value`` to be present, parses it, asserts ``isinstance(parsed,
  dict)``, and persists a canonical ``json.dumps(parsed)``; otherwise it
  returns ``undone=False`` without writing. (filigree-cb4cd68f80)

- **``check_scan_cooldown()`` blocks active runs regardless of age.** The
  cooldown query applied a single 30-second ``updated_at`` cutoff to
  ``pending``, ``running``, and ``completed`` rows. Because ``updated_at``
  is only refreshed on status transitions, a scanner that ran longer
  than ``SCAN_COOLDOWN_SECONDS`` fell outside the cutoff and a duplicate
  trigger for the same ``(scanner, file_path)`` would pass the cooldown
  check and spawn a parallel process. The query now treats active rows
  (``pending``/``running``) as a singleton lock that always blocks, while
  ``completed`` rows continue to use the timestamp cutoff as a debounce
  window. (filigree-254e1299a3)

- **``update_scan_run_status()`` uses compare-and-swap on the prior
  status.** The method read the current status, validated the transition
  in Python, then issued ``UPDATE scan_runs SET ... WHERE id = ?`` —
  with no guard against the row changing between read and write. Two
  concurrent writers (dead-PID auto-fail in ``get_scan_status``,
  result-ingestion completion in ``db_files``) could both observe
  ``running``, both pass validation, and the second commit would
  silently overwrite the first writer's terminal state. The UPDATE now
  includes ``AND status = ?`` (the previously-observed value) and
  raises a stale-transition ``ValueError`` when ``rowcount == 0``, which
  the existing retry mitigation in ``process_scan_results`` already
  handles. (filigree-c835e730fb)

- **``write_atomic()`` no longer collides on a shared temp filename.**
  Concurrent writers to the same target both staged through
  ``target.tmp``: the second writer's ``open(...)`` truncated the first
  writer's in-flight stage, ``os.replace()`` could then install partial
  content or fail spuriously, and the failure-path ``unlink()`` could
  delete the other writer's stage. ``write_atomic`` now allocates a
  unique per-writer temp file via ``tempfile.mkstemp(dir=path.parent)``,
  matching the pattern already used by ``write_summary``. The error
  cleanup test was also tightened to ignore unique temp suffixes.
  (filigree-9bb033331a)

- **``get_mode()`` raises ``ValueError`` on non-string ``mode`` values.**
  A JSON-valid but non-hashable ``mode`` (e.g. ``"mode": []``) used to
  raise ``TypeError: unhashable type`` from the ``frozenset`` membership
  test, escaping the ``ValueError`` recovery paths in ``hooks.py``,
  ``cli_commands/admin.py``, and ``install_support/doctor.py``.
  ``get_mode`` now ``isinstance``-checks the value and raises the same
  invalid-mode ``ValueError`` for any non-string. (filigree-cff0de463f)

- **``read_conf()`` rejects malformed ``prefix``/``db``/``enabled_packs``
  values.** A ``.filigree.conf`` with ``{"prefix":"x","db":[]}`` used
  to round-trip through ``read_conf`` and then crash downstream when
  ``FiligreeDB.from_conf`` evaluated ``Path / data["db"]`` —
  ``TypeError: unsupported operand type(s) for /: 'PosixPath' and
  'list'``. Doctor's handled-failure path catches ``ValueError`` only,
  so the ``TypeError`` surfaced as an unhandled crash. ``read_conf``
  now type-checks ``prefix`` and ``db`` (non-empty strings) and
  ``enabled_packs`` (list of strings, when present). (filigree-0f0e76f4b6)

- **``_seed_future_release()`` tolerates corrupt ``fields`` JSON.**
  ``FiligreeDB.initialize()`` queries release rows with
  ``json_extract(fields, '$.version')``; a single release row with
  malformed ``fields`` raised ``OperationalError: malformed JSON`` and
  aborted the entire DB open, leaving the project unrecoverable
  without manual SQL surgery. The Future-singleton check is an
  idempotent maintenance step, not a place to enforce schema integrity,
  so it now guards ``json_extract`` with ``json_valid(fields)`` —
  matching the defensive handling already applied during migrations.
  (filigree-20ea5411e1)

- **Category SQL predicates now compare ``(type, status)`` pairs.**
  ``list_issues`` status-as-category filters and every blocker query
  (``_build_issues_batch`` blocked_by / open-blocker counts /
  ``is_ready``, ``has:blockers`` virtual label, ``get_ready`` /
  ``get_blocked`` / ``get_critical_path``, ``get_stats`` /
  ``_compute_virtual_has_counts``, ``archive_closed``) compared status
  names against a deduplicated category-state list. Once two enabled
  packs share a state name with different categories — the bundled
  templates already do this: ``incident.resolved`` is ``wip``,
  ``debt_item.resolved`` is ``done`` — an ``incident`` row in
  ``resolved`` matched both ``status="wip"`` and ``status="done"``
  filters, and dependents of an ``incident.resolved`` blocker
  hydrated with ``blocked_by=[]`` and ``is_ready=True``. New
  ``_get_type_states_for_category`` and ``_category_predicate_sql``
  helpers build type-aware predicates; the synthetic ``'archived'``
  blocker semantic is preserved. (filigree-b55aa3191f)

- **``claim_issue()`` normalizes the assignee identity.** Validation
  used ``assignee.strip()`` but the original padded value was stored
  and used as the CAS guard, so claiming with ``"  bob  "`` blocked a
  later canonical ``"bob"`` claim with "already assigned to '  bob  '".
  ``claim_issue`` now runs the same ``_normalize_assignee`` step that
  ``create_issue`` / ``update_issue`` already applied.
  (filigree-694f7e9bf8)

- **``start_work()`` rollback only releases claims it acquired.**
  ``claim_issue`` is idempotent for the same identity, so calling
  ``start_work`` against an issue you already own and hitting a
  transition failure used to wipe out the pre-existing claim
  (``release_claim`` clears the assignee unconditionally). The
  rollback path now captures the prior assignee and skips the
  release when the row was already claimed by the same identity
  before the call. (filigree-31404d228f)

- **``update_issue()`` rejects empty / whitespace-only titles.**
  ``create_issue`` rejected them; ``update_issue`` had no
  equivalent guard, so ``title=""`` or ``title="   "`` silently
  overwrote a valid title. The same invariant now applies on update.
  (filigree-365dff403e)

- **``ProjectStore.get_db()`` no longer races on first open.** In server
  mode, the lazy-open path was a check-then-open-then-assign with no
  lock, so two concurrent first requests for the same project key could
  both pass the cache-miss check, both run ``FiligreeDB.from_filigree_dir``
  (which migrates and seeds), and silently leak the loser's handle —
  only the winner of the assign race was cached. Adds an internal
  ``threading.Lock`` with a fast-path cache hit, double-checked open,
  and lock-guarded ``reload()`` eviction and ``close_all()``.
  (filigree-732f6b31e4)

- **Dashboard ``/api/reload`` response now matches the frontend contract.**
  Backend returned ``{"status": "ok", **diff}``; the shipped UI checks
  ``data.ok`` and ``data.projects``, so a successful reload rendered
  "Reload failed" and skipped the post-reload data refresh. The
  endpoint now returns ``{"ok": True, "status": "ok", "projects": N,
  **diff}`` — preserving every existing field for any direct API
  consumer. (filigree-173e76a28a)

- **Dashboard ``main()`` clears module-level ``_config`` on start and
  in ``finally``.** ``_config`` is a persistent dict; previously
  ``main()`` reset ``_db`` and ``_project_store`` but only
  ``dict.update()``-ed ``_config``, so any keys absent from the next
  project's config (notably ``name``, which ``read_config`` does not
  default) leaked from the previous run. ``/api/projects`` prefers
  ``_config["name"]`` over the live DB prefix, so a second in-process
  ethereal run could serve a stale project name.
  (filigree-154a23794c)

- **``filigree doctor`` now exits ``1`` when non-schema checks fail.**
  Previously the command counted failures, printed them, and fell through
  with exit code ``0`` — silently green for CI scripts and ``set -e``
  shells even when ``config.json`` was missing or ``context.md`` was
  stale. The schema-mismatch (v+1) exit code ``3`` still wins precedence;
  ``1`` is reserved for generic unfixed failures. ``--fix`` exits ``0``
  only when every fixable failure was repaired. (filigree-467d1e7487)

- **``filigree init`` and ``filigree doctor --fix`` now honour the
  ``.filigree.conf`` ``db`` path.** Both paths previously constructed
  ``FiligreeDB`` directly against the legacy ``.filigree/filigree.db``,
  bypassing the v2.0 anchor-aware constructors. On installs that
  relocate the DB via the conf, ``init`` re-runs and schema repairs
  silently created or migrated a phantom legacy DB while the project's
  actual DB stayed un-migrated. Both surfaces now use
  ``FiligreeDB.from_conf()`` when an anchor exists, falling back to
  ``FiligreeDB.from_filigree_dir()`` for legacy installs.
  (filigree-fa6309d551)

- **``filigree init`` on a legacy install now backfills
  ``.filigree.conf``.** The existing-project branch previously returned
  without writing the v2.0 anchor — only the fresh-init path created it
  — so re-running ``init`` to "upgrade" a legacy install left
  conf-anchored discovery (the strict ``find_filigree_conf`` walk-up)
  unable to locate the project. Re-init now writes the anchor when
  missing and never overwrites an existing custom anchor.
  (filigree-f22fc98687)

- **``filigree install --mode server`` now reloads a running daemon.**
  ``install`` called ``register_project()`` to write into ``server.json``
  but never POSTed ``/api/reload``, so the daemon kept serving the stale
  registry until manually restarted. Now matches the behaviour of
  ``filigree server register`` (which already calls
  ``_reload_server_daemon_if_running()``); reload failure is reported as
  a warning since registration is already committed.
  (filigree-80753e4b54)

- **``filigree dashboard --port`` and ``filigree ensure-dashboard
  --port`` now validate at the CLI boundary.** Both flags were declared
  as plain ``int`` and accepted ``0``, negative, and out-of-range
  values; in ``--server-mode`` the bogus value could be persisted into
  daemon state before the bind failed. They now use
  ``click.IntRange(1, 65535)``, mirroring ``filigree server start``.
  (filigree-31da65493c)

- **``filigree.__version__`` no longer reports ``"0.0.0-dev"`` in
  source-only execution paths.** When ``importlib.metadata.version``
  raises ``PackageNotFoundError`` (no installed ``dist-info``),
  ``src/filigree/__init__.py`` now falls through to ``tomllib`` and
  reads ``[project].version`` from the checkout's ``pyproject.toml``,
  gated on ``[project].name == "filigree"`` so a parent project's
  ``pyproject.toml`` cannot shadow ours. ``"0.0.0-dev"`` is reserved
  as the final fallback when neither installed metadata nor a source
  ``pyproject.toml`` is reachable. Affects the ``filigree --version``
  output and the ``/api/health`` ``version`` field for vendored or
  embedded deploys. (filigree-694e4821bd)

- **``filigree templates reload`` no longer crashes with a raw
  ``ValueError`` when ``.filigree/config.json`` is malformed.** The CLI
  path now mirrors the MCP handler: corrupt config surfaces as
  ``ErrorCode.VALIDATION`` (``--json``) or a clean stderr message
  (default), with exit code 1 in either case — no traceback. The reload
  also forces ``templates.list_types()`` to materialise the new registry
  and calls ``cli_common.refresh_summary`` so persistent ``context.md``
  reflects the change before the CLI process exits, closing the gap
  where ``Templates reloaded`` printed success while ``context.md``
  stayed stale. ``templates reload`` now accepts ``--json``, emitting
  ``{"status": "ok"}`` on success. (filigree-259e5b58ef,
  filigree-00359c8498)

- **Workflow CLI ``--json`` error paths now emit the 2.0 flat error
  envelope.** ``type-info``, ``transitions``, ``validate``, and
  ``explain-status`` previously printed plain stderr text on missing
  arguments even when ``--json`` was set, leaking format-violating
  output into JSON-consuming pipelines. All four now route through a
  shared ``_emit_error`` helper that emits ``{error, code}`` with
  ``ErrorCode.NOT_FOUND`` on ``--json`` and falls back to plain stderr
  otherwise. ``_guide_impl`` already followed this pattern; it now uses
  the same helper for consistency. (filigree-dfbcc84687)

- **``undo_last`` now reaches earlier reversible events when the newest
  reversible event has already been undone.** ``db_events.py::undo_last``
  selected the latest reversible event and then returned "already undone"
  if it had a covering ``undone`` marker, leaving older reversible
  history unreachable. Repro: change title, change priority, undo
  (priority restored), undo again returned "already undone" instead of
  restoring the title. The candidate-selection query now filters out
  already-undone events via ``NOT EXISTS`` so the fall-through to older
  history works. The post-select check is removed; the surfaced
  no-result reason changes from "Most recent reversible event already
  undone" to "No reversible events to undo". (filigree-a849860f2e)

- **Archived blockers no longer re-block dependents in readiness,
  hydration, and ``has:blockers``.** ``archive_closed`` writes the
  literal status ``'archived'`` (preserving ``closed_at``), but no
  bundled workflow declares ``'archived'`` as a done state. The
  blocker-active queries in ``db_planning.get_ready``, ``db_meta``
  (``get_stats``, ``_compute_virtual_has_counts``), and ``db_issues``
  (``blocked_by`` hydration, open-blocker counts, ``has:blockers``
  virtual label) all checked ``blocker.status NOT IN done_states``, so
  archiving a closed blocker re-blocked its dependents — every
  dependent that became ready when the blocker closed flipped back to
  blocked once archival ran. A new ``_blocker_done_states()`` helper
  returns the workflow done states plus ``'archived'`` and is used at
  every blocker-check call site; ``_get_states_for_category("done")``
  is preserved for archive selection (which must not include
  ``'archived'``) and label semantics. Mirrors the analytics-side fix
  shipped earlier in this section. (filigree-42045dd065)

- **``parent_changed`` events are now undoable.** The reparenting event
  was added to update/audit paths but ``_REVERSIBLE_EVENTS`` and the
  ``undo_last`` ``match`` ladder both omitted it, so ``undo_last`` on a
  reparented child returned "No reversible events" while the parent
  pointer remained set. ``parent_changed`` joins the reversible set
  and a new ``case`` restores ``parent_id`` from the event's
  ``old_value`` — empty/None becomes ``NULL``, a non-empty value is
  validated to still exist before being written back (we refuse rather
  than re-pointing at a deleted issue). (filigree-fc6bb28c23)

- **``import_jsonl`` now normalizes ISO timestamps to canonical UTC.**
  ``_now_iso`` always emits ``+00:00``, but the import boundary in
  ``db_meta.import_jsonl`` preserved ``created_at`` / ``updated_at`` /
  ``closed_at`` as supplied. SQLite TEXT compares lexicographically, so
  an imported ``2026-01-01T01:00:00+02:00`` (chronologically equal to
  ``2025-12-31T23:00:00+00:00``) sorted *after* ``2026-01-01T00:00:00+00:00``
  in ``get_events_since`` and ``archive_closed``'s ``closed_at < ?``
  cutoff — events were returned out of chronological order, and
  archival cutoffs miscompared. New ``db_base._normalize_iso_to_utc``
  applies on the import path to the columns these queries read:
  ``issues.created_at`` / ``updated_at`` / ``closed_at`` and
  ``events.created_at``. Other timestamp columns
  (``dependencies.created_at``, ``scan_runs.*_at``, ``scan_findings.*_at``,
  ``file_*.created_at``, ``comments.created_at``) are not yet
  normalized — extend the helper if a future query reads them
  lexicographically. Naive inputs are treated as UTC, ``Z`` suffixes
  are accepted. Existing rows with non-UTC TEXT remain in the DB
  as-is — re-import to canonicalize if needed; no automatic data
  migration is performed.
  (filigree-20911dfe6d)

- **Dependency undo handles ``dep_type`` values containing ``':'``.**
  ``db_planning.add_dependency`` and ``remove_dependency`` record event
  payloads as ``f"{dep_type}:{depends_on_id}"``; the corresponding undo
  cases in ``db_events.undo_last`` parsed via ``split(":", 1)``, which
  assigned the wrong target when ``dep_type`` itself contained ``':'``
  (a namespaced dep type round-trips back as a malformed payload).
  ``rsplit(":", 1)`` puts the issue ID on the right side regardless;
  the legacy "no colon" branch is preserved for older event rows. No
  CLI/MCP/HTTP surface currently exposes a non-default ``dep_type``,
  so the practical exposure is via ``import_jsonl`` of foreign-source
  events; the parse is now correct regardless of source.
  (filigree-2cd923c1d8)

- **CLI ``promote-finding`` now honours the global ``--actor`` flag.**
  ``cli_commands/files.py::promote_finding_cmd`` declared a command-local
  ``--actor`` defaulting to ``"cli"`` and skipped ``@click.pass_context``,
  so ``filigree --actor bot-1 promote-finding <id>`` silently recorded
  ``"cli"`` in the audit trail — every other write-path command pulls
  the sanitized actor from ``ctx.obj["actor"]`` set in ``cli.py``. The
  command now defaults the local option to ``None`` and falls back to
  the group actor; an explicit local ``--actor`` is sanitized through
  ``filigree.validation.sanitize_actor`` so blank/control/overlong
  values are rejected at the CLI boundary instead of reaching the
  observation row. (filigree-cb82dc6b37)

- **CLI ``list-files``, ``get-file-timeline``, ``list-findings``
  classify ``sqlite3.Error`` as ``IO``, not ``VALIDATION``.** Three
  read commands in ``cli_commands/files.py`` collapsed
  ``(ValueError, sqlite3.Error)`` into a single ``ErrorCode.VALIDATION``
  envelope. The unified envelope contract in ``types/api.py`` lets
  callers branch on ``ErrorCode``, so misclassifying transient infra
  failures (locked database, table missing) as caller-input errors
  prevented retry logic from kicking in. The handlers now split
  ``ValueError`` (``VALIDATION``) and ``sqlite3.Error`` (``IO``),
  matching the sibling ``get-file`` / ``get-finding`` pattern.
  (filigree-ef5db29b89)

- **CLI ``get-issue-files`` and ``add-file-association`` no longer
  leak raw SQLite tracebacks past the JSON envelope.**
  ``get-issue-files`` called ``db.get_issue_files()`` outside its
  ``sqlite3.Error`` handler, and ``add-file-association`` only caught
  ``KeyError`` around its ``db.get_file()`` / ``db.get_issue()``
  existence checks. A locked or corrupt database at those lines
  bypassed ``--json`` envelope handling and surfaced an uncaught
  ``OperationalError``. The lookups now share a single ``try`` block
  (or ``sqlite3.Error`` handler) so failures are reported as the
  ``ErrorCode.IO`` envelope consistently. (filigree-c7f94428c4)

- **``analytics.lead_time`` now treats archived issues as completed.**
  ``archive_closed()`` rewrites done-category status to the synthetic
  literal ``'archived'`` while preserving ``closed_at``. No bundled
  template declares ``'archived'``, so ``_resolve_status_category`` fell
  through to ``'open'`` and ``lead_time()`` returned ``None`` for those
  rows. ``get_flow_metrics()`` still counted them in ``throughput`` (via
  the dual-bucket scan) but silently dropped them from the lead-time
  average — the displayed mean was biased toward unarchived recent work.
  ``lead_time()`` now accepts ``status='archived'`` with non-null
  ``closed_at`` as completed, restoring symmetry between the throughput
  and lead-time aggregates. (filigree-93777393d7)

- **``analytics.get_flow_metrics`` ``by_type[t]['count']`` now reports
  the closed-issue count, not the cycle-time-sample count.** ``by_type``
  was built by appending only when ``_cycle_time_from_events`` returned
  a value, so a closed issue without a WIP→done transition (allowed by
  the task workflow's direct ``open → closed`` path in
  ``templates_data.py``) was silently absent from ``by_type`` even
  though it counted toward overall throughput. The CLI labels the field
  as ``"X closed"`` (``cli_commands/admin.py``) and the dashboard
  renders it under "Count", so the displayed number under-reported real
  closed work for any type with direct-close issues. ``get_flow_metrics``
  now tracks per-type closed counts independently from cycle-time
  samples and emits ``avg_cycle_time_hours=None`` when no samples
  exist. (filigree-744c36d621)

- **CLI ``changes --since`` normalizes non-UTC offsets to UTC.**
  ``cli_commands/planning.py::_normalize_iso_timestamp`` parsed the input
  but then returned the raw string, preserving its original offset.
  Stored events use ``datetime.now(UTC).isoformat()`` (always ``+00:00``)
  and ``db_events.get_events_since`` compares them as SQLite TEXT, so a
  cursor like ``2026-06-15T13:00:00+01:00`` was lexically *after* a
  stored event ``2026-06-15T12:30:00+00:00`` even though chronologically
  the event was 30 minutes later — events were silently skipped. The
  normalizer now parses, attaches UTC if naive, and returns
  ``parsed.astimezone(UTC).isoformat()``, mirroring the dashboard
  ``/activity`` route. (filigree-9aacfcd253)

- **CLI ``create-plan`` recursively validates ``steps`` and ``deps``.**
  ``cli_commands/planning.py::create_plan`` validated only the
  top-level shape, then handed unvalidated nested data to
  ``db.create_plan``. A non-dict step (``"steps": ["bad"]``) leaked an
  ``AttributeError`` traceback because ``step_data.get(...)`` was called
  on a string, and a JSON float dep like ``0.1`` was silently
  ``str()``-ed to ``"0.1"`` and reinterpreted as cross-phase ref
  ``phase 0, step 1``. The CLI now mirrors the MCP-layer rules
  (``mcp_tools/planning.py::_validate_plan_deps``): each step must be an
  object, ``steps`` must be a list, and each dep must be ``int >= 0`` or
  an ``"N"``/``"P.S"`` string — bools, floats, dicts, and malformed
  strings are rejected with a clean error. (filigree-c8eeb8f825)

- **CLI ``changes --limit`` rejects non-positive values.**
  The option was a plain ``int`` and ``_changes_impl`` passed
  non-positive values straight through to SQLite, which treats
  ``LIMIT -1`` as unbounded (and combined with the post-fetch
  ``raw[:limit]`` slice, ``--limit=-5`` returned all-but-the-last-5
  rows). The option now uses ``click.IntRange(min=1)`` on both the
  ``changes`` and ``get-changes`` commands, matching the positive-limit
  contract used by the dashboard and MCP surfaces.
  (filigree-302ab21704)

- **JSONL export/import now round-trips ``scan_runs``.**
  ``db_meta.py::_EXPORT_TABLES`` listed every persisted table except
  ``scan_runs``, and the corresponding import bucket was missing too. A
  full export/import cycle silently dropped scan-run history, status, and
  cooldown anchors — ``get_scan_run("run-1")`` raised ``KeyError`` after the
  round trip. The export now emits ``scan_run`` records and the importer
  rejects unknown ``status`` values up front (``pending``/``running``/
  ``completed``/``failed``/``timeout``) before inserting all 15 columns.
  (filigree-6160591254)

- **``import_jsonl()`` enforces the same label-namespace invariants as
  ``add_label()``.** The importer raw-inserted any label record, so a
  hand-crafted JSONL containing ``age:older_than_30d`` (a reserved virtual
  namespace) bypassed ``_validate_label_name()`` and shadowed the computed
  ``age:`` virtuals — ``list_labels()`` populates physical namespaces first,
  then ``setdefault("age", …)`` becomes a no-op. The label import loop now
  validates each row, skips invalid ones with a logger warning, and counts
  them under ``skipped_types["<invalid_label>"]`` so the failure is visible.
  (filigree-22ed219abc)

- **``add_label("review:foo")`` reports ``added=False`` on no-op re-adds.**
  The mutual-exclusivity DELETE for the ``review:`` namespace turned an
  idempotent re-add into delete-then-reinsert, so ``cursor.rowcount`` was
  always 1 and the function returned ``(True, "review:foo")`` — propagating
  to ``cli_commands/meta.py`` and ``mcp_tools/meta.py`` as a false
  ``"added"`` status. ``add_label()`` now short-circuits when the existing
  ``review:%`` set is exactly ``{normalized}``, returning
  ``(False, normalized)`` without touching the table. Genuine replacements
  still return ``(True, …)``. (filigree-c9d223e24e)

- **``observe`` CLI accepts the documented ``--file`` alias.**
  ``src/filigree/data/instructions.md`` documented
  ``filigree observe "note" --file=src/foo.py --line=42``, but
  ``cli_commands/observations.py`` only registered ``--file-path``, so the
  documented form failed with ``Error: No such option: --file``. The option
  now declares both ``--file-path`` and ``--file`` against the same
  ``file_path`` parameter. (filigree-6f8d9816b7)

- **Observation CLI commands surface ``sqlite3.Error`` as ``ErrorCode.IO``.**
  ``observe``, ``list-observations``, ``dismiss-observation``, and
  ``promote-observation`` only caught ``ValueError`` (or had no exception
  handler at all), so a transient SQLite failure (e.g.
  ``OperationalError("database is locked")``) escaped uncaught and
  ``--json`` callers got an empty stdout plus an unhandled traceback instead
  of the ``{"error": ..., "code": "IO"}`` envelope already used by
  ``mcp_tools/observations.py``, ``cli_commands/files.py``, and the
  neighboring ``batch-dismiss-observations`` command. Each affected handler
  now wraps its DB call in ``except sqlite3.Error`` and emits
  ``{"error": f"Database error: {e}", "code": ErrorCode.IO}``.
  (filigree-9ca1f5ace8)

- **``/api/loom/changes`` canonicalizes ``since`` to UTC before SQLite text-compare.**
  ``dashboard_routes/analytics.py::api_loom_changes`` only rewrote a trailing
  ``Z`` to ``+00:00`` and passed the raw offset string to
  ``db.get_events_since()``, which compares against stored ``+00:00`` ISO
  timestamps lexically.  Two ``since`` values representing the same instant
  but with different offsets returned different events.  The handler now
  parses ``since``, treats naive timestamps as UTC, and passes
  ``parsed.astimezone(UTC).isoformat()`` to the DB — mirroring the classic
  ``/api/activity`` route. (filigree-d808d8b70f)

- **``/api/loom/changes`` rejects ``?offset=`` query param.**
  The loom contract (``tests/fixtures/contracts/loom/changes.json``) declares
  the cursor is the ``since`` timestamp; ``offset`` is not exposed.  The
  handler reused the generic offset-pagination helper, validating ``offset``
  but then discarding it.  Any caller passing ``?offset=`` got an undocumented
  silent no-op.  ``api_loom_changes`` now parses only ``limit`` and rejects
  ``offset`` with a 400 ``VALIDATION``. (filigree-f0f47f5b9d)

- **Graph v2 ``?types=`` validates against registered template types.**
  ``dashboard_routes/analytics.py::_parse_graph_v2_params`` validated the
  filter against ``{i.type for i in issues}``, so a registered type with no
  current issues (e.g. ``release`` in a fresh project, or ``bug`` after all
  bugs are closed) was rejected as "Unknown".  Validation now uses
  ``db.templates.list_types()`` — the canonical source already used for
  ``/api/types`` and issue creation. (filigree-68c24cee62)

- **Scanner CLI surfaces ``ForeignDatabaseError`` instead of a generic "not initialized" line.**
  ``cli_commands/scanners.py::_get_filigree_dir`` caught
  ``(ProjectNotInitialisedError, Exception)`` and returned ``None``, which
  every caller then converted into ``"Project directory not initialized"``
  with ``ErrorCode.NOT_INITIALIZED``. Because ``ForeignDatabaseError`` is a
  ``ProjectNotInitialisedError`` subclass, the rich "Refusing to latch onto
  another project's filigree database…" diagnostic — explicitly contracted by
  ``test_doctor.py::test_foreign_database_is_reported_with_specific_message``
  — was silently dropped for ``list-scanners``, ``trigger-scan``,
  ``trigger-scan-batch``, and ``preview-scan``. The helper now raises and a
  new ``_resolve_filigree_dir_or_die`` emits ``str(exc)`` so the rich message
  reaches the user. (filigree-ae5a8db639)

- **``report-finding`` validates field types and maps ``ValueError`` to ``VALIDATION``.**
  ``cli_commands/scanners.py::report_finding_cmd`` only checked truthiness,
  so a JSON ``"severity": []`` raised ``TypeError`` from ``severity not in
  VALID_SEVERITIES`` (unhashable membership), and non-string-but-truthy
  ``path`` / ``rule_id`` / ``message`` / ``line_start`` / ``line_end`` slipped
  past to the DB validator — whose ``ValueError`` was then mismapped to
  ``ErrorCode.IO`` (the HTTP route at ``dashboard_routes/files.py`` already
  mapped the same exception to ``VALIDATION``). The CLI now isinstance-checks
  every field, splits the ``ValueError`` and ``sqlite3.Error`` branches, and
  emits ``VALIDATION`` for caller-side malformed input. (filigree-a59f82c87b)

- **``_read_graph_runtime_config`` reads ``.filigree/config.json`` from the project root, not the DB's parent.**
  ``dashboard_routes/common.py::_read_graph_runtime_config`` derived the config
  directory from ``db.db_path.parent``, which is the ``.filigree/`` metadata dir
  only for the legacy ``.filigree/filigree.db`` layout. For ``.filigree.conf``
  projects with a relocated DB (e.g. ``storage/track.db``), ``read_config``
  silently looked in ``storage/`` and fell back to defaults — so
  ``graph_v2_enabled`` / ``graph_api_mode`` from ``.filigree/config.json`` were
  ignored on ``/api/config`` and ``/api/graph``. Now derives the config
  directory from ``db.project_root / FILIGREE_DIR_NAME`` when present, falling
  back to ``db.db_path.parent`` for direct construction. (filigree-a9bedb09a9)

- **``/api/files/hotspots`` and ``/api/scan-runs`` cap ``limit`` at ``_MAX_PAGINATION_LIMIT``.**
  Both endpoints parsed ``limit`` via ``_safe_int`` with only ``min_value=1`` —
  the same bypass-of-``_parse_pagination`` pattern fixed in filigree-393cfab62c
  for ``/api/files``. A request like ``?limit=9223372036854775808`` passed route
  validation and then crashed sqlite3 binding with ``OverflowError: Python int
  too large to convert to SQLite INTEGER``, surfacing as 500. The two limit-only
  routes now pass ``max_value=_MAX_PAGINATION_LIMIT`` so oversize values return
  a 400 ``VALIDATION`` response before reaching the database layer.
  (filigree-873962aa58)

- **``promote_observation`` now serializes the idempotency check + create_issue.**
  ``db_observations.py:promote_observation`` checked ``json_extract(fields,
  '$.source_observation_id')`` with a plain read on an autocommit transaction,
  then called ``create_issue`` — two concurrent ``FiligreeDB`` connections could
  both pass the check and both insert, leaving two issues for one observation
  (no UNIQUE constraint backstop). The check + insert is now wrapped in
  ``BEGIN IMMEDIATE`` so peers queue on the writer lock and see the committed
  row when they retry the check (mirrors ``db_scans.py:trigger_scan_locked``).
  (filigree-58aa8fb4ac)

- **``promote_observation`` tolerates corrupt ``issues.fields`` JSON on unrelated rows.**
  The idempotency lookup ran ``json_extract`` over the entire ``issues`` table,
  so one malformed ``fields`` row anywhere — the kind ``_safe_fields_json``
  exists to absorb at the read path (``db_issues.py:99``) — raised
  ``OperationalError: malformed JSON`` and broke every promote. The query now
  guards with ``json_valid(fields)``, restoring the project-wide convention of
  surviving corrupt JSON. (filigree-9bb842088d)

- **Pagination params can no longer overflow SQLite ``LIMIT``/``OFFSET`` binds.**
  ``dashboard_routes/common.py::_parse_pagination`` only enforced minimums, so
  ``?limit=9223372036854775807`` (or any ``offset`` past int64 max) propagated
  unchecked into ``LIMIT ? OFFSET ?`` — and even at int64 max the routes that
  overfetch by ``limit + 1`` (``dashboard_routes/issues.py:693``) overflowed on
  bind, raising an uncaught ``OverflowError`` from ``sqlite3`` and surfacing as
  500. Added module caps ``_MAX_PAGINATION_LIMIT = 10_000`` and
  ``_MAX_PAGINATION_OFFSET = 2**63 - 2``; extended ``_safe_int`` with an
  optional ``max_value``. Pagination violations now return 400 ``VALIDATION``.
  (filigree-393cfab62c)

- **``_semver_sort_key`` falls back to title when ``version`` is non-empty but unparseable.**
  ``dashboard_routes/releases.py`` previously gated title-based Future detection on
  ``not version`` and built ``text = version or title`` for loose-semver fallback,
  so any non-empty junk string in the imported ``version`` field blocked both
  paths. Releases with ``{"version": "planned", "title": "v2.0.0"}`` or
  ``{"version": "junk", "title": "Future"}`` now sort correctly. Priority is
  reordered: version Future → version semver (strict, then loose) → title
  Future → title loose semver → non-semver. Whitespace-only ``version`` is
  treated as absent. (filigree-5e1e2e0eae)

- **``filigree get-comments --json`` wraps items in the ListResponse envelope.**
  ``get-comments`` was missed by the Phase E1 ``--json`` migration and emitted a
  bare JSON list instead of ``{items, has_more}``. The CLI now matches MCP's
  ``_list_response(...)`` shape and the loom HTTP ``GET /api/loom/issues/{id}/comments``
  contract. Per-item shape (classic ``CommentRecord`` with ``id``) is preserved
  to maintain CLI↔MCP parity. (filigree-d2263e721d)

- **``filigree batch-close --json`` omits ``newly_unblocked`` when empty.**
  The CLI emitted ``"newly_unblocked": []`` unconditionally, contradicting
  ``BatchResponse``'s ``NotRequired`` rule (``types/api.py:392-398``) and the
  loom ``POST /api/loom/batch/close`` fixture, both of which require the field
  to be omitted entirely when no issue was unblocked. The CLI now mirrors
  ``mcp_tools/issues.py::_handle_batch_close``: present only when at least one
  issue became ready as a result of the close. (filigree-893edb553a)

- **``filigree server start --port`` validates the TCP range at the CLI boundary.**
  The Click option used bare ``type=int``, so ``--port 0`` silently fell
  through ``port or config.port`` (server.py:248) and out-of-range values
  (negative or >65535) bypassed ``ServerConfig.__post_init__`` because the
  daemon path assigns to ``config.port`` after construction. ``--port`` now
  uses ``click.IntRange(1, 65535)`` and rejects invalid input with a clear
  usage error before reaching the daemon launch path. (filigree-1e1cb5eeeb)

- **CLI startup failures honour ``--json`` envelope.**
  ``cli_common.get_db()`` now emits the 2.0 flat ``{error, code}`` envelope on
  stdout when the active invocation passed ``--json`` and project discovery
  or DB-open fails — instead of leaking a plain-text stderr message that
  every JSON-capable command (e.g. ``stats --json``) inherited at startup.
  Mapping: ``ProjectNotInitialisedError`` → ``NOT_INITIALIZED``,
  ``SchemaVersionMismatchError`` → ``SCHEMA_MISMATCH``,
  ``OSError``/``sqlite3.Error`` → ``IO``,
  ``ValueError``/``TypeError``/``KeyError`` → ``VALIDATION``. Plain-text
  output (without ``--json``) is unchanged. (filigree-3741fc571b)

- **``_safe_json_loads``: out-of-band corruption flag, no in-band sentinel keys.**
  ``Issue`` / ``FileRecord`` / ``ScanFinding`` no longer falsely strip user
  data named ``_fields_error`` or ``_metadata_error``. The helper now
  returns a ``_ParsedJson`` (``dict`` subclass) carrying a
  ``_filigree_corrupt`` attribute; ``models.py::to_dict()`` consults the
  attribute via duck-typing rather than mining a user-visible dict key.
  Custom fields/metadata with those legacy names round-trip unchanged.
  (filigree-7ea6b80f3b §1)

- **``_safe_json_loads`` handles undecodable bytes from BLOB-typed columns.**
  SQLite's flexible typing can return ``bytes`` for JSON-text columns when
  the row contains BLOB data; invalid-UTF-8 input previously raised
  ``UnicodeDecodeError`` past the safety net. The helper now accepts
  ``str | bytes | None`` and treats ``UnicodeDecodeError`` as corruption,
  matching the documented contract. (filigree-7ea6b80f3b §2)

- **``--json`` detection ignores tokens after Click's ``--`` terminator.**
  The raw-argv scan that drives JSON-mode envelope emission previously
  matched any literal ``--json`` token, including positional values that
  follow ``--`` (e.g. ``filigree create -- --json`` makes the title
  ``"--json"``). Group-level ``--actor`` validation and ``get_db()``
  startup failures now share one helper that slices at the first ``--``
  before the membership test, so non-JSON invocations get plain Click
  usage / stderr output. (filigree-df988a37fc)

## [2.0.0] — 2026-04-28 — The Filigree Component of Loom

Filigree 2.0 reframes the product from "standalone issue tracker with an HTTP API"
to "standalone issue tracker, plus a loosely-coupled component of the Loom federation."
This release adds a stable HTTP generation contract (`/api/loom/*`), forward-migrates
MCP and CLI to the loom vocabulary, ships composed operations (`start_work` /
`start_next_work`), brings CLI to full parity with MCP, and adds schema-mismatch UX
across every entry point.

### Changed (BREAKING — MCP)

- **MCP forward-migrated to the loom vocabulary (Phase D of the 2.0 federation work package).**
  - **Single-issue tool input field renamed.** ``id`` → ``issue_id`` across
    ``get_issue`` / ``update_issue`` / ``close_issue`` / ``reopen_issue`` /
    ``claim_issue`` / ``release_claim`` / ``undo_last`` / ``add_comment`` /
    ``get_comments`` / ``add_label`` / ``remove_label`` /
    ``get_issue_events``. ``list_issues.parent_id`` filter renamed to
    ``parent_issue_id``.
  - **Batch tool input field renamed.** ``ids`` → ``issue_ids``
    (``batch_update`` / ``batch_close`` / ``batch_add_label`` /
    ``batch_add_comment``); ``ids`` → ``observation_ids``
    (``batch_dismiss_observations``). ``batch_update_findings`` already used
    ``finding_ids`` — no change.
  - **Dependency tools renamed.** ``add_dependency`` /
    ``remove_dependency`` take ``from_issue_id`` / ``to_issue_id`` (was
    ``from_id`` / ``to_id``).
  - **Observation tools renamed.** ``dismiss_observation`` /
    ``promote_observation`` take ``observation_id`` (was ``id``).
  - **Create-issue parent rename.** ``create_issue.parent_id`` /
    ``update_issue.parent_id`` input fields renamed to ``parent_issue_id``.
  - **SlimIssue projection.** ``SlimIssue.id`` renamed to
    ``SlimIssue.issue_id``. Affects every MCP tool emitting slim
    projections (``search_issues``, ``get_ready``, ``get_blocked``, batch
    response ``succeeded[]`` / ``newly_unblocked[]``, ``close_issue``
    response ``newly_unblocked[]``).
  - **Batch container keys unified.** Legacy
    ``{updated|closed, errors, count}`` and ``BatchActionResponse.results``
    consolidated to ``{succeeded, failed}`` per ``BatchResponse[T]``.
    ``batch_close`` / ``batch_update`` return
    ``BatchResponse[SlimIssue]`` (succeeded carries slim projections);
    ``batch_add_label`` / ``batch_add_comment`` /
    ``batch_dismiss_observations`` / ``batch_update_findings`` return
    ``BatchResponse[str]``.
  - **List response envelope unified.** Every MCP list tool
    (``list_issues`` / ``search_issues`` / ``get_ready`` / ``get_blocked`` /
    ``get_comments`` / ``get_issue_events`` / ``get_changes`` /
    ``list_files`` / ``list_findings`` / ``list_observations`` /
    ``list_scanners`` / ``list_packs`` / ``list_types`` / ``list_labels``)
    returns ``ListResponse[T] = {items, has_more, next_offset?}``. Drops
    legacy siblings (``total``, ``stats``, ``errors``, ``hint``,
    ``limit``, ``offset``); ``list_labels`` flattens its
    dict-of-namespaces to a list of ``{namespace, type, writable,
    labels}`` entries.
  - **Workflow tools renamed.** ``get_workflow_states`` →
    ``get_workflow_statuses`` (response key ``states`` → ``statuses``);
    ``explain_state`` → ``explain_status`` (input arg ``state`` →
    ``status``, response key ``state`` → ``status``). CLI commands
    follow: ``workflow-states`` → ``workflow-statuses``,
    ``explain-state`` → ``explain-status``.
  - **``get_issue.include_files`` defaults to ``False``.** Aligns with the
    loom HTTP ``GET /api/loom/issues/{issue_id}`` contract; consumers
    needing the file-association payload pass ``include_files=true``.

### Added

- **MCP composed operations: ``start_work`` and ``start_next_work``.**
  Atomic claim+transition tools backed by ``FiligreeDB.start_work`` /
  ``FiligreeDB.start_next_work``. ``target_status`` defaults to the
  type's ``canonical_working_status()``; ambiguous (multi-wip) types
  raise ``AmbiguousTransitionError`` so the caller specifies. Rollback
  uses compensating actions — if the transition fails after a
  successful claim, ``release_claim`` is called to restore the prior
  assignee, and the audit trail preserves both ``claimed`` and
  ``released`` events.
- **``TypeTemplate.canonical_working_status()`` helper.** Returns the
  unique wip-category status name; raises
  ``AmbiguousTransitionError`` on multi-wip types and
  ``InvalidTransitionError`` on no-wip types.

### Notes

- **Classic HTTP unchanged.** Federation consumers using
  ``/api/loom/*`` are unaffected (the loom shape has been the
  contract since Phase C).
- **MCP clients pinning to legacy schemas** (``id`` / ``ids`` /
  ``{updated, errors}``, ``state`` arg, ``WorkflowStatesResponse``,
  ``StateExplanation``, ``IssueListResponse``, ``SearchResponse``,
  ``BatchUpdateResponse``, ``BatchCloseResponse``,
  ``BatchActionResponse``) must update to the new shapes. The
  legacy TypedDicts have been removed from ``filigree.types.api``.

- **Cross-surface error-envelope parity test module (`tests/util/test_cross_surface_parity.py`).** Sixteen tests fire the same logical bad input at dashboard (`AsyncClient`), MCP (in-process tool handler), and CLI (`CliRunner --json`) and assert the three surfaces emit the same `ErrorCode`. Covers the seven bed-down cases from Stages 1 + 2a (`NOT_FOUND` on get, `VALIDATION` on out-of-range priority / unknown type / blank actor / blank assignee, `INVALID_TRANSITION` on bad status, `CONFLICT`/`INVALID_TRANSITION` on already-closed, batch-per-item envelopes) plus four `POST /api/v1/scan-results` envelope pins — the dashboard-only route is the highest-risk Clarion-facing hop for Stage 2B and has no staging environment, so these tests are the pre-release contract. Twelve tests pass; four are strict `xfail` marking 2B worklist items (CLI `--priority`/`--actor` Click-layer validators bypassing the 2.0 envelope; CLI `close --json` emitting a batch-shape wrapper for single-id close; dashboard `batch_update` returning `errors` while MCP returns `failed` — wire-contract unification scope for 2B). Each divergence is also filed as an `observe` for the 2B rebaseline's work list.

- **Stage 2B task 2b.3c — CLI `filigree close <id> --json` emits the flat 2.0 envelope on single-id close failure.** Previously the CLI always returned the batch-shape wrapper (`{closed, unblocked, errors:[{id,error,code}]}`) even for a single-id close. When `len(issue_ids) == 1` and the close failed, the command now emits the flat envelope (`{"error": ..., "code": ...}`) matching the dashboard and MCP shapes. Multi-id close calls (`filigree close a b c --json`) keep the batch-shape wrapper since batching is the documented behaviour for N≥2. Converts `TestAlreadyClosedParity::test_cli_emits_flat_envelope` from strict-xfail to passing.

- **Stage 2B task 2b.3b — CLI `--actor` (group-level) now emits the 2.0 envelope on `--json`.** `filigree --actor "   " update <id> --title x --json` previously emitted Click's stderr usage error (`Error: Invalid value for '--actor': actor must not be empty`) with exit 2; callers using `--json` got no JSON output. The group now uses a `_FiligreeGroup` subclass whose `parse_args` stashes the raw invocation into `ctx.meta["filigree_raw_args"]`; the group callback checks that stash for `--json` (since `ctx.args`/`ctx.protected_args` are empty at group-callback time, and `sys.argv` is untouched by `CliRunner`), and emits the envelope when found. Non-JSON invocations keep the existing `click.BadParameter` → stderr behaviour. Converts `TestBlankActorUpdateParity::test_cli_emits_envelope` from strict-xfail to passing.

- **Stage 2B task 2b.3a — CLI `--priority` now emits the 2.0 envelope on `--json`.** `filigree create --priority 99 --json` previously emitted Click's stderr usage error with exit 2, because `click.IntRange(0, 4)` intercepted the value at parse time and bypassed any `--json`-aware output. The range check moved into the command body where `as_json` is reliably available (callbacks fire in cmdline order — `--priority 99 --json` processes priority first, so `as_json` may not yet be in `ctx.params` at callback time). Converts `TestPriorityOutOfRangeCreateParity::test_cli_emits_envelope` in the parity module from strict-xfail to passing. CLI now matches dashboard and MCP on out-of-range priority: `{"error": ..., "code": "VALIDATION"}` with exit 1.

- **Stage 2B task 2b.4 — `classify_value_error` boundary rule (docs + enforcement test).** The substring-based ValueError classifier (`src/filigree/types/api.py`) now documents its boundary rule explicitly in its docstring: **state-machine sites MUST use the helper** (enumerated: `db._batch_with_transition_errors`, MCP update/close/reopen handlers, CLI update/close/reopen `--json` paths, dashboard PATCH/close/reopen routes); **input-validation sites MUST hardcode `VALIDATION`** (enumerated: `dashboard_routes/common.py`, `mcp_tools/meta.py/files.py/scanners.py/observations.py`). `tests/util/test_classify_value_error_boundary.py` greps the input-validation modules for `classify_value_error` imports and fails CI if the rule is broken in a future change. Prevents the foreseeable drift where someone reaches for the helper at a site where it would mis-classify future error-message additions.

- **Stage 2B task 2b.-1 — `POST /api/v1/scan-results` success-shape pin.** `TestScanResultsEnvelope::test_success_shape_empty_findings` asserts the exact `ScanIngestResult` key set (eight keys) and value-type invariants returned on 200 for a valid empty-findings body. Closes the gap identified in the 2B rebaseline §4: the four pre-existing error-path tests pinned 400+`VALIDATION` shapes, but the 200 shape was unpinned. Any 2B task that changes `db.process_scan_results(...)`'s return dict must update this test in the same commit; absent a Clarion staging environment, this is the concrete pre-release contract.

### Changed (BREAKING — CLI)

- **CLI forward-migrated to the loom vocabulary (Phase E of the 2.0 federation work package).**
  - **`add-label` arg order reversed.** ``filigree add-label <label>
    <issue_id>`` (was: ``<issue_id> <label>``). Aligns with the
    already-correct ``batch-add-label <label> <issue_ids...>`` order.
    Scripts using the old positional order must update. This is the
    only positional-arg break in Phase E.
  - **`--json` list envelopes unified.** All CLI list commands now emit
    ``{items, has_more, next_offset?}`` (``ListResponse[T]``) on
    ``--json``. Previously some emitted bare lists or legacy shapes
    (``{issues: [...]}``, ``{observations: [...]}``). Clients pinning
    to legacy ``--json`` list output must re-pin.
  - **Slim-issue ``--json`` projection uses ``issue_id``.** Slim
    projections in ``ready --json``, ``blocked --json``,
    ``search --json``, ``batch-update --json``, ``batch-close --json``
    now use ``issue_id`` (was ``id``), matching the loom/MCP shape.

### Added (CLI)

- **Phase E CLI commands — observations, files, scanners (Phase E2).**
  Every MCP tool now has a CLI counterpart. New modules:
  - ``cli_commands/observations.py`` — ``observe``,
    ``list-observations``, ``dismiss-observation``,
    ``promote-observation``, ``batch-dismiss-observations``.
  - ``cli_commands/files.py`` — ``list-files``, ``get-file``,
    ``get-file-timeline``, ``get-issue-files``,
    ``add-file-association``, ``register-file``, ``list-findings``,
    ``get-finding``, ``update-finding``, ``promote-finding``,
    ``dismiss-finding``, ``batch-update-findings``.
  - ``cli_commands/scanners.py`` — ``trigger-scan``,
    ``trigger-scan-batch``, ``get-scan-status``, ``preview-scan``,
    ``report-finding``, ``list-scanners``.
  All commands emit loom-shape JSON on ``--json``.

- **Verb-noun CLI aliases (Phase E3, permanent).** Every existing
  short-form CLI command gains a permanent verb-noun alias matching the
  MCP tool name: ``get-ready``, ``get-blocked``, ``get-plan``,
  ``get-changes``, ``get-critical-path``, ``get-valid-transitions``,
  ``validate-issue``, ``get-workflow-guide``, ``get-type-info``,
  ``list-types``, ``list-packs``, ``list-labels``,
  ``get-label-taxonomy``, ``update-issue``, ``get-issue``,
  ``list-issues``, ``release-claim``, ``get-issue-events``,
  ``undo-last``. Both names appear in ``--help``; both are stable.

- **``filigree start-work`` and ``filigree start-next-work`` (Phase E4).**
  CLI wrappers for the D6 composed operations. Backed by
  ``FiligreeDB.start_work`` / ``start_next_work`` (same path MCP uses).
  Returns the updated issue dict on success; ``ErrorResponse`` on
  failure.

- **``filigree show --with-files`` flag (Phase E5).** ``filigree show
  <id>`` no longer includes file associations by default — matches
  ``get_issue.include_files=False`` (D4) and loom HTTP
  ``GET /api/loom/issues/{issue_id}`` (since C3). Pass ``--with-files``
  to opt in.

- **CLI↔MCP↔HTTP parity battery (Phase E7).** ``tests/util/
  test_cross_surface_parity.py`` extended with five new envelope-
  equivalence tests covering the new CLI commands:
  ``list-observations`` CLI↔MCP, ``list-files`` CLI↔loom-HTTP,
  ``start-work`` error and success shapes CLI↔MCP. The Phase D gate
  ("MCP↔HTTP parity") is now "CLI↔MCP↔HTTP parity".

### Notes (Phase E)

- **Classic HTTP unchanged.** The C2 ``test_container_key_parity``
  strict xfail remains strict-xfailed; Phase E did not touch classic.
- **CLI clients pinning to legacy ``--json`` shapes** (bare lists,
  ``{issues: [...]}``, ``{id: ...}`` in slim projections,
  ``<issue_id> <label>`` positional order on ``add-label``) must
  update. The new loom-shape envelopes are the stable forms going
  forward.

### Changed (envelope unification)

- **Stage 2B task 2b.0 — `BatchFailureDetail` retired in favour of `BatchFailure` (Python API only; no wire-contract change).** The unused Stage 1 `BatchFailure.item_id` field reverted to `id` before the type got any live wire consumers, so `db_issues.py` batch constructions, the three legacy response types (`BatchUpdateResponse`, `BatchCloseResponse`, `BatchActionResponse`), and the `valid_transitions` enrichment at `db_issues.py:899` all migrate cleanly. The HTTP/MCP/CLI surfaces emit the same `{id, error, code, valid_transitions?}` shape for batch failures. `BatchFailure` gains the optional `valid_transitions: NotRequired[list[TransitionHint]]` field that `BatchFailureDetail` previously carried. Python consumers importing `BatchFailureDetail` from `filigree.types.api` now raise `ImportError`; migrate to `BatchFailure`.

### Fixed (envelope bed-down)

- **2.0 envelope bed-down — residual cross-surface parity fixes.**
  - `db._batch_with_transition_errors` (used by `batch_close` and `batch_update`) now routes per-item `ValueError`s through `classify_value_error` instead of hardcoding `ErrorCode.INVALID_TRANSITION`. Validation-class errors (`"Field validation failed: …"`, `"Priority must be between 0 and 4"`, `"Cannot close issue {id}: hard-enforcement gate requires fields: …"`) now surface as `VALIDATION` across dashboard batch endpoints, MCP `batch_*` tools, and CLI `--json` output. `valid_transitions` enrichment is gated on `code == INVALID_TRANSITION`, so validation failures no longer get a meaningless transition list attached.
  - CLI `claim` and `claim-next` now pre-validate the `--assignee` value, matching the MCP handlers (`mcp_tools/issues.py:603-604`, `646-647`). A blank or whitespace assignee now returns `ErrorCode.VALIDATION` at both surfaces; previously the CLI fell through to `db.claim_*`, caught the "Assignee cannot be empty" `ValueError`, and miscoded it as `CONFLICT`. The `ValueError` handler in `claim-next` now emits `VALIDATION` (the only remaining propagated case) to match the MCP handler's code.

- **filigree-1c7b2776a5** (P1): `release_claim()` is now atomic — it no longer silently erases a newer claim. Previously the method read the current assignee via `get_issue()`, then issued an unconditional `UPDATE ... WHERE id = ?`. A concurrent `claim_issue()` or `update_issue(assignee=...)` landing in the gap between those two statements had its write overwritten by the unguarded clear, with no error surfaced to either caller. The sibling `claim_issue()` has used compare-and-swap for exactly this reason. `release_claim()` now matches: `SELECT assignee` once, then `UPDATE ... WHERE id = ? AND assignee = ?` with the observed value, and branches on `rowcount == 0` to distinguish deleted (`KeyError: not found`), already-released (`ValueError: already released`), and reassigned (`ValueError: reassigned to 'X'`) — the latter two land in the existing 409 `CLAIM_CONFLICT` path at every boundary (dashboard HTTP, MCP, CLI) with no caller changes.

- **Core subsystem cluster (1 P2 + 1 P3 bug)**:
  - **filigree-3449322141**: `FiligreeDB.from_filigree_dir` and `from_conf` no longer leak the SQLite connection when `initialize()` raises. The classmethods now wrap `db.initialize()` in try/except, call `db.close()` on failure, and re-raise. `initialize()`'s first statement opens the connection lazily via `get_schema_version()` → `self.conn`, so a downstream failure (schema newer than this build supports, migration error, seed failure) previously exited the factory before `return db`, leaving the caller without a handle to close. The leaked connection — plus its WAL/SHM sidecar files — survived until the interpreter exited or GC collected the instance, which in long-lived processes like the dashboard never happened.
  - **filigree-29bc9117ab**: `filigree/__init__.py` no longer eagerly re-exports `FiligreeDB` and `Issue`. Both are still accessible on the package (`filigree.FiligreeDB`, `filigree.Issue`) but resolve via a PEP 562 module-level `__getattr__`. Previously, any import of `filigree` or of a lightweight submodule (e.g. `from filigree import migrations`) paid for loading the entire DB mixin stack (`db_issues`, `db_files`, `db_scans`, `db_workflow`, etc.) and the templates loader. Import-time cost drops to the package's own trivial work for callers that do not touch the DB API.

- **Analytics-correctness cluster (1 P1 + 1 P3 bug)**:
  - **filigree-fc30d6efd9**: `/api/graph` no longer silently truncates large projects at a hidden 10000-row preload. The handler now paginates `list_issues` through every row before building `issue_map` and running v2 filters, so projects beyond the old cap stop losing nodes and `scope_root` validation stops false-404ing for any issue past the first 10000. The page size (`_GRAPH_LIST_PAGE_SIZE = 1000`) is a module-level constant so tests can exercise pagination boundaries.
  - **filigree-0fe4558ea9**: `get_flow_metrics` no longer double-counts issues returned by both the `status="closed"` (template-defined done states) and `status="archived"` (literal) scans. The two buckets overlap when a workflow pack defines an `archived` done state, or when `archive_closed()` runs mid-scan. Results are now deduped by `issue.id` before throughput count and cycle-/lead-time averaging, so a single archived issue contributes exactly one observation to the window.

- **Silent failure cluster (2 P2 + 2 P3 bugs)**:
  - **filigree-769a192252**: `_safe_json_loads` now returns `{error_key: True}` when parsed JSON is valid but not a dict (arrays, scalars), matching the behaviour for malformed JSON. Previously the non-dict branch returned bare `{}`, so callers (`Issue.fields`, `FileRecord.metadata`, `ScanFinding.metadata`) saw an empty payload with no warning, and `to_dict()` emitted zero `data_warnings`. A corrupt column containing `"[1,2,3]"` now produces the same corruption signal as `"{bad json"`.
  - **filigree-c6c7842661**: MCP `get_issue` no longer swallows `sqlite3.Error` from the file-association lookup and masquerade-returns `files: []`. The handler now fails fast — matching `dashboard_routes/issues.py` and the dedicated `get_issue_files` MCP tool — so schema/query failures surface as MCP errors instead of being misread as "no associations".
  - **filigree-613e9f5f66**: `filigree update --design ""` now clears the `design` field as documented. The previous truthiness guard (`if field or design:`) treated an empty-string value as "not provided" and dropped the update entirely; the check now distinguishes unset (`None`) from cleared (`""`).
  - **filigree-55c5347992**: `_build_transition_error` no longer lets a backend failure during transition enrichment (`get_valid_transitions` re-reads the issue from SQLite) escape and mask the caller's `invalid_transition` payload. The enrichment branch now catches any exception with a debug log and returns the original error intact, so a transient "database is locked" during error construction does not turn a handled validation failure into a crash.

- **Logging observability cluster (1 P2 + 1 P3 bug)**:
  - **filigree-0983c839c5**: `_JsonFormatter` now emits the full traceback and exception class for `exc_info=True` logs. Previously the formatter reimplemented `logging.Formatter.format()` and serialised exceptions as only `str(record.exc_info[1])`, so a `KeyError("missing-key")` logged as `"exception": "'missing-key'"` — no class, no traceback. Across the 50+ `exc_info=True` call sites in hooks, MCP, and DB layers, every production error log was silently losing the diagnostic detail that made `exc_info` worth asking for. The formatter now adds `exception_type` (class name) and `traceback` (formatted stack) fields alongside the existing `exception` message, and includes a `stack` field when callers pass `stack_info=True`.
  - **filigree-c9ee8025cc**: `setup_logging` now closes every stale or duplicate `RotatingFileHandler` instead of bailing out after the first path match. The old loop scanned `logger.handlers` and returned as soon as it found one matching handler, so a handler list shaped like `[matching, stale]` or `[matching, duplicate]` — produced by a prior leak, a failed reconfiguration, or external code adding a handler — left the trailing entries attached forever, keeping file descriptors open and writing records to the wrong file. The new loop walks the full list, keeps at most one matching handler, and closes/removes every other rotating handler before returning.

- **Install integrity cluster (2 P2 bugs)**:
  - **filigree-c0312c2f6c**: `inject_instructions` now fully repairs a filigree block whose end marker is missing. Previously the repair preserved everything after the opening marker, so the stale body became orphan content below the newly-inserted block; subsequent runs found the *new* end marker and treated that orphan tail as legitimate user content, so the corruption lived forever. An unclosed block is now replaced from the opening marker through EOF — content before the marker is preserved, content after is assumed to belong to the broken block. Regression test runs the repair twice and asserts convergence.
  - **filigree-b74c12e014**: `ensure_gitignore` no longer accepts any `.filigree/` substring as proof that the directory is ignored. The check now parses `.gitignore` line by line, skipping blank/comment/negated lines and matching only normalised forms (`.filigree`, `.filigree/`, `/.filigree`, `/.filigree/`). Previously `#.filigree/` (a comment), `!.filigree/` (a negation that *un-ignores*), and `src/some/.filigree/cache/` (a subpath) all short-circuited the check, so projects could end up reporting "already ignored" while the real `.filigree/` directory remained tracked.

- **Install / doctor cluster (5 P2 bugs)**:
  - **filigree-e1ef3675f7**: `filigree install --claude-code` and `--codex` now install only the MCP, matching their help text. Previously both flags implicitly pulled in hooks + skills (and `--codex` pulled in codex-skills), making it impossible to update an MCP config alone and duplicating behaviour that the dedicated `--hooks`, `--skills`, and `--codex-skills` flags already cover.
  - **filigree-e671d07d56**: `filigree server register` and `filigree server unregister` no longer exit non-zero when the daemon reload step fails. The registry change is already committed by that point, so a reload failure is a best-effort warning ("restart the daemon manually") rather than a masked-success error. Scripts chaining these commands no longer misinterpret a completed registration as a failure.
  - **filigree-36539914b3**: `filigree doctor` now detects broken module-form `SessionStart` hooks. For hooks shaped like `python -m filigree session-context`, the interpreter existing is necessary but not sufficient — doctor additionally runs `python -c "import filigree"` to verify the module is still installed in that interpreter, catching venv purges and pip uninstalls that previously left the hook looking healthy.
  - **filigree-9fb21f2b4b**: `install_claude_code_hooks` no longer appends new `SessionStart` hooks to a user block that merely *mentions* "filigree" in a command. Reuse is now strict: only a block whose `matcher` is empty/missing AND that already holds a recognised filigree hook command (via `_hook_cmd_matches`) is a valid reuse target. Otherwise a dedicated unscoped block is created, so `session-context` and `ensure-dashboard` fire for every session source (startup, resume, clear, compact) instead of inheriting a narrower user matcher.
  - **filigree-09d0dff729**: `_find_filigree_mcp_command` now probes both `filigree-mcp` and `filigree-mcp.exe` in the uv-tool branch. Previously the Windows filename was skipped in favour of the bare-`filigree-mcp` fallback, even when an absolute `~/.local/bin/filigree-mcp.exe` existed.

### Added (2.0 foundations)

- **Unified error envelope (2.0 wire shape).** Every error response across MCP tools, dashboard routes, and CLI `--json` output — including the per-item `failed[]` entries inside batch responses — now emits the same flat shape: `{"error": "<message>", "code": "<UPPERCASE_CODE>", "details"?: {…}}`. The 11-member `ErrorCode` enum (`VALIDATION`, `NOT_FOUND`, `CONFLICT`, `INVALID_TRANSITION`, `PERMISSION`, `NOT_INITIALIZED`, `IO`, `INVALID_API_URL`, `STOP_FAILED`, `SCHEMA_MISMATCH`, `INTERNAL`) replaces 27 ad-hoc lowercase codes that previously differed per surface. `ErrorResponse` is defined as a `TypedDict` and the dashboard helper `_error_response` now constructs through it so mypy gates the shape at every emit site; a new `errorcode_to_http_status()` function uses `match` + `assert_never` so adding a 12th member fails the build.
- **`classify_value_error(message)`** helper in `filigree.types.api` — substring heuristic that maps `ValueError` messages mentioning `status`/`transition`/`state` to `ErrorCode.INVALID_TRANSITION` and everything else to `ErrorCode.VALIDATION`. Previously the MCP surface used the heuristic inline while dashboard and CLI blanket-classified every `ValueError` as `INVALID_TRANSITION`, producing different codes for the same input. The three surfaces now share one implementation; the heuristic retires once Stage 3 introduces typed `InvalidTransitionError` raise sites.
- **New envelope TypedDicts** for future use: `BatchResponse[_T]`, `BatchFailure`, `ListResponse[_T]`. Shape contracts are pinned by `tests/api/test_envelope_types.py`; consumer wiring lands in Stage 2b.
- **Typed exceptions** `SchemaVersionMismatchError`, `AmbiguousTransitionError`, `InvalidTransitionError`. Structured carriers for installed/database versions, ambiguous transition candidates, and invalid-transition context respectively. Raise sites land in Stage 2b / Stage 3 — defined here so the exception type and fields are stable.
- **`ForeignDatabaseError`** — runtime guard against cross-project latch-on.
  Discovery now tracks the first ``.git/`` directory it sees during walk-up;
  if it subsequently finds a ``.filigree.conf`` (or legacy ``.filigree/``)
  *above* that git boundary, it raises ``ForeignDatabaseError`` instead of
  silently returning the ancestor anchor. The primary failure this prevents:
  when ``filigree`` is installed globally (``uv tool install filigree``) and
  an LLM runs commands in a git repo that has no anchor of its own, the old
  walk-up would dump tickets into whichever parent project's database it
  found first. The error message tells the caller exactly what to do —
  ``cd <project> && filigree init`` and restart MCP — so the LLM can
  self-correct rather than corrupting a sibling project's data. Monorepos
  (conf at the git root) and anchor-less trees (no git in ancestry) remain
  unaffected. ``ForeignDatabaseError`` subclasses ``ProjectNotInitialisedError``
  so existing generic "not set up" handlers still catch it, and ``filigree
  doctor`` now emits the full message as a CheckResult.
- **`.filigree.conf`** — JSON anchor file at the project root. The authoritative discovery target: walk-up looks for this file (not the `.filigree/` directory). Nested `.filigree.conf` files override their parents — first hit wins. Carries `version`, `project_name`, `prefix`, and `db` (path to the database, relative to the conf file).
- **`FiligreeDB.from_conf(conf_path)`** classmethod — open a project DB by its conf anchor.
- **`WrongProjectError`** (`ValueError` subclass) — raised on write operations against IDs whose prefix doesn't match the open DB's prefix. Catches an agent that climbed into a parent's database and tries to mutate a foreign-prefix ticket. Read methods (`get_issue`, `get_comments`, etc.) intentionally do not enforce, so legitimate cross-prefix lookups (migration, history) still work.
- **`ProjectNotInitialisedError`** (`FileNotFoundError` subclass) — raised when no `.filigree.conf` is found anywhere up to `/`. Error message points at `filigree init` and `filigree doctor`.
- **`filigree doctor`** flags `~/.filigree.conf` if present (a conf at `$HOME` claims everything beneath it; almost always a mistake) and reports whether the project's `.filigree.conf` anchor exists.

### Changed (HTTP wire-shape)

- **Wire-shape unification (breaking for callers branching on error `code`).**
  - `GET /api/release/{id}/tree` on a non-release issue now returns `code: "NOT_FOUND"` (was `"VALIDATION"`). Status remains 404.
  - `GET /api/type/{type_name}` on an unknown type now returns status 400 + `code: "VALIDATION"` (was 404 + `"NOT_FOUND"`). `details.valid_types` lists the registered types.
  - MCP `trigger_scan` rate-limited responses now emit `code: "CONFLICT"` (was `"IO"`). The blocking run id is in `details.blocking_run_id`; the cooldown window is retriable when the blocking run completes.
  - MCP `restart_dashboard` failure paths now include a `code` field (previously absent). `ErrorCode.STOP_FAILED` for the dead-process-won't-die case, `ErrorCode.PERMISSION` for SIGTERM/SIGKILL denied, `ErrorCode.INTERNAL` for unexpected exception, and `ErrorCode.IO` when the spawn succeeds but returns no URL.
  - MCP `_build_transition_error` now emits `code: "INVALID_TRANSITION"` (uppercase) instead of `"invalid_transition"`. Previously sibling emit sites (e.g. `reopen_issue`) were already uppercase, so a case-sensitive client saw two codes for the same logical error.
  - MCP scanner error responses (`trigger_scan`, `trigger_scan_batch`) now nest extras (`blocking_run_id`, `available_scanners`, `scanner`, `spawn_errors`, `batch_id`, `scan_run_ids`, `per_file`, `exit_code`, `log_path`, `skipped`) inside `details` instead of as top-level keys. Clients that read these fields must now read `payload["details"]["<key>"]`.
  - `BatchFailureDetail.code` is now typed as `ErrorCode` (was `str`) and emits uppercase values (`"NOT_FOUND"`, `"INVALID_TRANSITION"`, `"VALIDATION"`) across every surface that surfaces batch results — dashboard `POST /api/batch/*`, MCP `batch_close`/`batch_update`/`batch_add_label`/`batch_add_comment`, and CLI `close`/`reopen`/`batch-update`/`batch-close`/`batch-add-label`/`batch-add-comment` in `--json` mode. Previously batch responses mixed an uppercase top-level `code` with lowercase per-item codes; clients no longer need to branch on the nesting level.
  - Dashboard uncaught-exception paths in `GET /api/releases`, `GET /api/release/{id}/tree`, and `POST /api/issue/{id}/comments` now emit `code: "INTERNAL"` (was `"IO"`). `IO` is reserved for transient I/O failures that a client may retry; these paths catch `except Exception` with a `BUG:` log and should signal "file a bug" instead.
  - Dashboard `HTTPException` envelope now maps 401 and 422 explicitly (401 → `PERMISSION`, 422 → `VALIDATION`). Previously any unmapped status coerced to `INTERNAL`, which misled clients into treating FastAPI's default 422 validation as a server bug. Unknown status codes still fall back to `INTERNAL` but now log a warning so operators can discover new mappings.

- **`filigree init`** writes `.filigree.conf` alongside `.filigree/`.
- **Discovery** is split: `find_filigree_conf` is strict (returns the conf path or raises) and `find_filigree_anchor` walks up for either a `.filigree.conf` or a legacy `.filigree/` directory, returning `(project_root, conf_path_or_None)`. Both are pure reads — discovery never writes. Legacy installs are still discoverable; the conf is created only by explicit init/install paths so inspection commands work on read-only mounts.
- `find_filigree_root` continues to return the literal `.filigree/` directory next to the project anchor, regardless of any custom `db` location declared in the conf.
- `FiligreeDB.from_project` now resolves via `find_filigree_anchor`, falling back to `from_filigree_dir` for legacy installs.
- Error messages for "project not initialised" now point at `filigree init` and `filigree doctor` explicitly.

### Fixed (early 2.0 bug-fix wave)

- **filigree-7840eae0bd**: agents in a directory with no `.filigree/` would silently walk up into a parent's `.filigree/` and write tickets into the wrong DB. Mitigated by the explicit `.filigree.conf` claim model plus the `WrongProjectError` write guard.
- `WrongProjectError` no longer rejects legitimate IDs from projects whose prefix contains a hyphen. The check is now anchored on `startswith(prefix + "-")` instead of splitting the ID on the first `-` (which broke any project initialised with a hyphenated `cwd.name`, e.g. `my-app/` generating IDs like `my-app-abc1234567`).
- Project discovery no longer writes during the walk-up. Previously a legacy install discovered via `find_filigree_conf` triggered a `.filigree.conf` backfill, causing `PermissionError` for inspection-only commands (`filigree list`, `filigree doctor`, MCP startup) on read-only checkouts.
- `find_filigree_root` no longer misroutes callers when the conf's `db` field points outside `.filigree/`. It now returns the project's `.filigree/` directory directly, so `mcp_server`, `install`, `dashboard`, `hooks`, and the summary writers operate against the correct database and filesystem location.
- **filigree-fe8956fb16**: `compact_events` no longer accepts a negative `keep_recent` and silently wipes all archived event history. The core method now raises `ValueError`, the MCP tool schema enforces `minimum: 0`, and the MCP handler validates the argument before dispatch. Defense-in-depth now matches the existing CLI guard.
- **filigree-33a938b515**: concurrent MCP tool invocations no longer corrupt each other. The MCP SDK dispatches tool calls concurrently (`tg.start_soon` per request) and `FiligreeDB` caches a single `sqlite3.Connection` — a failing mutation's `finally`-block rollback could erase a sibling coroutine's uncommitted writes on the shared connection. `call_tool` now acquires a per-`FiligreeDB` `asyncio.Lock` around handler execution and the safety-net rollback, serialising tool calls against the shared connection.
- **filigree-78903e4ff7**: MCP `register_file` with `path="."` (project root) no longer escapes as an uncaught `ValueError`. The handler now catches the normalization failure and returns a clean `invalid_path` error response, matching the existing traversal-rejection contract.
- **filigree-0911b35955**: scan ingestion with `path="."` no longer silently persists a `file_records` row with an empty path. `_validate_scan_findings` now re-checks the normalized path and raises `ValueError` with the per-finding index, symmetric with `register_file`'s post-normalization guard.
- **filigree-fda0e2a340**: `FiligreeDB.from_filigree_dir` no longer adopts a hardcoded `prefix="filigree"` when `config.json` is missing or lacks an explicit `prefix` key. It now falls back to the project directory's own name — matching `filigree init`'s default — so a legacy install whose config was deleted or never fully written doesn't silently open with the wrong identity and reject every write to its own issues.
- **filigree-bac0797445**: `import_jsonl` now fails fast when the JSONL file references issue IDs whose prefix doesn't match the destination DB. Previously imports preserved source IDs verbatim, creating rows that could be read but never mutated — every guarded write path raised `WrongProjectError` on them. Migration tools that deliberately need to preserve foreign IDs can opt in via `import_jsonl(..., allow_foreign_ids=True)` (or `filigree import --allow-foreign-ids`).
- **filigree-f863b9d1f8**: `filigree dashboard --server-mode` no longer overwrites the configured daemon port in `server.json` when the caller omits `--port`. The Click option now defaults to `None`, and server mode resolves `--port or read_server_config().port` before invoking `dashboard_main`. Omitting `--port` leaves the persisted config alone; passing one still updates it.
- **filigree-ceb2da2411**: `filigree dashboard --server-mode` now refuses to start when `claim_current_process_as_daemon()` reports a different live daemon is already tracked. Previously the return value was silently discarded and a second server process raced the tracked one for the daemon port.
- **filigree-563d5454e9**: `verify_pid_ownership` now distinguishes this project's dashboard from another filigree project's after PID recycling. `write_pid_file` embeds the dashboard port in the record; `verify_pid_ownership` requires that `--port <N>` appear in the live process argv when a port is recorded. Cross-project PID collisions no longer misidentify a foreign dashboard as our own, preventing `restart_dashboard` from sending SIGTERM to the wrong process.
- **filigree-73e909e6cc**: `cleanup_stale_pid` no longer unlinks a freshly written PID file under TOCTOU. The stale record is now moved aside with an atomic rename, re-verified from quarantine, and either committed (unlinked) or restored if a concurrent writer re-populated it during the check.
- **filigree-ea2a1959e1**: `ensure_dashboard_running` no longer spawns a second dashboard when a hook fires during startup. `write_pid_file` now records a `startup_ts`; when the recorded PID is alive, ours, and the port isn't yet listening but startup is within a 30-second grace window, the hook reports "initializing" instead of respawning.
- **filigree-bff063de18**: Repeated in-process `filigree.dashboard.main()` calls no longer serve the wrong database. The `_db` / `_project_store` module globals are cleared on both entry and exit, so a subsequent call in the opposite mode routes through the correct resolver instead of inheriting stale state from the previous run.
- **Dashboard input validation cluster (6 P1 bugs)**:
  - **filigree-719f0abbb5**: `POST /issues`, `POST /issue/{id}/comments`, `POST /issue/{id}/claim`, and `POST /claim-next` no longer surface non-string body fields as uncaught `AttributeError` (500). Each route now rejects non-string `title`/`text`/`assignee` with `VALIDATION_ERROR` 400 before the value reaches `str.strip()` in core.
  - **filigree-6c21f57786**: `POST /files/{file_id}/associations` now type-checks `issue_id` and `assoc_type` before calling `add_file_association`, rejecting non-string values with 400 instead of relying on truthiness.
  - **filigree-2b756a5a44**: `PATCH /api/issue/{id}` now accepts and forwards `parent_id`, so dashboard clients can re-parent or clear (`""`) an issue via the API. Non-string `parent_id` is rejected with 400.
  - **filigree-237bbad946**: `GET /api/activity?since=…` now normalizes the parsed timestamp to UTC isoformat before running the SQL text comparison. Offset-bearing and naive inputs now compare correctly against the stored UTC-offset column instead of being compared byte-for-byte.
  - **filigree-6e6411daba**: `graph_v2_enabled` from `.filigree/config.json` is now parsed via the same strict bool allowlist as env vars. Previously `bool("false")` → `True` via Python truthiness; now `"false"`, `"0"`, `"no"` etc. disable the feature as intended.
  - **filigree-37c95a7e51**: Malformed `FILIGREE_GRAPH_V2_ENABLED` / `FILIGREE_GRAPH_API_MODE` values no longer override a valid config. Invalid env values log a warning and fall back to the project-config value, matching the documented resolution order (explicit → compatibility → feature-flag default).
- **Install / lifecycle cluster (3 P1 bugs)**:
  - **filigree-3572d3b273**: `filigree doctor` now resolves the database path from `.filigree.conf` when one exists, instead of always inspecting `.filigree/filigree.db`. Projects with a relocated `db` (e.g. `db = "storage/track.db"`) no longer get a false "Missing" DB report and a schema check against the wrong file. Legacy installs without a conf still fall back to the old layout; an unreadable conf surfaces as its own check failure without blocking the DB check.
  - **filigree-83c52565d6**: `filigree install --hooks` now repairs stale `PreToolUse` `ensure-dashboard` commands in `.claude/settings.json` after a binary move. Previously the substring match `"ensure-dashboard" in cmd` short-circuited the check and left the old absolute path in place, so the PreToolUse guard silently stopped firing. The registration walk now uses `_hook_cmd_matches()` (the same matcher used for `SessionStart` upgrades), rewrites any bare-form or stale-absolute-path command to the current `ensure_dashboard_cmd`, and repairs the enclosing `matcher` back to `mcp__filigree__.*` if it has drifted.
  - **filigree-37b1452e59**: `install_codex_mcp` no longer corrupts a valid `~/.codex/config.toml` whose `[mcp_servers.filigree]` header carries trailing whitespace or an inline `# …` comment. The `_upsert_toml_table` replacement regex now accepts `[ \t]*(?:#[^\r\n]*)?` between the closing `]` and the line terminator, so the existing block is replaced in place instead of being left intact while a second (duplicate) block is appended — which would render the file unparseable under tomllib's duplicate-table rule.
- **CLI error-handling cluster (2 P2 + 1 P2 + 1 P3)**:
  - **filigree-25daf4e886**: `filigree remove-label` no longer escapes a `ValueError` as an unhandled traceback when the label is empty, contains control characters, collides with a reserved type name, or uses a reserved auto-/virtual namespace (`area:`, `severity:`, `scanner:`, `pack:`, `lang:`, `rule:`, `age:`, `has:`). The command now mirrors `add-label`'s handling — `try/except ValueError` emits a clean stderr message (or `{"error": ...}` JSON) and exits 1.
  - **filigree-565ff86495**: `filigree remove-dep` no longer escapes `WrongProjectError` / `ValueError` from `_check_id_prefix` as an unhandled exception. Passing a foreign-prefix ID (e.g. after copying one from another project's docs) now produces the same clean error shape as `add-dep`: plain-text on stderr or `{"error": ...}` JSON, exit 1. `refresh_summary(db)` only runs when the mutation actually succeeds.
  - **filigree-62c5b61f68**: `cli_common.refresh_summary()` is now best-effort end-to-end. Previously only `FileNotFoundError` was suppressed, so an `OSError` from `summary.write_summary`'s `mkstemp` / `os.replace` (disk full, permission denied, cross-device rename) turned a successful `filigree create --json` — which had already committed and printed its JSON result — into a non-zero exit. `refresh_summary` now logs `OSError` as a warning and broad `Exception` with traceback, matching the MCP server's best-effort pattern.
  - **filigree-3c4196854b**: `cli_common.get_db()` now surfaces corrupt `.filigree.conf`, unreadable database, and newer-than-CLI schema as clean `ClickException`-style exits (stderr + exit 1) rather than leaking raw `ValueError` / `OSError` / `sqlite3.Error` tracebacks from every CLI command. Previously only `ProjectNotInitialisedError` was handled; `read_conf` failures on malformed JSON or missing `prefix`/`db` keys, and `initialize`'s "schema newer than CLI" `ValueError`, propagated unhandled.
- **Core API input-validation cluster (1 P1 + 2 P2 + 1 P3)**:
  - **filigree-0903743222**: `archive_closed(days_old=-1)` no longer silently archives every freshly closed issue by computing a future cutoff timestamp. The core method now rejects `days_old < 0` with `ValueError`, the MCP `archive_closed` schema declares `minimum: 0`, and the MCP handler runs `_validate_int_range` before dispatch — defense-in-depth now matches the existing CLI `click.IntRange(min=0)` guard.
  - **filigree-7c1932b74e**: `create_issue` / `update_issue` no longer persist whitespace-only `assignee` values that `claim_issue` would then treat as "already assigned". Assignees are now normalised via `_normalize_assignee` (strip to either `""` or a trimmed real identity) before insert/update, and non-string values raise `TypeError` instead of being stored raw. Whitespace-only input now silently normalises to unassigned, so a subsequent `claim_issue` succeeds instead of reporting `already assigned to '   '`.
  - **filigree-0b4fcb6d30**: `create_issue(labels="urgent")` no longer iterates the bare string character-by-character — `"urgent"` would previously yield one-char labels `"u"`, `"r"`, `"g"`, … — and `create_issue(deps="abc")` no longer emits a misleading "Invalid dependency IDs" error for `a`/`b`/`c`. Both `labels` and `deps` are now validated up front with `_validate_string_list`, so a bare string or mixed-type iterable raises `TypeError` with a clear message.
  - **filigree-39c410ef92**: `filigree labels --top -1` no longer behaves as "unlimited" despite the help text advertising `0` as the unlimited sentinel. The option type is now `click.IntRange(min=0)`, matching the MCP `list_labels.top` schema.

- **Dashboard lifecycle cluster (1 P1 + 2 P2)**:
  - **filigree-2298877675**: `restart_dashboard` (MCP) no longer reports `status: "restarted"` when the old dashboard never exits. The SIGTERM path previously set `stopped = True` unconditionally after the 2-second grace wait; if the process was wedged, `ensure_dashboard_running` would then reuse the same still-alive process and the tool happily labelled the no-op a successful restart. The handler now polls `is_pid_alive` after the grace window, escalates to SIGKILL with a second grace window when needed, and returns `{"code": "stop_failed"}` instead of proceeding to respawn when the old PID genuinely refuses to die.
  - **filigree-89e7a1c833**: `ensure_dashboard_running` no longer leaves a detached dashboard running untracked when `write_pid_file` / `write_port_file` raises OSError after a successful `Popen(..., start_new_session=True)`. The metadata writes are now wrapped in try/except; on failure the just-spawned child is terminated (SIGTERM then SIGKILL escalation with bounded waits), any partial PID/port files are unlinked, and the function returns a clean `"Failed to record dashboard metadata: …"` error instead of propagating the exception and orphaning the session-detached process.
  - **filigree-aa80d21b97**: `_doctor_ethereal_checks` now uses `verify_pid_ownership` (liveness + argv identity + recorded-port match) instead of raw `is_pid_alive` when evaluating the Ephemeral PID check. Previously a PID that had been recycled to an unrelated process passed the check as a healthy dashboard; now the same record is reported as stale, matching the existing ownership semantics used by `cleanup_stale_pid`, `ensure_dashboard_running`, and `restart_dashboard`.

- **Planning cluster (3 P2 bugs)**:
  - **filigree-fcac6acf6c**: `create_plan` no longer emits duplicate `dependency_added` events when a step's `deps` list repeats the same index (e.g. `deps: [0, 0]`). The dep row write already uses `INSERT OR IGNORE`, but the event write was unconditional, so duplicate events piled up in the audit log and wedged `undo_last()` — the sibling duplicate looked like a fresh reversible event the undo machinery couldn't clear. The event is now only recorded when `cursor.rowcount > 0`, matching `add_dependency`'s semantics.
  - **filigree-a5e7090f76**: `create_plan` now rejects out-of-range or non-integer `priority` values with a clean `ValueError` up front instead of letting them slip through to the DB-layer `CHECK (priority BETWEEN 0 AND 4)` and surface as an uncaught `sqlite3.IntegrityError` traceback at the CLI. Validation runs before the transaction begins at all three levels (milestone, phase, step); booleans are rejected explicitly since `bool` is a subclass of `int`.
  - **filigree-6b0f8cfb49**: `filigree plan <milestone>` now derives phase and step markers from `status_category` (`open`/`wip`/`done`), matching the pattern already used by `summary.py`. Previously the CLI hardcoded the legacy `open`/`in_progress`/`closed` raw status names, so the built-in planning workflow (`pending → in_progress → completed` for steps; `pending → active → completed` for phases) rendered every pending step as `[?]` and never showed the `[WIP]` marker for an active phase.

- **CLI `--json` output cluster (2 P1 + 1 P2)**:
  - **filigree-7676d82aa2**: `filigree guide <pack> --json` no longer emits `guide` as a stringified `mappingproxy(...)` literal. `WorkflowPack.guide` is a `MappingProxyType` that `json.dumps` cannot serialise, and the old call's `default=str` escape hatch silently stringified the whole mapping. The CLI now converts via `dict(pack.guide)` (matching the MCP handler), echoes the canonical `pack.pack` id, and drops `default=str` so future unserialisable fields raise instead of silently stringifying.
  - **filigree-5e3e587396**: `filigree close <ids...> --json` now reports only *newly* unblocked issues in `unblocked`, matching the documented contract (`docs/cli.md`) and the MCP `close_issue` implementation. The previous code snapshotted `db.get_ready()` after the close and excluded closed IDs, so any pre-existing ready issue was falsely claimed as newly unblocked — scripts coordinating work off this payload would re-claim/notify on work that wasn't actually released. The handler now captures `ready_before` before the close loop and returns `ready_after - ready_before`.
  - **filigree-89ab20068d**: `filigree type-info <type> --json` now emits the full field schema (`options`, `default`, `required_at`) instead of dropping those keys. The CLI previously rebuilt the dict inline and omitted every field attribute except `name`/`type`/`description`/`pattern`/`unique`, so callers reading enum options (e.g. `severity.options = ["critical", "major", ...]`) or defaults via `--json` saw nothing. The CLI now delegates to the canonical `FiligreeDB._field_schema_to_info()` serialiser that the MCP `get_type_info` handler already uses.

- **Timestamp handling cluster (1 P1 + 3 P2)**:
  - **filigree-a693bdfab2**: `migrate._safe_timestamp()` re-runs no longer synthesize a fresh `datetime.now(UTC)` for every invalid/blank source timestamp. Event and comment dedup keys both include `created_at`, so non-deterministic fallbacks produced a new key on every re-migration and silently duplicated rows. `_safe_timestamp` now takes an optional stable `fallback` and `migrate_from_beads` threads each issue's already-normalized `updated_at` as the fallback for its events and comments — dedup is now idempotent across runs.
  - **filigree-be53912410**: `migrate_from_beads` no longer imports `closed_at` raw. Non-done source issues now have `closed_at` cleared (beads doesn't clear it on reopen — filigree does), and done-category issues with blank/malformed `closed_at` fall back to the issue's normalized `updated_at`. Downstream code (`archive_closed`'s SQL `closed_at < cutoff`, `analytics.lead_time`'s `datetime.fromisoformat`) can now assume a parseable ISO timestamp or `NULL`.
  - **filigree-51ad2aa743**: `cycle_time` and `get_flow_metrics` no longer sort `status_changed` events via SQL `ORDER BY created_at ASC`. Imported/migrated rows can carry heterogeneous ISO offsets (e.g. `+00:00` vs `+10:00`); lexical ordering placed chronologically-earlier `+10:00` events after `+00:00` events, picking the wrong WIP→done pair. A new `_sort_events_chronologically` helper parses each `created_at` to UTC, drops unparseable rows, and orders by `(parsed_utc, id)` in Python.
  - **filigree-735977d7bc**: `filigree changes --since <ts>` now normalizes `Z` → `+00:00` and validates the input with `datetime.fromisoformat` before passing it to SQLite. Stored events use `datetime.now(UTC).isoformat()` which emits `+00:00`; a `Z`-suffixed `--since` would miscompare lexically against fractional-second stored rows, silently dropping matching events. Malformed input now produces a clean `stderr` error and exit 1 instead of a silent empty result.

- **Scan subsystem cluster (2 P1 + 2 P2)**:
  - **filigree-ed3be5a092**: `trigger_scan` / `trigger_scan_batch` now reserve a `pending` `scan_run` row *before* spawning the scanner process, closing the TOCTOU gap between `check_scan_cooldown` and `create_scan_run`. Concurrent triggers previously both read "no recent run", both spawned a scanner, and both recorded independent runs — bypassing the 30-second rate limit. The new `reserve_scan_run` wraps the cooldown read and the row insert in a single `BEGIN IMMEDIATE` transaction, so the second caller blocks on the writer lock, then sees the reservation and returns `rate_limited`. Spawn failures transition the reservation to `failed` so retries aren't blocked by a dead reservation.
  - **filigree-ec33df4b86**: `trigger_scan_batch` now assigns each file its own `scan_run_id` instead of sharing one id across every spawned child. Per-file scanners invoked with `--max-files 1` each run their own completion POST; with a shared id the fastest child finalised the batch while siblings were still scanning, so later findings landed against a `completed` run. The handler now returns `batch_id` plus a `scan_run_ids` list and a `per_file` breakdown (pid, log path, file id per child), and each child's lifecycle is tracked independently. Repeated file paths in one request are deduped before reservation.
  - **filigree-daefeda81d**: `get_scan_status` now resolves scanner log paths against an explicit `FiligreeDB.project_root` set by `from_filigree_dir` / `from_conf`, instead of assuming `db_path.parent.parent`. The old derivation broke for any `.filigree.conf` install with a relocated `db` (e.g. `db = "storage/db/track.db"`): `db_path.parent.parent` landed on `storage/`, so `log_tail` was always empty because the resolved path pointed at a directory that didn't exist. Legacy direct-path construction still falls back to the old derivation.
  - **filigree-f1cce0f474**: `_validate_localhost_url` now requires a parseable URL with an `http` or `https` scheme and an explicit localhost hostname. Previously `""` was accepted as a valid host and `urlparse("no-scheme").hostname or ""` coerced malformed URLs back into the empty-string allowlist, so scans reported "triggered" with an unusable callback — the scanner helper's `f"{api_url}/api/v1/scan-results"` POST would then fail or target whatever `no-scheme/api/v1/scan-results` resolved to on the box. Empty strings, scheme-less values, and non-HTTP schemes are now rejected before the scanner is spawned.

- **Template registry cluster (1 P1 + 2 P2 + 1 P3)**:
  - **filigree-5c1605d349**: `_infer_status_category` no longer misclassifies built-in done states as `open` when the active template registry can't resolve the issue's type — e.g. a pack disabled after issues were created in it, or an import that predates pack registration. The hardcoded 6-name done set missed `released`, `completed`, `mitigated`, `verified`, `accepted`, and ~20 other done-category states shipped in bundled packs, so `close_issue` / `reopen_issue` / stats gated on the category resolved a `release` in `released` as still-open. The fallback now derives a `(type, state) → category` map from `templates_data.BUILT_IN_PACKS` at module load and is type-aware; state-name-only disambiguation only promotes names whose category is identical across every bundled type that declares them, so ambiguous names like `resolved` (wip in `incident`, done elsewhere) stay ambiguous and fall through to `open`.
  - **filigree-910f1cb024**: `validate_issue` no longer returns `valid=True` for issues whose type isn't in the active registry or whose current status isn't a declared state for that type. Both cases are reachable via bulk import, migration, and pack disable after creation; the previous short-circuit on `tpl is None` plus the missing state-membership check meant the CLI and MCP `validate_issue` quietly rubber-stamped structurally broken rows. `ValidationResult.errors` now surfaces both conditions with actionable messages listing the valid state names.
  - **filigree-5c9f9aa7c2**: MCP `reload_templates` no longer propagates `ValueError` as an internal server error when `.filigree/config.json` is corrupt. `_refresh_enabled_packs` raises on malformed JSON, and `mcp_server.call_tool` re-raises unhandled exceptions — the handler now catches `ValueError` and returns a structured `validation_error` response, matching the contract of every other MCP tool.
  - **filigree-33e7bf9947**: MCP `reload_templates` now calls `_refresh_summary()` after a successful reload, so `context.md` reflects the new enabled-packs state. Every other MCP mutation refreshes the summary; this one skipped, so the `In Progress` and `Needs Attention` sections (both template-derived) went stale until the next unrelated mutation.

### Frozen (no changes)

- **The `classic` generation at `/api/v1/*`** continues to work unchanged.
  Existing 1.x integrations require no code changes for filigree 2.0
  compatibility at the HTTP surface. ADR-002 §8 specifies the retirement
  process — none planned.

### Stability posture

- `classic` generation: frozen indefinitely. Retirement requires a new
  ADR (ADR-002 §8).
- `loom` generation: stable contract. Additions must preserve wire
  compatibility; breaking evolution introduces a new named generation.
- MCP and CLI: reflect the living surface. Shape evolves alongside
  filigree releases; pinning consumers should use HTTP generations.
- Schema versions: forward-migration only. Downgrade is not supported;
  schema-mismatch surfaces as `ErrorCode.SCHEMA_MISMATCH` (CLI / dashboard
  / MCP) or a stderr warning + `.filigree/INSTALL_VERSION` marker
  (`filigree init`).

## [1.6.1] - 2026-04-01

### Fixed

- `filigree doctor` no longer reports a false "duplicate install" warning when running from a uv tool venv whose Python is symlinked to a uv-managed interpreter outside the venv

## [1.6.0] - 2026-03-30

### Changed

- Codex MCP install now always writes global stdio config with runtime project autodiscovery instead of project-pinned `--project` args or URL-based routing
- Claude Code stdio MCP install now also uses runtime autodiscovery (`args = []`) so folder switches do not leave stale project targets behind
- Installation and migration docs now describe autodiscovery-based MCP wiring and correct the remaining MCP tool-count references to 71

### Fixed

- `filigree doctor` now rejects deprecated Codex URL routing and stale project-pinned Codex config with a clearer remediation message
- Server-mode Codex installs no longer write daemon URLs that can misroute writes across workspaces

### Tests

- Updated install, doctor, and CLI-admin coverage for autodiscovery-based Claude Code and Codex MCP config

## [1.5.2] - 2026-03-23

### Fixed

- **README accuracy** — MCP tool count corrected from 53 to 71; ruff line-length corrected from 120 to 140
- **Accessibility** — added `aria-label` attributes to `role="button"` elements in dashboard detail panel (blocker links, downstream links, file links)
- **XSS defense** — tour tooltip text now escaped via `escHtml()` (was safe from constants, now safe by construction)
- **CLI help text** — `reopen` command clarifies it returns issues to their type's initial state, not previous state
- Ruff formatting applied to 5 source files that had drifted

### Tests

- **New `tests/test_dashboard.py`** — 25 tests covering `ProjectStore` init/load/corruption, idle watchdog, idle tracking middleware, `_get_db` error paths, ethereal vs server mode app creation
- **New `tests/test_doctor.py`** — 70 tests covering `CheckResult`, `_is_venv_binary`, `_is_absolute_command_path`, config/DB/context/gitignore/MCP/hooks/skills/instruction file checks
- **Expanded `tests/api/test_scanner_tools.py`** — 36 new tests (was 2) covering scan run CRUD, status transitions, cooldown logic, batch runs, log tailing, edge cases

## [1.5.1] - 2026-03-18

### Added

- **Label taxonomy system** — namespace reservation, virtual labels (`age:fresh`, `age:stale`, `has:findings`, `has:plan`, `has:dependencies`), array labels, prefix search (`--label-prefix=cluster/`), and not-label exclusion in `list_issues`
- MCP tools for label discovery: `list_labels` and `get_label_taxonomy`
- CLI commands: `filigree labels`, `filigree taxonomy`, `--label-prefix`, `--not-label`, repeatable `--label` on `list`
- Mutual exclusivity enforcement for `review:` namespace labels
- **Scanner lifecycle tracking** — `scan_runs` table with schema v7→v8 migration, `ScansMixin` with CRUD, cooldown checks, and status transitions
- **Finding triage tools** — `get_finding`, `list_findings` (global), `update_finding` (file_id optional), `dismiss_finding`, `promote_finding`, `batch_update_findings` MCP tools
- **Scanner module extraction** — new `mcp_tools/scanners.py` with `trigger_scan_batch`, `get_scan_status`, `preview_scan`; DB-persisted cooldown replaces in-memory dict
- **Shared scanner pipeline** — `run_scanner_pipeline()` in `scripts/scan_utils.py` with argparse integration, batch orchestration, and API completion logic; slimmed `claude_bug_hunt.py` and `codex_bug_hunt.py`
- Scanner config file: `.filigree/scanners/claude-code.toml`

### Changed

- **Breaking (API):** `POST /api/v1/scan-results` response replaces `issues_created`/`issue_ids` with `observations_created` count. The `create_issues` parameter is replaced by `create_observations`.
- **Breaking:** `update_finding` signature changed — `file_id` is now keyword-only and optional
- `process_scan_results` replaces `create_issues` with `create_observations` for lightweight triage
- Narrowed `except Exception` to specific exception types in scanner MCP handlers to avoid masking programming errors as DB failures
- `batch_update_findings` response now includes `"partial": true` flag when some updates succeed and others fail
- `ScanIngestResult` now tracks `observations_failed` count and reports per-finding failure messages
- Batch scan data warning now distinguishes files from processes
- `process_scan_results` terminal-state detection uses direct DB query instead of brittle string matching

### Fixed

- `batch_update_findings` now logs individual failure warnings server-side (previously only in MCP response)
- `promote_finding_to_observation` surfaces a note when file record is missing instead of silently losing context
- `process_scan_results` docstring corrected: `severity` is optional (defaults to `"info"`), `suggestion` added to optional fields
- `_handle_get_scan_status`, `_handle_dismiss_finding`, `_handle_list_labels`, and `_handle_get_label_taxonomy` now catch `sqlite3.Error` instead of returning raw exception traces
- Scanner batch file report read wrapped in try/except so one corrupt file no longer kills the entire batch
- Scan-run completion POST failure now counted in `api_failures` for correct exit code
- Fragile parallel-list index coupling in batch scan replaced with `zip(..., strict=True)`
- Unused variable lint violation in test_scans.py

### Tests

- 6 new test files: `test_scans.py`, `test_finding_triage.py`, `test_label_discovery.py`, `test_label_query.py`, `test_scanner_lifecycle_tools.py`, `test_finding_triage_tools.py`
- Test for breaking `create_issues` → `create_observations` parameter rename
- Test for `update_finding` with mismatched `file_id` raises `KeyError`
- Parametrized severity-to-priority mapping tests for all 5 severity levels
- Security boundary tests: path traversal, non-localhost URL rejection, reserved namespace enforcement

## [1.5.0] - 2026-03-09

### Added

- **Observations subsystem** — fire-and-forget agent scratchpad with TTL expiry, audit trail, atomic promote-to-issue, and file anchoring (schema v6→v7 migration)
- MCP tools for observations: `observe`, `list_observations`, `dismiss_observation`, `promote_observation`
- Observation awareness in session context, project summary, and MCP prompt
- Observation triage workflow with promote-to-type selection and requirements pack support
- Dashboard observation stats on Insights page and observation counts in Files table and detail panel
- Kanban List mode with sortable table view
- Scoped subtree explorer replacing the standalone Graph tab — sidebar-driven, renders parent-child hierarchy edges

### Changed

- Consolidated dashboard from 7 tabs to 5: Activity merged into Insights, Health merged into Files as collapsible Code Quality Overview, Workflow demoted to Settings modal
- Redesigned header filter bar with status pills, Done time-bound dropdown, and cleaner layout
- Decomposed `process_scan_results` monolith into focused helpers with table-driven `export_jsonl`
- Simplified TypedDict patterns using `PlanResponse` inheritance and `NotRequired`
- **Breaking (MCP):** `get_valid_transitions` and `get_issue` `missing_fields` now returns bare field name strings instead of full schema objects — consumers expecting `{name, type, description}` dicts must update to plain `list[str]`
- Threaded `Severity`/`FindingStatus`/`AssocType` Literal types through API signatures

### Fixed

- Codex MCP install and doctor now validate the config Codex actually uses (`~/.codex/config.toml`), rewrite stale `filigree` entries that still target another project, and support server-mode MCP URL installs
- Restored schema `v6` compatibility for historical databases by reinstating the missing `v5 -> v6` migration for the `issues.parent_id` self-foreign-key, including FTS rebuild handling after the table swap
- JSONL export/import now round-trips the file subsystem (`file_records`, `scan_findings`, `file_associations`, `file_events`), reconciles the seeded `Future` release singleton on restore, and makes `merge=True` idempotent for imported comments and file history rows
- Ethereal/server lifecycle helpers now degrade cleanly under restricted socket permissions, treat `PermissionError` liveness checks as live processes, and verify PID ownership against the expected dashboard command shape before reusing or stopping processes
- Older Filigree binaries now refuse to open databases with a newer schema version instead of silently attempting an unsupported downgrade path
- Dashboard issue creation now preserves custom `fields`, so release/version metadata and other template-backed values survive `POST /api/issues`
- CLI, dashboard, hooks, and MCP project openers now honor configured `enabled_packs` instead of silently falling back to the default pack set
- File lookups by path now normalize equivalent path spellings on read, matching the write-time identity rules used by scan ingestion and file registration
- Transaction safety hardening: rollback guards on promote/close, savepoint leak fixes, undo race conditions, and phantom write prevention
- Template engine hardening: reverse-reachability BFS validation, crash-on-anomaly for category cache, rejection of unknown types in transitions and initial state lookups. `get_mode` raises `ValueError` for unknown modes (all callers already handle this). `get_initial_state` raises `ValueError` for unknown types (callers guard upstream or propagate correctly). `list_issues` raises `ValueError` for negative limit/offset (API schema prevents negative values at boundary).
- TOCTOU race fixes in PID ownership and cleanup, unchecked return codes in OS command reads
- Numerous type-safety fixes: generic `PaginatedResult`, typed observations and planning responses, `EventType` Literal enforcement at SQL boundary
- CLI runtime fixes: partial-failure data loss prevention, correct exit codes, and `--json` support for all commands
- Issue creation/update now reject non-dict `fields` inputs with a stable validation error instead of crashing with an internal `AttributeError`
- Dashboard issue create/update and batch update endpoints now translate invalid non-dict `fields` payloads into `400 VALIDATION_ERROR` responses instead of leaking `500` errors
- Dashboard filter composability: type filter and cluster mode now work together correctly

### Tests

- Shape contract tests for 14 MCP handler response TypedDicts
- 42 new tests for previously untested error paths and edge cases
- DB core test gap closure for transactions, cycle detection, and import paths

## [1.4.1] - 2026-03-03

### Changed

- Dashboard (`fastapi`, `uvicorn`) is now part of core dependencies — no more `filigree[dashboard]` extra required

### Fixed

- `filigree init` on existing installs now reports schema migrations ("Schema upgraded v1 → v5") instead of silently applying them
- `filigree doctor --fix` can now auto-repair outdated database schemas (was missing from the fixable check map)
- Dashboard broken by Tailwind CSS CDN SRI integrity hash mismatch — removed incompatible SRI attribute from dynamic CDN resource

## [1.4.0] - 2026-03-01

Architectural refactor: decompose monolithic modules into domain-specific subpackages, add type safety with TypedDicts, boundary validation, releases tracking, and comprehensive test restructuring.

### Added

#### Workflow

- `not_a_bug` done-state for bug workflow — distinct from `wont_fix` for triage rejections (transitions from `triage` and `confirmed`)
- `retired` state added to release workflow with quality-check refinements

#### Dashboard UX

- Click-to-copy on issue IDs in kanban cards and detail panel header (hover underline, toast feedback, keyboard accessible)
- "Updated in last X days" dropdown filter in the main issue toolbar (1d, 7d, 14d, 30d, 90d) — persisted with other filter settings
- Sticky headers for metrics, activity, files, and health views (header stays visible while content scrolls)

#### Configuration

- `name` field in `ProjectConfig` / `.filigree/config.json` — separates human-readable project name from the technical ID prefix
- `filigree init --name` option to set display name independently of `--prefix`
- Dashboard title and server-mode project list now use `name` with fallback to `prefix`

### Changed

#### Architecture (v1.4.0 refactor)

- `FiligreeDB` decomposed into domain mixins: `EventsMixin`, `WorkflowMixin`, `MetaMixin`, `PlanningMixin`, `IssuesMixin`, `FilesMixin` — each in its own module under `src/filigree/`
- `DBMixinProtocol` wired into all mixins, eliminating 33 `type: ignore` comments
- CLI commands split from monolithic `cli.py` into `cli_commands/` subpackage
- MCP tools split into domain modules
- Dashboard routes split into `dashboard_routes/` subpackage
- `install.py` split into `install_support/` subpackage

#### Documentation

- Plugin system & language packs design document added with 8-specialist review consensus
- ADR-001 superseded in favour of workflow extensibility design
- Issue ID format documentation corrected from `{6hex}` to `{10hex}`

### Fixed

- Issue ID entropy increased from 6 to 10 hex characters to reduce collision probability at scale
- `import_jsonl` uses `cursor.rowcount` for all record types — accurate counts for merge dedup
- Batch error reporting enriched with `code` and `valid_transitions` fields
- Stale `filigree[mcp]` extra removed from packaging; WMIC parsing made quoting-aware for Windows compatibility
- PID verification abstracted beyond `/proc` for cross-platform support
- `fcntl.flock()` replaced with `portalocker` for cross-platform file locking
- Dead code `_generate_id_standalone()` removed

## [1.3.0] - 2026-02-24

Server/ethereal operating modes, file intelligence + scanner workflows, Graph v2, and broad safety hardening.

### Added

#### Operating modes and server lifecycle

- `filigree init --mode` and `filigree install --mode` for explicit ethereal/server setup
- Server-mode config and registration system with schema-version enforcement
- Server daemon lifecycle commands and process tracking helpers
- Deterministic port selection and PID lifecycle tracking with atomic writes
- Streamable HTTP MCP endpoint (`/mcp/`) for server mode
- Session context now includes dashboard URL
- Mode-aware doctor checks for ethereal/server installations

#### Files, findings, and scanner platform

- File records and scan findings workflow with metadata timeline events
- Files and Code Health dashboard views (file list/detail/timeline, hotspots, health donut/coverage)
- Split-pane findings workflow and live scan history in dashboard
- Scanner registry loaded from TOML configs in `.filigree/scanners/`
- New MCP tools: `list_scanners` and `trigger_scan`
- Scanner trigger support for `scan_run_id` correlation
- Optional `create_issues` flow for scan ingest to promote findings into candidate `bug` issues and create `bug_in` file associations
- Scan ingest stats extended with `issues_created` and `issue_ids`
- CLI init support for scanner directory creation
- Shared scanner utilities and Claude scanner integration

#### Dashboard UX

- Kanban cards now display a left-edge colour band indicating issue type (bug=red, feature=purple, task=blue, epic=amber, milestone=emerald, step=grey)

#### Dashboard graph v2

- Graph v2 shipped with improved focus/path workflows and traversal behavior
- Time-window filter with persisted default
- Progressive-disclosure toolbar with grouped advanced controls
- Improved interaction diagnostics and plain-language status messaging

#### Installation and Codex integration

- `filigree install --codex-skills` to install Codex skills into `.agents/skills/`
- Doctor health check for Codex skills installation state

### Changed

- Dashboard frontend restructured from monolithic HTML script to ES-module architecture
- Dashboard behavior split by mode: ethereal uses simplified single-project flow; server mode uses `ProjectStore` multi-project routing
- API errors standardized, schema discovery surfaced, and instruction generation extracted for reuse
- `filigree server register` and `filigree server unregister` now trigger daemon reload when server mode is already running
- Scanner command validation now resolves project-relative executables (for example `./scanner_exec.sh`) during trigger checks
- Install instruction marker parsing improved to tolerate missing metadata/version fields
- Release workflow pack now enabled by default for all new projects alongside core and planning; `suggested_children` for release type expanded to include epic, milestone, task, bug, and feature
- ADR-001 added documenting the structured project model (strategic/execution/deliverable layers)
- README/docs expanded with architecture plans, mode guidance, and dashboard visuals
- Stale comments and docstrings fixed across 10 source files: endpoint counts, module docstrings, internal spec references (WFT-*), naming discrepancies, and misleading path references all corrected or removed

### Fixed

#### Security and correctness

- Dashboard XSS sinks fixed across detail, workflow, kanban, and move-modal surfaces
- File view click-handler escaping fixed for issue IDs containing apostrophes
- All onclick handlers in detail panel, activity feed, and code health views now use `escJsSingle()` for JS string contexts — fixes 6+ XSS injection points where `escHtml()` was misused or escaping was missing entirely
- HTTP MCP request context isolation fixed for per-request DB/project directory selection
- Issue type names now reserved from label taxonomy to prevent collisions
- Duplicate workflow transitions (same `from_state -> to_state`) now rejected at parse and validation time — previously silently accepted with inconsistent dict/tuple behavior
- Enforcement value `"none"` rejected from templates — only `"hard"` and `"soft"` are valid `EnforcementLevel` values
- Release `rolled_back` state recategorized from `done` to `wip` — allows resumption transition to `development`, matching the `incident.resolved` fix pattern
- `ProjectStore.get_db()` guarded against `UnboundLocalError` when `read_config()` fails before DB initialization
- `FindingStatus` type alias aligned with DB schema — added `acknowledged` and `unseen_in_latest`, removed stale `wont_fix` and `duplicate`
- Dead `_OPEN_FINDINGS_FILTER_F` and duplicate `_VALID_SEVERITIES` class attributes removed from `FiligreeDB`

#### Server/daemon reliability

- Multi-project reload and port consistency hardened in server mode
- Reload failures now surface as `RELOAD_FAILED` instead of reporting a false-success response
- `unregister_project` updates locked to prevent concurrent config races
- Daemon ownership checks fixed for `python -m filigree` launch mode
- Portable PID ownership fallback added when command-line process inspection is unavailable
- Registry fallback key-collision handling corrected
- Hook command resolution hardened across installation methods
- `read_server_config()` now validates JSON shape and types: non-dict top-level returns defaults, port coerced to int and clamped to 1–65535, non-dict project entries dropped
- Invalid port values in server config now log at WARNING before falling back to default (previously silent coercion)
- `start_daemon()` serialized with `fcntl.flock` on `server.lock` to prevent concurrent start races
- `start_daemon()` and `daemon_status()` verify PID ownership via `verify_pid_ownership()` — stale PIDs from reused processes no longer cause false "already running" or false status
- `start_daemon()` wraps `subprocess.Popen` in `try/except OSError` to return a clean `DaemonResult` instead of propagating raw exceptions while holding the lock
- `stop_daemon()` verifies process death after SIGKILL and reports failure when the process survives; PID file cleaned up in all terminal paths to prevent permanent stuck state
- `claim_current_process_as_daemon()` now verifies PID ownership before refusing to claim — a reused PID from a non-filigree process no longer blocks the claim
- `stop_daemon()` catches `ProcessLookupError` on SIGTERM when the process dies between the liveness check and the signal delivery
- Off-by-one in `find_available_port()` retry loop — now tries `base + PORT_RETRIES` candidates as documented
- `setup_logging()` now removes and closes stale `RotatingFileHandler`s when `filigree_dir` changes — prevents handler leaks and duplicate log writes in long-lived processes
- Session skill freshness check now covers Codex installs under `.agents/skills/` in addition to `.claude/skills/`

#### Files/findings and scanner robustness

- `_parse_toml()` now distinguishes `OSError` from `TOMLDecodeError` with `exc_info` — unreadable scanner TOML files no longer silently vanish from `list_scanners`
- Scanner paths canonicalized; datetime crash fixed; command templates expanded
- Scan API hardened (`scan_run_id` persistence, suggestion support, severity fallback)
- Findings metadata persistence corrected for create/update ingest paths
- Metadata change detection fixed to compare parsed dictionary values
- `min_findings` now counts all non-terminal finding statuses
- `list_files` filter validation and project-fallback detail-state behavior corrected
- `/api/v1/scan-results` now enforces boolean validation for `create_issues`
- `scan_source` validated as string in `/api/v1/scan-results` — non-string values return 400 instead of crashing
- Pagination `limit` and `offset` enforce minimum values (`limit >= 1`, `offset >= 0`) across all API endpoints — prevents SQLite `LIMIT -1` unbounded queries
- `trigger_scan` cooldown set immediately after rate-limit check (before any await) and rolled back on failure — closes check-then-act race window
- `process_scan_results()` validates `path`, `line_start`/`line_end`, and `suggestion` types upfront with clear error messages instead of crashing in SQL/JSON operations
- `add_file_association` pre-checks issue existence and returns `not_found` instead of misclassifying as `validation_error`

#### Dashboard and analytics quality

- Flow metrics now batch status-event loading to remove N+1 event-query behavior
- Graph toolbar overflow/stacking/disclosure behavior corrected across Graph v2 iterations
- Graph controls hardened for inactive focus/path states and large-graph zoom readability
- Files API sort-direction wiring and stale detail-selection clearing fixed
- Missing split-pane window bindings restored; async loader error handling tightened
- Flow metrics now include `archived` issues so `archive_closed()` results count in throughput
- Analytics SQL queries use deterministic tiebreaker (`id ASC`) for stable cycle-time computation when events share timestamps
- `list_issues` returns empty result when `status_category` expansion yields no matching states, instead of silently dropping the filter
- `import_jsonl` event branch uses shared `conflict` variable and counts via `cursor.rowcount` so `merge=True` accurately reports 0 for skipped duplicates
- Migration atomicity restored for FK-referenced table rebuilds; dashboard startup guard added
- Graph zoom-in no longer jumps aggressively from extreme zoom-out levels — `wheelSensitivity` reduced from Cytoscape default (1.0) to 0.15
- Page title reversed from "[project] — Filigree" to "Filigree — [project]"
- `_read_graph_runtime_config()` failure logging elevated from DEBUG to WARNING
- `api_scan_runs` exception handler narrowed from `Exception` to `sqlite3.Error`
- Tour onboarding text corrected from "5 views" to "7 views" (adds Files and Code Health)

#### CLI

- `import` command catches `OSError` for filesystem errors — clean message instead of traceback
- `claim-next` wraps `db.claim_next()` in `ValueError` handling with JSON/plaintext error output
- `session-context` and `ensure-dashboard` hooks now log at WARNING and emit stderr message on failure instead of swallowing at DEBUG
- `read_config()` catches `JSONDecodeError`/`OSError` — corrupt `config.json` returns defaults with warning instead of cascading crashes
- MCP `_build_workflow_text` now separates `sqlite3.Error` (with actionable "run `filigree doctor`" message) from generic exceptions; both log at ERROR
- MCP `get_workflow_prompt` narrows `except RuntimeError` to only silence "not initialized"; unexpected RuntimeErrors now logged at ERROR
- `generate_session_context` freshness-check now splits expected errors (`OSError`, `UnicodeDecodeError`, `ValueError`) at WARNING from unexpected errors at ERROR; both include `project_root` for debuggability
- `ProjectStore.reload()` DB close errors now log at WARNING (matching `close_all()`) instead of DEBUG
- `create_app` MCP ImportError now logged at DEBUG with `exc_info` instead of silently swallowed
- MCP `release_claim` tool description corrected: clarifies it clears assignee only (does not change status)
- `_install_mcp_server_mode` prefix-read failure narrowed to `JSONDecodeError`/`OSError` and elevated to WARNING; `_install_mcp_ethereal_mode` logs `claude mcp add` stderr on failure
- Duplicate `_check_same_thread` assignment removed from `FiligreeDB.__init__`
- `list_templates()` now includes `required_at`, `options`, and `default` in field schema — matches `get_template()` output
- `claim_issue()` now records prior assignee as `old_value` in claimed event; `undo_last` restores it instead of always blanking
- `SCHEMA_V1_SQL` refactored from brittle `SCHEMA_SQL.split()` to standalone constant with test assertions for subset integrity

#### Migration

- Priority normalization hardened (`_safe_priority()`) — non-numeric and out-of-range values coerced during migration instead of crashing
- Timestamp normalization added (`_safe_timestamp()`) — NULL/empty timestamps replaced with valid ISO-8601 fallbacks
- `apply_pending_migrations()` guarded against being called inside an existing transaction — raises `RuntimeError` immediately
- Caller's `foreign_keys` PRAGMA setting preserved across migrations instead of unconditionally restoring to ON

### Removed

- Hybrid registration system (`registry.py`) removed in favor of explicit mode-based registration paths
- Checked-in `.mcp.json` removed from version control

## [1.2.0] - 2026-02-21

Multi-project dashboard, UX overhaul, and Deep Teal color theme.

### Added

#### Multi-project support

- Ephemeral project registry (`src/filigree/registry.py`) for discovering local filigree projects
- `ProjectManager` connection pool for serving multiple SQLite databases from a single dashboard instance
- Project switcher dropdown in the dashboard header
- Per-project API routing via FastAPI `APIRouter` — all endpoints scoped to the selected project
- MCP servers self-register with the global registry on startup (best-effort, never fatal)
- `/api/health` endpoint for dashboard process detection

#### Dashboard UX improvements

- Equal-width Kanban columns (`flex: 1 1 0` with `min-width: 280px`) — empty columns no longer shrink
- Drag-and-drop between Kanban columns with transition validation — pre-fetches valid transitions on dragstart, dims invalid targets, optimistic card move with toast confirmation
- Keyboard shortcut `m` opens "Move to..." dropdown as accessible alternative to drag-and-drop
- Type-filter / mode toggle conflict resolved — Standard/Cluster buttons dim when type filter is active, active filter shown as dismissible pill
- WCAG-compliant status badges — open badges use tinted background with higher-contrast text
- P0/P1 text priority labels — critical and high priorities show text badges instead of color-only dots
- Stale badge click shows all stale issues (not just the first)
- Workflow view auto-selects first type on initial load
- Disabled transition buttons show inline `(missing: field)` hints
- Claim modal shows "Not you?" link when pre-filling from localStorage
- Header density reduction — removed duplicate stat spans (footer has the full set)
- Settings gear menu (⚙) in header — replaces standalone theme toggle with a dropdown containing "Reload server" and "Toggle theme"
- `POST /api/reload` endpoint — soft-reloads server state (closes DB connections, re-reads registry, re-registers projects) without process restart

#### Deep Teal color theme

- 20 CSS custom properties on `:root` (dark default) and `[data-theme="light"]` for all surface, border, text, accent, scrollbar, graph, and status colors
- 15 utility classes (`.bg-raised`, `.text-primary`, `.bg-accent`, etc.) for static HTML elements
- `THEME_COLORS` global JS object for Cytoscape graphs (which cannot read CSS custom properties), synced in `toggleTheme()` and theme init
- Dark palette: deep teal surfaces (#0B1215 → #243A45), sky-blue accent (#38BDF8)
- Light palette: teal-tinted whites (#F0F6F8 → #DCE9EE), darker sky accent (#0284C7)
- Theme toggle mechanism changed from `classList.toggle('light')` to `dataset.theme` with CSS `[data-theme="light"]` selector
- All `bg-slate-*`, `text-slate-*`, `border-slate-*` Tailwind classes eliminated from dashboard
- Old `.light` CSS override block (9 lines with `!important`) removed

### Changed

- Dashboard API restructured from flat routes to `APIRouter` with project-scoped prefix
- `CATEGORY_COLORS.wip` updated from `#3B82F6` (blue-500) to `#38BDF8` (sky-400)
- `CATEGORY_COLORS.done` updated from `#9CA3AF` (gray) to `#7B919C` (teal-tinted gray)
- `@keyframes flash` color updated to match accent (`rgba(56,189,248,0.5)`)
- Sparkline stroke color uses `THEME_COLORS.accent` instead of hardcoded blue

### Fixed

- Cytoscape graph and workflow graph colors now update on theme toggle (re-render triggered)
- Graph legend status dots use CSS custom properties instead of hardcoded hex
- Kanban column header dots use `CATEGORY_COLORS` instead of hardcoded hex
- Progress bars in cluster cards and plan view use `CATEGORY_COLORS` instead of hardcoded hex

## [1.1.1] - 2026-02-20

Comprehensive bug-fix and hardening release. 31 bugs resolved across 13 source files,
identified through systematic static analysis and verified against HEAD.

### Added

- Template quality checker (`check_type_template_quality()`) wired into template load pipeline

### Changed

- `_category_cache` uses hierarchical keys matching `_transition_cache` convention
- Core `batch_close()` return type changed from `list[Issue]` to `tuple[list[Issue], list[dict[str, str]]]` matching `batch_update()` pattern

### Fixed

#### Transaction safety

- `create_issue()` and `update_issue()` restructured to validate-then-write with explicit rollback on failure, preventing orphaned rows/events via MCP's long-lived connection
- `reopen_issue()` wrapped in try/except rollback to prevent orphaned events on failure
- MCP `call_tool()` safety net: rolls back any uncommitted transaction after every tool dispatch
- `close_issue()` respects hard-enforcement gates on workflow transitions
- `close_issue()` validates `fields` type before processing

#### Template and workflow validation

- `StateDefinition.category` validated at construction time — invalid categories raise `ValueError`
- Duplicate state names detected at both parse and validation time (defense in depth)
- `enabled_packs` config validated as `list[str]` — strings wrapped, non-lists fall back to defaults
- `parse_type_template()` validates transitions/fields_schema types — raises `ValueError` not raw `TypeError`
- Incident `resolved` state re-categorized from `done` to `wip` — `close_issue()` from resolved now works correctly
- Incident workflow guide: stale `resolved(D)` notation corrected to `resolved(W)` in state diagram

#### Dashboard and API

- Batch endpoints validate `issue_ids` as list of strings — null/missing/non-list values return 400
- Batch close returns per-item `closed`/`errors` instead of fail-fast 404/409
- Claim endpoints reject empty/whitespace assignee with 400
- All sync handlers converted to async to fix concurrency race
- Non-string batch IDs rejected with validation error

#### CLI

- `create-plan` validates milestone/phases types, catches `TypeError`/`AttributeError`
- `create-plan --file` wraps file read in error handling (`OSError`, `UnicodeDecodeError`)
- `import` catches `sqlite3.IntegrityError` for constraint violations
- Backend validation errors properly surfaced in `create-plan` output

#### Install and doctor

- `install_claude_code_mcp()` validates `mcpServers` is a dict before use
- Hook detection handles non-dict/non-list JSON structures throughout `_has_hook_command`
- `install_codex_mcp()` rejects malformed TOML instead of silently appending
- `run_doctor()` uses `finally` block to prevent SQLite connection leaks
- `ensure_dashboard_running()` checks `fastapi`/`uvicorn` imports explicitly
- `ensure_dashboard_running()` polls process after spawn, captures stderr on failure
- Executable path resolution uses `Path.parent / "filigree"` instead of string replacement

#### Analytics

- `cycle_time()` guards done-scan with `start is not None` — no break before WIP found
- `get_flow_metrics()` paginates all closed issues instead of hardcoded 10k cap
- `lead_time()` accepts pre-loaded `Issue` object to avoid N+1 re-fetch

#### Logging

- `setup_logging` guarded by `threading.Lock` to prevent duplicate handlers from concurrent calls
- Handler dedup uses `os.path.abspath()` normalization to handle symlink aliases

#### Migration

- Comment dedup includes `created_at` to preserve legitimate repeated comments
- Zero-value filter removed — numeric `0` preserved in migrated fields
- `rebuild_table()` FK check results read and validated, not silently ignored
- `rebuild_table()` FK fallback hardened with `BEGIN IMMEDIATE`

#### Summary generation

- Parent ID lookup chunked in batches of 500 to avoid SQLite variable limit
- `_sanitize_title()` strips control chars, collapses newlines, truncates — prevents markdown/prompt injection

#### MCP server

- `no_limit=true` pagination uses 10M effective limit and computes `has_more` correctly
- Spike cross-pack spawns direction corrected to match dependency contract

#### Undo safety

- `undo_last()` guards against NULL `old_value` in `priority_changed` events — returns graceful error instead of `TypeError` crash
- `undo_last()` guards against NULL `new_value` in `dependency_added` events — returns graceful error instead of `AttributeError` crash

#### Dashboard (additional)

- `remove_dependency` endpoint now passes `actor="dashboard"` for audit trail consistency
- `update_issue`, `create_issue`, and `batch_update` validate priority is an integer — returns 400 instead of 500 `TypeError`

#### MCP server (additional)

- `batch_close` and `batch_update` validate all IDs are strings before processing
- `batch_update` validates `fields` is a dict (or null) before passing to core

### Known Issues

- `cycle_time()` still executes per-issue events query inside `get_flow_metrics()` loop — lead_time N+1 fixed but cycle_time N+1 remains (tracked as filigree-f34f66)

## [1.1.0] - 2026-02-18

### Added

- Claude Code session hooks — `filigree session-context` injects a project snapshot (in-progress, ready queue, critical path, stats) at session start; `filigree ensure-dashboard` auto-starts the web dashboard
- Workflow skill pack — `filigree-workflow` skill teaches agents triage patterns, sprint planning, dependency management, and multi-agent team coordination via progressive disclosure
- `filigree install --hooks` and `filigree install --skills` for component-level setup
- Doctor checks for hooks and skills installation
- MCP pagination — list/search endpoints cap at 50 results with `has_more` indicator and `no_limit` override
- Codex bug hunt script for per-file static analysis

### Changed

- CI workflow is now reusable via `workflow_call` — release pipeline invokes it instead of duplicating logic
- Release workflow adds post-publish smoke test (installs from PyPI, runs `filigree --version`)
- `github-release` job is idempotent — re-runs fall back to artifact upload instead of failing
- Dependency caching enabled across all CI jobs (`enable-cache`)
- Main branch ruleset now requires lint, typecheck, and test status checks before merge

### Fixed

- Core logic: claim race condition, create_plan rollback, dependency validation
- Analytics: summary, templates, flow metrics bugs
- Error handling: CLI exit codes, MCP validation, dashboard robustness
- Security: migration DDL atomicity, MCP path traversal, release branch guard
- Peripheral modules: migration, install, version robustness
- FTS5 search query sanitization
- File discovery now allows custom exclusion directories
- Batch-size validation and out-of-repo scan root handling
- Dev/internal files excluded from sdist

## [1.0.0] - 2026-02-16

### Added

- First PyPI release — all features from 0.1.0 plus CI/CD pipeline and packaging

## [0.1.0] - 2026-02-15

### Added

- SQLite-backed issue database with WAL mode and convention-based `.filigree/` project discovery
- 43 MCP tools for native AI agent interaction (read, write, claim, batch, workflow, data management)
- Full CLI with 30+ commands, `--json` output for scripting, and `--actor` flag for audit trails
- 24 issue types across 9 workflow packs (core and planning enabled by default):
  - **core**: task, bug, feature, epic
  - **planning**: milestone, phase, step, work_package, deliverable
  - **risk**, **spike**, **requirements**, **roadmap**, **incident**, **debt**, **release**
- Enforced workflow state machines with transition validation and field requirements
- Dependency graph with cycle detection, ready queue, and critical path analysis
- Hierarchical planning (milestone/phase/step) with `create-plan` for bulk hierarchy creation
- Atomic claiming with optimistic locking for multi-agent coordination (`claim`, `claim-next`)
- Pre-computed `context.md` summary regenerated on every mutation for instant agent orientation
- Flow analytics: cycle time, lead time, and throughput metrics
- Comments, labels, and full event audit trail with per-issue and global event queries
- Session resumption via `get_changes --since <timestamp>` for agent downtime recovery
- `filigree install` for automated MCP config, CLAUDE.md injection, and .gitignore setup
- `filigree doctor` health checks with `--fix` for auto-repair
- Web dashboard (`filigree-dashboard`) via FastAPI
- Batch operations (`batch-update`, `batch-close`) with per-item error reporting
- Undo support for reversible actions (`undo`)
- Issue validation against workflow templates (`validate`)
- PEP 561 `py.typed` marker for downstream type checking

[Unreleased]: https://github.com/tachyon-beep/filigree/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/tachyon-beep/filigree/compare/v1.5.2...v1.6.0
[1.5.2]: https://github.com/tachyon-beep/filigree/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/tachyon-beep/filigree/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/tachyon-beep/filigree/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/tachyon-beep/filigree/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/tachyon-beep/filigree/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/tachyon-beep/filigree/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/tachyon-beep/filigree/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/tachyon-beep/filigree/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/tachyon-beep/filigree/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/tachyon-beep/filigree/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/tachyon-beep/filigree/releases/tag/v0.1.0
