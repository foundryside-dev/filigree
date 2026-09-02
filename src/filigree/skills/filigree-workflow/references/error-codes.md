# Response Shapes and Error Codes

Load this reference when parsing `--json` output or MCP responses, or when
deciding how to react to a failure.

## Response Envelopes (2.0)

- **Batch ops** → `{succeeded: [...], failed: [{id, error, code}, ...], newly_unblocked?: [...]}`.
  `failed` is always present (empty list if none); `newly_unblocked` is
  present only when non-empty (omitted when the op unblocked nothing). Pass `--detail=full` (CLI) or
  `response_detail="full"` (MCP) to get full records back.
- **List ops** → `{items: [...], has_more: bool, next_offset?: int}`.
  `next_offset` only appears when there is a next page.
- **Errors** → `{error: str, code: ErrorCode, details?: dict}`.

The issue ID is always `issue_id` in 2.0 — in MCP inputs, response payloads,
and CLI JSON. Status is always `status`; "state" was retired as a
user-facing word.

## ErrorCode — the complete set

Switch on `code`, never on message text. The full enum:

`VALIDATION`, `NOT_FOUND`, `CONFLICT`, `INVALID_TRANSITION`, `PERMISSION`,
`NOT_INITIALIZED`, `IO`, `INVALID_API_URL`, `FILE_REGISTRY_DISPLACED`,
`REGISTRY_UNAVAILABLE`, `LOOMWEAVE_REGISTRY_VERSION_MISMATCH`,
`LOOMWEAVE_OUT_OF_SYNC`, `BRIEFING_BLOCKED`, `STOP_FAILED`,
`SCHEMA_MISMATCH`, `INTERNAL`.

Branch on `code` for retry policy: `CONFLICT` → CLI exit 4, retryable
(another agent owns the claim — retry against a different issue); everything
at exit 1 needs operator intervention.

## Failure modes that deserve a specific response

- **`INVALID_TRANSITION`** — the workflow does not allow that status hop from
  here. Call `workflow_transition_list` (MCP) or `filigree transitions <id>`
  to see what *is* allowed, then walk it (or pass `--advance` / `advance=true`
  to walk the soft transitions automatically).
- **`SCHEMA_MISMATCH`** — the installed `filigree` is older than the project
  database. The error message contains upgrade guidance. Surface it to the
  user; do not retry.
- **`CONFLICT`** — someone else holds the claim, or the record changed under
  you. Safe to retry against different work; never force-overwrite. A close
  of a Legis-governed issue also returns `CONFLICT` when the closure gate
  does not PROCEED (a tampered-ledger integrity failure is `INTERNAL`
  instead); `error` is the gate reason and may end in
  `rename lineage: <sei> -> <new_locator> (<event>)` — Loomweave reports the
  bound SEI orphaned, so re-bind to the new locator rather than retrying.
- **`REGISTRY_UNAVAILABLE`** — Loomweave could not be reached or negotiated
  with in fail-closed loomweave mode (`registry_backend=loomweave`,
  `loomweave.allow_local_fallback=false`). CLI verbs exit 1 with the
  envelope; the stdio MCP server stays up in degraded mode
  (`mcp_status_get.status == "registry_unavailable"`, re-probing at most
  once per 10 s — `registry_retry` shows the schedule) and every other tool
  returns this code until Loomweave answers; the `session-context` hook
  (always exit 0) prints the remedy inside the snapshot instead of the
  generic hook-failed warning. `details` carry `cause_kind`, `url`,
  `backend` and `hint`, a remedy specific to `cause_kind`: only
  reachability failures (`network` / `timeout` / `http_error`) are fixed by
  `loomweave serve`; `auth` / `auth_token_missing` name the bearer-token env
  var to export; `role_declined` / `auth_mode_unsupported` point at
  Loomweave's serving configuration; `invalid_response` needs a matching
  Filigree/Loomweave pair. Every hint except `invalid_response` also offers
  `loomweave.allow_local_fallback=true` as an interim fallback to local file
  ids. Surface the hint; do not retry in a loop.
- **`ForeignDatabaseError`** — filigree found a parent project's database but
  no local `.filigree.conf`. Run `filigree init` in the current directory. Do
  **not** `cd` upward to a different project unless that was the actual intent.
