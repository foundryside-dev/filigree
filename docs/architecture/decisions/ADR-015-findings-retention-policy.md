# ADR-015: Findings Retention Policy and the Scan-Run-Create Contract

**Status**: Accepted
**Date**: 2026-05-30
**Deciders**: John (project lead)
**Context**: Clarion's REQ-FINDING-05/-06 lifecycle tail (Clarion tracking issue `clarion-dd29e69e0e`). Clarion emits findings via `POST /api/v1/scan-results` with `mark_unseen: true`; findings that drop out of later scans accumulate in `unseen_in_latest`. Clarion wants `clarion analyze --prune-unseen` (REQ-FINDING-06) but, as a Rust HTTP client, has no server-side retention route to call. Clarion also asked whether the "tolerate unknown `scan_run_id`, warn, proceed" behaviour is the permanent contract (REQ-FINDING-05).

## Summary

Two Filigree-side decisions for the finding-lifecycle tail:

1. **Retention is a soft archive, exposed as a loom-generation HTTP surface.** Filigree already had the retention semantics as a core method (`clean_stale_findings`) wired only to the CLI. This ADR exposes that same operation over `POST /api/loom/findings/clean-stale` so federation consumers can drive it. Retention **moves stale `unseen_in_latest` findings to `fixed`** — it does **not** hard-delete. Scoping by `scan_source` is mandatory on the HTTP surface.
2. **"Tolerate unknown `scan_run_id`, warn, proceed" is the permanent contract.** Filigree does **not** add a scan-run-create endpoint or a create-first handshake.

## Decision

### 1. Retention surface: reuse the existing core method, soft semantics

`POST /api/loom/findings/clean-stale` is a thin adapter over the existing `FiligreeDB.clean_stale_findings(days, scan_source, actor)` — the same operation as CLI `filigree finding clean-stale`. There is **one** retention implementation; the HTTP route adds no new lifecycle logic.

Wire contract (pinned by `tests/fixtures/contracts/loom/findings-clean-stale.json`):

- **Request** — `{ "scan_source": str (required, non-empty), "older_than_days": int (optional, default 30, >= 0), "actor": str (optional, default "dashboard") }`.
- **Response** — `{ "findings_fixed": int, "scan_source": str, "older_than_days": int }` (echoes inputs so the caller can log the outcome in its `stats.json`).
- **Auth** — none. Filigree's HTTP API is loopback-only; `actor` comes from the body, mirroring `POST /api/loom/scan-results`. There is no inbound bearer-token / `x-filigree-actor` handling anywhere on the HTTP surface (the bearer token in the codebase is Filigree's *outbound* registry client). **A bearer token a client sends (e.g. Clarion's `FILIGREE_API_TOKEN`) is ignored on inbound** — this is a pre-existing posture, not introduced here; reconciling Clarion's token configuration with that fact is a separate cross-product cleanup, tracked as `filigree-30cd35bcb9`.

#### Why soft (mark `fixed`), not hard-delete

REQ-FINDING-06 says "removes," but the durability policy is Filigree's to set (the finding lifecycle is Filigree's). Soft archive wins on three counts:

- **Consistency.** It is exactly the existing `clean_stale_findings` / CLI behaviour. One retention semantics, not two.
- **Identity preservation.** A finding that reappears in a later scan auto-reopens `fixed` → `open` with its `seen_count` intact (`db_files.py:1195`). Hard-delete throws that away — a reappearing finding would return brand-new, losing its history.
- **Sufficiency.** REQ-FINDING-06 targets the `unseen_in_latest` population specifically — its stated concern is the *unseen working set* accumulating across repeated scans, not the size of the `scan_findings` table. Moving stale unseen findings to a terminal status drains that working set, which is what the requirement asks for. Row reclamation (the only thing hard-delete buys) is **not** a stated requirement; soft retention does **not** bound total row count. If a future concern is unbounded table growth on a long-lived, repeatedly-scanned project, that is a *different* requirement and the answer is a hard-delete extension (a `hard_delete` flag on `clean_stale_findings`), not a reinterpretation of REQ-FINDING-06. We note this explicitly so "soft = REQ-FINDING-06 done" does not later paper over a storage-growth concern.

No tombstone is written: findings are not federated through the `/changes` feed (only issues are — cf. ADR's `deleted_issues` tombstone, which exists for hard-deletes of federated entities), and this is a soft transition anyway. A single server-side `logger.info` line records each sweep (count / scan_source / days / actor).

#### Why `scan_source` is mandatory on the HTTP surface — and what it is *not*

The core method treats `scan_source=None` as "all sources." That is appropriate for a local operator at the CLI, but on the federation surface omitting the source would silently sweep every tool's findings — a footgun. The HTTP route therefore rejects a missing/empty `scan_source` with `400 VALIDATION`; the "all sources" mode is not reachable over HTTP.

This is an **accident-guard, not an authorization boundary.** `scan_source` is read from the request body and is not bound to any caller identity (there is no inbound auth — see above). A loopback caller could pass *any* source string and sweep that source's unseen findings. The only real trust boundary is **loopback-only binding**. Combined with `older_than_days=0` being permitted (cutoff = now → sweep the whole current unseen backlog), the blast radius of a mistaken or hostile call is "all *unseen* findings for one named source move to `fixed`." That is bounded and self-healing: the operation is soft, only touches findings that already dropped out of the latest scan, and any that reappear auto-reopen. We therefore allow `older_than_days=0` rather than imposing a floor.

#### Age definition

Inherited from the core method: a finding is stale when `coalesce(last_seen_at, updated_at) < (now − days)`. `last_seen_at` is the precise "last observed in a scan" signal; `updated_at` is the fallback for findings that predate `last_seen_at` population. No new "became-unseen-at" column is needed.

#### Enrich-only (loom.md §3–§5, ADR-002 §7)

The route is a pure local DB write, fully functional with no federation peer present. Filigree's finding lifecycle is correct whether or not Clarion ever calls it — prune is an optional retention convenience, never a required step. Filigree has no dependency on Clarion calling it.

#### Surface placement

`/api/v1/` (the `classic` generation) is frozen (ADR-002 §1) — no new operations land there. The route is a `loom`-generation endpoint at `/api/loom/findings/clean-stale`. A living-surface alias at `/api/findings/clean-stale` is **deferred**, matching the Phase-C4 precedent for loom-only endpoints with no classic counterpart: federation consumers commit to the pinnable `/api/loom/...` generation.

### 2. Scan-run-create contract (REQ-FINDING-05): tolerate unknown `scan_run_id`, proceed — permanently

Filigree accepts findings carrying a client-supplied `scan_run_id` it has never seen; the findings ingest successfully. This is the **permanent, intended contract**:

- **No create endpoint, no create-first handshake.** A required "announce the run first" step cuts against the enrich-only / loose-coupling posture (loom.md §3–§5, ADR-002 §7): findings must ingest correctly whether or not the run was pre-announced.
- **History is not lost.** `get_scan_runs` already surfaces such runs from the `scan_findings` union (`db_files.py:1404`, "legacy/orphan ingestion paths"), so `GET /api/scan-runs` reflects them.

#### Resolving the completion warning (do not bless a noisy happy-path)

The original framing of this answer was self-contradictory: it declared the warning "informational" while leaving it firing on the *normal* path. End-to-end, if a client never creates runs but always sends `complete_scan_run: true`, Filigree's completion step (`db_files.py:1389`) appends an unknown-run warning to `response.warnings[]` on **every** ingest — and a consumer that logs `warnings[]` at WARN then cries wolf on every healthy run. A contract whose happy path emits a warning is not "informational"; it is noise we ratified.

The resolution keeps the contract but removes the noise at the source of the mismatch:

- **Primary (no Filigree change): a client that does not manage scan-run lifecycle must send `complete_scan_run: false`.** There is no run to complete, so asking to complete one is incorrect, and `complete_scan_run: true` buys such a client nothing — the per-run `findings_count` update (`db_files.py:1356`) is a no-op `UPDATE ... WHERE id = ?` that neither errors nor warns when the row is absent. With `complete_scan_run: false`, **no warning is emitted and the happy path is silent.** This is the recommended guidance to Clarion (REQ-FINDING-05).
- **Alternative (Filigree-side, if a client cannot change):** suppress the completion warning for the specifically-sanctioned case — `scan_run_id` provided, but no `scan_runs` row exists at all — so only genuine completion *failures* (a row exists but could not transition) warn. This is a small post-commit change to the warning condition, not the rejected "auto-adopt."

We deliberately reject **auto-adopt** (upserting a `scan_runs` row on an unknown id): it would mutate the most-pinned, lock-serialized ingest path purely to record a row, and would have to fabricate a `scanner_name` (a `NOT NULL` column) it does not have. Bad trade. The two options above resolve the noise without that cost.

## Consequences

- Clarion gets a federation-reachable retention trigger (`POST /api/loom/findings/clean-stale`) without Filigree taking on any dependency on Clarion.
- A finding pruned (archived) then re-emitted in a later scan auto-reopens to `open` with `seen_count` intact — correct retention behaviour, and distinct from what a hard-delete would do (return brand-new).
- No schema migration; no change to `process_scan_results`; no new MCP/CLI (the CLI `finding clean-stale` already exists).
- The wire shape is pinned in `tests/fixtures/contracts/loom/findings-clean-stale.json` for Clarion to mirror in its `docs/federation/contracts.md`.

## References

- **ADR-002** (API generations / federation posture): `docs/architecture/decisions/ADR-002-api-generations-and-federation-posture.md`.
- **Federation contracts**: `docs/federation/contracts.md`.
- **Core method**: `FiligreeDB.clean_stale_findings` (`src/filigree/db_files.py`); CLI `filigree finding clean-stale` (`src/filigree/cli_commands/admin.py`).
- **Loom doctrine**: `/home/user/clarion/docs/suite/loom.md` (§3–§5 enrich-only).
- **Clarion**: tracking issue `clarion-dd29e69e0e`; REQ-FINDING-05 (scan_run lifecycle), REQ-FINDING-06 (dedup / mark_unseen).
