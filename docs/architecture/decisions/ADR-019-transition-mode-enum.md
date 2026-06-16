# ADR-019: `TransitionMode` Enum Replaces the Internal `backward` Boolean

**Status**: Accepted
**Date**: 2026-06-08
**Deciders**: John (project lead)
**Context**: 3.0.0 breaking-bundle (`filigree-9b4bb6e52e`). The major-version boundary is the cheap window to replace an internal, wire-invisible flag with a self-documenting type.

## Summary

The transition-direction flag that distinguished a *forward* status change from
a *reverse/escape* one was a bare `backward: bool`, threaded across
`TemplateRegistry.validate_transition`, `update_issue`, the `DBMixinProtocol`
signature, and `InvalidTransitionError`. 3.0.0 replaces it with a
`TransitionMode{FORWARD, BACKWARD}` enum in `filigree.types.api`. The flag has
**no MCP / CLI / HTTP / wire exposure**, so it is replaced outright with no
compatibility alias; the only affected callers are embedders of the internal
Python API.

## Context

ADR-013 introduced declared reverse/escape transitions (`reverse_transitions`)
and required callers to opt into the escape lane with `backward=True`. The
resulting flag had two problems:

- **Opaque call sites.** `update_issue(..., backward=True)` and the bare `True` /
  `False` passed through nine internal call sites in `db_issues.py` and
  `templates.py` carried no hint of what the boolean *meant*. A reader had to
  trace the parameter to learn that `True` selects the `reverse_transitions`
  validation lane.
- **A boolean is not the right shape for a closed set of directions.** Direction
  is a two-valued *category*, not a yes/no answer to a question. A boolean
  invites "what does `False` mean here?" at every site and gives no anchor for a
  third mode should one ever be needed.

Critically, the flag is **internal only**. A call-site enumeration
(`filigree-9b4bb6e52e`) confirmed it appears on no MCP tool input, no CLI flag,
and no HTTP request body — it is reachable only from `FiligreeDB` Python methods
and the template registry. That removes the usual major-version constraint
(serve both names through a deprecation window): there is no external consumer to
migrate, so the rename can be a clean cut.

## Decision

We will define a `TransitionMode` enum and replace every `backward: bool`
parameter and read with it:

```python
class TransitionMode(Enum):
    """Direction of a status transition. Replaces the historical `backward` bool."""
    FORWARD = "forward"
    BACKWARD = "backward"
```

- `validate_transition`, `update_issue`, and `DBMixinProtocol.update_issue` take
  `mode: TransitionMode = TransitionMode.FORWARD` instead of
  `backward: bool = False`.
- `InvalidTransitionError.backward` becomes `InvalidTransitionError.mode`.
- Call sites read `if mode is TransitionMode.BACKWARD:` instead of `if backward:`.

Because the change is wire-invisible, there is **no** `backward=` alias and no
deprecation window. `mypy` drove completeness: every remaining `backward=`
keyword and `if backward` read was a type error until migrated.

The close / reopen / release-revert behaviour and the `transition_forced` audit
event are **unchanged** — this is a rename of the selector, not a change to what
the selector selects (which remains the ADR-013 `reverse_transitions` lane).

## Alternatives Considered

### Alternative 1: Keep `backward: bool`

**Pros**: zero churn; no migration for internal embedders.

**Cons**: the opacity that motivated the change persists; the major-version
window — the one cheap moment to make a wire-invisible breaking rename — is
wasted.

**Why rejected**: the cost is paid once at a major boundary; the readability gain
is permanent.

### Alternative 2: A `str` literal / `StrEnum` (`mode="backward"`)

**Pros**: marginally lighter; serialises trivially if it ever became wire-facing.

**Cons**: a bare string is unvalidated at the call site — `mode="bacward"` type-checks
and fails at runtime. A plain `Enum` member is the strongest compile-time guard.

**Why rejected**: the flag is internal, so the serialisation upside is moot, and
the validation downside is real. `Enum` over `StrEnum` because nothing needs the
value to *be* a string.

### Alternative 3: Replace it *and* keep a `backward=` alias for one minor

**Pros**: belt-and-braces for any out-of-tree embedder.

**Cons**: an alias implies a wire/contract obligation that does not exist here; it
would carry dead translation code through 3.x for a parameter no published
surface exposes.

**Why rejected**: there is no external consumer to protect — the call-site
enumeration proved it. An alias would be cargo-culted major-version discipline.

## Consequences

### Positive

- Call sites are self-documenting: `mode=TransitionMode.BACKWARD` states intent.
- `mypy` mechanically guarantees no stray `backward` boolean survives.
- A future third direction (if ever needed) has a home.

### Negative

- A breaking change for any code that calls the internal `update_issue` /
  `validate_transition` with `backward=...` or reads `InvalidTransitionError.backward`
  (`backward=True` → `mode=TransitionMode.BACKWARD`; `.backward` → `.mode`).

### Neutral

- The wire surfaces (MCP, CLI, HTTP) are untouched — agents and federation
  consumers see no difference.

## Related Decisions

- **Refines**: [ADR-013: Backward Edges in Workflow Templates](./ADR-013-backward-edges-in-workflow-templates.md) — the `backward=True` opt-in ADR-013 defined is now `mode=TransitionMode.BACKWARD`. The reverse-lane *semantics* are unchanged.
- **Refines**: [ADR-005: Workflow Enforcement and Explicit Cleanup Paths](./ADR-005-workflow-enforcement-and-cleanup-paths.md)
- **Related to**: [ADR-009: Response Shape Philosophy](./ADR-009-response-shape-philosophy.md) — `InvalidTransitionError` carries structured recovery data on the wire; this rename touches only the internal attribute, not that payload.

## References

- `docs/plans/2026-06-06-pr52-section4-3.0.0-items.md` §Task 3
- `src/filigree/types/api.py` (`TransitionMode`, `InvalidTransitionError`)
- `src/filigree/templates.py` (`validate_transition`), `src/filigree/db_issues.py` (`update_issue`)
- CHANGELOG `[3.0.0]` — *Changed (BREAKING)*: "`TransitionMode` enum replaces the internal `backward: bool`"
