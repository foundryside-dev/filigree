# Governed cascade close (Design A) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **REQUIRED SUB-SKILL:** Use superpowers:test-driven-development for every task.

**Goal:** Make the finding→issue cascade respect the Legis closure gate for governed issues (PR #52 "Legis H-02"), using **Design A** — consult the gate in the existing **post-commit** cascade and close only if Legis allows; otherwise fail closed and record reconciliation debt.

**Design record:** `docs/superpowers/specs/2026-06-05-governed-cascade-close.md` (decision = A; Design B specified there as the deferred zero-network fallback). **Read it first** — it explains why the review's "A is invasive" premise was wrong (the close cascade is already post-commit) and why B is retained.

**Architecture:** The close cascade runs post-commit, outside any transaction, in two call sites — scan ingest (`db_files.py:1519-1544`) and retention/age-out (`clean_stale_findings`, `db_files.py:2069-2075`). Both delegate per-issue to `_close_issue_for_fixed_finding` (`:1922`) → `FindingIssueCascadeService.close_fixed_finding` (`finding_issue_cascade.py:73`). The gate goes in `close_fixed_finding`; both call sites get it for free. `evaluate_closure_gate` (`governance.py:72`) already short-circuits cheaply for ungoverned/unconfigured issues (no network), so only governed issues in a Legis-configured deployment hit the network.

**Tech Stack:** Python 3.11+, SQLite, pytest, the existing `governance.py` / `legis_client.py` / `finding_issue_cascade.py`.

**Prerequisites:**
- On branch `release/3.0.0` (do not create/switch branches without owner approval).
- Baseline green: `uv run pytest --tb=short`.
- The Legis test stub `tests/_fakes/legis_http.py` (`legis_stub`) and the `check_closure_gate` monkeypatch indirection (`governance.py:67`) are the test seams — `governance.check_closure_gate` is the cleanest thing to monkeypatch.

**Not in this plan:** a *retry/sweep* verb for deferred closes (3.1.0 follow-up). The merge bar is: gate enforced + debt recorded idempotently + debt **listable** (Task 4).

---

## Task ordering

| # | Task | Depends on |
|---|------|------------|
| 1 | Idempotent reconciliation-debt write | — |
| 2 | Gate `close_fixed_finding` (Design A core) | 1 |
| 3 | Batch short-circuit (latency mitigation) | 2 |
| 4 | Reconciliation-debt list surface (B5) | 1 |

---

## Task 1: Make the reconciliation-debt write idempotent

**Files:**
- Modify: `src/filigree/finding_issue_cascade.py:47-66` (`record_reconciliation_debt_comment`)
- Test: the cascade test module (`grep -rl "record_reconciliation_debt_comment\|reconciliation" tests/`)

**Context:** Under Design A, a governed issue that Legis BLOCKS (or that is unreachable) is re-evaluated on every scan ingest and every age-out sweep. `record_reconciliation_debt_comment` is a plain `INSERT` (`:58-60`), so without a guard it appends a duplicate debt comment every run, drowning Task 4's list surface in noise.

**Step 1: Write the failing test**

```python
def test_reconciliation_debt_comment_is_idempotent(db) -> None:
    """Recording the same reconciliation debt twice leaves a single comment."""
    from filigree.finding_issue_cascade import record_reconciliation_debt_comment, RECONCILIATION_DEBT_ACTOR

    issue_id = db.create_issue(type="task", title="governed").id
    text = "Finding f1 fixed but issue blocked by Legis"
    record_reconciliation_debt_comment(db.conn, issue_id, text)
    record_reconciliation_debt_comment(db.conn, issue_id, text)

    rows = db.conn.execute(
        "SELECT COUNT(*) AS n FROM comments WHERE issue_id = ? AND author = ?",
        (issue_id, RECONCILIATION_DEBT_ACTOR),
    ).fetchone()
    assert rows["n"] == 1
```

**Step 2: Run to verify failure**

Run: `uv run pytest <cascade test file> -k reconciliation_debt_comment_is_idempotent -v`

Expected: FAIL — `n == 2` (two identical comments inserted).

**Step 3: Implement — guard against an existing identical debt row**

In `record_reconciliation_debt_comment`, before the INSERT, skip if an identical debt comment already exists for this issue:

```python
        full_text = f"{RECONCILIATION_DEBT_PREFIX} {text}"
        existing = conn.execute(
            "SELECT 1 FROM comments WHERE issue_id = ? AND author = ? AND text = ? LIMIT 1",
            (issue_id, actor, full_text),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
            (issue_id, actor, full_text, _now_iso()),
        )
        conn.commit()
```

(Keep the existing `try/except sqlite3.Error` wrapper and the ADR-012 comment.)

**Why this shape:** matches Task 4's discriminator (`author = RECONCILIATION_DEBT_ACTOR`) and dedups on the exact debt text, so a *different* debt reason on the same issue still records. No new index needed at 3.0.0 volumes (Task 4 documents the scan cost).

**Step 4: Run to verify pass**

Run: `uv run pytest <cascade test file> -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/filigree/finding_issue_cascade.py tests/<cascade test file>
git commit -m "fix(cascade): make reconciliation-debt write idempotent

Design A re-evaluates a blocked governed issue every ingest/sweep; the plain
INSERT would append a duplicate debt comment each run. Skip when an identical
(issue_id, author, text) debt comment already exists."
```

**Definition of Done:**
- [ ] Duplicate-suppression guard added
- [ ] Different-reason debt on the same issue still records (add a second assertion if useful)
- [ ] Idempotency test passes
- [ ] Committed

---

## Task 2: Gate `close_fixed_finding` on the closure gate (Design A core)

**Files:**
- Modify: `src/filigree/finding_issue_cascade.py` (`FindingIssueCascadeStore` Protocol + `close_fixed_finding`)
- Test: the cascade test module

**Context:** `close_fixed_finding` (`:73`) currently calls `_close_issue_for_fixed_finding_tx` directly. Insert the gate before the close. `evaluate_closure_gate(reader, issue_id)` needs a reader with `list_entity_associations`; `FiligreeDB` has it, but the `FindingIssueCascadeStore` Protocol (`:26-44`) does not declare it — add it so the structural type matches `governance._AssocReader`.

**Step 1: Write the failing tests** (monkeypatch `governance.check_closure_gate` to control the verdict; `evaluate_closure_gate` calls it only for governed+configured issues)

```python
import filigree.governance as governance
from filigree.legis_client import LegisGateResult, LegisGateStatus


def _govern(db, issue_id, entity="ent-1"):
    db.add_entity_association(issue_id, entity, "hash-v1", signature="sig", signoff_seq=1)


def test_governed_issue_not_closed_when_legis_blocks(db, monkeypatch) -> None:
    monkeypatch.setenv("LEGIS_URL", "http://legis.invalid")  # is_configured() → True
    monkeypatch.setattr(governance, "check_closure_gate",
                        lambda _id: LegisGateResult(LegisGateStatus.BLOCKED, reason="not signed off"))
    issue_id, finding_id = make_resolved_finding_linked_to_issue(db)   # helper: issue + fixed finding
    _govern(db, issue_id)

    warnings: list[str] = []
    closed = db._close_issue_for_fixed_finding(finding_id, issue_id, warnings=warnings)

    assert closed is False
    assert db.get_issue(issue_id).status != "closed"           # stays open
    assert any("not auto-closed" in w for w in warnings)        # surfaced
    debt = db.conn.execute(
        "SELECT COUNT(*) AS n FROM comments WHERE issue_id=? AND author='filigree:reconciliation'", (issue_id,)
    ).fetchone()
    assert debt["n"] == 1                                        # debt recorded


def test_governed_issue_closed_when_legis_allows(db, monkeypatch) -> None:
    monkeypatch.setenv("LEGIS_URL", "http://legis.invalid")
    monkeypatch.setattr(governance, "check_closure_gate",
                        lambda _id: LegisGateResult(LegisGateStatus.ALLOWED))
    issue_id, finding_id = make_resolved_finding_linked_to_issue(db)
    _govern(db, issue_id)

    closed = db._close_issue_for_fixed_finding(finding_id, issue_id, warnings=[])
    assert closed is True


def test_governed_issue_fails_closed_when_legis_unreachable(db, monkeypatch) -> None:
    monkeypatch.setenv("LEGIS_URL", "http://legis.invalid")
    monkeypatch.setattr(governance, "check_closure_gate",
                        lambda _id: LegisGateResult(LegisGateStatus.UNREACHABLE, reason="timeout"))
    issue_id, finding_id = make_resolved_finding_linked_to_issue(db)
    _govern(db, issue_id)
    assert db._close_issue_for_fixed_finding(finding_id, issue_id, warnings=[]) is False  # fail-closed


def test_ungoverned_issue_still_auto_closes(db, monkeypatch) -> None:
    monkeypatch.setenv("LEGIS_URL", "http://legis.invalid")
    # No signature attached → evaluate_closure_gate short-circuits PROCEED, no network.
    issue_id, finding_id = make_resolved_finding_linked_to_issue(db)
    assert db._close_issue_for_fixed_finding(finding_id, issue_id, warnings=[]) is True


def test_governed_issue_closes_when_legis_unconfigured(db, monkeypatch) -> None:
    monkeypatch.delenv("LEGIS_URL", raising=False)   # governance OFF → PROCEED, no network
    issue_id, finding_id = make_resolved_finding_linked_to_issue(db)
    _govern(db, issue_id)
    assert db._close_issue_for_fixed_finding(finding_id, issue_id, warnings=[]) is True
```

> NOTE: build `make_resolved_finding_linked_to_issue` / `db` from the cascade test module's existing helpers (it already constructs resolved findings linked to issues for the un-gated cascade tests). Reuse them.

**Why these tests:** they pin all five branches — blocked (no close, debt, warning), allowed (close), unreachable (fail-closed), ungoverned (close, no network), unconfigured (close, no network). The last two prove the cheap short-circuit and that A does not regress the common ungoverned path.

**Step 2: Run to verify failure**

Run: `uv run pytest <cascade test file> -k "governed_issue or ungoverned" -v`

Expected: the blocked/unreachable tests FAIL (issue is closed today regardless of Legis); allowed/ungoverned/unconfigured pass.

**Step 3: Implement the gate**

Add `list_entity_associations` to the `FindingIssueCascadeStore` Protocol so the store satisfies `governance._AssocReader`:

```python
class FindingIssueCascadeStore(Protocol):
    @property
    def conn(self) -> sqlite3.Connection: ...

    def get_issue(self, issue_id: str) -> Issue: ...
    def _resolve_status_category(self, issue_type: str, status: str) -> StatusCategory: ...
    def _close_issue_for_fixed_finding_tx(self, finding_id: str, issue_id: str) -> bool: ...
    def list_entity_associations(self, issue_id: str) -> list[Any]: ...   # NEW (satisfies _AssocReader)
    ...
```
(add `from typing import Any` if not already imported.)

Gate inside `close_fixed_finding`:

```python
    def close_fixed_finding(self, finding_id: str, issue_id: str, *, warnings: list[str]) -> bool:
        """Best-effort close of an issue whose linked finding just went fixed.

        Governed issues (DECISION 1A) are closed only if the Legis closure gate
        allows; a blocked / unavailable / integrity verdict fails closed and is
        recorded as reconciliation debt (Design A). The gate makes no network
        call for ungoverned issues or when LEGIS_URL is unset.
        """
        from filigree import governance

        decision = governance.evaluate_closure_gate(self.store, issue_id)
        if not decision.allowed:
            warning = f"governed issue {issue_id} not auto-closed by cascade: {decision.reason}"
            warnings.append(warning)
            record_reconciliation_debt_comment(
                self.store.conn,
                issue_id,
                f"Finding {finding_id} was marked fixed, but the linked governed issue "
                f"was not auto-closed ({decision.outcome.value}): {decision.reason}",
            )
            return False
        try:
            return self.store._close_issue_for_fixed_finding_tx(finding_id, issue_id)
        except (KeyError, ValueError, sqlite3.Error) as exc:
            warning = f"cascade close of issue {issue_id} failed: {exc}"
            warnings.append(warning)
            record_reconciliation_debt_comment(
                self.store.conn,
                issue_id,
                f"Finding {finding_id} was marked fixed, but the linked issue could not be cascade-closed: {warning}",
            )
            return False
```

**Why this shape:** one gate call covers both call sites (ingest + age-out) since both route through here. The reopen cascade is deliberately untouched — **governance gates closure, not reopen** (a regressed finding reopening a governed issue needs no Legis approval). The `import governance` is function-local to avoid any import cycle with the data layer.

**Step 4: Run to verify pass**

Run:
```bash
uv run pytest <cascade test file> -v
uv run pytest tests/ -k "cascade or ingest_scan or clean_stale or governance or closure_gate" -q
```

Expected: all PASS — including the existing un-gated cascade tests (ungoverned issues still close) and the age-out tests.

**Step 5: Commit**

```bash
git add src/filigree/finding_issue_cascade.py tests/<cascade test file>
git commit -m "fix(cascade): gate governed finding->issue auto-close on Legis (design A)

The post-commit cascade closed governed issues with force=True, never
consulting Legis (review Legis H-02). Route close_fixed_finding through
evaluate_closure_gate: governed issues close only when Legis allows; a
blocked/unavailable/integrity verdict fails closed and records reconciliation
debt. Ungoverned/unconfigured paths short-circuit with no network call. The
reopen cascade is intentionally not gated.

Design: docs/superpowers/specs/2026-06-05-governed-cascade-close.md"
```

**Definition of Done:**
- [ ] `list_entity_associations` declared on the Protocol
- [ ] Gate enforced; blocked/unavailable/integrity → no close + idempotent debt + warning
- [ ] Ungoverned and unconfigured paths still close (no network)
- [ ] Reopen cascade untouched
- [ ] Existing cascade + age-out tests green
- [ ] Committed

---

## Task 3: Batch short-circuit (Legis-down latency mitigation)

**Files:**
- Modify: `src/filigree/finding_issue_cascade.py` (add a batch method) and the two call sites in `src/filigree/db_files.py` (`:1532-1537`, `:2073-2075`)
- Test: the cascade test module

**Context:** `legis_client` has a 5 s default timeout. With Legis down and a batch resolving *N* governed findings, the per-issue loop incurs up to *N × 5 s* of serial timeouts on the post-commit hot path. Mitigate: after the first `UNAVAILABLE` verdict, treat the rest of the batch as deferred debt **without** re-calling Legis.

**Step 1: Write the failing test**

```python
def test_batch_short_circuits_after_legis_unreachable(db, monkeypatch) -> None:
    """Once Legis is seen unreachable, the rest of the batch is deferred to debt
    without further gate calls (bounds the timeout cost to one per batch)."""
    monkeypatch.setenv("LEGIS_URL", "http://legis.invalid")
    calls = {"n": 0}

    def _gate(_issue_id):
        calls["n"] += 1
        from filigree.legis_client import LegisGateResult, LegisGateStatus
        return LegisGateResult(LegisGateStatus.UNREACHABLE, reason="timeout")

    monkeypatch.setattr(governance, "check_closure_gate", _gate)

    candidates = []
    for _ in range(3):
        issue_id, finding_id = make_resolved_finding_linked_to_issue(db)
        _govern(db, issue_id)
        candidates.append((finding_id, issue_id))

    warnings: list[str] = []
    db._finding_issue_cascade_service().close_resolved_findings(candidates, warnings=warnings)

    assert calls["n"] == 1                       # only the first governed issue called Legis
    # all three recorded debt, none closed
    n = db.conn.execute("SELECT COUNT(*) AS n FROM comments WHERE author='filigree:reconciliation'").fetchone()["n"]
    assert n == 3
```

**Step 2: Run to verify failure**

Run: `uv run pytest <cascade test file> -k batch_short_circuits -v`

Expected: FAIL — `close_resolved_findings` does not exist yet (AttributeError).

**Step 3: Implement the batch method**

Add to `FindingIssueCascadeService` a batch driver that carries a `legis_down` flag and skips the gate for the remainder once an `UNAVAILABLE` verdict is seen:

```python
    def close_resolved_findings(self, candidates: list[tuple[str, str]], *, warnings: list[str]) -> list[str]:
        """Gate-and-close a batch of (finding_id, issue_id). Short-circuits the
        Legis gate after the first UNAVAILABLE verdict so a down/slow Legis costs
        at most one timeout per batch (the rest defer to reconciliation debt)."""
        from filigree import governance
        from filigree.governance import GateDecision, GateOutcome

        closed: list[str] = []
        legis_down = False
        for finding_id, issue_id in candidates:
            if legis_down:
                decision = GateDecision(GateOutcome.UNAVAILABLE, "Legis unreachable earlier in this batch")
            else:
                decision = governance.evaluate_closure_gate(self.store, issue_id)
                if decision.outcome is GateOutcome.UNAVAILABLE:
                    legis_down = True
            if self._apply_close_decision(finding_id, issue_id, decision, warnings=warnings):
                closed.append(issue_id)
        return closed
```

Refactor `close_fixed_finding` so the decision-application logic is shared with the batch path (extract `_apply_close_decision(finding_id, issue_id, decision, *, warnings) -> bool` holding the Step-2 close/debt body; `close_fixed_finding` becomes "evaluate gate, then `_apply_close_decision`"). Keep `close_fixed_finding` for any single-issue callers/tests.

Rewire the two `db_files.py` call sites to the batch method:

- Ingest (`:1532-1537`): build the filtered candidate list, then call the batch method:
```python
        warnings_before_close = len(stats["warnings"])
        close_candidates = [(fid, iid) for fid, iid in sorted(resolved) if iid not in regressed_issue_ids]
        closed_issue_ids = self._finding_issue_cascade_service().close_resolved_findings(
            close_candidates, warnings=stats["warnings"]
        )
        for warning in stats["warnings"][warnings_before_close:]:
            logger.warning("finding→issue close cascade: %s", warning)
```
- Age-out (`clean_stale_findings`, `:2071-2075`):
```python
        warnings: list[str] = []
        valid = [(fid, str(iid)) for fid, iid in fixed if iid]
        closed_issue_ids = self._finding_issue_cascade_service().close_resolved_findings(valid, warnings=warnings)
        for warning in warnings:
            logger.warning("clean_stale_findings cascade: %s", warning)
```

**Why this shape:** the batch driver is the natural home for cross-item state (the `legis_down` flag); both call sites already collected a list of `(finding_id, issue_id)`, so the rewire is a small simplification, not a restructure. `INTEGRITY_FAILURE` is intentionally **not** short-circuited (it is a per-issue ledger-tamper verdict, not a connectivity problem).

**Step 4: Run to verify pass**

Run:
```bash
uv run pytest <cascade test file> -v
uv run pytest tests/ -k "ingest_scan or clean_stale or cascade" -q
```

Expected: all PASS — including the batch short-circuit and unchanged ungoverned-close behavior.

**Step 5: Commit**

```bash
git add src/filigree/finding_issue_cascade.py src/filigree/db_files.py tests/<cascade test file>
git commit -m "perf(cascade): short-circuit Legis gate after first UNAVAILABLE in a batch

A's synchronous gate call costs up to N*5s when Legis is down and a batch
resolves N governed findings. Add a batch driver that defers the remainder to
reconciliation debt after the first UNAVAILABLE verdict, bounding the timeout
cost to one per batch. Rewire the ingest and age-out cascade loops to it."
```

**Definition of Done:**
- [ ] `close_resolved_findings` batch driver with `legis_down` short-circuit
- [ ] Both `db_files.py` call sites rewired
- [ ] `INTEGRITY_FAILURE` not short-circuited (per-issue)
- [ ] Short-circuit test asserts exactly one gate call for an all-governed down-Legis batch
- [ ] Existing ingest/age-out tests green
- [ ] Committed

---

## Task 4: Reconciliation-debt list surface (B5)

**Files:**
- Add: a `db_meta.py` (or `db_issues.py`) query method, e.g. `list_reconciliation_debt(limit, offset) -> list[...]`
- Add: CLI verb (`cli_commands/` — mirror an existing list verb) + MCP tool (`mcp_tools/` — mirror an existing list tool, register in `mcp_tools/tiers.py` and the rename map if applicable)
- Test: CLI test + MCP test + a DB-layer test

**Context:** Reconciliation debt is now durable and idempotent (Tasks 1–2) but not actionable — there is no verb to find issues carrying it. Add a cross-issue read surface. **Discriminate on `author = 'filigree:reconciliation'`** (`RECONCILIATION_DEBT_ACTOR`), NOT a `LIKE '[reconciliation-debt]%'` scan on the unindexed `comments.text`.

**Step 1: Write the failing DB-layer test**

```python
def test_list_reconciliation_debt_returns_issues_with_debt(db) -> None:
    from filigree.finding_issue_cascade import record_reconciliation_debt_comment

    with_debt = db.create_issue(type="task", title="blocked").id
    without = db.create_issue(type="task", title="clean").id
    record_reconciliation_debt_comment(db.conn, with_debt, "blocked by Legis")

    rows = db.list_reconciliation_debt(limit=50, offset=0)
    ids = {r["issue_id"] for r in rows}
    assert with_debt in ids
    assert without not in ids
```

**Step 2: Run to verify failure**

Run: `uv run pytest <db test file> -k list_reconciliation_debt -v`

Expected: FAIL — `AttributeError: ... has no attribute 'list_reconciliation_debt'`.

**Step 3: Implement the query + CLI + MCP**

DB method (group debt comments by issue; newest first):
```python
    def list_reconciliation_debt(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Issues carrying reconciliation debt (a filigree:reconciliation comment).

        Discriminates on author, not the comment-text prefix, so it does not
        depend on the human-readable prefix string and does not table-scan
        comments.text.
        """
        from filigree.finding_issue_cascade import RECONCILIATION_DEBT_ACTOR

        rows = self.conn.execute(
            """
            SELECT issue_id, COUNT(*) AS debt_count, MAX(created_at) AS latest, MAX(text) AS latest_text
            FROM comments
            WHERE author = ?
            GROUP BY issue_id
            ORDER BY latest DESC
            LIMIT ? OFFSET ?
            """,
            (RECONCILIATION_DEBT_ACTOR, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
```

CLI verb: mirror an existing list command in `cli_commands/` (e.g. the `ready`/`list` shape) — `filigree reconciliation-debt [--limit N] [--json]` → prints issue_id, debt_count, latest. MCP tool: mirror an existing list tool in `mcp_tools/`, register it in `mcp_tools/tiers.py` (and `mcp_tools/rename.py` if the namespaced-name discipline requires a rename row — check `tests/mcp/test_rename_map.py`'s "every tool has exactly one rename row" assertion and add one if needed).

> NOTE: match this repo's actual list-verb/list-tool conventions (envelope `{items, has_more, next_offset?}`, pagination via `_parse_pagination`). Grep an existing list endpoint (e.g. `list_observations` → its CLI + MCP wrappers) and mirror it exactly — including the `ListResponse` envelope and the rename-map row.

**Step 4: Run to verify pass**

Run:
```bash
uv run pytest <db test file> <cli test file> <mcp test file> -v
uv run pytest tests/mcp/test_rename_map.py -q   # if an MCP tool was added
```

Expected: all PASS.

**Step 5: Commit**

```bash
git add src/filigree/ tests/
git commit -m "feat(cascade): list reconciliation debt (CLI + MCP)

Make Design A's deferred-close debt actionable: a cross-issue read surface
listing issues that carry reconciliation debt, discriminating on
author='filigree:reconciliation' (not a comments.text prefix scan). Retry/sweep
of deferred closes is a 3.1.0 follow-up."
```

**Definition of Done:**
- [ ] `list_reconciliation_debt` queries by author, not text prefix
- [ ] CLI verb + MCP tool added, mirroring existing list conventions + envelope
- [ ] MCP rename-map row added if the namespacing discipline requires it (rename-map test green)
- [ ] DB/CLI/MCP tests pass
- [ ] Committed

---

## Pre-merge verification (run once, after Task 4)

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/filigree/
uv run pytest --tb=short
make coverage-floors
```

Expected: all green. If a new MCP tool was added, also confirm `tests/mcp/test_rename_map.py` and any served-prose / old-name guards pass.

## Handoff notes

- **Spec & decision:** `docs/superpowers/specs/2026-06-05-governed-cascade-close.md`. Design B (zero-network fallback) is fully specified there; switching A→B later is a one-branch change in `close_fixed_finding`.
- **Retry/sweep verb** (re-attempt deferred closes) is deliberately out of scope → 3.1.0.
- **Latency:** Task 3 bounds the Legis-down cost to one timeout per batch; if operators still find the synchronous gate on the ingest path unacceptable, that is the trigger to switch to Design B.
- **Correction to umbrella plan v2:** v2 (and the PR #52 review) called Design A "invasive / reorders the cascade." That was based on the false premise that the cascade is in-transaction. It is post-commit (both callers); update v2's B2 section to reflect this if it is revisited.
