# Plan — close-on-fixed cascade during scan-results ingest (Filigree ask #3)

> **SUPERSEDED (2026-06-05).** This first-pass plan covered only the close
> cascade and misdiagnosed the real product gap (clean files are never swept) as
> a test-construction rule. The implemented, two-part plan — close cascade **plus**
> the prerequisite `scanned_paths`-driven sweep — and its no-decoy acceptance
> tests live at `/home/john/.claude/plans/fluttering-dreaming-tower.md`. The work
> shipped per that plan; this file is kept for history only.

Re-verified against working tree at HEAD `54cdd65`. Source is schema **v23**
(`db_schema.py:575`); the session-start warning is only the *globally
installed* `filigree` CLI being stale at v22 — irrelevant to `uv run` tests.

## Problem

Reopen-on-regress is wired into ingest; close-on-fixed is not. After an agent
fixes code and re-scans, `_mark_unseen_findings` flips the finding to
`unseen_in_latest` but the linked issue stays open. The close helper
(`_close_issue_for_fixed_finding`) has exactly one caller — the age-gated
`clean_stale_findings` sweep — which Wardline never invokes (it drives
everything through `POST /api/loom/scan-results`).

Fix: a close cascade symmetric to the existing reopen cascade, fired from
`process_scan_results`.

## Key facts verified

- Service indirection is a thin wrapper: `_close_issue_for_fixed_finding`
  (db_files.py:1825) → `FindingIssueCascadeService.close_fixed_finding`
  (finding_issue_cascade.py:70) → `store._close_issue_for_fixed_finding_tx`
  (db_files.py:1834). **The status guard at db_files.py:1846 is the live, only
  copy** — the ask's pointer is correct, not dead code.
- `_mark_unseen_findings` is a `@staticmethod` (db_files.py:1323) with a single
  caller; `_ingest_resolved_findings` has a single caller. Both signature
  changes are safe.
- `TERMINAL_FINDING_STATUSES = {"fixed", "false_positive"}` (db_files.py:71).
- ⚠️ **`_mark_unseen_findings` only sweeps files present in the current batch.**
  It iterates `seen_finding_ids` (keyed by `file_id`, populated only from this
  batch). A file with no finding in the batch is never visited. This shapes the
  tests (see below) and is the single easiest thing to get wrong.

## Implementation

### Step 1 — capture resolved pairs in `_mark_unseen_findings` (db_files.py:1323)

Add a keyword-only `resolved: set[tuple[str, str]]` param. For each `(fid, fids)`
in `seen_finding_ids`, **before** the existing UPDATE, SELECT the rows about to
genuinely transition and collect `(finding_id, issue_id)`:

```python
rows = conn.execute(
    f"SELECT id, issue_id FROM scan_findings "
    f"WHERE file_id = ? AND scan_source = ? AND issue_id IS NOT NULL "
    f"AND status NOT IN ({terminal_ph}) "
    f"AND status != 'unseen_in_latest' "          # only real open/new → unseen
    f"AND id NOT IN ({placeholders})",
    [fid, scan_source, *terminal, *fids],
).fetchall()
for row in rows:
    resolved.add((row["id"], str(row["issue_id"])))
```

The `status != 'unseen_in_latest'` clause is load-bearing: it stops a finding
that was *already* unseen from a prior scan re-firing the close every batch
(acceptance #5, idempotency). Run the existing UPDATE unchanged afterward.

### Step 2 — thread `resolved` through the call chain

- `process_scan_results` (db_files.py:1417): add `resolved: set[tuple[str, str]] = set()`
  beside `regressed_issue_ids`, pass it into `_ingest_resolved_findings`.
- `_ingest_resolved_findings` (db_files.py:1494): add the `resolved` param;
  `resolved.clear()` on entry, right next to `regressed_issue_ids.clear()`
  (db_files.py:1528) — `@_retry_busy` re-runs the method, so a rolled-back
  transient BUSY must not double-accumulate. Pass `resolved=resolved` into the
  `_mark_unseen_findings` call (db_files.py:1560). Only populated when
  `mark_unseen=True`, which is exactly Wardline's path.

### Step 3 — widen the close-tx status guard (db_files.py:1846)

```python
if finding is None or finding["status"] not in ("fixed", "unseen_in_latest"):
    return False
```

Safe for both callers. clean-stale sets `fixed` before the cascade reads (still
passes). The post-commit race guard still holds: if ingest reopened the finding
to `open` between commit and cascade, status is `open` → skipped. (Verified:
`test_reingest_between_sweep_and_cascade_does_not_close_issue` drives the
finding to `open`, which fails the widened set → no close. No behavior change.)

### Step 4 — close post-commit, beside the reopen loop (after db_files.py:1464)

```python
warnings_before_close = len(stats["warnings"])
closed_issue_ids = [
    issue_id
    for finding_id, issue_id in sorted(resolved)
    if self._close_issue_for_fixed_finding(finding_id, issue_id, warnings=stats["warnings"])
]
for warning in stats["warnings"][warnings_before_close:]:
    logger.warning("finding→issue close cascade: %s", warning)
if closed_issue_ids:
    logger.info(
        "finding→issue cascade: closed %d issue(s) on fix (scan_source=%r): %s",
        len(closed_issue_ids), scan_source, ", ".join(closed_issue_ids),
    )
```

`_close_issue_for_fixed_finding` already: stamps `FINDING_CASCADE_MARKER` (so
regress can reopen it), no-ops on a `done`-category issue (terminal human
decisions preserved, db_files.py:1850), runs its own `BEGIN IMMEDIATE`
(best-effort, never fails ingest), and records reconciliation-debt on failure.

Reopen runs before close; the two sets are normally disjoint (a finding is
either in the batch → maybe-regressed, or absent → maybe-resolved). Do **not**
add a comment claiming strict disjointness — a single issue with two findings
in opposite directions in one batch resolves by loop order (ends closed). Rare,
the existing reopen path shares the ambiguity, and the task scopes to symmetry.

## Tests — `tests/core/test_finding_issue_cascade.py`, no `clean_stale_findings` call

Add a `TestCloseOnFixedFromIngest` class. Mirror `_wln` / `_ingest` /
`_is_done` helpers already in the file.

**Same-file batch is mandatory.** Because the sweep only visits batched files,
F at `src/a.py` resolves only if the re-POST contains another finding **in
`src/a.py`** (different fingerprint). A decoy in `src/b.py` would leave
`src/a.py` unvisited and F open — a false failure.

1. **Immediate close from ingest** — ingest F (`fp-fix`, `src/a.py`), promote →
   I open. Re-POST `mark_unseen=True` with a sibling finding (`fp-other`,
   `src/a.py`) and F absent. Assert F is `unseen_in_latest` AND `_is_done(I)`.
2. **Reopen still works** — from (1), re-POST including F (`fp-fix`, `src/a.py`).
   Assert `not _is_done(I)` and F `open`.
3. **Terminal human decision preserved** — promote, `close_issue(actor="human",
   force=True)`, then run the (1) batch (F absent, sibling present): I is **not**
   reopened by regress nor re-closed by the cascade (the `== "done"` guard wins).
4. **Idempotent with clean-stale** — after (1), call `clean_stale_findings`;
   no error, no double-transition (`closed_issue_ids == []` second time).
5. **No spurious close** — re-POST including *all* prior fingerprints; `resolved`
   is empty → nothing closed, I stays open.

Optional: a cascade-close-failure-surfaced-in-warnings test mirroring the
existing reopen-failure test (monkeypatch `close_issue` to raise; assert the
warning rides `stats["warnings"]` and is logged).

TDD: write all five, watch #1 fail **for the right reason** (I stays open with
correct same-file setup) before implementing.

## Verification (memory: pre-push CI)

```bash
uv run pytest tests/core/test_finding_issue_cascade.py tests/core/test_scans.py \
              tests/core/test_scan_finding_fingerprint.py --tb=short
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run mypy src/filigree/
uv run pytest --tb=short
```

## Non-asks (do not touch)

No new route or payload (internal to `process_scan_results`; cascade is logged,
not surfaced — the envelope is a frozen passthrough). No change to asks #1/#2.
No Wardline change (it already POSTs `mark_unseen=True`). clean-stale is not
retired — it still archives stale rows to `fixed`; its close then hits the
`== "done"` guard and no-ops. Ingest closes eagerly; clean-stale archives
eventually.
```
