# PR #52 Security Remediation (D2/D3-independent slice) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **REQUIRED SUB-SKILL:** Use superpowers:test-driven-development for every task that touches code (Tasks 2–7).

**Goal:** Land the merge-blocking and low-risk fixes from the PR #52 review that do **not** depend on the two open decisions (D2 = cascade-close policy, D3 = §4 breaking-bundle tiering), so the 3.0.0 release branch closes its known security/correctness holes while those decisions are discussed.

**Architecture:** Seven independent, individually-committable changes against `release/3.0.0`. Each is isolable — no shared state, no ordering dependency between tasks (one trivial file-collision note on `db_meta.py`, called out in Task 5). Five are near-one-liners with test work; one (Task 7, the Legis redirect hardening) carries real design nuance and is specced in detail.

**Tech Stack:** Python 3.11+, FastAPI/Starlette, stdlib `urllib.request`, SQLite, pytest (async via `anyio`/`pytest-asyncio`), ruff, mypy, biome (JS only — not needed here).

**Scope boundary — explicitly NOT in this plan (blocked on decisions):**
- **B2 / B5** (governed finding→issue cascade fail-closed + reconciliation-debt list surface) — gated by **D2**.
- **T2.1** (remove `get_stats` deprecated alias keys) and the §4 deferrals (TransitionMode, `clarion_entity_id` rename, `safe_message` parity) — gated by **D3**.
- These remain in `/tmp/filigree-3.0.0-remediation-plan.md` (v2) and will be planned once D2/D3 are settled.

**Prerequisites:**
- On branch `release/3.0.0` (do **not** create or switch branches without owner approval).
- `source .venv/bin/activate` (the dashboard venv is already active in the maintainer's session).
- Baseline green: `uv run pytest --tb=short` passes before starting.

**Decision already taken (D1):** the signatureless-reattach un-govern (review M-01) is a **defect**. Task 6 fixes it. See the risk note in Task 6 about Clarion drift-refresh — flag to the owner, do not silently change that path.

**Commit attribution:** commit messages below use a placeholder co-author line; the executing agent substitutes its own identity. Per repo policy, `release/3.0.0` is the integration branch for this work — commit directly to it (no new branches without owner approval).

---

## Task ordering & parallelism

All seven are independent. Recommended order (cheapest/lowest-risk first):

| # | Task | Risk | Files |
|---|------|------|-------|
| 1 | B6 — untrack audit scratch | trivial | git + `.gitignore` |
| 2 | W2 — pin starlette range | trivial | `pyproject.toml` |
| 3 | T4 — coverage floors for 3 security modules | trivial | `scripts/check_coverage_floors.py` |
| 4 | B1 — close `/api/v1/observations` auth hole | low | `dashboard_auth.py`, `dashboard.py`, test |
| 5 | B4 — preserve `actor` on `file_events` JSONL import | low | `db_meta.py`, test |
| 6 | D1 — reject silent signatureless un-govern | medium | `db_entity_associations.py`, test |
| 7 | B3 — Legis redirect-leak hardening (strip-not-reject) | medium-high | `legis_client.py`, test + fake |

Each task ends green and committable. Run the full pre-merge gate (bottom of this doc) once after Task 7.

---

## Task 1: B6 — Untrack internal audit scratch files

**Files:**
- Modify: `.gitignore`
- Untrack: `READ_ONLY_CODEBASE_AUDIT_2026-06-04.md`, `READ_ONLY_CODEBASE_AUDIT_2026-06-04-5AGENT.md` (repo root)

No test (git/config change). This is a hygiene fix: two internal audit scratch files are tracked and would ship in the release.

**Step 1: Confirm they are tracked**

Run: `git ls-files | grep -i 'READ_ONLY_CODEBASE_AUDIT'`

Expected output:
```
READ_ONLY_CODEBASE_AUDIT_2026-06-04-5AGENT.md
READ_ONLY_CODEBASE_AUDIT_2026-06-04.md
```

**Step 2: Untrack (keep the local files) and ignore**

```bash
git rm --cached "READ_ONLY_CODEBASE_AUDIT_2026-06-04.md" "READ_ONLY_CODEBASE_AUDIT_2026-06-04-5AGENT.md"
```

Append to `.gitignore`:
```
# Internal codebase-audit scratch — never ship in a release
READ_ONLY_CODEBASE_AUDIT_*.md
```

**Step 3: Verify they are no longer tracked but still present on disk**

Run: `git ls-files | grep -i 'READ_ONLY_CODEBASE_AUDIT' | wc -l && ls READ_ONLY_CODEBASE_AUDIT_2026-06-04.md`

Expected output:
```
0
READ_ONLY_CODEBASE_AUDIT_2026-06-04.md
```

**Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: untrack internal codebase-audit scratch files

These READ_ONLY_CODEBASE_AUDIT_* files are internal review scratch and
must not ship in the 3.0.0 release. Untracked and gitignored."
```

**Definition of Done:**
- [ ] Both files untracked (`git ls-files` returns nothing for them)
- [ ] `.gitignore` pattern added
- [ ] Local copies still on disk (not deleted)
- [ ] Committed

---

## Task 2: W2 — Pin the Starlette version range

**Files:**
- Modify: `pyproject.toml` (the `[project] dependencies` / `dependencies = [...]` list)

**Context:** All three auth findings sit on Starlette middleware/Request semantics. The lockfile pins `starlette 1.0.1`, but the dependency constraint floats it (it comes transitively via `fastapi>=0.115`). Pin an explicit range so CI and production share middleware semantics across the 0.x→1.0 major boundary.

**Step 1: Find the current constraint**

Run: `grep -n -E 'fastapi|starlette' pyproject.toml`

Expected: a `fastapi>=...` entry and **no** explicit `starlette` entry.

**Step 2: Add an explicit Starlette constraint**

In the `dependencies` array of `pyproject.toml`, add (alongside `fastapi`):
```toml
    "starlette>=1.0,<2",
```

**Step 3: Re-resolve and verify the lock is unchanged at 1.0.x**

Run: `uv lock && grep -A1 'name = "starlette"' uv.lock | head -3`

Expected output (version line ≥ 1.0, < 2):
```
name = "starlette"
version = "1.0.1"
```

**Step 4: Verify the suite still imports/collects**

Run: `uv run pytest tests/api/test_loom_auth.py -q`

Expected: all auth tests pass (this is the middleware drift signal).

**Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: pin starlette>=1.0,<2 explicitly

All bearer-auth middleware findings depend on Starlette 1.0 Request/
middleware semantics. Pin the major so CI and production cannot diverge
across the 0.x->1.0 boundary (was floating via fastapi)."
```

**Definition of Done:**
- [ ] `starlette>=1.0,<2` in `pyproject.toml` dependencies
- [ ] `uv.lock` resolves to a 1.0.x starlette
- [ ] `tests/api/test_loom_auth.py` passes
- [ ] Committed

---

## Task 3: T4 — Coverage floors for the new security modules

**Files:**
- Modify: `scripts/check_coverage_floors.py` (the `FILE_FLOORS` dict, ~line 17)
- Verify: `tests/test_quality_gates.py` (already exercises this script — must stay green)

**Context:** Per-module floors exist (`dashboard_auth.py:90`, `registry.py:80`) but the three modules carrying the Legis-governance and transport-identity security logic — `governance.py`, `actor_identity.py`, `legis_client.py` — have **no** floor. B2/B3 correctness lives in these.

**Step 1: Measure current coverage for the three modules**

Run:
```bash
uv run pytest --cov=filigree --cov-report=json -q >/dev/null
uv run python -c "import json;d=json.load(open('coverage.json'))['files'];\
print({k.split('/')[-1]: round(v['summary']['percent_covered'],1) for k,v in d.items() if k.split('/')[-1] in {'governance.py','actor_identity.py','legis_client.py'}})"
```

Expected output (illustrative — record the real numbers):
```
{'governance.py': 9X.X, 'actor_identity.py': 9X.X, 'legis_client.py': 8X.X}
```

**Step 2: Add floors set just below the measured values**

In `scripts/check_coverage_floors.py`, add to `FILE_FLOORS` (pick floors a few points below the measured coverage so normal churn doesn't trip them, but high enough to catch a real regression):
```python
    "src/filigree/governance.py": <floor>,
    "src/filigree/actor_identity.py": <floor>,
    "src/filigree/legis_client.py": <floor>,
```
Keep the dict alphabetically ordered to match the existing style.

**Step 3: Run the floor check against the fresh coverage.json**

Run: `uv run python scripts/check_coverage_floors.py coverage.json`

Expected output:
```
(no output, exit 0)
```

**Step 4: Verify the quality-gate test still passes**

Run: `uv run pytest tests/test_quality_gates.py -q`

Expected: PASSED.

**Step 5: Commit**

```bash
git add scripts/check_coverage_floors.py
git commit -m "test: add coverage floors for governance/actor_identity/legis_client

These three modules carry the Legis closure-gate, transport-identity, and
governance HTTP client logic but had no per-module floor. Floors set just
below current coverage to catch regressions in the security-critical paths."
```

**Definition of Done:**
- [ ] Three new `FILE_FLOORS` entries, floors below measured coverage
- [ ] `scripts/check_coverage_floors.py coverage.json` exits 0
- [ ] `tests/test_quality_gates.py` green
- [ ] Committed

---

## Task 4: B1 — Close the `/api/v1/observations` unauthenticated-write hole

**Files:**
- Modify: `src/filigree/dashboard_auth.py:31` (`CLASSIC_FEDERATION_ALIASES`)
- Modify: `src/filigree/dashboard.py:104` (`_dashboard_auth_scope` introspection list)
- Test: `tests/api/test_loom_auth.py` (extend `TestIsLoomScopedPath` drift guard + add a `TestLoomAuthEnforcement` case)

**Context:** `is_loom_scoped_path("/api/v1/observations")` → `rest="v1/observations"`, which is in neither alias set → returns `False` → the route at `dashboard_routes/analytics.py:544` accepts unauthenticated writes when a federation token is set. The three sibling observation-write routes are all gated; this classic alias is the lone hole.

**Step 1: Write the failing tests**

In `tests/api/test_loom_auth.py`, add to `class TestIsLoomScopedPath` the classic alias to the true-paths list and, critically, **extend the drift guard to the classic router** (the existing `test_every_living_surface_route_is_loom_scoped` only covers living-surface routers — this hole was on the classic router):

```python
    def test_classic_v1_observations_is_loom_scoped(self) -> None:
        """Regression: the classic observation-write alias must be gated."""
        assert is_loom_scoped_path("/api/v1/observations") is True
        assert is_loom_scoped_path("/api/p/acme/v1/observations") is True

    def test_every_classic_federation_alias_is_loom_scoped(self) -> None:
        """Drift guard for the CLASSIC router. The living-surface guard above
        does not iterate classic routes; the v1/observations hole proves a
        classic-router guard is needed. Every classic federation write alias
        must be gated under both the bare and server-mode mounts.
        """
        for alias in ("v1/scan-results", "v1/observations"):
            assert is_loom_scoped_path(f"/api/{alias}") is True, alias
            assert is_loom_scoped_path(f"/api/p/acme/{alias}") is True, alias
```

And add an end-to-end enforcement case to `class TestLoomAuthEnforcement` (mirror `test_classic_v1_scan_results_enforced` and `test_living_alias_observations_correct_token`):

```python
    async def test_classic_v1_observations_enforced(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """The classic observation-write alias must require the bearer token."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            unauth = await c.post("/api/v1/observations", json={"summary": "must be gated"})
            authed = await c.post(
                "/api/v1/observations",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"summary": "classic alias accepted"},
            )
        assert unauth.status_code == 401
        assert authed.status_code == 201
```

**Why these tests:** the predicate tests pin the gate; the enforcement test pins the real middleware behavior (401 without token, **201** — first create — with token); the classic-router drift guard prevents the *next* classic write alias from re-opening the same class of hole.

**Step 2: Run to verify failure**

Run: `uv run pytest "tests/api/test_loom_auth.py::TestIsLoomScopedPath::test_classic_v1_observations_is_loom_scoped" "tests/api/test_loom_auth.py::TestLoomAuthEnforcement::test_classic_v1_observations_enforced" -v`

Expected: both FAIL — predicate returns `False`; the POST returns 201 (or 200) without a token instead of 401.

**Step 3: Implement the fix**

In `src/filigree/dashboard_auth.py:31`, add the classic alias:
```python
CLASSIC_FEDERATION_ALIASES: frozenset[str] = frozenset({"v1/scan-results", "v1/observations"})
```

In `src/filigree/dashboard.py:104`, add the path to the federation `protected_paths` introspection list so operator tooling reports it correctly:
```python
            "protected_paths": ["/api/loom/*", "/api/scan-results", "/api/observations", "/api/v1/scan-results", "/api/v1/observations"],
```

**Why minimal:** the predicate already strips the `/api/` and `p/{key}/` prefixes; adding the alias string is the entire behavioral fix. The introspection edit keeps the `/api/health` auth report truthful.

**Step 4: Run to verify pass**

Run: `uv run pytest tests/api/test_loom_auth.py -v`

Expected: all PASS (including the new cases and the existing health-introspection tests).

**Step 5: Commit**

```bash
git add src/filigree/dashboard_auth.py src/filigree/dashboard.py tests/api/test_loom_auth.py
git commit -m "fix(auth): gate POST /api/v1/observations behind the federation token

is_loom_scoped_path missed the classic v1/observations write alias, leaving
it unauthenticated when FILIGREE_FEDERATION_API_TOKEN was set (the 06-04 fix
covered v1/scan-results but not v1/observations). Add the alias, surface it
in the auth-scope introspection, and add a classic-router drift guard so the
next classic write alias cannot re-open the hole."
```

**Definition of Done:**
- [ ] `v1/observations` in `CLASSIC_FEDERATION_ALIASES`
- [ ] `protected_paths` introspection updated
- [ ] New predicate + enforcement + classic-router drift-guard tests pass
- [ ] Full `test_loom_auth.py` green
- [ ] Committed

---

## Task 5: B4 — Preserve the `actor` column on `file_events` JSONL import

**Files:**
- Modify: `src/filigree/db_meta.py:1415-1437` (merge import INSERT) and `:1439-1452` (non-merge import INSERT)
- Test: `tests/core/test_verified_actor.py` (extend the existing file_events round-trip test)

**Context:** The `file_events` schema (`db_schema.py:230-240`) has both `actor` and `verified_actor`. Both import INSERTs list `verified_actor` but omit `actor`, so an export→import round-trip silently drops the claimed `actor`. (Verified: the `events`-table import at `db_meta.py:1354-1368` already includes `actor`; only `file_events` is affected — this is genuinely two one-line column additions.)

**Step 1: Write the failing test**

In `tests/core/test_verified_actor.py`, extend the existing file_events export/import round-trip test (around lines 143–177 it asserts `verified_actor` survives) to also assert `actor`. Mirror that test's existing scaffolding (DB fixture, the `export_jsonl`/`import_jsonl` helpers it already uses); add a `file_events` row with a non-empty `actor`, round-trip, and assert it is preserved:

```python
    def test_file_event_actor_survives_jsonl_round_trip(self, tmp_path) -> None:
        # Arrange: register a file and record a file_event carrying a claimed actor.
        #   (reuse this test module's existing DB + export/import helpers)
        src = make_db_with_file_event(actor="agent-claimed", verified_actor="agent-verified")
        dump = export_jsonl(src)

        # Act: import into a fresh DB.
        dst = import_jsonl_into_fresh_db(dump)

        # Assert: BOTH actor and verified_actor round-trip.
        row = dst.conn.execute(
            "SELECT actor, verified_actor FROM file_events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row["verified_actor"] == "agent-verified"
        assert row["actor"] == "agent-claimed"   # currently dropped → fails
```

> NOTE for the executor: use the *exact* fixture/helper names already present in `test_verified_actor.py` rather than the illustrative `make_db_with_file_event`/`export_jsonl` placeholders above — the existing round-trip test already constructs all of this for `verified_actor`; copy it and add the `actor` arm.

**Why this test:** pins the data-loss regression — `verified_actor` survives today, `actor` does not.

**Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_verified_actor.py -k file_event_actor -v`

Expected: FAIL — `assert row["actor"] == "agent-claimed"` gets `''` (the column default), because the import never wrote it.

**Step 3: Implement — add `actor` to both INSERTs**

Non-merge INSERT (`db_meta.py:1439-1452`): add `actor` to the column list and `record.get("actor", "")` to the values tuple (place it before `verified_actor` to match):
```python
                    cursor = self.conn.execute(
                        "INSERT INTO file_events "
                        "(file_id, event_type, field, old_value, new_value, actor, verified_actor, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            file_id,
                            record.get("event_type", "file_metadata_update"),
                            record.get("field", ""),
                            record.get("old_value", ""),
                            record.get("new_value", ""),
                            record.get("actor", ""),
                            record.get("verified_actor"),
                            _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                        ),
                    )
```

Merge INSERT (`db_meta.py:1415-1437`): add `actor` to the inserted column list and the `SELECT` placeholder values **only** — leave the `WHERE NOT EXISTS` dedup clause unchanged (actor is not part of dedup identity):
```python
                    cursor = self.conn.execute(
                        "INSERT INTO file_events (file_id, event_type, field, old_value, new_value, actor, verified_actor, created_at) "
                        "SELECT ?, ?, ?, ?, ?, ?, ?, ? "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM file_events "
                        "  WHERE file_id = ? AND event_type = ? AND field = ? AND old_value = ? AND new_value = ? AND created_at = ?"
                        ")",
                        (
                            file_id,
                            record.get("event_type", "file_metadata_update"),
                            record.get("field", ""),
                            record.get("old_value", ""),
                            record.get("new_value", ""),
                            record.get("actor", ""),
                            record.get("verified_actor"),
                            created,
                            file_id,
                            record.get("event_type", "file_metadata_update"),
                            record.get("field", ""),
                            record.get("old_value", ""),
                            record.get("new_value", ""),
                            created,
                        ),
                    )
```

**Why minimal:** `actor` is purely additive to the column list; the dedup identity (file_id/event_type/field/old/new/created_at) is intentionally left unchanged so import idempotency is preserved.

**Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_verified_actor.py -v`

Expected: all PASS.

**Step 5: Commit**

```bash
git add src/filigree/db_meta.py tests/core/test_verified_actor.py
git commit -m "fix(import): preserve file_events.actor on JSONL round-trip

Both file_events import INSERTs listed verified_actor but omitted actor,
silently dropping the claimed actor on export->import. Add the column to
both the merge and non-merge paths; dedup identity is unchanged."
```

**Definition of Done:**
- [ ] `actor` added to both `file_events` import INSERTs
- [ ] `WHERE NOT EXISTS` dedup clause unchanged
- [ ] Round-trip test asserts both `actor` and `verified_actor` and passes
- [ ] Committed

---

## Task 6: D1 — Stop silent signatureless un-govern by *preserving* the prior signature

**Files:**
- Modify: `src/filigree/db_entity_associations.py:185-202` (`add_entity_association` UPSERT)
- Test: `tests/` entity-association test module (find it: `grep -rl "add_entity_association" tests/`)

**Decision context (D1 = defect):** Today a re-attach with no `signature` clears a prior signature to NULL (UPSERT `signature = excluded.signature`), and the closure gate then short-circuits to PROCEED for an ungoverned issue — a loopback caller can dodge governance by re-attaching without a signature.

**Why PRESERVE, not REJECT (verified against the callers):** the obvious "reject a signed→signatureless re-attach" guard **breaks confirmed-existing flows**. Two of the three callers re-attach without a signature as normal operation:
- `mcp_tools/entities.py:196` (`entity_association_add` MCP tool) passes no `signature`.
- `db_files.py:2443` (`promote_finding_and_attach_entity`) passes no `signature`, and its docstring (db_files.py:2425-2439) *relies on* idempotent re-attach refreshing the hash, with a convergence test (`test_promote_and_attach_retry_converges_after_attach_failure`).
Only `dashboard_routes/entities.py:161` threads `signature` through (from the request body). A reject guard would raise on the MCP/promote re-attach of any governed issue.

The fix is therefore to **preserve** the stored signature when a re-attach supplies none — `signature = COALESCE(excluded.signature, entity_associations.signature)`. This closes the dodge (the signature is never cleared → the gate keeps firing → the issue stays governed) without breaking idempotent re-attach. Filigree never verifies the signature itself (Legis does, at gate time), so a stored signature carried across a `content_hash` change is **not a filigree correctness problem** — and un-governing now requires the explicit `remove_entity_association` path, which is the right place for it.

> ⚠️ **OWNER FLAG (governance-policy, not code-blocking):** preserve carries a Legis sign-off forward onto changed content. That is safe *for filigree* (it never checks the signature), but it is a governance-semantics choice: "stay governed until explicitly removed" (preserve, chosen here) vs "a content change invalidates the sign-off" (which the original clear-on-drift encoded, but did so *silently* and exploitably). Confirm the preserve semantics with the owner. The reject alternative is documented above and is viable only if the MCP/promote callers are changed to thread the signature through — do not take it without owner sign-off.

**Step 1: Write the failing test**

In the entity-association test module (mirror its actual fixtures — `grep -rl "add_entity_association" tests/`):

```python
def test_signatureless_reattach_preserves_prior_signature(db) -> None:
    """The governed->ungoverned dodge: a re-attach with no signature must NOT
    clear a prior signature. It is preserved, so the issue stays governed."""
    issue_id = db.create_issue(type="task", title="governed").id
    db.add_entity_association(issue_id, "ent-1", "hash-v1", signature="sig-abc", signoff_seq=1)

    # Idempotent refresh (e.g. promote_finding_and_attach_entity) passes no signature.
    db.add_entity_association(issue_id, "ent-1", "hash-v2")

    rows = db.list_entity_associations(issue_id)
    row = next(r for r in rows if (r.get("clarion_entity_id") or r.get("entity_id")) == "ent-1")
    assert row["signature"] == "sig-abc"          # preserved → still governed
    assert row["signoff_seq"] == 1                # preserved alongside
    assert row["content_hash_at_attach"] == "hash-v2"  # hash still refreshes


def test_reattach_with_new_signature_updates(db) -> None:
    """Re-signing (explicit new signature) still refreshes the binding."""
    issue_id = db.create_issue(type="task", title="governed").id
    db.add_entity_association(issue_id, "ent-1", "hash-v1", signature="sig-abc", signoff_seq=1)
    db.add_entity_association(issue_id, "ent-1", "hash-v2", signature="sig-def", signoff_seq=2)
    rows = db.list_entity_associations(issue_id)
    row = next(r for r in rows if (r.get("clarion_entity_id") or r.get("entity_id")) == "ent-1")
    assert row["signature"] == "sig-def"
    assert row["signoff_seq"] == 2
```

**Why these tests:** the first locks the security property (signatureless re-attach cannot clear/un-govern) while proving the idempotent refresh path still works; the second proves explicit re-signing still updates.

**Step 2: Run to verify failure**

Run: `uv run pytest <entity-assoc test file> -k "preserves_prior_signature or new_signature" -v`

Expected: `test_signatureless_reattach_preserves_prior_signature` FAILS — `row["signature"]` is `None` (silently cleared) today.

**Step 3: Implement — COALESCE-preserve signature and signoff_seq on re-attach**

In `add_entity_association`'s UPSERT (`db_entity_associations.py:191-199`), change the two governance columns so a missing (`None`) incoming value keeps the stored one:

```python
            ON CONFLICT(issue_id, clarion_entity_id) DO UPDATE SET
                content_hash_at_attach = excluded.content_hash_at_attach,
                attached_at = excluded.attached_at,
                entity_kind = CASE
                    WHEN excluded.entity_kind <> '' THEN excluded.entity_kind
                    ELSE entity_associations.entity_kind
                END,
                signature = COALESCE(excluded.signature, entity_associations.signature),
                signoff_seq = COALESCE(excluded.signoff_seq, entity_associations.signoff_seq)
```

Update the misleading comment at `db_entity_associations.py:181-184`:
```python
        # signature/signoff_seq (v25, B1) are refreshed on re-attach ONLY when a
        # new value is supplied; a signatureless re-attach preserves the prior
        # signature (D1) so it cannot silently un-govern. Explicit un-govern goes
        # through remove_entity_association().
```

**Why minimal:** the entire behavioral fix is the two `COALESCE`s; no extra read, no new code path. Passing a real `signature` still updates it (excluded value is non-null); passing `None` preserves the stored one.

**Step 4: Run to verify pass — including the callers that re-attach tokenless**

Run:
```bash
uv run pytest <entity-assoc test file> -v
uv run pytest tests/ -k "promote_and_attach or entity_association" -q
uv run pytest tests/ -k "governance or closure_gate" -q
```

Expected: all PASS — crucially the `promote_and_attach` retry/convergence test (which re-attaches without a signature) is unaffected.

**Step 5: Commit**

```bash
git add src/filigree/db_entity_associations.py tests/<entity-assoc test file>
git commit -m "fix(governance): preserve signature on signatureless re-attach

A re-attach with no signature cleared a prior signature to NULL, letting a
loopback caller dodge the Legis closure gate (review M-01). COALESCE-preserve
signature/signoff_seq so a tokenless re-attach can no longer un-govern; the
hash still refreshes. Chosen over a reject guard because the MCP add tool and
promote_finding_and_attach_entity re-attach without a signature as normal
idempotent operation. Explicit un-govern remains remove_entity_association().

OWNER NOTE: preserve carries a sign-off across a content_hash change — safe
for filigree (never verifies the signature; Legis does at gate time) but a
governance-semantics choice to confirm (see plan Task 6 flag)."
```

**Definition of Done:**
- [ ] UPSERT uses `COALESCE` for `signature` and `signoff_seq`
- [ ] Misleading "documented flip" comment corrected
- [ ] Preserve + re-sign tests pass
- [ ] Existing **entity-association re-attach AND `promote_and_attach` convergence** tests still pass (this is where the reject alternative would have broken)
- [ ] Owner governance-semantics flag captured in the commit message
- [ ] Committed

---

## Task 7: B3 — Legis redirect-leak hardening (strip-token-on-redirect, classify benign vs malicious)

**Files:**
- Modify: `src/filigree/legis_client.py:94-146` (request construction + error classification)
- Test/fake: `tests/_fakes/legis_http.py` (extend to a two-server 302→sink harness)
- Test: the Legis client test module (`grep -rl "legis_stub\|check_closure_gate" tests/`)

**Context & the landmine:** `check_closure_gate` (legis_client.py:94-101) sends `LEGIS_API_TOKEN` as a bearer and, via stdlib `urllib.request.urlopen`, **follows redirects with the token attached** — a compromised/malicious Legis server can 302-exfiltrate the bearer to another host. The naive fix (drop `HTTPRedirectHandler`) is wrong: with no handler, a *legitimate* 3xx (load balancer, http→https, path normalization) is raised as `urllib.error.HTTPError`, falls through `_classify_http_error`'s catch-all (legis_client.py:144-146) to `UNREACHABLE`, and combined with governance's fail-closed policy **blocks every governed close federation-wide**. `registry.py`'s httpx `follow_redirects=False` is non-raising and is **not** behavior-equivalent — do not copy it literally.

**Chosen semantics (matches `registry.py`'s intent): STRIP, don't reject.** On a redirect, drop the `Authorization` header and follow the benign redirect; never carry the bearer cross-origin. This keeps legitimate redirecting topologies working while closing the exfiltration vector.

**Step 1: Extend the test fake to a two-server redirect harness**

In `tests/_fakes/legis_http.py`, add a redirecting-stub context manager. The primary server returns `302 + Location` pointing at a sink server; the sink records any inbound `Authorization` header so the test can assert it was stripped:

```python
@dataclass
class RedirectSinkState:
    """Records requests (and any Authorization header) that reach the sink."""

    requests: list[str] = field(default_factory=list)
    auth_headers: list[str | None] = field(default_factory=list)
    body: dict[str, Any] = field(default_factory=lambda: {"allowed": True, "reason": "ok"})


def _build_sink_handler(state: RedirectSinkState) -> type[BaseHTTPRequestHandler]:
    class _Sink(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            state.requests.append(self.path)
            state.auth_headers.append(self.headers.get("Authorization"))
            payload = json.dumps(state.body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _Sink


def _build_redirect_handler(location: str) -> type[BaseHTTPRequestHandler]:
    class _Redirect(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

    return _Redirect


@contextmanager
def legis_redirect_to_sink() -> Iterator[tuple[str, RedirectSinkState]]:
    """Yield (primary_base_url, sink_state). The primary 302-redirects every
    closure-gate GET to a sink server that records inbound Authorization."""
    sink_state = RedirectSinkState()
    sink = ThreadingHTTPServer(("127.0.0.1", 0), _build_sink_handler(sink_state))
    sink_host, sink_port = sink.server_address[:2]
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()
    # Redirect any path to the sink root; the sink answers 200 for any path.
    primary = ThreadingHTTPServer(
        ("127.0.0.1", 0), _build_redirect_handler(f"http://{sink_host}:{sink_port}/redirected")
    )
    primary_host, primary_port = primary.server_address[:2]
    primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
    primary_thread.start()
    try:
        yield f"http://{primary_host}:{primary_port}", sink_state
    finally:
        for srv in (primary, sink):
            srv.shutdown()
            srv.server_close()
        primary_thread.join(timeout=2.0)
        sink_thread.join(timeout=2.0)
```

**Step 2: Write the failing tests**

In the Legis client test module:

```python
def test_redirect_does_not_leak_bearer_token(monkeypatch) -> None:
    """A 302 from Legis must NOT carry the Authorization header to the redirect
    target — the token is stripped before following."""
    from filigree import legis_client

    with legis_redirect_to_sink() as (base_url, sink_state):
        monkeypatch.setenv(legis_client.LEGIS_URL_ENV, base_url)
        monkeypatch.setenv(legis_client.LEGIS_TOKEN_ENV, "super-secret-bearer")
        result = legis_client.check_closure_gate("filigree-abc123")

    # The redirect was followed (sink saw the request) ...
    assert sink_state.requests, "redirect was not followed"
    # ... but the bearer was stripped, never reaching the redirect target.
    assert all(h is None for h in sink_state.auth_headers), sink_state.auth_headers
    # And a legitimate redirect does NOT degrade to a fail-closed UNREACHABLE.
    assert result.status is legis_client.LegisGateStatus.ALLOWED


def test_non_redirecting_gate_still_sends_token(monkeypatch) -> None:
    """Sanity: without a redirect, the bearer is still sent (no regression)."""
    from filigree import legis_client

    with legis_stub() as (base_url, state):
        state.body = {"allowed": True, "reason": "ok"}
        monkeypatch.setenv(legis_client.LEGIS_URL_ENV, base_url)
        monkeypatch.setenv(legis_client.LEGIS_TOKEN_ENV, "tok")
        result = legis_client.check_closure_gate("filigree-abc123")
    assert result.status is legis_client.LegisGateStatus.ALLOWED
```

**Why these tests:** the first pins both halves of the fix — the redirect is *followed* (no fail-closed regression) AND the token is *stripped* (no leak). The second guards against over-correcting into never sending the token.

**Step 3: Run to verify failure**

Run: `uv run pytest <legis client test file> -k "redirect or still_sends" -v`

Expected: `test_redirect_does_not_leak_bearer_token` FAILS — today the sink's `auth_headers` contains `"Bearer super-secret-bearer"` (urllib's default redirect handler re-sends the header).

**Step 4: Implement — a token-stripping redirect handler + scheme/loopback guard**

In `legis_client.py`, add a custom redirect handler that drops `Authorization` on redirect, and build an opener that uses it. Add scheme validation for the configured base URL. Replace the bare `urlopen` with the opener:

```python
class _StripAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects but never carry the bearer token across them.

    urllib's default handler re-sends request headers (including
    Authorization) to the redirect target. A malicious or compromised Legis
    server could 302 the token to an attacker host. We strip Authorization on
    every redirected request, so a benign redirect (LB / http->https) still
    works while the bearer never leaves the originally-configured origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - stdlib signature
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def _validate_legis_scheme(url: str) -> None:
    """Reject a non-http(s) LEGIS_URL before attaching a bearer to it."""
    from urllib.parse import urlparse

    scheme = urlparse(url).scheme
    if scheme not in {"http", "https"}:
        msg = f"LEGIS_URL must be an http(s) URL, got scheme {scheme!r}"
        raise ValueError(msg)
```

Then in `check_closure_gate`, after computing `url` and before the request, validate the scheme, and use an opener built with the stripping handler:

```python
    _validate_legis_scheme(url)
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    opener = urllib.request.build_opener(_StripAuthRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            body = _read_json(resp.read())
            ...
```

(Keep the existing `except urllib.error.HTTPError` / `URLError` handling unchanged — with the stripping handler, a benign 3xx is followed rather than raised, so it no longer hits the `UNREACHABLE` catch-all.)

**Why this shape:** strips the token on redirect (closes the leak) while still following benign redirects (no federation-wide fail-closed); scheme validation prevents attaching a bearer to a `file://`/other-scheme `LEGIS_URL`. This mirrors `registry.py`'s strip-then-follow intent without copying its httpx-only kwargs.

> **Residual trust note (non-blocking):** strip-and-follow trusts the verdict returned by the *redirect target*. This is defensible — Legis is the governance trust authority, and a malicious Legis could simply return `allowed:true` directly without a redirect; TLS protects the honest path. But an *honest-Legis open-redirect* would let an attacker-controlled host answer the gate. If you want to also close that, restrict the follow to the **same origin** as `LEGIS_URL` (compare scheme+host+port in `redirect_request`, return `None` to refuse a cross-origin redirect). Recommended as a cheap hardening; call the owner's preference if unsure.

**Step 5: Run to verify pass**

Run: `uv run pytest <legis client test file> -v`

Expected: all PASS — redirect followed, token stripped, non-redirect path still sends the token, existing classification tests unaffected.

**Step 6: Commit**

```bash
git add src/filigree/legis_client.py tests/_fakes/legis_http.py tests/<legis client test file>
git commit -m "fix(legis): strip bearer token on redirect, validate scheme

check_closure_gate followed redirects with the LEGIS_API_TOKEN attached, so
a malicious Legis server could 302-exfiltrate the bearer. Add a redirect
handler that drops Authorization across redirects (benign LB/https redirects
still work, the token never leaves the configured origin) and validate the
LEGIS_URL scheme before attaching the token. Dropping HTTPRedirectHandler
entirely was rejected: a benign 3xx would raise and degrade every governed
close to fail-closed UNREACHABLE federation-wide."
```

**Definition of Done:**
- [ ] `_StripAuthRedirectHandler` follows redirects but removes `Authorization`
- [ ] `LEGIS_URL` scheme validated before the bearer is attached
- [ ] Two-server harness in `legis_http.py`; leak + no-regression tests pass
- [ ] Existing closure-gate classification tests unaffected
- [ ] Committed

---

## Pre-merge verification (run once, after Task 7)

Per the repo CI gate (CLAUDE.md pre-push checklist):

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/filigree/
uv run pytest --tb=short
make coverage-floors    # exercises the new governance/actor_identity/legis_client floors
```

No JS changed in this slice, so the biome gate is not required.

Expected: all green. If `make coverage-floors` trips on one of the three new floors, the floor was set too high in Task 3 — lower it to just below the measured value (it documents current coverage, it is not a coverage *goal*).

---

## Handoff notes

- **Decisions still open (not in this plan):** D2 (cascade-close policy → B2/B5) and D3 (§4 tiering → T2.1 + deferrals). v2 of the umbrella plan (`/tmp/filigree-3.0.0-remediation-plan.md`) holds those; plan them after the discussion.
- **Owner flag in Task 6:** the D1 fix preserves the signature on tokenless re-attach (so Clarion drift-refresh keeps working). Confirm the governance semantics — "stay governed until explicit removal" — are what's intended.
- **External wire (carried, not in this slice):** before any D3 work, confirm Clarion/Wardline do not read the `get_stats` deprecated keys or the `clarion_entity_id` JSONL key.
- **F-003 correction:** the review/umbrella-plan-v1 inverted this — bundled scanners are *protected* by the legacy-token fallback (`scan_utils.py:348-352`); only a custom scanner with a non-default `--api-token-env` and legacy-only token is exposed. No code task here; ensure the release note reflects the corrected scope.
