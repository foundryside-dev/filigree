# ADR-018: Opt-in Bearer-Token Auth for the Loom Federation Surface

**Status**: Accepted
**Date**: 2026-06-03
**Deciders**: John (project lead)
**Context**: `filigree-30cd35bcb9` (Reconcile inbound auth posture across
Filigree↔Clarion — token sent, ignored). Implements **option (b)** from that
issue, chosen by the project lead during the 2026-06-03 API review.

## Summary

Filigree's HTTP API gains **opt-in** inbound authentication for the **loom
federation surface** only. When an operator sets `FILIGREE_API_TOKEN`, requests
to `/api/loom/*` and the living-surface federation aliases (today: `POST
/api/scan-results`) must carry a matching `Authorization: Bearer <token>` or
receive `401 PERMISSION`. With the env var unset, behaviour is byte-identical to
today (the loopback boundary of [ADR-012](ADR-012-actor-identity-threat-model.md)).
This **partially lifts** ADR-012 §5's deferral: the **access-gate** half lands
now; **verified identity** (binding a proven actor into the audit trail) stays
deferred to `filigree-81d3971467`.

## Context

Clarion's Filigree HTTP client already sends `Authorization: Bearer
<FILIGREE_API_TOKEN>` on its federation requests (scan-results ingest,
`POST /api/loom/findings/clean-stale`). Filigree ignored it: the API is
loopback-only and reads `actor` from the request body. Operators therefore
configured a token that did nothing on the Filigree side — a dishonest contract
(one side authenticates, the other discards the credential).

ADR-012 established that for Filigree 2.x the trust boundary is the transport,
and that HTTP authentication is a 2.3.0+ work package. That posture is correct
**while the federation is co-located on loopback** (Clarion `127.0.0.1:9111`,
Filigree `127.0.0.1:8377`, same host). But honouring the token Clarion already
sends is cheap, removes the contract dishonesty, and gives operators a real gate
the day they want one — without waiting for the full verified-identity design.

## Decision

### 1. Opt-in enforcement

Auth is active **only** when `FILIGREE_API_TOKEN` is set (non-empty after
strip). When unset, `create_app` does not install the auth middleware at all, so
there is zero per-request overhead and no behavioural change. This is the only
model compatible with [ADR-002](ADR-002-api-generations-and-federation-posture.md)'s
freeze of the `loom` generation: making a token *mandatory* would break every
consumer not yet sending one, which would require minting a new generation.

### 2. Scope — the loom federation surface only

Enforcement covers `/api/loom/*` and the living-surface federation aliases that
route to loom (`LIVING_FEDERATION_ALIASES`, today `{scan-results}`). It also
covers those paths under the server-mode project mount (`/api/p/{key}/loom/…`,
`/api/p/{key}/scan-results`).

**Out of scope, deliberately:**

- The **classic** surface (`/api/issue/…`, `/api/issues`, `/api/v1/scan-results`,
  …) stays open. Classic is the frozen human/legacy surface; adding required
  auth would break 1.x callers.
- The **local dashboard UI** (a tokenless browser app at `/` hitting classic
  reads/writes) keeps working.
- `/api/health` and `/` stay open.
- **MCP and CLI** are unaffected (stdio / shell transport boundary).

This yields a clean human/machine split: humans use the dashboard over loopback;
machines (Clarion/Wardline/Shuttle) use the loom generation and authenticate.

### 3. Token source and comparison

The token is read from the **`FILIGREE_API_TOKEN`** environment variable — the
name operators already set and Clarion already sends. A single server-wide token
(one federation trust domain); per-project tokens are future work. The token is
read once at `create_app` time and compared **constant-time**
(`hmac.compare_digest`). Filigree writes nothing to disk.

### 4. Error shape

A rejected request returns the existing error envelope `{"error": str, "code":
"PERMISSION"}` with status `401` and a `WWW-Authenticate: Bearer` header. No new
`ErrorCode` member is introduced — `PERMISSION` already pairs with 401 — so
generation enum-closure and wire-compatibility are preserved.

### 5. Non-goal: verified identity

This gates **access**, not **identity**. The `actor` field is still read from
the request body and remains an unauthenticated claim per ADR-012. Binding the
authenticated transport to a proven actor (so the audit trail records proofs,
not claims) is the separate verified-actor work tracked as
`filigree-81d3971467` and is **not** delivered here.

## Consequences

### Positive

- The Clarion↔Filigree contract is honest: the bearer token now does something.
- Operators get a real inbound gate the moment they set the env var, with no
  code change and no consumer breakage.
- Wire-compatible with ADR-002: default (unset) behaviour is unchanged, so no
  new generation is needed.

### Negative / known boundaries

- **`/api/v1/scan-results` (the classic outlier) is not enforced.** A federation
  producer posting to the classic path bypasses auth. Acceptable: contracts
  direct federation consumers to the loom/living path, and classic is the frozen
  human-compat surface. Documented here so it is a known boundary, not a silent
  gap.
- A single shared secret is symmetric: any holder can call any loom route. This
  is an access gate, not per-actor authorization.
- Enforcement is opt-in, so an operator who never sets the token gets no gate —
  which is the intended default for the loopback deployment.

### Neutral

- The cross-host trigger added to ADR-012 (2026-06-03) still stands: if a peer
  binds off-loopback, the verified-identity work (`filigree-81d3971467`) becomes
  near-term. This ADR does not change that trigger; it delivers the access-gate
  half early.

## Related

- [ADR-012](ADR-012-actor-identity-threat-model.md) — the threat model this
  partially lifts (access-gate yes; verified identity still deferred).
- [ADR-002](ADR-002-api-generations-and-federation-posture.md) — the generation
  freeze that forces opt-in enforcement.
- `filigree-30cd35bcb9` — the tracking issue (closed on this ADR landing).
- `filigree-81d3971467` — verified transport-bound *identity* (stays open).
- Design spec: `docs/superpowers/specs/2026-06-03-loom-bearer-token-auth-design.md`.
