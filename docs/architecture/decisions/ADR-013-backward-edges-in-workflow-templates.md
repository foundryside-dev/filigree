# ADR-013: Backward Edges in Workflow Templates

**Status**: Accepted
**Date**: 2026-05-18
**Deciders**: John (project lead)
**Context**: 2.1.0 release prep Phase 4 removed the internal transition-check bypass used by reopen, release-revert, and forced close paths.

## Summary

Workflow escape paths are declared in templates with `reverse_transitions`.
They are validated separately from normal forward transitions and are not
returned as suggested next states.

## Context

Before 2.1.0 Phase 4, reverse workflow operations used an internal bypass:
`reopen_issue`, `release_claim` status reverts, and `close_issue(force=True)`
could write statuses that were not present in the normal transition graph.
That kept cleanup behavior practical, but it also made workflow templates an
incomplete contract. New reverse operations had to remember to use the same
private bypass, and audit readers had to infer intent from implementation
details.

## Decision

Templates may declare:

```json
{
  "reverse_transitions": [
    {"from": "closed", "to": "open", "enforcement": "soft"}
  ]
}
```

Each reverse transition uses the same field names as a forward transition:
`from`, `to`, `enforcement`, and optional `requires_fields`.

The registry stores reverse transitions in a separate cache. Normal transition
queries and reachability checks remain forward-only. Callers must opt into the
escape lane with `backward=True`; when they do, the registry validates against
`reverse_transitions` and missing edges raise `InvalidTransitionError`.

Forced close is intentionally represented as a reverse/escape edge even when
the same state pair also exists as a forward transition. This preserves the
cleanup-lane behavior from ADR-005: `force=True` may bypass normal hard gates,
but the edge is still declared and audited.

## Consequences

### Positive

- Workflow templates now describe both normal work and controlled escape paths.
- `transition_forced` is tied to declared reverse transitions, not an internal
  skip-check parameter.
- Reopen and release-revert behavior can be reviewed at the template layer.

### Negative

- Custom packs that depend on reopen, release-revert, or forced close must add
  `reverse_transitions`.
- Built-in templates carry more transition data.

### Neutral

- `get_valid_transitions` remains forward-only so reverse edges do not appear
  as routine workflow recommendations.
- Reverse transitions enforce only their explicit `requires_fields`; they do
  not inherit target-state `required_at` gates. This preserves forced cleanup
  semantics while still allowing packs to add explicit reverse gates.

## Related Decisions

- **Refines**: [ADR-005: Workflow Enforcement and Explicit Cleanup Paths](./ADR-005-workflow-enforcement-and-cleanup-paths.md)
- **Related to**: [ADR-003: Operational Durability, Not Audit-Proof Records](./ADR-003-operational-durability-not-audit-proofing.md)

## References

- `docs/plans/2026-05-18-2.1.0-release-prep.md`
- `docs/plans/2026-05-17-2.1-db-issues-hardening-design.md`
