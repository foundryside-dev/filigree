# Loomweave / Weft Rebrand — Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut Filigree's entire federation contract surface from the Clarion/Loom names to the locked Loomweave/Weft names — code identifiers, the stored data (DB column, SEI prefix, finding rule-id prefix), the wire surface (`/api/loom`→`/api/weft`, audience, token env var), and the docs — as the **last change on `release/3.0.0`** before the major is cut.

**Architecture:** Hard wire-break, **no compatibility aliases** (owner decision 2026-06-05). Two independent rename axes — **A: Clarion→Loomweave** (sibling product / registry / SEI), **B: Loom→Weft** (federation + the named API generation). Tier-ordered by coordination cost: isolated code renames first (T2A, T2B), then the `v25→v26` data migration (T0), then the wire flip (T1), then docs (T3). A Legis HMAC re-sign pass is coordinated out-of-band (see Task 9).

**Tech Stack:** Python 3 / SQLite (PRAGMA `user_version` migrations) / FastAPI / pytest / ruff / mypy. Dashboard JS under `static/js/` uses the separate biome gate.

---

## Locked contract (G0 — published by the hub)

| Surface | Old | New (LOCKED) |
|---|---|---|
| Sibling product | `Clarion` | `Loomweave` |
| Federation | `Loom` | `Weft` |
| HTTP generation | `/api/loom/*`, gen `"loom"` | `/api/weft/*`, gen `"weft"` |
| Entity-assoc column + emit key | `clarion_entity_id` | `loomweave_entity_id` |
| SEI value prefix | `clarion:eid:` | `loomweave:eid:` |
| Finding rule-id prefix | `CLA-` | `LMWV-` |
| Federation token env var | `CLARION_LOOM_TOKEN` | **`WEFT_TOKEN`** ⚠️ (hub locked the short form — our inventory had proposed `LOOMWEAVE_WEFT_TOKEN`; `WEFT_TOKEN` wins) |
| Token audience claim | `"loom"` | `"weft"` |
| `registry_backend` literal / `[clarion]` section | `"clarion"` / `[clarion]` | `"loomweave"` / `[loomweave]` |

**Decisions baked into this plan (owner, 2026-06-06):**
- **SEI prefix ships now**, with a Legis re-sign pass coordinated in lockstep (Task 9). Stored signatures are stale-pending-reissue — an accepted, documented transient; Filigree never verifies them, so reads do not break.
- **Lands directly on `release/3.0.0`** (current branch). No branch switch.
- **This is the last change on the branch.** Cover all tiers to completion; leave the suite green and the CHANGELOG closed.

## QUARANTINED — explicitly NOT in this plan (hub has not locked them)

Do **not** touch these; they are not yet blessed by G0. Each is a tracked residual:
1. Registry **error codes** `CLARION_REGISTRY_VERSION_MISMATCH` / `CLARION_OUT_OF_SYNC` — table is silent on whether the `ErrorCode` enum values rename. Leave as-is.
2. **`weft://` URI scheme** (`loom://` stays until locked).
3. **Capabilities / `api_version` probe** endpoint + header semantics.
4. **Legis surface rename** — Legis's new name is unpublished (parked subtask `filigree-58ccd105b7`).

> If the hub publishes these mid-execution, add tasks; do not improvise names.

## Pre-flight (run once, before Task 1)

- [ ] Confirm on branch `release/3.0.0`: `git -C . branch --show-current` → `release/3.0.0`.
- [ ] Baseline green: `uv run pytest --tb=short` → all pass. (If red at baseline, stop and report — do not start a rename on a red suite.)
- [ ] Skim the inventory for anchor context: `docs/plans/2026-06-05-clarion-loomweave-loom-weft-rebrand-inventory.md`.

> **Execution caveat (applies to every task):** `clarion` is a safe, distinctive token. **`loom` is NOT** — it is a substring of `bloom`/`gloom` and doubles as the API-generation name. **Axis B (`loom`→`weft`) must be renamed by identifier, never by blind `sed`.**

---

## Task 1: T2A — rename Clarion→Loomweave internal code (axis A)

**Scope:** Python *identifiers* only — types, functions, constants, attrs, locals. **Do NOT change** in this task: the DB column string `clarion_entity_id`, the `SEI_PREFIX = "clarion:eid:"` value, the `registry_backend` literal `"clarion"`, or any historical migration body — those are DATA, migrated in Tasks 3–4.

**Files (heaviest first):** `src/filigree/registry.py` (177), `core.py` (150), `sei_backfill.py` (55), `db_entity_associations.py` (34), `cli_commands/files.py` (15), `cli_commands/sei.py` (12), `mcp_tools/entities.py` (12), `dashboard_routes/files.py` / `entities.py`, `types/core.py`, `db_schema.py`, `install_support/doctor.py`. Tests under `tests/` that exercise them (rename in lockstep).

**Rename map (identifiers):**
- Types/classes: `ClarionRegistry`→`LoomweaveRegistry`, `ClarionConfig`→`LoomweaveConfig`, `ClarionEntityId`→`LoomweaveEntityId`, `ClarionResolvedFile`→`LoomweaveResolvedFile`, `ClarionOutOfSyncError`→`LoomweaveOutOfSyncError`, `ClarionRotationBanner`→`LoomweaveRotationBanner`.
- Factories/helpers: `make_clarion_entity_id`→`make_loomweave_entity_id`, `_build_clarion_registry`→`_build_loomweave_registry`, `normalize_clarion_base_url`→`normalize_loomweave_base_url`, `_ClarionLocalFallbackRegistry`→`_LoomweaveLocalFallbackRegistry`, `probe_clarion_capabilities`→`probe_loomweave_capabilities`, `reprobe_clarion_capabilities`→`reprobe_loomweave_capabilities`, `validate_clarion_capabilities`→`validate_loomweave_capabilities`, `_run_initial_clarion_capability_probe`→`_run_initial_loomweave_capability_probe`, `_resolve_clarion_auth_token`→`_resolve_loomweave_auth_token`, `_validate_clarion_token_origin`→`_validate_loomweave_token_origin`, `require_clarion_base_url`→`require_loomweave_base_url`, `skip_clarion_capability_probe`→`skip_loomweave_capability_probe`, `_clarion_headers`→`_loomweave_headers`, `_clarion_follow_redirects`→`_loomweave_follow_redirects`, `clarion_files_batch_url`→`loomweave_files_batch_url`, `clarion_file_read_url`→`loomweave_file_read_url`.
- Constants: `DEFAULT_CLARION_TOKEN_ENV`→`DEFAULT_LOOMWEAVE_TOKEN_ENV`, `CLARION_BATCH_MAX_QUERIES`→`LOOMWEAVE_BATCH_MAX_QUERIES`, `EXPECTED_CLARION_API_VERSION`→`EXPECTED_LOOMWEAVE_API_VERSION`, `CLARION_RESOLVE_FILE_MAX_ATTEMPTS`→`LOOMWEAVE_RESOLVE_FILE_MAX_ATTEMPTS`, `CLARION_RESOLVE_FILE_RETRY_BACKOFF_SECONDS`→`LOOMWEAVE_RESOLVE_FILE_RETRY_BACKOFF_SECONDS`.
- Attrs/locals: `clarion_config`→`loomweave_config`, `clarion_api_version`→`loomweave_api_version`, `_clarion_base_url`→`_loomweave_base_url`, `_clarion_headers`→`_loomweave_headers`, `_clarion_timeout_seconds`→`_loomweave_timeout_seconds`, `_clarion_follow_redirects`→`_loomweave_follow_redirects`, `clarion_conn`→`loomweave_conn`, `clarion_db_path`→`loomweave_db_path`, `clarion_instance_id`→`loomweave_instance_id`, `clarion_instance_rotated`→`loomweave_instance_rotated`, `unknown_clarion_keys`→`unknown_loomweave_keys`, `clarion_identity_resolve_batch_url`→`loomweave_identity_resolve_batch_url`, `clarion_files_batch_url`→`loomweave_files_batch_url`, `clarion_capabilities_url`→`loomweave_capabilities_url`, `clarion_file_read_url`→`loomweave_file_read_url`.

- [ ] **Step 1: Rename by identifier (IDE/LSP rename-symbol, one symbol at a time).**

Use symbol-rename (not text replace) for each entry in the map above. The DB-column TypedDict field `clarion_entity_id` (in `EntityAssociationRow`, `db_entity_associations.py`) **stays** this task — it tracks the physical column, renamed in Task 3. The `ClarionEntityId` *type alias* renames to `LoomweaveEntityId` now; update the `EntityAssociationRow` field *type* (`entity_id: LoomweaveEntityId`, `clarion_entity_id: LoomweaveEntityId`) but not the field *name* `clarion_entity_id`.

- [ ] **Step 2: Verify no brand *identifier* remains (data values excepted).**

Run:
```bash
grep -rnE "Clarion|clarion_(config|conn|api_version|db_path|instance|headers|timeout|capabilities|file|files|identity|base_url)|_clarion_|make_clarion|probe_clarion|_build_clarion|DEFAULT_CLARION|EXPECTED_CLARION|CLARION_(BATCH|RESOLVE)" src/filigree/ --include=*.py
```
Expected: the **only** surviving `clarion` hits are the deliberate data values — the column name `clarion_entity_id` (TypedDict field + historical migrations), `SEI_PREFIX = "clarion:eid:"`, `registry_backend == "clarion"` literals, the `[clarion]` config keys, and the `CLARION_*` error-code enum (quarantined). Everything CamelCase/identifier should be gone. Eyeball the list against that allowlist.

- [ ] **Step 3: Type-check (drives completeness — flags every missed reference).**

Run: `uv run mypy src/filigree/`
Expected: clean. A `name-defined`/`attr-defined` error names any call site you missed — fix and re-run.

- [ ] **Step 4: Run the suite.**

Run: `uv run pytest tests/ -k "registry or clarion or loomweave or sei or entit or capabilit" -q`
Expected: PASS (rename test files in lockstep so imports resolve).

- [ ] **Step 5: Commit.**

```bash
git add src/filigree/ tests/
git commit -m "refactor(rebrand): rename Clarion->Loomweave internal code (axis A, T2A)

Mechanical identifier rename of the registry/SEI internals — types,
factories, constants, attrs. Data values (clarion_entity_id column,
clarion:eid: SEI prefix, registry_backend literal) are untouched here;
they migrate in the v26 data pass. No external contract changes in this
commit."
```

**Definition of Done:**
- [ ] All map identifiers renamed; grep shows only the data-value allowlist surviving
- [ ] mypy clean; targeted suite green
- [ ] DB column field name `clarion_entity_id` deliberately unchanged (Task 3 owns it)
- [ ] Committed

---

## Task 2: T2B — rename `generations/loom/` → `generations/weft/` (axis B)

**Scope:** the named API-generation module. **By identifier only** — `loom` is substring-hazardous.

**Files:** package dir `src/filigree/generations/loom/` → `generations/weft/` (`__init__.py`, `types.py` ~79 hits, `adapters.py` ~76 hits); call sites `dashboard_routes/issues.py` (103), `files.py` (44), `analytics.py` (23), `releases.py`; imports throughout. Tests in lockstep.

**Rename map:**
- Dir: `src/filigree/generations/loom/` → `src/filigree/generations/weft/` (`git mv`).
- Router factory: `create_loom_router` → `create_weft_router` (8 sites).
- DTO types: every `*Loom` → `*Weft` — `IssueLoom`, `SlimIssueLoom`, `IssueLoomWithFiles`, `IssueLoomWithUnblocked`, `BlockedIssueLoom`, `FileRecordLoom`, `FileAssocLoom`, `ScanFindingLoom`, `ScanIngestResponseLoom`, `ScannerLoom`, `PackLoom`, `ObservationLoom`, `CommentRecordLoom`, `ChangeRecordLoom`, `IssueEventLoom`, `TypeSummaryLoom`, `ScannerConfigLoom`, … → `*Weft`.
- Adapter fns: every `*_to_loom` → `*_to_weft` — `issue_to_loom`, `slim_issue_to_loom`, `scan_finding_to_loom`, `observation_to_loom`, `file_record_to_loom`, `change_record_to_loom`, `comment_record_to_loom`, `scan_ingest_result_to_loom`, `type_template_to_loom`, `scanner_config_to_loom`, `blocked_issue_to_loom`, `pack_to_loom`, `file_assoc_to_loom`, `issue_event_to_loom`, … → `*_to_weft`.

> The HTTP **route** prefix `/api/loom` and the generation **token** `"loom"` are the *wire* surface — they flip in **Task 5**, not here. This task is the Python module/type/fn names only.

- [ ] **Step 1: Move the package.**

```bash
git mv src/filigree/generations/loom src/filigree/generations/weft
```

- [ ] **Step 2: Rename symbols by identifier.**

Symbol-rename each `*Loom` type, each `*_to_loom` fn, and `create_loom_router`→`create_weft_router`. Update all imports (`from filigree.generations.loom...` → `...weft...`). Do **not** text-replace bare `loom` — only the listed identifiers.

- [ ] **Step 3: Verify no `*Loom`/`_to_loom`/`generations.loom` identifier remains.**

Run:
```bash
grep -rnE "Loom\b|_to_loom|generations[./]loom|create_loom_router" src/filigree/ --include=*.py
```
Expected: zero hits. (Bare `/api/loom` strings and gen `"loom"` token remain until Task 5 — those are not matched by this pattern.)

- [ ] **Step 4: Type-check + suite.**

Run: `uv run mypy src/filigree/ && uv run pytest tests/ -k "generation or weft or issues_routes or dashboard_routes or adapter" -q`
Expected: mypy clean, tests PASS.

- [ ] **Step 5: Commit.**

```bash
git add -A src/filigree/generations tests/
git commit -m "refactor(rebrand): rename generations/loom -> generations/weft (axis B, T2B)

By-identifier rename of the named API-generation module: package dir,
*Loom DTOs -> *Weft, *_to_loom adapters -> *_to_weft, create_loom_router
-> create_weft_router. The /api/loom HTTP route prefix and the \"loom\"
generation token are wire surface and flip in the wire-flip task."
```

**Definition of Done:**
- [ ] Package moved; all `*Loom`/`*_to_loom`/`create_loom_router` identifiers renamed
- [ ] grep clean for the identifier pattern; mypy clean; targeted suite green
- [ ] Committed

---

## Task 3: T0 — `v25→v26` data migration (column + SEI prefix + rule-id prefix)

**Files:**
- Modify: `src/filigree/db_schema.py:587` (`CURRENT_SCHEMA_VERSION = 25` → `26`)
- Modify: `src/filigree/migrations.py` (add `migrate_v25_to_v26`; register in `MIGRATIONS` dict ~line 829)
- Modify: `src/filigree/db_entity_associations.py` (projection emit key `clarion_entity_id` → `loomweave_entity_id`; `EntityAssociationRow` field rename)
- Modify: JSONL export/import (`db_meta.py:897,1389,1394` embed the column name — confirm with `grep -n "clarion_entity_id" src/filigree/db_meta.py`)
- Test: `tests/` migration test module + an entity-assoc export/import round-trip test

**Context:** The physical SQLite column is `clarion_entity_id` (created in `migrate_v14_to_v15`). v26 renames the column, rewrites stored SEI-prefixed values, and rewrites stored finding `rule_id` prefixes — all in one version hop. SQLite ≥3.25 `ALTER TABLE ... RENAME COLUMN` rewrites the `PRIMARY KEY` reference automatically; verify in the test.

- [ ] **Step 1: Write the failing migration test.**

```python
def test_migrate_v25_to_v26_renames_column_and_rewrites_prefixes(tmp_path) -> None:
    import sqlite3
    from filigree.migrations import migrate_v25_to_v26

    conn = sqlite3.connect(":memory:")
    # minimal v25 shape
    conn.execute("CREATE TABLE entity_associations (issue_id TEXT NOT NULL, clarion_entity_id TEXT NOT NULL, "
                 "content_hash_at_attach TEXT NOT NULL, attached_at TEXT NOT NULL, attached_by TEXT NOT NULL, "
                 "PRIMARY KEY (issue_id, clarion_entity_id))")
    conn.execute("CREATE TABLE findings (id TEXT PRIMARY KEY, rule_id TEXT)")
    conn.execute("INSERT INTO entity_associations VALUES ('filigree-a', 'clarion:eid:deadbeef', 'h', 't', 'me')")
    conn.execute("INSERT INTO findings VALUES ('f1', 'CLA-PY-UNSAFE-EVAL')")
    conn.commit()

    migrate_v25_to_v26(conn)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(entity_associations)").fetchall()]
    assert "loomweave_entity_id" in cols and "clarion_entity_id" not in cols
    eid = conn.execute("SELECT loomweave_entity_id FROM entity_associations").fetchone()[0]
    assert eid == "loomweave:eid:deadbeef"          # prefix rewritten, suffix preserved
    rid = conn.execute("SELECT rule_id FROM findings").fetchone()[0]
    assert rid == "LMWV-PY-UNSAFE-EVAL"             # CLA- -> LMWV-, suffix preserved
```

- [ ] **Step 2: Run to verify failure.**

Run: `uv run pytest tests/ -k migrate_v25_to_v26 -v`
Expected: FAIL — `ImportError: cannot import name 'migrate_v25_to_v26'`.

- [ ] **Step 3: Implement the migration + register it + bump the version.**

In `migrations.py` (place after `migrate_v24_to_v25`):
```python
def migrate_v25_to_v26(conn: sqlite3.Connection) -> None:
    """v25 -> v26: Loomweave/Weft rebrand data pass.

    Renames the entity-association column ``clarion_entity_id`` ->
    ``loomweave_entity_id`` (the PRIMARY KEY reference is rewritten by
    SQLite's RENAME COLUMN), rewrites stored SEI prefixes
    ``clarion:eid:`` -> ``loomweave:eid:`` and finding rule-id prefixes
    ``CLA-`` -> ``LMWV-`` in place. Suffixes are preserved. Idempotent
    under re-run (guards on the source name/prefix existing).

    NOTE (Legis): rewriting the SEI prefix changes the entity_id string the
    Legis HMAC was cut over, so every stored ``signature`` becomes
    stale-pending-reissue. Filigree never verifies the signature, so reads
    do not break; Legis re-signs in lockstep (see the rebrand epic, T0b).
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(entity_associations)").fetchall()]
    if "clarion_entity_id" in cols and "loomweave_entity_id" not in cols:
        conn.execute("ALTER TABLE entity_associations RENAME COLUMN clarion_entity_id TO loomweave_entity_id")
    # index rename: drop the old (named in v14->v15) and re-add under the new name
    conn.execute("DROP INDEX IF EXISTS ix_entity_assoc_entity")
    add_index(conn, "ix_entity_assoc_entity", "entity_associations", ["loomweave_entity_id"])
    conn.execute(
        "UPDATE entity_associations SET loomweave_entity_id = "
        "'loomweave:eid:' || substr(loomweave_entity_id, length('clarion:eid:') + 1) "
        "WHERE loomweave_entity_id LIKE 'clarion:eid:%'"
    )
    conn.execute(
        "UPDATE findings SET rule_id = 'LMWV-' || substr(rule_id, length('CLA-') + 1) "
        "WHERE rule_id LIKE 'CLA-%'"
    )
```
Register it: in the `MIGRATIONS` dict add `25: migrate_v25_to_v26,`. Bump `db_schema.py:587` to `CURRENT_SCHEMA_VERSION = 26`.

> Confirm the findings table/column names with `grep -nE "CREATE TABLE findings|rule_id" src/filigree/db_schema.py` before trusting the `findings`/`rule_id` literals above; adjust the UPDATE if they differ.

- [ ] **Step 4: Update the projection emit key + TypedDict field.**

In `db_entity_associations.py`: the projection currently emits both `entity_id` and `clarion_entity_id` — rename the compat key to `loomweave_entity_id` (canonical `entity_id` stays primary). Rename the `EntityAssociationRow` field `clarion_entity_id` → `loomweave_entity_id`. Update the SELECT/row-mapping to read the renamed column. Update any JSONL export/import that names the column (`db_meta.py`).

- [ ] **Step 5: Write the export/import round-trip test, then run all.**

```python
def test_entity_assoc_export_import_roundtrips_renamed_key(db) -> None:
    iid = db.create_issue(type="task", title="t").id
    db.add_entity_association(iid, "loomweave:eid:abc", content_hash="h", attached_by="me")
    blob = db.export_jsonl()
    assert "loomweave_entity_id" in blob and "clarion_entity_id" not in blob
    # re-import into a fresh DB round-trips without loss
```
Run:
```bash
uv run pytest tests/ -k "migrate_v25_to_v26 or entity_assoc or export_import or roundtrip" -v
uv run pytest tests/ -k "migration or schema" -q
uv run mypy src/filigree/
```
Expected: all PASS, mypy clean.

- [ ] **Step 6: Commit.**

```bash
git add src/filigree/db_schema.py src/filigree/migrations.py src/filigree/db_entity_associations.py src/filigree/db_meta.py tests/
git commit -m "feat!: v26 migration — Loomweave/Weft data rename (column, SEI prefix, rule-id)

Rename entity_associations.clarion_entity_id -> loomweave_entity_id,
rewrite stored clarion:eid: -> loomweave:eid: and CLA- -> LMWV- finding
rule-ids in place, bump CURRENT_SCHEMA_VERSION to 26, flip the projection
emit key and JSONL export/import. Stored Legis signatures are
stale-pending-reissue by design (Legis re-signs in lockstep).

BREAKING CHANGE: the entity-association compat key is now loomweave_entity_id;
stored SEI prefixes and finding rule-ids are rewritten to the new namespace."
```

**Definition of Done:**
- [ ] `migrate_v25_to_v26` renames column + rewrites both prefixes; idempotent; registered; version bumped to 26
- [ ] Projection emit key + `EntityAssociationRow` field renamed; JSONL round-trips the new key
- [ ] Migration test + export/import test green; full migration/schema suite green; mypy clean
- [ ] Legis re-sign coupling noted in the migration docstring (Task 9 tracks the pass)
- [ ] Committed

---

## Task 4: T0 — flip the `SEI_PREFIX` constant and its code-side checks

**Files:**
- Modify: `src/filigree/sei_backfill.py:56` (`SEI_PREFIX = "clarion:eid:"` → `"loomweave:eid:"`) — all checks at `:241,248,307,339,386,466` read this constant, so they follow automatically
- Modify: `src/filigree/registry.py:168,175` (prefix checks), `cli_commands/sei.py:34`, `src/filigree/data/instructions.md:80` (doc mention)
- Test: SEI backfill test module

**Context:** The constant is the emitter-match for stored values. Task 3 migrated the *stored rows*; this task flips the *code constant* so newly-stored/checked values use the new prefix. Do them in the same plan so stored data and code agree.

- [ ] **Step 1: Write/adjust the failing test.**

```python
def test_sei_prefix_is_loomweave() -> None:
    from filigree.sei_backfill import SEI_PREFIX
    assert SEI_PREFIX == "loomweave:eid:"

def test_sei_value_recognised_with_new_prefix() -> None:
    from filigree.sei_backfill import SEI_PREFIX
    assert "loomweave:eid:abc".startswith(SEI_PREFIX)
```

- [ ] **Step 2: Run to verify failure.**

Run: `uv run pytest tests/ -k "sei_prefix or sei_value_recognised" -v`
Expected: FAIL — constant still `clarion:eid:`.

- [ ] **Step 3: Flip the constant + the literal mentions.**

Set `SEI_PREFIX = "loomweave:eid:"`. Update any hard-coded `"clarion:eid:"` literals that don't go through the constant (grep below), and the `instructions.md:80` doc line, and the `cli_commands/sei.py:34` reference.

- [ ] **Step 4: Verify no `clarion:eid:` literal remains in code, then run.**

Run:
```bash
grep -rn "clarion:eid:" src/filigree/ --include=*.py
uv run pytest tests/ -k "sei or backfill" -q
```
Expected: grep returns zero (test fixtures handled in Task 8); suite PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/filigree/sei_backfill.py src/filigree/registry.py src/filigree/cli_commands/sei.py src/filigree/data/instructions.md tests/
git commit -m "feat!: flip SEI_PREFIX to loomweave:eid: (T0)

The emitter-match constant and its code-side prefix checks now use the
loomweave:eid: namespace, matching the v26-migrated stored values and the
Loomweave SEI emitter.

BREAKING CHANGE: Filigree recognises loomweave:eid: SEI values, not clarion:eid:."
```

**Definition of Done:**
- [ ] `SEI_PREFIX = "loomweave:eid:"`; no `clarion:eid:` literal in `src/` Python; doc mention updated
- [ ] SEI/backfill suite green
- [ ] Committed

---

## Task 5: T0 — `registry_backend` literal / `[clarion]` section → loomweave + config migration

**Files:**
- Modify: `src/filigree/core.py:690,733,775-796,1049-1078` (literal `"clarion"`, `[clarion]` section keys, validation)
- Modify: the `RegistryBackend` type literal (find: `grep -rn "RegistryBackend = \|Literal\[.*clarion" src/filigree/`) — change member `"clarion"` → `"loomweave"`; `VALID_REGISTRY_BACKENDS` (`core.py:655`) derives from it via `get_args`
- Modify: `cli_commands/files.py:797` (`--to` Choice includes `clarion`), `admin.py:599`, `sei_backfill.py:158`, `registry.py:58-59` (`DEFAULT_TEST_REGISTRY_BACKENDS`, `REGISTRY_BACKEND_FEATURES` tuples)
- Add: a deployed-config migration (the literal lives in on-disk `.filigree.conf`)
- Test: config-load/validation test module

**Context:** `registry_backend = "clarion"` and `[clarion]` live in deployed `.filigree.conf` files. A code-literal flip alone breaks reading any project already on the Clarion backend. Provide a config read-shim or migration so an existing `"clarion"` config still loads (rename-on-read to `"loomweave"`), per the hard-break-with-clean-migration posture.

- [ ] **Step 1: Write the failing test.**

```python
def test_registry_backend_accepts_loomweave_literal() -> None:
    from filigree.core import VALID_REGISTRY_BACKENDS
    assert "loomweave" in VALID_REGISTRY_BACKENDS
    assert "clarion" not in VALID_REGISTRY_BACKENDS

def test_existing_clarion_config_migrates_on_load(tmp_path) -> None:
    # a deployed config still saying registry_backend="clarion" must load as loomweave
    conf = tmp_path / ".filigree.conf"
    conf.write_text('{"prefix":"f","version":1,"registry_backend":"clarion","clarion":{"base_url":"http://x"}}')
    cfg = load_project_config(conf)        # use the repo's actual loader name
    assert cfg["registry_backend"] == "loomweave"
    assert "loomweave" in cfg and "clarion" not in cfg
```

- [ ] **Step 2: Run to verify failure.**

Run: `uv run pytest tests/ -k "registry_backend_accepts_loomweave or clarion_config_migrates" -v`
Expected: FAIL — `"loomweave"` not yet a valid backend / loader doesn't rename.

- [ ] **Step 3: Implement.**

- Change the `RegistryBackend` literal member `"clarion"` → `"loomweave"`. `VALID_REGISTRY_BACKENDS` updates automatically.
- Replace the `"clarion"` literals in `core.py:690,733`, the `[clarion]`-section reads (`core.py:781-796`: `"clarion" not in raw`, `raw["clarion"]`, `clarion.base_url`), and the `--to` Choice / tuples with `"loomweave"`.
- Add a **rename-on-load shim** in the config loader: if `registry_backend == "clarion"`, set it to `"loomweave"`; if a `[clarion]` section is present and `[loomweave]` is not, move it. (One-shot, on read — keeps deployed configs working without manual edits.)

- [ ] **Step 4: Verify + run.**

Run:
```bash
grep -rnE "\"clarion\"|'clarion'|\[clarion\]" src/filigree/ --include=*.py | grep -v "migrate\|shim\|# legacy\|rename-on-load"
uv run pytest tests/ -k "config or registry_backend or validate_registry" -q
uv run mypy src/filigree/
```
Expected: only the deliberate legacy-shim references survive the grep; suite + mypy green.

- [ ] **Step 5: Commit.**

```bash
git add src/filigree/core.py src/filigree/registry.py src/filigree/cli_commands/files.py src/filigree/admin.py src/filigree/sei_backfill.py src/filigree/types/ tests/
git commit -m "feat!: rename registry_backend clarion -> loomweave + config migration (T0)

Flip the RegistryBackend literal, [clarion] config section, and the
migrate-registry --to choice to loomweave. Add a rename-on-load shim so a
deployed .filigree.conf still saying registry_backend=clarion loads as
loomweave without manual edits.

BREAKING CHANGE: registry_backend value and config section are now
'loomweave'; 'clarion' is accepted only via the one-shot load shim."
```

**Definition of Done:**
- [ ] `RegistryBackend`/`VALID_REGISTRY_BACKENDS` use `loomweave`; literals + section + `--to` choice flipped
- [ ] Deployed `clarion` config migrates on load; test proves it
- [ ] grep clean except the deliberate shim; suite + mypy green
- [ ] Committed

---

## Task 6: T1 — wire flip `/api/loom` → `/api/weft` + generation token

**Files:**
- Modify: `src/filigree/dashboard.py:104` (`protected_paths`), `:565` (docstring), `:594-597` (four `create_weft_router` mounts, `prefix="/loom"`→`"/weft"`), `:936` (docstring)
- Modify: `src/filigree/generations/weft/adapters.py:10` (gen token `"loom"`→`"weft"`)
- Modify: `dashboard_auth.py:53` generation-name branch (`rest == "loom"` / `rest.startswith("loom/")`) — **see Task 7 for the audience semantics; this task is the path token**
- Test: dashboard route test + an auth-scope test

**Context:** This is the externally-observable HTTP contract. Hard-break: consumers hard-coded to `/api/loom/` break at 3.0.0 (Wardline already updated per the peer table).

- [ ] **Step 1: Write the failing test.**

```python
def test_weft_routes_mounted_and_loom_gone(client) -> None:
    # a known loom endpoint now answers under /api/weft and 404s under /api/loom
    assert client.get("/api/weft/ready").status_code != 404
    assert client.get("/api/loom/ready").status_code == 404
```
(Use a real `/api/loom/*` endpoint from `dashboard_routes/issues.py` in place of `ready`.)

- [ ] **Step 2: Run to verify failure.**

Run: `uv run pytest tests/ -k weft_routes_mounted -v`
Expected: FAIL — routes still mounted under `/loom`.

- [ ] **Step 3: Flip the prefix + token.**

Change the four `prefix="/loom"` mounts to `"/weft"`; update `protected_paths` `"/api/loom/*"`→`"/api/weft/*"`; flip the generation token literal `"loom"`→`"weft"` in `adapters.py` and the negotiation branch in `dashboard_auth.py:53` (`rest == "weft"` / `rest.startswith("weft/")`); update the two docstrings.

- [ ] **Step 4: Verify + run.**

Run:
```bash
grep -rnE "/api/loom|prefix=\"/loom\"|== \"loom\"|startswith\(\"loom" src/filigree/ --include=*.py
uv run pytest tests/ -k "dashboard or route or auth_scope or generation" -q
```
Expected: grep zero (docs handled in Task 8); suite PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/filigree/dashboard.py src/filigree/dashboard_auth.py src/filigree/generations/weft/adapters.py tests/
git commit -m "feat!: flip wire surface /api/loom -> /api/weft + generation token (T1)

Mount the named generation under /api/weft, update protected_paths and the
generation negotiation token from loom to weft.

BREAKING CHANGE: the generation API is served at /api/weft/*; /api/loom/* is gone."
```

**Definition of Done:**
- [ ] Routes under `/api/weft`; `/api/loom` 404s; `protected_paths` + token flipped
- [ ] grep clean for the path/token; route + auth-scope suite green
- [ ] Committed

---

## Task 7: T1 — token AUDIENCE `"loom"` → `"weft"` (security-sensitive)

**Files:**
- Modify: `src/filigree/dashboard_auth.py:53` (audience match) + `LIVING_FEDERATION_ALIASES`/`CLASSIC_FEDERATION_ALIASES` if they encode the audience
- Test: auth/token-validation test module

**Context:** 🔴 The audience claim gates federation auth. Both sides must agree **and tokens must be re-issued** with `aud="weft"` — a mismatch fails auth federation-wide. The code change is small; the operational re-issue is a deploy step (note it; it is not a code commit). Sequenced **after** Task 6 because both touch `dashboard_auth.py:53`.

- [ ] **Step 1: Write the failing test.**

```python
def test_token_audience_is_weft(client_with_token) -> None:
    # a token minted with aud="weft" is accepted; aud="loom" is rejected
    assert client_with_token(aud="weft").get("/api/weft/ready").status_code != 401
    assert client_with_token(aud="loom").get("/api/weft/ready").status_code == 401
```
(Adapt to the repo's actual token-minting test helper and audience-check location.)

- [ ] **Step 2: Run to verify failure.**

Run: `uv run pytest tests/ -k token_audience_is_weft -v`
Expected: FAIL — `loom` audience still accepted.

- [ ] **Step 3: Flip the audience.**

Change the accepted audience value `"loom"`→`"weft"` wherever the token's `aud` is validated. If `LIVING_FEDERATION_ALIASES`/`CLASSIC_FEDERATION_ALIASES` carry audience semantics (not just path aliases), update accordingly — otherwise leave them (they are the scan-results/observations path aliases, unrelated to audience).

- [ ] **Step 4: Run.**

Run: `uv run pytest tests/ -k "auth or token or audience or federation" -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/filigree/dashboard_auth.py tests/
git commit -m "feat!: token audience loom -> weft (T1, security-sensitive)

The federation token audience claim is now weft. Deployments MUST re-issue
tokens with aud=weft; a loom-audience token is rejected.

BREAKING CHANGE: federation tokens must carry aud=weft."
```

**Operational note (NOT a code step):** schedule token re-issuance (`aud: loom`→`weft`) with the 3.0.0 deploy. Record on the rebrand epic.

**Definition of Done:**
- [ ] Accepted audience is `weft`; `loom`-aud token rejected; test proves both
- [ ] Auth suite green; re-issuance recorded as a deploy step on the epic
- [ ] Committed

---

## Task 8: T1 — federation token env var `CLARION_LOOM_TOKEN` → `WEFT_TOKEN`

**Files:**
- Modify: `src/filigree/registry.py:56` (`DEFAULT_LOOMWEAVE_TOKEN_ENV = "CLARION_LOOM_TOKEN"` → `"WEFT_TOKEN"`) — note the *constant* was renamed in Task 1; this changes its *value*
- Modify: `core.py:50,1155,1161` (references / log lines naming the env var), `registry.py:18` (docstring)
- Test: token-resolution test module

**Context:** 🔴 Deployment-set — breaks every deployment env, not just code. The hub locked the short form **`WEFT_TOKEN`** (not `LOOMWEAVE_WEFT_TOKEN`). Hard-break: no fallback read of the old var.

- [ ] **Step 1: Write the failing test.**

```python
def test_default_token_env_is_weft_token(monkeypatch) -> None:
    from filigree.registry import DEFAULT_LOOMWEAVE_TOKEN_ENV
    assert DEFAULT_LOOMWEAVE_TOKEN_ENV == "WEFT_TOKEN"

def test_token_resolved_from_weft_token_env(monkeypatch) -> None:
    monkeypatch.setenv("WEFT_TOKEN", "secret")
    monkeypatch.delenv("CLARION_LOOM_TOKEN", raising=False)
    # resolve via the repo's actual token-resolution path; expect "secret"
```

- [ ] **Step 2: Run to verify failure.**

Run: `uv run pytest tests/ -k "default_token_env_is_weft or token_resolved_from_weft" -v`
Expected: FAIL — default still `CLARION_LOOM_TOKEN`.

- [ ] **Step 3: Flip the value + references.**

Set `DEFAULT_LOOMWEAVE_TOKEN_ENV = "WEFT_TOKEN"`. Update the docstring at `registry.py:18-20` and the `core.py` log/reference lines that name `CLARION_LOOM_TOKEN`. **No fallback** to the old var (hard-break).

- [ ] **Step 4: Verify + run.**

Run:
```bash
grep -rn "CLARION_LOOM_TOKEN" src/filigree/ --include=*.py
uv run pytest tests/ -k "token or registry_auth or env" -q
```
Expected: grep zero in `src/` Python (docs handled in Task 8 below / CLAUDE.md in Task 9); suite PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/filigree/registry.py src/filigree/core.py tests/
git commit -m "feat!: federation token env var CLARION_LOOM_TOKEN -> WEFT_TOKEN (T1)

The default federation Bearer-token env var is now WEFT_TOKEN (hub-locked
short form). No fallback to the old name.

BREAKING CHANGE: set WEFT_TOKEN; CLARION_LOOM_TOKEN is no longer read."
```

**Operational note:** update every deployment env (`CLARION_LOOM_TOKEN`→`WEFT_TOKEN`) with the deploy. Record on the epic.

**Definition of Done:**
- [ ] Default env var value is `WEFT_TOKEN`; no `CLARION_LOOM_TOKEN` in `src/` Python; no fallback
- [ ] Token-resolution suite green
- [ ] Committed

---

## Task 9: T3 — docs, agent instructions, CHANGELOG, CI, test fixtures, dashboard JS

**Files:**
- `src/filigree/data/instructions.md` (Clarion/SEI/error-code mentions — **leave the quarantined `CLARION_*` error codes**), `src/filigree/skills/filigree-workflow/SKILL.md`
- Repo `CLAUDE.md` (entity-association ADR-029 blurb, `Clarion` mentions — **leave the `CLARION_REGISTRY_VERSION_MISMATCH` error-code list entry**, it's quarantined)
- ADRs: ADR-002, ADR-014, ADR-017, ADR-029 (note the rename; do not rewrite shipped decisions — add a rebrand note)
- `docs/federation/contracts.md`, `registry-backend-launch-runbook.md`
- `README.md`, `ROADMAP.md` (Loom→Weft framing, `/api/loom`→`/api/weft`)
- `CHANGELOG.md` — **only the `[3.0.0]` section**: add a `### Changed (BREAKING)` rebrand entry; leave shipped-version history untouched
- Test fixtures: `tests/fixtures/contracts/{classic,loom}/scan-results.json` (`CLA-PY-UNSAFE-EVAL`→`LMWV-PY-UNSAFE-EVAL`), `tests/_fakes/clarion_http.py`→`weave`/`loomweave` fake server, `tests/federation/test_sei_oracle_live_clarion.py`, `tests/integration/test_clarion_*`, `tests/unit/test_clarion_capabilities_probe.py`
- `.github/workflows/ci.yml` (job names, env, test selectors)
- Dashboard JS (**separate biome gate**): `static/js/views/detail.js`, `static/js/app.js`, `static/dashboard.html` (`clarionRotationBanner`, generation labels)

- [ ] **Step 1: Update fixtures + fakes (these are exercised by tests).**

Rename fixture rule-ids `CLA-`→`LMWV-`, rename `tests/_fakes/clarion_http.py`→`loomweave_http.py` and its server class, rename `test_clarion_*` files in lockstep with the code they exercise. Run the suite after to catch breaks: `uv run pytest --tb=short`.

- [ ] **Step 2: Update agent-facing instructions + CLAUDE.md.**

Rename `Clarion`→`Loomweave` and `Loom`→`Weft` framing in `instructions.md`, `SKILL.md`, `CLAUDE.md`. **Preserve** the quarantined `CLARION_*` error-code names in the error-codes lists (they are NOT renamed in this plan).

- [ ] **Step 3: Update README/ROADMAP/ADRs/federation docs + CHANGELOG `[3.0.0]`.**

Add the breaking-change rebrand entry to CHANGELOG `[3.0.0]`:
```markdown
### Changed (BREAKING)

- **Loomweave / Weft rebrand (schema v26).** The Clarion→Loomweave (sibling/
  registry/SEI) and Loom→Weft (federation + named API generation) renames land
  as a hard wire-break: `/api/loom/*`→`/api/weft/*`, the entity-association key
  `clarion_entity_id`→`loomweave_entity_id`, the SEI prefix
  `clarion:eid:`→`loomweave:eid:`, finding rule-ids `CLA-`→`LMWV-`, the token
  audience `loom`→`weft`, and the token env var `CLARION_LOOM_TOKEN`→`WEFT_TOKEN`.
  No compatibility aliases. Deployments must re-issue tokens (aud=weft) and set
  WEFT_TOKEN. The `registry_backend` value/section is now `loomweave` (a deployed
  `clarion` config is migrated on load). Stored Legis signatures are
  stale-pending-reissue until Legis re-signs over the renamed entity_ids.
```

- [ ] **Step 4: Update dashboard JS + run biome.**

Rename `clarionRotationBanner`→`loomweaveRotationBanner` and generation labels in the JS/HTML. Then (per CLAUDE.md, JS-only gate):
```bash
npx biome lint src/filigree/static/js/
npx biome format src/filigree/static/js/
```

- [ ] **Step 5: Full verification + commit.**

Run the full pipeline (Step "Pre-merge verification" below), then:
```bash
git add -A
git commit -m "docs(rebrand): Loomweave/Weft across docs, instructions, fixtures, CI, JS (T3)

Rename Clarion->Loomweave and Loom->Weft in agent instructions, CLAUDE.md,
README/ROADMAP/ADRs, federation docs, test fixtures/fakes, CI, and dashboard
JS. CHANGELOG [3.0.0] gets the breaking rebrand entry. Quarantined CLARION_*
error codes and the loom:// URI scheme are deliberately left for a follow-up."
```

**Definition of Done:**
- [ ] Fixtures/fakes/tests renamed; full suite green
- [ ] Instructions/CLAUDE.md/README/ROADMAP/ADRs/federation docs updated; quarantined error codes preserved
- [ ] CHANGELOG `[3.0.0]` breaking entry added; shipped history untouched
- [ ] Dashboard JS updated; biome lint + format clean
- [ ] Committed

---

## Task 10: Legis re-sign coordination (out-of-band, tracking only)

**Not a code change in this repo.** The v26 SEI-prefix rewrite (Task 3) changes the `entity_id` string the Legis HMAC was cut over, so every stored `signature` is stale-pending-reissue.

- [ ] Notify the Legis owner: re-cut the HMAC for every governed binding over the renamed `loomweave:eid:` entity_ids, in lockstep with the 3.0.0 deploy.
- [ ] Record the coordination on subtask `filigree-2cf022fff2` (T0b) and the epic `filigree-1d08ffb493`.
- [ ] Confirm the documented transient is acceptable for the deploy window (Filigree never verifies signatures, so reads do not break; only downstream governance re-verification is affected until Legis re-signs).

**Definition of Done:**
- [ ] Legis owner notified; T0b updated with the re-sign requirement and the lockstep deploy dependency

---

## Pre-merge verification (run after Task 9, before declaring done)

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/filigree/
uv run pytest --tb=short
make coverage-floors
# JS gate (Task 9 touched JS):
npx biome lint src/filigree/static/js/
npx biome format src/filigree/static/js/
```
Expected: all green. Then a final brand sweep — these should return only the deliberate quarantine survivors:
```bash
grep -rnE "Clarion|clarion" src/filigree/ --include=*.py | grep -vE "CLARION_REGISTRY_VERSION_MISMATCH|CLARION_OUT_OF_SYNC|migrate_v14_to_v15|rename-on-load|# legacy"
grep -rnE "/api/loom|clarion:eid:|CLARION_LOOM_TOKEN|clarion_entity_id" src/filigree/ --include=*.py
```
Expected: first grep — only the quarantined error codes + historical-migration column reference; second grep — zero.

## Self-review checklist (run once, after the plan executes)

- [ ] Every locked-contract row (table at top) has a task that flips it: routes→T6, column→T3, SEI prefix→T3+T4, rule-id→T3, env var→T8, audience→T7, registry_backend→T5, code identifiers→T1+T2.
- [ ] Every quarantined item is still in its old form (error codes, `loom://`, capabilities probe, Legis surface) — confirmed by the verification greps.
- [ ] No `backward`-style leftover: the brand sweep greps are clean modulo the documented allowlist.
- [ ] Legis re-sign coupling is documented in the v26 docstring and tracked (Task 10).

## Handoff / sequencing notes

- **Co-sequence with MCP namespacing** (`filigree-7771610917`) in the same 3.0.0 cut so consumers absorb one cutover, not two. That is a *separate* plan; coordinate the merge order with its owner.
- **Operational deploy steps** (not code): re-issue tokens with `aud=weft`; set `WEFT_TOKEN` in every env; Legis re-sign pass. Record all three on epic `filigree-1d08ffb493`.
- **Issue mapping:** Task 1→`filigree-0d403dc684` (T2A), Task 2→`filigree-cda5448d48` (T2B), Tasks 3–5→`filigree-e0896844cd` (T0), Tasks 6–8→`filigree-648e6460d4` (T1), Task 9→`filigree-44a56a8912` (T3), Task 10→`filigree-2cf022fff2` (T0b). The gate `filigree-23709c5975` (G0) closes once the WEFT_TOKEN correction + the 4 residual quarantines are recorded.
