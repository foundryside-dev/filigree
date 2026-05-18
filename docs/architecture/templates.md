# Workflow Template Architecture

Workflow templates are runtime contracts for issue status changes. Each
template declares normal forward `transitions` and, as of 2.1.0,
controlled `reverse_transitions`.

## Forward Transitions

Forward transitions are the ordinary workflow path. They drive
`get_valid_transitions`, ready-state guidance, close validation, and quality
checks. Forward transitions also inherit fields marked `required_at` for the
target state.

## Reverse Transitions

`reverse_transitions` declare controlled escape paths:

```json
{
  "reverse_transitions": [
    {"from": "closed", "to": "open", "enforcement": "soft"}
  ]
}
```

The shape matches forward transitions:

| Field | Required | Meaning |
|-------|----------|---------|
| `from` | yes | Source status |
| `to` | yes | Target status |
| `enforcement` | yes | `hard` blocks on missing explicit fields; `soft` warns |
| `requires_fields` | no | Fields explicitly required for this reverse edge |

Reverse transitions are not returned by `get_valid_transitions` and do not
participate in normal reachability or workflow recommendations. Code must opt
in with `backward=True`.

Reverse transitions enforce their own `requires_fields` only. They do not
inherit target-state `required_at` gates, so cleanup-lane operations such as
forced close preserve their historical behavior while still requiring a
declared edge.

## Built-In Escape Paths

Built-in packs declare reverse edges for:

- `reopen_issue`: done-category states back to the last non-done status.
- `release_claim`: wip-category statuses back to the template release target.
- `close_issue(force=True)`: non-done statuses into done-category statuses.

When a reverse edge is used, Filigree records `transition_forced` before the
normal `status_changed` event.
