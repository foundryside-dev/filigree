# MCP tool-name namespacing — methodology & rollout plan

**Issue:** `filigree-7771610917` — *[MCP] Namespace MCP tool names by subsystem
(`filigree_finding_list`, …) — breaking wire change*
**Parent epic:** `filigree-18bd3b8c98` (Toolkit DX)
**Author:** Claude (opus-4.8), 2026-06-02
**Status:** DRAFT — methodology for review; not yet scheduled for execution.

---

## 0. TL;DR

Rename the ~115 flat MCP tools (`get_issue`, `list_findings`, …) to a
subsystem-namespaced convention (`filigree_issue_get`, `filigree_finding_list`,
…) so an agent — and the ToolSearch ranker — can disambiguate a 115-tool
catalogue by prefix instead of by fuzzy keyword match.

The single most important structural decision: **this is two phases split on
the only act that actually breaks the wire.**

| Phase | Act | Breaking? | Ships on | Gated by |
|------|-----|-----------|----------|----------|
| **1** | Register new names as **aliases** to the same handlers; both resolve; docs/skills/consumers cut over | **No — purely additive** | any minor (2.x) | nothing |
| **2** | **Delete** the old names | **Yes** | major boundary (3.0) | consumer migration + zero old-name traffic |

Phase 1 *is* the "transition window where both names resolve" the issue
mandates. It delivers ~90% of the DX value without waiting for a major. Only
the deletion waits. **The dependency we attach to gate this work gates Phase 2,
not the whole task.**

---

## 1. Why it matters (recap, for the reviewer)

- **Disambiguation.** ToolSearch ranks deferred tools by keyword match against
  name + description. With ~115 equal-weight flat verbs (`get_file`,
  `get_finding`, `get_issue`, `get_stats`…), an agent struggles to surface the
  handful it needs. A prefix namespace (`finding_*` vs `file_*` vs `issue_*`)
  carries half the meaning and makes the catalogue navigable.
- **Federation collision safety.** Filigree runs alongside Clarion / Wardline /
  Shuttle in one agent tool table. Generic verbs (`get_stats`, `list_findings`)
  are exactly what collides across MCP servers.

**Honest caveat to record in the ADR:** clients already surface these as
`mcp__filigree__<name>`, so the literal `filigree_` prefix is partly redundant
for cross-server collision. The *real* win is **intra-server disambiguation**
(`finding_` vs `file_` vs `issue_`). The issue specifies the `filigree_` prefix,
so we keep it — but we record *why*, because a reviewer will ask.

This is the same "don't break the wire surface mid-release" discipline already
applied to F3 (`filigree-17694d2db8`). Approach (a) — tier tags + curated core
index, **no rename** — already shipped (`tiers.py`, `_record_subsystem`,
`get_workflow_guide` catalogue). This plan is Approach (b), the heavier
alternative, and it builds *on top of* (a)'s seam.

---

## 2. The codebase seam (already clean — this is why (b) is tractable)

All tool assembly funnels through one place in `mcp_server.py`:

- `_all_tools: list[Tool]` — exactly what `list_tools()` returns (verbatim).
- `_all_handlers: dict[str, Callable]` — what `call_tool(name, args)` dispatches
  on: `handler = _all_handlers.get(name)`.
- `_tool_subsystem: dict[str, str]` — name → owning module short-name, captured
  at assembly by `_record_subsystem`.
- `_apply_tier_metadata(_all_tools)` — the **single central post-processing
  seam**, run once at import, before anything reads descriptions/schemas.
- `_tool_argument_names: dict[str, set[str]]` — per-tool allowed args, derived
  from each tool's `inputSchema`.
- `TIER_MAP` in `tiers.py` — name → tier, with a CI completeness test
  (`tests/mcp/test_tool_tiers.py`) that *fails loudly if any registered tool is
  un-tiered*. This guarantees `TIER_MAP`'s keys == the full tool set.

There is one assembly loop, so no per-tool-*module* edits are required. But
dispatch is **not** a pure dict lookup, and tagging is name-shape-coupled — see
§5.1.1. The rename touches `call_tool`'s guard logic and the import-time
annotation/tier tagging. "A few lines at one seam" undersells it; the correct
design is **canonicalize-at-top** (§5.1).

### 2.1 ⚠ `_tool_subsystem` is NOT the rename source

It is tempting to derive new names from `_tool_subsystem`. **Do not.** Counter-
example from the issue's own text: `list_findings` lives in `files.py`
(`_tool_subsystem["list_findings"] == "files"`) but the target is
`filigree_finding_list` — namespace `finding`, *not* `files`. The rename is
also a verb-reorder + singularization (`list_findings` → `…_finding_list`).

**The module grouping is not the target namespace.** The rename map is a
**hand-curated, reviewed artifact — one row per tool** — not a derivation. A
mechanical draft is fine to save typing, but every one of the ~115 rows gets
human eyes. **This is the single largest correctness risk in the whole task.**

---

## 3. Convention (settle BEFORE enumerating) — write as an ADR

Proposed rule: **`filigree_<singular-noun>_<verb>`**

| Old | New |
|-----|-----|
| `get_issue` | `filigree_issue_get` |
| `list_issues` | `filigree_issue_list` |
| `create_issue` | `filigree_issue_create` |
| `list_findings` | `filigree_finding_list` |
| `get_file` | `filigree_file_get` |

The rule survives the easy cases. It **breaks on the nounless / compound verbs**,
which is exactly why the convention must be ratified with an explicit exception
table before anyone fills in 115 rows. Open decisions (need owner sign-off):

| Old | Problem | Candidate(s) — DECIDE |
|-----|---------|------------------------|
| `get_ready` | no noun; returns ready issues | `filigree_issue_ready` / `filigree_work_ready` |
| `get_blocked` | same | `filigree_issue_blocked` / `filigree_work_blocked` |
| `start_next_work` | compound verb | `filigree_work_start_next` |
| `start_work` | verb-first | `filigree_work_start` |
| `heartbeat_work` | verb-noun already | `filigree_work_heartbeat` |
| `session_context` | no verb | `filigree_session_context` (keep) |
| `undo_last` | admin, no domain noun | `filigree_admin_undo` / `filigree_undo_last` |
| `get_stats` / `get_summary` / `get_metrics` | project-level, no entity | `filigree_project_stats` / `…_summary` / `…_metrics` |
| `observe` | single-word verb | `filigree_observation_create` |
| `restart_dashboard` / `reload_templates` | admin ops | `filigree_admin_*` |

**Collision policy (hard invariants, CI-enforced):**
1. No two old names may map to the same new name (injective).
2. No new name may already exist in the old set.
3. Every registered handler key appears in the map (total coverage).

Deliverable of this step: **ADR `docs/architecture/decisions/ADR-0xx-mcp-tool-namespacing.md`** —
rule + exception table + collision policy + the `filigree_`-prefix rationale.
The curated map is built *against* this ADR.

---

## 4. The RENAME_MAP — single source of truth ✅ LANDED

`src/filigree/mcp_tools/rename.py` exists and is frozen (ADR-016 §7 ratified):

```python
RENAME_MAP: dict[str, str]   # old name -> "<entity>_<verb>" (no filigree_ prefix), 114 rows
NEW_TO_OLD:  dict[str, str]  # derived inverse, for the canonicalize-at-top resolve step
```

**Everything derives from this one dict** — the `call_tool` resolve step, the
`list_tools` rename, deprecation telemetry. Do not scatter the names.

CI guard `tests/mcp/test_rename_map.py` pins the three invariants against the
**live `_all_handlers`** set (total coverage, injective, no-shadow) plus a shape
check (`<entity>_<verb>`, no `filigree_` prefix). A new tool added without a
rename row fails CI loudly — same discipline as the tier completeness test.
**Status: green** (ruff/format/mypy clean, tests pass).

---

## 5. Phase 1 — additive aliasing (ships on a minor)

### 5.1 Seam implementation — canonicalize-at-top (`mcp_server.py`)

**Internal canonical name = the OLD name.** This is the key design choice: it
keeps every existing handler key, `_tool_argument_names` entry, the three
`call_tool` string guards, and `TIER_MAP` key valid as-is. The *new* name is a
surface alias that exists only at the edges (`list_tools` output + an inbound
resolve step). Concretely:

- **Resolve once at the top of `call_tool`:** `name = NEW_TO_OLD.get(name, name)`
  *before* any of the existing logic runs. After this line, the entire body —
  the schema-mismatch guard, the registry guard, the runtime-drift gate, the
  `_all_handlers.get(name)` dispatch, `_unknown_argument_error`, and the logging
  — operates on the canonical (old) name **unchanged**. (`NEW_TO_OLD` is the
  inverse of `RENAME_MAP`, built once at import.)
- **`list_tools` emits new names:** build the served `Tool` objects with
  `tool.name = RENAME_MAP[old]` (and the canonical name in the description).
  Per the §5.2 decision, old-name `Tool` objects are NOT in `_all_tools`.
- Because the internal name never changes, `_all_handlers`,
  `_tool_argument_names`, and `TIER_MAP` need **no re-keying**.

#### 5.1.1 Seam interactions that name-shape coupling will break if missed

The "canonicalize-at-top, old name is canonical" design is chosen precisely
because it neutralizes all three of these. If an implementer instead makes the
*new* name the internal identity (the naive "register both keys" approach), each
of these silently regresses:

1. **`call_tool` special-cases `get_mcp_status` by literal string — 3 sites.**
   The schema-mismatch guard, the registry-startup guard, and the runtime-drift
   gate all do `name != "get_mcp_status"` to keep the diagnostic reachable in
   degraded mode. A consumer calling the new name `filigree_mcp_status_get`
   matches none of them → the one tool meant to stay up in degraded mode gets
   blocked. *Canonicalize-at-top fixes this for free* (resolve runs before the
   guards, so they still see `get_mcp_status`). **Test:** call the new
   mcp-status name with `_schema_mismatch` set — must still return the status,
   not the SCHEMA_MISMATCH envelope.
2. **Import-time tagging keys off `tool.name` — one root cause, 3 silent losses.**
   `_apply_tier_metadata` runs `tier_for(tool.name)`, `_is_read_only(tool.name)`
   (prefix match on `get_`/`list_`/…), and `tool.name in _DESTRUCTIVE_TOOLS`. If
   the served `Tool.name` is the new name:
   - `_READ_ONLY_PREFIXES` won't match `filigree_*` → **every readOnlyHint lost.**
   - `_DESTRUCTIVE_TOOLS` won't match `filigree_issue_delete` → **destructiveHint lost.**
   - `TIER_MAP` (old-keyed) → `tier_for` returns the `niche` default for **all
     114**, silently collapsing Approach (a)'s entire tiering — the thing this
     epic just shipped.
   Fix: tagging must run against the **canonical (old) identity**, not the
   surface string. Either tag while names are still canonical and then rename for
   the served list, or pass canonical identity alongside. **Test:** after
   assembly, `filigree_issue_get` carries `readOnlyHint`, `filigree_issue_delete`
   carries `destructiveHint`, and no new name silently tiers as `niche`.
3. **`include_legacy=True` is a legacy-*variant* gate, not an alias mechanism.**
   Verified: `scanners.register(include_legacy=True)` adds older tool *variants*
   (`list_scanners`, single-file `trigger_scan`) as additional handlers — not
   dual names for one handler. Those names are already live `_all_handlers` keys
   and already have RENAME_MAP rows (the §validation confirmed total coverage),
   so there is nothing to reconcile. Not the pattern to mirror; not a conflict.

### 5.2 DECISION (RATIFIED 2026-06-02) — `list_tools` advertises NEW names only
**New names ONLY in `list_tools` (exactly 114); old names still resolve in
`call_tool` for the transition window but are never served.** Project lead's
explicit constraint: *"I don't want us blowing up the registry with 300 tools."*
Serving both old and new would put 228 tools in the catalogue — this is
**ruled out**. The served count stays flat at 114 across both phases; the
deprecation window is invisible in the catalogue and lives only in the
`call_tool` resolve step (§5.1) + telemetry (§5.3). Consumers that hardcode old
names call them directly and don't re-read `list_tools`, so hiding old names
from discovery costs them nothing.

### 5.3 Deprecation telemetry — the Phase-2 gate (NOT optional)
When an **old** name is dispatched, log/emit a structured deprecation event
(`tool`, `canonical`, `actor`, timestamp). This is the only way to *prove*
consumers have stopped calling old names before we delete them in Phase 2.
Without it, the Phase-2 cutover is a guess. Surface a rolling count via
`get_mcp_status` so the gate is observable.

### 5.4 External cutover (all to canonical/new names)
- `docs/federation/contracts.md` — the wire contract (highest signal).
- `docs/mcp.md`, `docs/agent-integration.md`, `docs/workflows.md`.
- `src/filigree/data/instructions.md` (bundled agent instructions).
- `src/filigree/skills/filigree-workflow/` skill pack.
- Both `CLAUDE.md` tool-name references.
- **Enforcement lever:** extend `tests/util/test_docs_contracts.py` to assert
  docs reference *canonical* (new) names only — turns the doc cutover into a CI
  gate instead of a manual sweep.

> **CLI is out of scope.** This task is the MCP wire surface. CLI verbs stay in
> dash form (`start-next-work`); they are a separate surface and not renamed.

### 5.5 Tests (Phase 1)
- Extend `tests/mcp/test_tool_tiers.py`: `RENAME_MAP` is injective, total over
  `_all_handlers`, no new∈old collision.
- New `tests/mcp/test_tool_aliases.py`: every old name still resolves; every new
  name resolves; both reach the same handler; arg validation identical;
  round-trip old→new→handler.
- Deprecation-path test: calling an old name emits the telemetry event.
- `list_tools` advertises new names only (per 5.2).
- **Seam-interaction tests (§5.1.1):**
  - Degraded-mode reachability: with `_schema_mismatch` set, calling the new
    mcp-status name returns the status (not a SCHEMA_MISMATCH envelope) — proves
    canonicalize-at-top runs before the three string guards.
  - Tagging integrity: every served new name whose canonical is read-only
    carries `readOnlyHint`; `filigree_issue_delete` / `filigree_file_delete`
    carry `destructiveHint`; and the served tier distribution still matches
    Approach (a) (no silent collapse to all-`niche`).

---

## 6. Phase 2 — breaking removal (gated; ships on a major)

1. **Consumer coordination (out-of-repo).** File migration tracking issues for
   **Clarion**, **Wardline**, **Shuttle**. This is precisely the cross-product
   wire work `CLAUDE.md` says must NOT hide as a stale P-low — surface each as a
   real `blocked_by` dependency on the Phase-2 removal issue.
2. **Zero-traffic gate.** Phase-2 removal proceeds only when the §5.3 telemetry
   shows zero old-name calls across a full observation window *and* all consumer
   issues are closed.
3. **Removal.** Delete old keys from `RENAME_MAP` consumers / drop the alias
   registration; old names now return the existing `Unknown tool` NOT_FOUND
   envelope. Update `docs/UPGRADING.md` with the breaking change + the full
   old→new table.
4. Land on the 3.0 boundary.

---

## 7. Sequencing & milestones

```
ADR (§3)  ──►  RENAME_MAP curated + reviewed (§4)  ──►  Phase 1 seam + telemetry (§5.1–5.3)
                                                              │
                          ┌───────────────────────────────────┘
                          ▼
              External cutover + CI doc gate (§5.4)  ──►  ship on next 2.x minor
                          │
                          ▼
        File Clarion/Wardline/Shuttle migration issues (§6.1)   ── consumers migrate ──┐
                          │                                                             │
                          ▼                                                             ▼
              telemetry shows zero old-name traffic (§5.3/§6.2)  ───────────►  Phase 2 removal on 3.0
```

## 8. Risk register

| Risk | Mitigation |
|------|------------|
| Wrong namespace noun (module ≠ namespace) | Curated map reviewed row-by-row against ADR (§2.1, §3) |
| Two old names collapse to one new | Injectivity invariant, CI-enforced (§3, §5.5) |
| Docs drift from canonical names | Extend `test_docs_contracts.py` to gate (§5.4) |
| Premature deletion breaks a consumer | Telemetry zero-traffic gate + `blocked_by` consumer issues (§5.3, §6) |
| Untiered new name crashes server | `tier_for` re-keyed from RENAME_MAP + completeness test (§5.1, §5.5) |
| `get_mcp_status` string-guard misses new name → diagnostic blocked in degraded mode | Canonicalize-at-top: resolve new→old before guards run (§5.1, §5.1.1#1) |
| Read-only/destructive/tier tagging keys off renamed `tool.name` → hints + tiering silently lost | Tag against canonical identity, not surface string (§5.1.1#2) |

## 9. Decisions — status
1. ✅ **RESOLVED** — convention + all 9 naming questions: see ADR-016 §6 (agent
   poll) + §7 (authoritative final map). Names are `<entity>_<verb>`, no prefix.
2. ✅ **RATIFIED** — `list_tools` advertises new-only; catalogue stays at 114
   (§5.2). Project-lead constraint: no 300-tool registry.
3. ✅ **RESOLVED** — `filigree_` prefix **dropped** (ADR-016 D5); it duplicated
   the client's `mcp__filigree__` wrapper.
4. ⏳ **OPEN** — exact major version for Phase 2 removal (assumed 3.0).
5. ⏳ **OPEN** — D7a micro: `dependency_critical_path` (chosen) vs
   `…_critical_path_get` for verb-suffix uniformity (non-blocking).
