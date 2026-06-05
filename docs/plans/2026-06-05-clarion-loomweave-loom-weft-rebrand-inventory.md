# Rebrand rub-point inventory: Clarion → Loomweave, Loom → Weft

**Date:** 2026-06-05
**Branch:** release/3.0.0
**Status:** Inventory only — *no renaming performed in this pass.*
**Decision (owner, 2026-06-05):** Ride the 3.0.0 breaking-change train as a **hard
wire-break**. No compatibility aliases for the new contracts. Consumers
(Loomweave, Wardline, Shuttle, and every deployment env) cut over at 3.0.0.

**Tracked as epic `filigree-1d08ffb493`** with tier-ordered subtasks G0 / T2A /
T2B / T0 / T1 / T0b / T3 / Parked (see §7).

### Names table (2026-06-05) — mind the provenance column

⚠️ **G0 CLOSED 2026-06-06** (`filigree-23709c5975`). The hub published the
renamed roster; the rows below are updated to **CONFIRMED** or **RESIDUAL**.
`CONFIRMED` rows are locked — safe to flip in code (see the execution plan
`docs/plans/2026-06-06-loomweave-weft-rebrand-execution-plan.md`). `RESIDUAL`
rows are NOT yet locked and are carved to the residual gate
`filigree-73a2d91f5c` — **do not flip them in code.** A wrong-but-confident
contract name is worse than a blank.

| Surface | Old | New | Status |
|---------|-----|-----|--------|
| Sibling product | `Clarion` | `Loomweave` | **CONFIRMED** (user, original brief) |
| Federation | `Loom` | `Weft` | **CONFIRMED** (user, original brief) |
| HTTP generation | `/api/loom/*`, gen `"loom"` | `/api/weft/*`, gen `"weft"` | **CONFIRMED** (relay) |
| Entity key | `clarion_entity_id` | `loomweave_entity_id` | **CONFIRMED** (relay) — see opaqueness caveat below |
| Finding rule-code prefix | `CLA-` | `LMWV-` | **CONFIRMED** (relay) |
| Token env var | `CLARION_LOOM_TOKEN` | `WEFT_TOKEN` | **CONFIRMED** (hub G0, 2026-06-06) — hub locked the **short form**, NOT the earlier proposed `LOOMWEAVE_WEFT_TOKEN` |
| Token audience | `"loom"` | `"weft"` | **CONFIRMED** (hub G0) — security-sensitive; re-issue tokens at deploy |
| Error codes | `CLARION_REGISTRY_VERSION_MISMATCH`, `CLARION_OUT_OF_SYNC` | `LOOMWEAVE_*` | **RESIDUAL — `filigree-73a2d91f5c`** (hub silent; not in this plan) |
| SEI value prefix | `clarion:eid:` | `loomweave:eid:` | **CONFIRMED** (hub G0) — sibling-produced; must match emitter |
| registry_backend literal / section | `"clarion"` / `[clarion]` | `"loomweave"` / `[loomweave]` | **CONFIRMED** (hub G0) |
| URI scheme | `loom://` | `weft://` | **RESIDUAL — `filigree-73a2d91f5c`** (not locked; `loom://` stays) |
| Legis (governance member) | `Legis` | **unknown** | **RESIDUAL — `filigree-73a2d91f5c`** (hub roster still unpublished) |

Two coordination reversals folded in (both CONFIRMED via the relay):
- **Wire layer renames NOW.** An earlier posture kept the `loom` wire layer until
  lockstep; "it's all changing" reverses that — `/api/loom/*` and the generation
  token flip in T1. (The sibling's `X-Loom-*` headers / `loom_*` JSON fields are
  *Loomweave's* surface — **Filigree has none**.)
- **Entity key is rebranded, not de-branded.** `clarion_entity_id` →
  `loomweave_entity_id` (the field Loomweave's `filigree.rs` deserializes), which
  **overrides** in-flight task `filigree-45d76e71bb` (planned de-brand to canonical
  `entity_id` + aliases).

> 🔶 **Opaqueness caveat (resolve in G0, before T0 executes).** Task
> `filigree-45d76e71bb` was filed on first-loom-test feedback to make the key
> *opaque* — `entity_id`/`external_entity_id`, "stop implying a brand-specific
> locator." Renaming to `loomweave_entity_id` **re-introduces a brand**, arguably
> re-creating the exact problem. The relay confirmed `loomweave_entity_id`, but if
> *opaqueness* was the real intent the column name changes again and **T0's
> migration differs**. Cheap to confirm now; expensive to re-migrate later.

Two coordination reversals folded in:
- **Wire layer renames NOW.** An earlier posture kept the `loom` wire layer until
  lockstep; "it's all changing" reverses that — `/api/loom/*` and the generation
  token flip in T1. (The sibling's `X-Loom-*` headers / `loom_*` JSON fields are
  *Loomweave's* surface — **Filigree has none**.)
- **Entity key is rebranded, not de-branded.** `clarion_entity_id` →
  `loomweave_entity_id` (the field Loomweave's `filigree.rs` deserializes), which
  **overrides** in-flight task `filigree-45d76e71bb` (planned de-brand to canonical
  `entity_id` + aliases). Canonical `entity_id` stays primary; the branded compat
  alias just changes brand.

---

## 1. Scope: two independent rename axes

| Axis | Old | New | What it names | Filigree blast radius |
|------|-----|-----|---------------|------------------------|
| **A** | **Clarion** | **Loomweave** | The sibling *product* — code-entity registry backend, capability/SEI provider | 131 files |
| **B** | **Loom** | **Weft** | The *federation* — and, inside Filigree, the **named API generation** (`/api/loom/*`, `generations/loom/`) and the `loom://` URI scheme | 130 files |

These collide in exactly one identifier — the federation token env var
`CLARION_LOOM_TOKEN` — which carries **both** brands and is **deployment-set**.

> **"It's all changing."** Treat the *entire* federation contract set as
> in-flight — no sibling is a stable anchor. A third member, **Legis** (the
> governance / closure-gate provider: `legis_client.py`, `governance.py`,
> `LEGIS_URL`, `LEGIS_API_TOKEN`, endpoint `{LEGIS_URL}/filigree/issues/{id}/closure-gate`),
> is also in scope and presumably rebranding — **its new name is not yet known
> to Filigree** (only Clarion→Loomweave and Loom→Weft are fixed). See §6.

> ⚠️ **Execution caveat (bake into the migration ticket):** `clarion` is a safe,
> distinctive token — global replace is low-risk. **`loom` is NOT** — it is a
> substring of `bloom`, `gloom`, and appears in prose; and it is the *harder*
> axis because it doubles as the API-generation name. **Do not `sed` "loom"
> blind.** Axis B must be done by identifier, not by text.

---

## 2. Classification model

Every item is tagged on **two** axes, because "where the code lives" does not
tell you whether Filigree can rename it alone:

- **Ownership** — `FILIGREE` (rename freely) · `JOINT` (lockstep contract with a
  sibling) · `SIBLING` (value is *produced* by Loomweave; Filigree must match
  whatever it emits).
- **Substrate** — `CODE` (source identifiers) · `WIRE` (HTTP path / header /
  env / error code / audience) · `DATA` (lives in deployed DBs or config files;
  find-replace won't touch it and a blind rename breaks reads).

The tiers below are ordered by **coordination cost**, highest first.

---

## Tier 0 — DATA in flight: stored values & deployed config (highest risk)

These do **not** live (only) in source. A source rename alone breaks reads of
existing rows / configs. Each needs a **migration**, and Tier-0 items tagged
`SIBLING`/`JOINT` also need the sibling to agree on the new value.

| Item | Location | Old → New | Own. | Notes / required work |
|------|----------|-----------|------|------------------------|
| **SEI value prefix** | `sei_backfill.py:56` `SEI_PREFIX = "clarion:eid:"`; checked in `registry.py:168/175`, `sei_backfill.py` (many), surfaced in `instructions.md:80`, `cli_commands/sei.py:34` | `clarion:eid:` → `loomweave:eid:` | **SIBLING** | Prefix is **produced by Loomweave** and **stored verbatim** in `entity_associations` values. Filigree's constant must match the sibling's emitter. Existing rows already carry `clarion:eid:` → needs a data migration (rewrite prefix) *or* a dual-accept read window. Hard-break decision ⇒ migrate rows + cut emitter together. **Coordinate with SEI-conformance work** (ADR-017, `project_sei_conformance`). |
| **`registry_backend` config value** | literal `"clarion"` in `core.py:690/733/781/795`, `cli_commands/files.py:797` (`--to` Choice), `admin.py:599`, `sei_backfill.py:158`; `VALID_REGISTRY_BACKENDS` set | `registry_backend = "clarion"` → `"loomweave"` | **JOINT** | Lives in deployed `.filigree.conf`. Any project on the Clarion backend has this literal on disk. Needs config migration + `VALID_REGISTRY_BACKENDS` update. The `migrate-registry --to clarion` CLI choice renames too. |
| **`[clarion]` config section + keys** | `core.py:781` (`"clarion" not in raw`), `_validate_registry_settings`; keys `base_url`, `token_env` | `[clarion]` → `[loomweave]` | **JOINT** | TOML section in deployed config. Migrate alongside the backend literal. |
| **`clarion_entity_id` column + emitted key** | `migrations.py:640/644/647/748/764`, `db_entity_associations.py:5/6/46/51-52` (projection emits both `entity_id` + `clarion_entity_id`) | `clarion_entity_id` → **`loomweave_entity_id`** | **JOINT** | Physical SQLite column **and** the wire compat key Loomweave's `filigree.rs` deserializes. **Resolved: rebrand, not de-brand** — this **overrides** in-flight `filigree-45d76e71bb` (which targeted canonical `entity_id` + aliases). Canonical `entity_id` stays primary; the branded alias changes brand. |
| **Stored finding `rule_id` prefix** | findings rows; fixtures `tests/fixtures/contracts/{classic,loom}/scan-results.json` (`CLA-PY-UNSAFE-EVAL`) | `CLA-` → `LMWV-` | **SIBLING** | Loomweave's 184-code diagnostic namespace (`CLA-CONFIG-*`, `CLA-FACT-*`, `CLA-INFRA-*`). Filigree stores `rule_id` opaquely → reads don't break, but any Filigree-side prefix filter/grouping does, and existing rows carry `CLA-*`. Co-design rewrite with the sibling; update fixtures in T3. |
| **Legis governance signature (HMAC)** | `db_entity_associations.py:60-66/181-201` (`signature`, `signoff_seq`, v25/B1); produced by **Legis**, stored verbatim, **never verified by Filigree** (no key) | re-issued, not renamed | **SIBLING** | 🔴 **The signature is an HMAC over `{issue_id, entity_id, content_hash, signoff_seq}`.** Because it covers `entity_id`, the Tier-0 SEI-prefix rename (`clarion:eid:` → `loomweave:eid:`) **silently invalidates every stored signature** — the HMAC was cut over the *old* entity_id string. Filigree cannot re-sign (no key). **Legis must re-issue every governed binding's signature over the renamed entity_id, in lockstep with the prefix migration.** Per the user, assume the HMAC simply hasn't been re-cut yet — the stored signatures are stale-pending-reissue, an *expected transient*, not corruption. This hard-couples the SEI-prefix migration to a Legis re-sign pass; don't ship the prefix rename without it. |

---

## Tier 1 — WIRE contracts (breaking; federation must cut over in lockstep)

External, observable contract. Hard-break ⇒ no aliases; siblings & deployments
break at 3.0.0.

| Item | Location | Old → New | Own. | Notes |
|------|----------|-----------|------|-------|
| **HTTP generation prefix** | `dashboard.py:104/559-597/936`, `generations/loom/__init__.py`, README/ROADMAP | `/api/loom/*` → `/api/weft/*` | **JOINT** | The named API generation (ADR-002). `protected_paths` list, all four `create_loom_router()` mounts, OpenAPI docs. Consumers hard-coded to `/api/loom/`. |
| **Generation name token** | `dashboard.py:564/569`, `generations/loom/adapters.py:10`, `dashboard_auth.py:53` (`rest == "loom"`) | generation `"loom"` → `"weft"` | **JOINT** | The string identifying the generation in negotiation/recommendation logic. |
| **Token AUDIENCE claim** | `dashboard_auth.py:53` `rest == "loom"`, `LIVING_FEDERATION_ALIASES`, `CLASSIC_FEDERATION_ALIASES` | audience `"loom"` → `"weft"` | **JOINT** | 🔴 **Security-sensitive.** Both sides must agree *and* **tokens must be re-issued** with the new audience. Higher risk than any code rename — a mismatch fails auth federation-wide. |
| **Federation token env var** | `registry.py:18/56` `DEFAULT_CLARION_TOKEN_ENV = "CLARION_LOOM_TOKEN"`, `core.py:50/1155/1161` | `CLARION_LOOM_TOKEN` → `LOOMWEAVE_WEFT_TOKEN` *(name TBD — carries both brands)* | **JOINT** | 🔴 **Deployment-set** — breaks every deployment env, not just code. New name is a naming decision (proposed `LOOMWEAVE_WEFT_TOKEN`). Confirm with federation hub. |
| **Error code `CLARION_REGISTRY_VERSION_MISMATCH`** | `types/api.py:464/760`, `registry_errors.py:24`, `mcp_server.py:87-89/280`, `instructions.md:103`, `SKILL.md:199`, **CLAUDE.md** | → `LOOMWEAVE_REGISTRY_VERSION_MISMATCH` | **JOINT** | In `ErrorCode` StrEnum (wire value = name). Agents switch on `code`. Documented in CLAUDE.md error-codes list. |
| **Error code `CLARION_OUT_OF_SYNC`** | `cli_commands/sei.py:47`, `instructions.md:103`, `SKILL.md:199` | → `LOOMWEAVE_OUT_OF_SYNC` | **JOINT** | Emitted as JSON `code`. |
| **Remediation command string** | `cli_commands/sei.py:47` `"remediation_command": "clarion analyze"` | `clarion analyze` → `loomweave analyze` | **SIBLING** | Hands an agent a literal sibling CLI invocation — must match Loomweave's renamed binary. |
| **Capability / api_version probe** | `registry.py:69/483-492` `EXPECTED_CLARION_API_VERSION`, `core.py:1071/1210/1257` `clarion_api_version`, capabilities URL | endpoint/header semantics | **JOINT** | The version-negotiation handshake with the registry. Endpoint path & header names need confirming against Loomweave's renamed surface. |
| **`loom://` URI scheme** | `docs/plans/2026-05-17-loom-uri-spec.md` (spec); `is_loom_scoped_path` / `LOOM_*` in code | `loom://` → `weft://` | **JOINT** | Federation-wide URI scheme. Check for stored `loom://` values (would become Tier 0). |

---

## Tier 2 — CODE (Filigree-owned; rename freely, mechanical)

No external contract. Safe to rename in one pass *for axis A*; axis B by
identifier only (substring hazard).

**Axis B — the `generations/loom/` API-generation module → `generations/weft/`:**
- Package dir `src/filigree/generations/loom/` → `generations/weft/`
  (`__init__.py`, `types.py` — 79 hits, `adapters.py` — 76 hits).
- Router factory `create_loom_router` (8) → `create_weft_router`.
- DTO types: `IssueLoom`, `SlimIssueLoom`, `IssueLoomWithFiles`,
  `IssueLoomWithUnblocked`, `BlockedIssueLoom`, `FileRecordLoom`,
  `FileAssocLoom`, `ScanFindingLoom`, `ScanIngestResponseLoom`, `ScannerLoom`,
  `PackLoom`, `ObservationLoom`, `CommentRecordLoom`, `ChangeRecordLoom`,
  `IssueEventLoom`, `TypeSummaryLoom`, `ScannerConfigLoom`, … → `*Weft`.
- Adapter fns `*_to_loom` (`issue_to_loom`, `slim_issue_to_loom`,
  `scan_finding_to_loom`, `observation_to_loom`, `file_record_to_loom`,
  `change_record_to_loom`, `comment_record_to_loom`,
  `scan_ingest_result_to_loom`, `type_template_to_loom`,
  `scanner_config_to_loom`, `blocked_issue_to_loom`, `pack_to_loom`,
  `file_assoc_to_loom`, `issue_event_to_loom`, …) → `*_to_weft`.
- Heaviest call sites: `dashboard_routes/issues.py` (103), `files.py` (44),
  `analytics.py` (23), `releases.py`.

**Axis A — Clarion registry/SEI internals → Loomweave:**
- Types/classes: `ClarionRegistry`, `ClarionConfig`, `ClarionEntityId`,
  `ClarionResolvedFile`, `ClarionOutOfSyncError`, `ClarionRotationBanner` →
  `Loomweave*`.
- Factories/helpers: `make_clarion_entity_id` (15), `_build_clarion_registry`,
  `normalize_clarion_base_url`, `_ClarionLocalFallbackRegistry`,
  `probe_clarion_capabilities`, `reprobe_clarion_capabilities`,
  `validate_clarion_capabilities`, `_run_initial_clarion_capability_probe`,
  `_resolve_clarion_auth_token`, `_validate_clarion_token_origin`,
  `require_clarion_base_url`, `skip_clarion_capability_probe`.
- Constants: `DEFAULT_CLARION_TOKEN_ENV`, `CLARION_BATCH_MAX_QUERIES`,
  `EXPECTED_CLARION_API_VERSION`, `CLARION_RESOLVE_FILE_MAX_ATTEMPTS`,
  `CLARION_RESOLVE_FILE_RETRY_BACKOFF_SECONDS`.
- Attrs/locals: `clarion_config`, `clarion_api_version`, `_clarion_base_url`,
  `_clarion_headers`, `_clarion_timeout_seconds`, `_clarion_follow_redirects`,
  `clarion_conn`, `clarion_db_path`, `clarion_instance_id`,
  `clarion_instance_rotated`, `unknown_clarion_keys`,
  `clarion_identity_resolve_batch_url`, `clarion_files_batch_url`,
  `clarion_capabilities_url`, `clarion_file_read_url`.
- Heaviest files: `registry.py` (177), `core.py` (150), `sei_backfill.py` (55),
  `db_entity_associations.py` (34), `cli_commands/files.py` (15),
  `cli_commands/sei.py` (12), `mcp_tools/entities.py` (12),
  `dashboard_routes/files.py` / `entities.py`, `types/core.py`,
  `db_schema.py`, `install_support/doctor.py`.

**Dashboard JS (separate biome gate, see CLAUDE.md):**
`static/js/views/detail.js`, `static/js/app.js`, `static/dashboard.html` —
`clarionRotationBanner`, generation labels.

---

## Tier 3 — DOCS, agent instructions, CHANGELOG (no wire risk; do for coherence)

| Surface | Files |
|---------|-------|
| **Agent-facing instructions** (ship in package) | `src/filigree/data/instructions.md` (Clarion/SEI/error codes), `src/filigree/skills/filigree-workflow/SKILL.md` |
| **Repo CLAUDE.md** | entity-association ADR-029 blurb, error-codes list (`CLARION_REGISTRY_VERSION_MISMATCH`), `Clarion` mentions |
| **ADRs** | ADR-002 (API generations/federation posture), ADR-014 (registry backend), ADR-017 (SEI conformance), ADR-012 (actor identity threat model) |
| **Federation docs** | `docs/federation/contracts.md`, `registry-backend-launch-runbook.md` |
| **Plans** | `loom-uri-spec.md`, `shuttle-design.md`, `2.0-federation-work-package.md`, `2.0-stage-2b-rebaseline.md`, registry-backend sequencing, planning-deprecation |
| **Top-level** | `README.md` (Loom federation framing, `/api/loom/*`), `ROADMAP.md`, `CHANGELOG.md` (70 hits — historical; rename *new* 3.0.0 entries only, leave shipped-version history) |
| **CI** | `.github/workflows/ci.yml` (15 hits — job names, env, test selectors) |
| **Tests** | 100+ files; `tests/_fakes/clarion_http.py` (the fake Clarion server), `tests/federation/test_sei_oracle_live_clarion.py`, `tests/integration/test_clarion_*`, `tests/unit/test_clarion_capabilities_probe.py`, conftests. Rename with the code they exercise. |

> **CHANGELOG nuance:** entries under already-shipped version headings record
> history accurately — leave them. Only rename references in the
> `[Unreleased]` / `[3.0.0]` section, and add a `### Changed` (BREAKING) note
> documenting the rename.

---

## 3. The cross-axis collision

`CLARION_LOOM_TOKEN` (`registry.py:56`) is the only identifier on **both** axes:
`CLARION` (product) + `LOOM` (federation). It is the federation auth token env
var, deployment-set. Proposed `LOOMWEAVE_WEFT_TOKEN` — confirm the exact name
with the federation hub doctrine since it pairs with the audience claim (Tier 1).

---

## 4. MCP tool surface — mostly already clean

The MCP tool names are **already de-branded** (`entity_association_add`,
`finding_promote_and_attach_entity`, `entity_association_list_by_entity` — not
`clarion_*`). The legacy survives in **semantics and values** (the
`clarion:eid:` SEI prefix an `entity_id` may carry; the
`CLARION_REGISTRY_VERSION_MISMATCH` envelope `mcp_server.py` emits), not in tool
names. So the MCP *naming* axis is largely done; the MCP *value/error* axis is
Tier 0/1 above.

---

## 5. Reconciliation with in-flight work

| Existing item | Effect of rebrand |
|---------------|-------------------|
| `filigree-45d76e71bb` "De-Clarionize entity-association naming **with compatibility aliases**" | **Redirected.** The public projection already exposes `entity_id`; this task planned *aliases*. Hard-break decision (§Decision) **drops the alias requirement** — finish it as a clean column rename `clarion_entity_id` → `entity_id` with a one-shot data migration, folded into the rebrand. |
| MCP namespacing `filigree-7771610917` (`filigree_finding_list`, …) | Independent breaking wire change also riding 3.0.0. Sequence together so consumers absorb one cutover, not two. |
| `project_sei_conformance` / ADR-017 / `filigree sei-backfill` | Owns the `clarion:eid:` prefix lifecycle. The Tier-0 SEI-prefix migration **must** be co-designed here — backfill is the natural vehicle to rewrite stored prefixes. |
| `project_3_0_0_release` (de-Clarionize / TransitionMode bundle already TODO) | The rebrand is a superset of the planned "de-Clarionize" work. Re-scope that bundle to "full Clarion→Loomweave / Loom→Weft rebrand." |

---

## 6. Open coordination items (cannot close unilaterally)

1. **Loomweave** must rename in lockstep: SEI emitter prefix (`loomweave:eid:`),
   capabilities endpoint/`api_version` header, CLI binary (`loomweave analyze`),
   and confirm the registry-backend handshake values.
2. **Federation hub** (`~/weft` formerly `~/loom`, `doctrine.md`) must bless the
   new audience claim, token env-var name, and `weft://` URI scheme **before**
   tokens are re-issued.
3. **Wardline / Shuttle** consume `/api/loom/*` and the federation token — they
   break at 3.0.0; notify and sequence.
4. **Token re-issuance** (audience `loom`→`weft`) is an operational step, not a
   code change — schedule with the deploy.
5. **Legis re-sign pass** (Tier 0): once the SEI prefix flips, Legis must re-cut
   the HMAC for every governed binding over the new `entity_id`. Until it does,
   stored `signature`s are stale-by-design (Filigree never verifies them, so
   reads don't break — but governance re-verification downstream would). Gate
   the prefix migration on this.
6. **Legis rename (target unknown).** If Legis is also rebranding, its surface
   in Filigree is a *parked* rub point — `legis_client.py`, `governance.py`,
   env `LEGIS_URL` / `LEGIS_API_TOKEN`, type names `LegisGateResult` /
   `LegisGateStatus`, and the `{LEGIS_URL}/filigree/issues/{id}/closure-gate`
   path. **Cannot execute without Legis's new name** — block on the hub
   publishing the full renamed roster.

---

## 7. Suggested execution order (when greenlit — not this pass)

1. Lock the **names** (env var, audience, error codes, URI scheme, SEI prefix)
   with the federation hub + siblings. Nothing else can start safely first.
2. Tier 2 axis A (Clarion→Loomweave code) — mechanical, isolated, low risk.
3. Tier 2 axis B (`generations/loom`→`weft`) — **by identifier, never `sed`**.
4. Tier 0 data migrations (column, SEI prefix, config literal/section) + tests.
5. Tier 1 wire flip + Tier 3 docs/CHANGELOG, in the same 3.0.0 cut as MCP
   namespacing.
6. Re-issue tokens; notify siblings; ship.
