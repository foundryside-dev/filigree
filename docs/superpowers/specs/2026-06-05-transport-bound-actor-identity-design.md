# Design: Transport-bound actor identity (v24 slice)

**Ticket:** `filigree-81d3971467` — Transport-bound actor identity verification
**Branch:** `release/3.0.0` (commits land directly on the branch)
**ADR:** extends ADR-012 (Actor Identity Threat Model)
**Date:** 2026-06-05
**Scope of this increment:** schema (v24) + CLI OS-user verification + MCP-stdio
parent attribution. **Out of scope (follow-up tickets):** MCP-HTTP peer identity,
HTTP dashboard session/token/mTLS auth.

## 1. Problem

Per ADR-012, the `actor` string on every Filigree write is an *unauthenticated
claim, not a proof*. 2.1.0 §1.4 capped its length and rejected control chars at
every entry point but never bound it to the transport. The audit trail therefore
cannot distinguish "agent X says it did this" from "the process verifiably
running as X did this." This is the keystone schema change (v24) that the other
3.0.0 breaking tickets build on.

## 2. Decisions (locked in brainstorming)

| # | Decision |
|---|----------|
| Scope | Schema + CLI + MCP-stdio. Defer MCP-HTTP and dashboard auth. |
| Schema | Nullable `verified_*` column on **all runtime event-bearing tables**. `actor`/`author` stay as the claimed value — no rename, no backfill. `NULL` = no transport proof (all historical rows + unverified surfaces). |
| Plumbing | **Session-level**: `FiligreeDB` carries `self._verified_actor: str \| None`, set once at the entry point. No per-call kwarg, no public-signature churn. |
| Conflict | **Record both without comparison.** Claimed value is the operational alias; verified value is stored alongside as transport provenance. Their namespaces differ, so inequality emits no warning and never blocks. |
| Column naming | Mirror the existing claimed column: `verified_author` on `comments`, `verified_actor` elsewhere. |

## 3. Data model — schema v24

`CURRENT_SCHEMA_VERSION` 23 → 24. Migration `_migrate_v23_to_v24` performs, per
table, `ALTER TABLE <t> ADD COLUMN verified_<col> TEXT` (nullable, no default —
existing rows read `NULL`):

| Table | Claimed column | New column |
|-------|----------------|-----------|
| `events` | `actor` | `verified_actor` |
| `file_events` | `actor` | `verified_actor` |
| `annotation_events` | `actor` | `verified_actor` |
| `comments` | `author` | `verified_author` |
| `observations` | `actor` (+ `observation_actor`) | `verified_actor` |

The base `db_schema.py` `CREATE TABLE` statements gain the column too (fresh DBs).
The dedup unique index on `events` (`issue_id, event_type, actor, …`) is **not**
extended — `verified_actor` is attribution metadata, not part of event identity.
Migration-time inserts (`migrations.py`, `migrate.py`) and other system writers
leave the column `NULL`; only runtime user writes stamp it.

> Open confirmation during implementation: `observations` also has an
> `observation_actor` column. The claimed actor for an observation is `actor`;
> `verified_actor` pairs with it. `observation_actor` (the promoting actor) is
> left as-is unless a test shows it is the relevant identity.

## 4. Session-level identity plumbing

- `FiligreeDB.__init__` gains `verified_actor: str | None = None`, stored as
  `self._verified_actor`. A setter `set_verified_actor(value)` allows entry points
  that construct the DB before resolving identity.
- `borrow_for_worker_thread` clones **propagate** `self._verified_actor` (loom
  HTTP worker-thread connections must keep the same verified identity).
- Every runtime event/comment/observation insert includes `self._verified_actor`.
  `_record_event` (events) is the primary chokepoint; `file_events`,
  `annotation_events`, `comments`, `observations` insert sites each read the same
  instance field. (Insert sites enumerated in §7.)

## 5. Verification resolvers

A small helper module `actor_identity.py`:

```
def resolve_os_actor() -> str | None:
    """Best-effort OS-user identity, or None on any failure."""
    try:
        import os, pwd
        return pwd.getpwuid(os.geteuid()).pw_name or None
    except Exception:
        return None
```

- **CLI** (`cli.py` / `cli_common.py`): after building the `FiligreeDB`, call
  `db.set_verified_actor(resolve_os_actor())` before dispatching the verb.
- **MCP-stdio** (`mcp_server.py`): the stdio server process runs as an OS user;
  set `db.set_verified_actor(resolve_os_actor())` on the runtime DB during
  context setup. (Windows lacks `pwd`; `resolve_os_actor` returns `None` →
  `verified_actor` stays `NULL`, no crash.)

## 6. Alias/provenance policy + read path

- **Logical aliases and OS principals are recorded without comparison.**
  Low-level insert sites stamp `self._verified_actor` while the caller-supplied
  `actor` remains the operational identity used for claim-aware writes. Shared
  development accounts routinely run several distinct agent aliases, so a
  difference between the two namespaces is expected and emits no warning. The
  original `ACTOR_MISMATCH` response/stderr policy was retired after it drove
  agents away from MCP toward the equally self-asserted CLI actor flag.
- **Read path**: `EventRecord` TypedDict (`types/events.py`) gains
  `verified_actor: str | None`; `_build_event_record(_with_title)` projects the
  new column. Comment/observation read projections gain the mirrored field.
  Historical rows surface `null`, making "claim-only" events visible so reviewers
  do not assume retroactive verification.

## 7. Insert sites to update (verified by grep)

- `db_events.py:_record_event` → `events`
- `db_files.py:286,567` → `file_events`
- `db_annotations.py:562` → `annotation_events`
- `db_observations.py:369` (`observations`), `:840` (`comments`)
- `db_meta.py` / `finding_issue_cascade.py` comment inserts: stamp when the writer
  is a runtime path; system/cascade inserts may legitimately leave `NULL`
  (decided per-site during implementation, documented in the commit).
- **Excluded** (system, not user): `migrations.py`, `migrate.py`.

## 8. Testing (TDD, full gate)

1. **Migration**: a v23 DB upgrades to v24; columns exist; pre-existing rows read
   `verified_actor IS NULL`; row data preserved.
2. **Fresh schema**: a new DB has the columns at v24.
3. **CLI write**: stamps `verified_actor = resolve_os_actor()` on the event.
4. **MCP-stdio write**: stamps verified identity.
5. **Session propagation**: `borrow_for_worker_thread` clone keeps the identity.
6. **Alias/provenance separation**: whether claimed and verified values match or
   differ, both are stored and no mismatch warning is emitted.
7. **Unverified surface**: with no resolver set, writes store `verified_actor =
   NULL`, no warning.
8. **Read path**: `EventRecord` exposes `verified_actor`; historical rows → `None`.
9. **Resolver robustness**: `resolve_os_actor` returns `None` (not raise) when
   `pwd` is unavailable.
10. Schema-version test + contract fixtures updated; `SCHEMA_MIGRATIONS.md` notes
    v24.

## 9. Files touched

**Update:** `db_schema.py` (CREATE + `CURRENT_SCHEMA_VERSION`), `migrations.py`
(new `_migrate_v23_to_v24`), `core.py` (`FiligreeDB.__init__` + setter +
`borrow_for_worker_thread`), `db_events.py`, `db_files.py`, `db_annotations.py`,
`db_observations.py`, `types/events.py` (+ comment/observation read types),
`cli.py`/`cli_common.py`, `mcp_server.py`, `docs/SCHEMA_MIGRATIONS.md`, CHANGELOG.
**Create:** `actor_identity.py`, tests, possibly extend `ADR-012`.

## 10. Non-goals

- No HTTP-MCP peer identity, no dashboard auth (separate tickets).
- No blocking on mismatch; no rejection of unverified writes.
- No retroactive backfill of `verified_actor` on historical rows.
- No change to the `events` dedup identity index.
