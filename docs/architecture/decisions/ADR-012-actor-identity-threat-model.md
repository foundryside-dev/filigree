# ADR-012: Actor Identity Threat Model

**Status**: Accepted
**Date**: 2026-05-18
**Deciders**: John (project lead)
**Context**: 2.1.0 hardening pass identified that `actor` strings on every write are unauthenticated. The 2.1.0 release-prep §1.4 pins what we *do* enforce (the length cap) and documents what the actor string is — and is not.

## Summary

The `actor` string carried on every Filigree write is an **identifier**, not an
**authentication credential**. The audit trail records *claims* about who acted,
not *proofs*. Transport-level identity verification (binding a transport to a
proven actor) is a 2.3.0+ work package, not a 2.1.0 deliverable. 2.1.0 closes the
narrowest hole (overlong / control-char actors making the audit trail unreadable
or feeding a downstream injection) by pinning the length cap at every entry
point: CLI, MCP, and HTTP.

## Context

Filigree exposes three entry points that accept an `actor` string:

| Entry point | Default actor | Sanitisation |
|-------------|---------------|--------------|
| CLI         | `cli`         | `sanitize_actor` at group-level (`cli.py:46`) |
| MCP         | `mcp`         | `_validate_actor` per tool (wraps `sanitize_actor`) |
| HTTP        | `dashboard`   | `_validate_actor` per route (wraps `sanitize_actor`) |

`sanitize_actor` (`src/filigree/validation.py:14`) enforces:

1. Type — must be a string.
2. Control / format characters — rejected before stripping so `"\nbad"` cannot
   smuggle a newline through the audit log.
3. Whitespace — stripped; result must be non-empty.
4. Length — at most 128 characters (`_MAX_ACTOR_LENGTH`).

None of these checks tell us *who* the caller actually is. A caller naming
themselves `alice` cannot be distinguished from a caller naming themselves
`bob`. The CLI runs under the user's shell with no transport. MCP and HTTP
authenticate the *transport* (localhost-only by default for HTTP; stdio for
MCP), but neither carries a verified identity into the actor field of the
audit event.

Reviewers reasonably ask: "if any caller can write any actor name, what is the
audit trail worth?"

## Decision

We adopt an explicit threat model for actor strings in Filigree 2.x:

1. **Actor strings are unauthenticated identifiers.** They tell a future
   reviewer "the caller said it was X". They do not prove it was X.
2. **The audit trail records claims, not proofs.** Events are tamper-evident
   against accidental loss (chain via `event_seq`, see 2.1.0 §0.2) but not
   against a peer who can write arbitrary actor strings.
3. **The trust boundary is the transport, not the actor field.** CLI invocation
   means "this OS user". MCP stdio means "this MCP client process". HTTP on
   localhost means "this loopback peer". Filigree 2.x assumes those boundaries
   are sufficient for its single-tenant, single-machine deployments.
4. **Within that trust model, the length cap, control-char rejection, and
   whitespace handling are still load-bearing.** They prevent a benign caller
   accidentally corrupting the audit trail (overlong values truncated by a
   downstream consumer; control characters breaking log parsers; empty values
   collapsing actor accountability). 2.1.0 §1.4 pins all three guarantees with
   tests at the CLI, MCP, and HTTP entry points.
5. **Transport-bound identity (the "verified actor" enhancement) is a 2.3.0+
   work package.** It would require: OS-user lookup on CLI invocations; MCP
   peer attribution from the transport; HTTP authentication (sessions, tokens,
   or mTLS) on the dashboard. Each surface needs its own decision and is too
   broad for the 2.1.0 hardening pass. Tracked as a Filigree issue and
   referenced from this ADR.

   **Partial lift (2026-06-03, [ADR-018](ADR-018-loom-bearer-token-auth.md)):**
   the **access-gate** half of HTTP authentication has since landed — opt-in
   bearer-token enforcement on the loom federation surface, active only when
   `FILIGREE_API_TOKEN` is set. That gates *access* but does **not** bind a
   proven identity into the `actor` field, so the **verified-actor** half of
   this deferral remains open (`filigree-81d3971467`). The cross-host trigger
   in the Negative consequences below still governs when the remaining identity
   work becomes near-term.

## Consequences

### Positive

- Reviewers reading 2.1.0 audit trails know the rules of the game: the actor
  field is what the caller wrote, sanitised but not verified.
- The length-cap + control-char invariants are pinned at every entry point and
  cannot regress silently.
- The 2.3.0+ scope for transport-bound identity has a clear starting point
  rather than being implied by ambiguous prose elsewhere.

### Negative

- Agents and operators must continue to use claim metadata, session labels,
  comments, observations, and findings (see [ADR-011](ADR-011-agent-sessions-deferred-beyond-2-0.md))
  as the working coordination model. Actor strings alone do not provide
  durable session identity.
- A malicious caller on the trusted transport can still impersonate any
  actor. Filigree 2.x is not the right tool for adversarial multi-tenant
  deployments.
- **The §5 deferral is conditioned on the loopback-transport boundary
  actually holding — it is not open-ended.** As of 2026-06 the Loom
  federation is co-located on a single host: peers bind loopback (Clarion
  `127.0.0.1:9111`, Filigree `127.0.0.1:8377`; the registry-backend runbook
  spawns Clarion on a free loopback port same-host). While that topology
  holds, "the trust boundary is the transport" (§Decision 3) is true and
  transport-bound identity stays legitimately 2.3.0+. **The trigger that voids
  the premise and re-opens this ADR is any federation peer binding
  off-loopback (cross-host).** At that point inbound HTTP authentication
  (transport-bound identity, tracked as `filigree-81d3971467`) becomes
  near-term, and the first thing to settle is the contract mismatch tracked
  in `filigree-30cd35bcb9` — Clarion sends an `Authorization: Bearer` token
  that Filigree currently ignores because loopback is the boundary.

### Neutral

- This ADR does not change `sanitize_actor`. It documents the existing
  semantics and pins them with tests.

## v24 increment — verified actor lands (schema v24, 3.0.0)

This is the full lift of the **verified-actor** half of the §5 deferral that
the "Partial lift (2026-06-03)" note anticipated. ADR-018 had already landed
the access-gate half (opt-in bearer-token enforcement on the loom surface);
schema v24 now binds the transport's proven identity into the audit trail for
the two surfaces whose transport boundary is unambiguous (CLI, MCP stdio),
while the remaining surfaces stay explicitly deferred.

### What landed

- **Schema (v24).** A nullable `verified_*` column is added to every runtime
  event-bearing table: `events.verified_actor`, `file_events.verified_actor`,
  `annotation_events.verified_actor`, `comments.verified_author`,
  `observations.verified_actor`. The claimed `actor`/`author` value is
  unchanged. `verified_*` holds the transport-verified identity (the OS user
  the writing process ran as) or `NULL` when no transport proof exists — the
  value for every historical row (no backfill), every unverified surface, and
  every system/cascade/migration-authored write. The `events` dedup unique
  index is **not** extended: `verified_actor` is attribution metadata, not part
  of event identity. Migration `migrate_v23_to_v24` is additive and idempotent.

- **Session-level plumbing.** The verified identity is resolved once at the
  process entry point and held on the session (`FiligreeDB._verified_actor`,
  `str | None`, set via `set_verified_actor()`); it propagates to worker-thread
  clones via `copy.copy`. Resolution is `actor_identity.resolve_os_actor()`
  (POSIX `pwd`), which returns `None` on Windows or any failure and never
  raises.

- **Entry-point resolvers.** The CLI (`get_db()`) and the MCP stdio startup
  path (`_attempt_startup`) set the verified actor. These are the surfaces
  whose transport boundary directly identifies an OS user.

- **Conflict policy: record both, warn, never block.** Both the claimed and
  verified identities are persisted. On mismatch a non-blocking `ACTOR_MISMATCH`
  warning is surfaced; the write always proceeds. Placeholder framework default
  claims (`cli`, `mcp`) are suppressed — they are not a real disagreement, so
  they raise no warning. Two surfaces carry the warning: the CLI emits it on
  **stderr** (always, so production stderr never pollutes `--json` stdout);
  MCP injects a top-level `warnings` array into the tool-response envelope
  (`_inject_warnings`). The MCP `add_comment` result also exposes
  `verified_author` via `comment_to_mcp`.

- **Backup/restore preserves, does not re-stamp.** `export_jsonl` carries
  `verified_*` (it is a `SELECT *`); `import_jsonl` preserves the stored value
  on restore (`record.get`) for all five tables. Restore reproduces the
  original verified identity rather than re-stamping it with the importer's.

### Explicitly out of scope (still deferred)

- **MCP-HTTP peer identity.** The MCP-over-HTTP transport does not yet bind a
  proven peer identity into `verified_*`. Only MCP stdio is covered.
- **HTTP dashboard authentication.** The dashboard remains unauthenticated;
  binding a verified actor there is still future work (the access-gate half is
  ADR-018's bearer token; the verified-actor half here does not extend to it).
- **The Loom federation wire shape.** `CommentRecordLoom` and the
  `/api/loom/...` projections deliberately do **not** carry `verified_*`. The
  federation contract is held stable via an explicit adapter; verified identity
  is a local-audit concern, not part of the cross-product wire shape. This is a
  decision, not an omission — re-opening it requires a federation-contract
  revision, governed by the same cross-host trigger in the Negative
  consequences above.

## Related

- [ADR-008](ADR-008-claim-aware-write-defaults.md) — claim-aware write defaults
  (the `expected_assignee` invariant is the closest thing 2.x has to a per-write
  ownership check; it is not authentication).
- [ADR-011](ADR-011-agent-sessions-deferred-beyond-2-0.md) — first-class agent
  sessions are deferred; this ADR explains what stands in for them.
- 2.1.0 release-prep §1.4 — the implementation of this ADR's enforcement
  pinning at all three entry points.
