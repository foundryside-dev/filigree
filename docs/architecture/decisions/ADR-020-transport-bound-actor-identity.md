# ADR-020: Transport-Bound Actor Identity — Deferred, Posture Made Honest

**Status**: Proposed (Deferred to `filigree-81d3971467`)
**Date**: 2026-06-08
**Deciders**: John (project lead)
**Context**: 3.0.0 release scoping. ADR-012 established that `actor` strings are unauthenticated claims; reviewers keep asking whether 3.0.0 *verifies* the caller. It does not — but it stops doing so *silently*.

## Summary

3.0.0 does **not** add transport-bound actor verification (binding an
authenticated transport to a proven `actor`/`author` identity). That work
remains deferred to `filigree-81d3971467`. What 3.0.0 *does* ship is honesty:
the previously-silent unverified posture is now **discoverable** on both wire
transports via an `actor_verification` object. This ADR records the
decision-to-defer and the posture surface, anchored on the ADR-012 threat model.
It is deliberately a short, complete ADR — the substantive threat analysis lives
in ADR-012; this one is the forward-decision marker the codebase points at.

## Context

[ADR-012](./ADR-012-actor-identity-threat-model.md) fixed the threat model: the
`actor` string is an *identifier*, not an *authentication credential*; the audit
trail records claims, not proofs; the trust boundary is the **transport**, not
the actor field. ADR-012 named transport-level identity verification a "2.3.0+
work package," not a deliverable.

Two facts forced a decision at the 3.0.0 boundary:

1. **The unverified state was silent.** Over HTTP (and MCP-HTTP), the `actor` is
   a self-asserted claim, and the `verified_actor` / `verified_author` columns
   are correctly `NULL` — stamping the server's OS user would be a *false*
   attestation. But callers had no signal that this was happening; a write
   looked identical whether or not the identity behind it was vouched for.
2. **The verification substrate is not settled.** 3.0.0 is still landing the
   federation token model (`WEFT_FEDERATION_TOKEN`, anchor auto-provisioning) and
   the Loomweave/Weft rebrand. Binding a *verified identity* to a transport before
   the token-identity story settles would build on shifting ground and risk a
   second breaking change one minor later.

## Decision

We will **defer** transport-bound actor identity verification to
`filigree-81d3971467`, and in 3.0.0 ship only the *posture surface* that makes
the current (unverified-over-HTTP) state honest:

- **MCP-stdio** stamps the OS identity → `actor_verification.verified = true`.
- **MCP-HTTP** cannot vouch for the caller → `verified = false`, `verified_actor`
  is `NULL`, and the `actor` argument is recorded as a self-asserted claim.
- The posture is exposed as an `actor_verification` object
  (`{verified, verified_actor, deferral, note}`) on the dashboard/Weft HTTP
  surface via the `/api/health` `auth` scope, and on the MCP surface via
  `mcp_status_get` (derived from live session state, so stdio reads `verified`
  and HTTP reads `unverified`).

Authentication itself — proving the caller is who the `actor` says — is **out of
scope for 3.0.0**. This ADR's status stays *Proposed (Deferred)* until
`filigree-81d3971467` lands the verification mechanism, at which point it is
revised to *Accepted* and records the chosen binding.

## Alternatives Considered

### Alternative 1: Implement transport-bound verification in 3.0.0

**Pros**: closes the gap reviewers point at; one fewer deferred item.

**Cons**: depends on a federation-token identity binding that is itself still
moving in 3.0.0; risks a follow-on breaking change; expands the already-large
breaking bundle.

**Why rejected**: building verification on an unsettled token model trades a known
deferral for an unknown rework. Defer until the substrate is stable.

### Alternative 2: Leave the unverified state silent (ship nothing)

**Pros**: zero work.

**Cons**: violates the ADR-012 honesty principle — a caller cannot tell a vouched
write from a self-asserted one. The drop of `verified_*` was a silent
information loss.

**Why rejected**: silence is the actual defect ADR-012 warns against; making the
posture discoverable is cheap and correct even while verification waits.

### Alternative 3: Fold this into ADR-012 instead of a new ADR

**Pros**: no new ADR number.

**Cons**: ADR-012 is the *threat model* — a shipped, stable decision. The
code (CHANGELOG, `mcp_status_get`, `/api/health`) cites a forward
*decision-to-defer plus posture*; that is a distinct decision with its own
lifecycle (it flips to Accepted when `filigree-81d3971467` lands). Overloading
ADR-012 would blur a stable record with an in-flight one.

**Why rejected**: the deferral is its own decision with its own status arc.

## Consequences

### Positive

- The current state is **honest, not silent**: agents and operators can read the
  `actor_verification` posture and know whether identity was vouched for.
- The deferral is recorded with its rationale, so the gap is tracked, not lost.

### Negative

- Over HTTP, the `actor` remains a self-asserted claim — downstream consumers
  **must not** treat an HTTP-surface `actor` as authenticated.
- One more decision carries a *Proposed/Deferred* status until the follow-up lands.

### Neutral

- No wire-breaking change: `actor_verification` is additive on `/api/health` and
  `mcp_status_get`.

## Related Decisions

- **Extends**: [ADR-012: Actor Identity Threat Model](./ADR-012-actor-identity-threat-model.md) — ADR-012 is the threat model; this ADR records the 3.0.0 decision to defer verification and surface the posture.
- **Related to**: [ADR-018: Loom Bearer-Token Auth](./ADR-018-loom-bearer-token-auth.md) — the federation token (`WEFT_FEDERATION_TOKEN`) authenticates the *transport*; transport-bound *actor* identity (this ADR) is the next layer up and depends on it settling.

## References

- Tracking issue: `filigree-81d3971467` — "Transport-bound actor identity verification"
- `docs/superpowers/specs/2026-06-05-transport-bound-actor-identity-design.md`
- `docs/superpowers/plans/2026-06-05-transport-bound-actor-identity.md`
- CHANGELOG `[3.0.0]` — *Fixed*: "HTTP / MCP-HTTP writes no longer silently drop `verified_author`/`verified_actor`"
- `mcp_status_get` / `GET /api/health` (`auth` scope) — the `actor_verification` posture object
