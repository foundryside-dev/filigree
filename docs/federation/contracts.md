# Filigree Federation Contracts

This directory documents filigree's published HTTP contracts for federation consumers — the stable, pinnable targets introduced by [ADR-002](../architecture/decisions/ADR-002-api-generations-and-federation-posture.md).

## What a "contract" is here

A **contract** is a named API generation at the HTTP surface. Filigree currently publishes two:

- **`classic`** — `/api/v1/*`. The pre-federation HTTP surface as it existed through the 1.x series. Frozen: no new operations, no shape changes. Continues to be fully supported. Retirement requires a new ADR with 12 months of deprecation notice.
- **`loom`** — `/api/loom/*`. Introduced in 2.0. The federation-era generation, named for the Loom federation (Clarion + Wardline + Shuttle + filigree). Uses the unified `BatchResponse[T]` / `ListResponse[T]` envelopes, the closed `ErrorCode` enum, the `issue_id` vocabulary, and composed operations like `start_work`.

The **living surface** at `/api/*` (no generation prefix) aliases the current recommended generation — as of 2026-04-24 that is `loom`. Living-surface endpoints are explicitly non-stability; production integrations across version boundaries must pin to a named generation.

MCP and CLI reflect the living surface only. They evolve forward with each release; they do not publish pinnable contracts. Callers who need pinned stability use HTTP.

## Fixture layout

```
tests/fixtures/contracts/
├── classic/
│   └── scan-results.json
└── loom/
    └── scan-results.json
```

Each fixture contains:

1. `_meta` — provenance, authority references, stability statement, and the test that verifies the shape in CI.
2. `shape_decl` — a human-readable shape declaration. Present for new (loom) generations where the shape is a design commitment; omitted for frozen generations where the shape is defined by the existing code.
3. `examples` — representative request/response pairs. Each example has a `name`, a `note` describing what it covers, a `request` (method, path, headers, body), and a `response` (status, headers, body).

Additional endpoints join the fixture set as their loom-generation implementations land (Phase C of the 2.0 federation work package).

## Pinning discipline: shape reference, not byte-equality

**Do not diff fixture bytes against a live response and expect equality.** Filigree does not guarantee field ordering, whitespace, or content-type parameter ordering in responses. What filigree does guarantee for a named generation is:

1. **Key set** — the keys present at each level of the response.
2. **Value types** — each key's value has the declared type (`int`, `str`, `list`, `dict`, nested TypedDict).
3. **Semantic invariants** — values encode the stated meaning (e.g. `stats.files_created` counts files newly created in this ingest; `succeeded` contains server-generated ids for newly-created findings; `warnings` is human-readable).
4. **Enum closure** — values declared as `ErrorCode` members are one of the enum's declared values; unknown strings never appear.
5. **Status-code + envelope pairing** — an ErrorCode paired with its documented HTTP status (`VALIDATION` → 400, `NOT_FOUND` → 404, etc.).

A consumer-side pinning test therefore asserts these five properties against parsed JSON, not against raw bytes. The examples in each fixture are *canonical representatives*, not the only shape a response will take — server-generated ids vary per request; counts vary per inputs; ordering within a list may vary.

### Recommended consumer-side pattern

The sketch below is **illustrative pseudocode**, not a maintained or tested recipe — it shows the pinning *pattern* (load fixture, replay request, assert shape). Adapt it to your language, test framework, and HTTP client; copy-pasting without adaptation is not supported.

```python
# consumer CI sketch — illustrative
import json, pytest, requests

FIXTURE = json.load(open("path/to/filigree/tests/fixtures/contracts/classic/scan-results.json"))

def test_scan_results_success_shape(filigree_url):
    request_body = FIXTURE["examples"][0]["request"]["body"]
    resp = requests.post(f"{filigree_url}/api/v1/scan-results", json=request_body)
    assert resp.status_code == FIXTURE["examples"][0]["response"]["status"]
    body = resp.json()
    expected = FIXTURE["examples"][0]["response"]["body"]
    assert set(body.keys()) == set(expected.keys())
    for key, val in expected.items():
        assert type(body[key]) is type(val), f"{key}: {type(body[key])} vs {type(val)}"
```

(Rust / Go / TypeScript analogues follow the same shape.)

## Living-surface alias decisions

Living-surface aliases (`/api/<endpoint>` with no generation prefix) land per-endpoint as Phase C of the 2.0 federation work package mounts each loom endpoint. Each decision is recorded here so the precedent for "alias vs. classic-only" is auditable.

| Endpoint | Living-surface path | Loom path | Classic path | Status | Decision rationale |
| --- | --- | --- | --- | --- | --- |
| `POST` scan-results | `/api/scan-results` | `/api/loom/scan-results` | `/api/v1/scan-results` | aliased (2026-04-26, Phase C1) | Loom and classic publish at distinct paths (`/v1/` vs. `/loom/`), so the un-prefixed `/api/scan-results` does not collide with classic. Aliasing it to loom gives federation consumers (Clarion, Wardline, Shuttle) the recommended generation at the canonical path without hard-coding the `/loom/` prefix. The handler is wire-identical to `/api/loom/scan-results`; equivalence is pinned by `tests/util/test_generation_parity.py::TestLivingSurfaceEquivalenceScanResults`. |
| `POST` batch/update | n/a | `/api/loom/batch/update` | `/api/batch/update` | classic-and-loom only (2026-04-26, Phase C2) | Classic occupies `/api/batch/update` with `{updated, errors}`; loom uses `{succeeded, failed}`. An un-prefixed alias would collide with the existing classic handler. Federation consumers pin to `/api/loom/batch/update` until classic is retired. |
| `POST` batch/close | n/a | `/api/loom/batch/close` | `/api/batch/close` | classic-and-loom only (2026-04-26, Phase C2) | Same reasoning as batch/update — classic owns the un-prefixed path, loom-only alias deferred. |
| Single-issue CRUD (GET, POST, PATCH, /close, /reopen, /claim, /release, /comments, /dependencies, DELETE /dependencies/*) | n/a | `/api/loom/issues/{issue_id}/...` | `/api/issue/{id}/...` (singular) | classic-and-loom only (2026-04-26, Phase C3) | Classic uses `/api/issue/...` (singular); loom uses `/api/issues/...` (plural). Paths do not collide, so a living-surface alias at `/api/issues/{issue_id}/*` is technically possible. **Deliberately not added in C3** — the single-issue surface is the most-coupled federation entry point, and we want consumers to commit to a pinnable generation (`/api/loom/...`) until at least Phase D when the federation is operating in production. Reconsider when stability data warrants. |
| `POST` /claim-next | n/a | `/api/loom/claim-next` | `/api/claim-next` | classic-and-loom only (2026-04-26, Phase C3) | Classic owns the un-prefixed `/api/claim-next`; loom-only alias same reasoning as above. |
| `GET` /issues (list) | n/a | `/api/loom/issues` | `/api/issues` | classic-and-loom only (2026-04-26, Phase C4) | Classic owns the un-prefixed path with the stream-all behavior; loom adds real `?limit=&offset=` pagination wrapped in `ListResponse[IssueLoom]`. Alias would collide with classic's existing handler. |
| `GET` /ready | n/a | `/api/loom/ready` | `/api/ready` | classic-and-loom only (2026-04-26, Phase C4) | Same reasoning — classic occupies the un-prefixed path. |
| `GET` /search | n/a | `/api/loom/search` | `/api/search` | classic-and-loom only (2026-04-26, Phase C4) | Classic returns `{results, total}`; loom drops `total` per the strict `ListResponse[T]` envelope. Alias would collide. |
| `GET` /files (list) | n/a | `/api/loom/files` | `/api/files` | classic-and-loom only (2026-04-26, Phase C4) | Classic returns `PaginatedResult` (`{results, total, limit, offset, has_more}`); loom drops the `total/limit/offset` siblings per the unified envelope. Alias would collide. |
| `GET` /types | n/a | `/api/loom/types` | `/api/types` | classic-and-loom only (2026-04-26, Phase C4) | Classic owns the un-prefixed path with a bare list; loom wraps in `ListResponse[TypeSummaryLoom]`. Alias would collide. |
| `GET` /blocked, /findings, /observations, /scanners, /packs, /changes | deferred (alias-eligible) | `/api/loom/<endpoint>` | none | loom-only (2026-04-26, Phase C4) | No classic dashboard counterpart — these were MCP-only in the classic generation. **Living-surface aliases at `/api/<endpoint>` are eligible per the precedent rule but deferred to a later pass**, mirroring the C3 decision to defer single-issue surface aliases: federation consumers should commit to a pinnable generation (`/api/loom/...`) until at least Phase D when the federation is operating in production. Reconsider when stability data warrants. |
| `GET` /issues/{issue_id}/{comments,events,files} | n/a | `/api/loom/issues/{issue_id}/...` | none (classic uses singular `/issue/...`) | loom-only (2026-04-26, Phase C4) | Classic uses `/api/issue/{id}/files` (singular); loom uses plural symmetric with `/issues`. No collision but **deliberately not aliased** for the same reason as C3's single-issue surface — these are the most-coupled federation entry points; consumers commit to the loom generation. Loom adds GET counterparts for `/comments` and `/events` (classic exposed them only via MCP / POST). |

The pattern is illustrative for later C tasks: where a loom endpoint has no classic counterpart at the un-prefixed path, prefer aliasing **unless** the endpoint is on a coupled surface where pinning the generation matters more (single-issue surface in C3; per-issue list endpoints in C4); where classic and loom would collide, classic stays at `/api/<endpoint>` and loom is reachable only at `/api/loom/<endpoint>`. The decision for each endpoint lands in the commit that mounts the loom handler.

### C5 — `response_detail` query param on loom batch endpoints (2026-04-26)

Loom batch endpoints `POST /api/loom/batch/update` and `POST /api/loom/batch/close` accept a `response_detail=slim|full` query parameter. Default is `slim` — preserves the C2 wire shape (`SlimIssueLoom` items in `succeeded[]`). Federation consumers needing the full issue projection without a follow-up GET pass `response_detail=full` to receive `IssueLoom` items in `succeeded[]`.

**Locked rule: `newly_unblocked[]` stays `SlimIssueLoom` regardless of `response_detail`.** It represents *secondary* state — consumers branch on its presence to decide whether to refetch. Upgrading every entry to a full `IssueLoom` would inflate the response without buying federation consumers anything new. This rule applies to the loom batch endpoints (C2/C5) AND the loom single-issue close endpoint (`POST /api/loom/issues/{issue_id}/close`, C3) — both compute `newly_unblocked` and both keep it slim.

Classic batch endpoints do NOT accept `response_detail`. The parameter is a loom-only addition.

Validation order: `response_detail` is parsed BEFORE the request body, so an invalid value (`?response_detail=banana`) returns 400 `VALIDATION` even on a malformed body. Pinned by the `response_detail_invalid` fixture example in `tests/fixtures/contracts/loom/batch-{update,close}.json`.

## Phase D — MCP forward-migration (2026-04-27)

Phase D landed the per-ADR-002 §5 commitment that **MCP reflects the
living surface only**. Every MCP tool now mirrors the loom HTTP
vocabulary that landed in Phase C:

- **Vocabulary.** Single-issue MCP tools (`get_issue`, `update_issue`,
  `close_issue`, `reopen_issue`, `claim_issue`, `release_claim`,
  `undo_last`, `add_comment`, `get_comments`, `add_label`,
  `remove_label`, `get_issue_events`) take `issue_id` as the input
  field name (was `id`). `create_issue.parent_id` and
  `update_issue.parent_id` input fields renamed to `parent_issue_id`.
  `add_dependency` / `remove_dependency` take `from_issue_id` /
  `to_issue_id` (was `from_id` / `to_id`). `dismiss_observation` /
  `promote_observation` take `observation_id`. The
  `SlimIssue.id` projection field renamed to `SlimIssue.issue_id`,
  matching `SlimIssueLoom`.

- **Batch tools.** Issue-batch input field unified to `issue_ids` —
  `batch_update`, `batch_close`, `batch_add_label`, `batch_add_comment`.
  Observation/finding batches use `observation_ids` / `finding_ids`
  per the entity-PK rule. Container keys unified to
  `{succeeded, failed}` (`BatchResponse[T]`); legacy
  `{updated|closed, errors, count}` and `BatchActionResponse.results`
  removed. `batch_close` / `batch_update` return
  `BatchResponse[SlimIssue]`; label/comment/observation/finding batches
  return `BatchResponse[str]`.

- **List tools.** Every MCP list tool (`list_issues`, `search_issues`,
  `get_ready`, `get_blocked`, `get_comments`, `get_issue_events`,
  `get_changes`, `list_files`, `list_findings`, `list_observations`,
  `list_scanners`, `list_packs`, `list_types`, `list_labels`) returns
  the unified `ListResponse[T]` envelope: `{items, has_more,
  next_offset?}`. Loose siblings (`stats`, `total`, `errors`, `hint`,
  `limit`, `offset`) dropped per the loom precedent.

- **Workflow tool rename.** `get_workflow_states` →
  `get_workflow_statuses` (response key `states` → `statuses`).
  `explain_state` → `explain_status` (input arg `state` → `status`,
  response key `state` → `status`). CLI commands follow:
  `workflow-states` → `workflow-statuses`, `explain-state` →
  `explain-status`. Internal types (`StateCategory`,
  `StateDefinition`, `state_changed` event, `issues.status` column)
  keep their existing names — the rename is only at the MCP/CLI
  surface.

- **`get_issue.include_files` defaults to `False`.** Aligns MCP with
  the loom HTTP `GET /api/loom/issues/{issue_id}` contract (defaulted
  to `False` since Phase C3). Federation consumers needing the
  file-association payload pass `include_files=true` explicitly.

- **Composed operations.** New atomic MCP tools `start_work` and
  `start_next_work` claim an issue and transition it to a working
  status in one call. `target_status` defaults to the unique wip-category
  status reachable from the issue's current status; statuses with multiple
  reachable wip targets raise `AmbiguousTransitionError` so the caller specifies.
  Backed by core
  methods on `FiligreeDB` with compensating-action rollback (the
  claim is released if the transition fails) so the assignee/status
  pair returns to its prior state on error.

**Forward-only.** MCP does not accept dual vocabularies. Federation
consumers using HTTP have used the loom vocabulary (`issue_id`,
unified envelopes) since Phase C; MCP clients re-pin against the new
schema. The Phase E milestone (CLI forward-migration + parity
fill-in) is the next step.

**Classic HTTP unchanged.** The C2 `test_container_key_parity` strict
xfail in `tests/util/test_cross_surface_parity.py` remains
strict-xfailed — its job is to flag classic drift, and Phase D did
not touch classic.

## Phase E — CLI forward-migration (2026-04-28)

Phase E completed the per-ADR-002 §5 commitment that **MCP and CLI
reflect the living surface only**. After Phase E, every MCP tool has a
CLI counterpart with a matching `--json` envelope. The CLI surface is
now at full parity with MCP and loom HTTP.

- **New CLI modules.** Three new modules added in Phase E2 bring the
  CLI surface to parity with MCP:
  - `cli_commands/observations.py` — 5 commands: `observe`,
    `list-observations`, `dismiss-observation`, `promote-observation`,
    `batch-dismiss-observations`.
  - `cli_commands/files.py` — 12 commands covering the full file and
    finding lifecycle: `list-files`, `get-file`, `get-file-timeline`,
    `get-issue-files`, `add-file-association`, `register-file`,
    `list-findings`, `get-finding`, `update-finding`, `promote-finding`,
    `dismiss-finding`, `batch-update-findings`.
  - `cli_commands/scanners.py` — 6 commands: `trigger-scan`,
    `trigger-scan-batch`, `get-scan-status`, `preview-scan`,
    `report-finding`, `list-scanners`.

- **Verb-noun aliases (permanent).** Phase E3 adds permanent verb-noun
  aliases for every short-form CLI command so they mirror the MCP tool
  names (e.g. `ready` → `get-ready`, `labels` → `list-labels`,
  `update` → `update-issue`). Both names appear in `--help` and
  produce identical output. No deprecation cycle — the short forms are
  stable.

- **Composed CLI operations.** Phase E4 adds `start-work` and
  `start-next-work` — CLI wrappers for the D6 `FiligreeDB.start_work` /
  `start_next_work` composed operations. Backed by the same core
  methods the MCP tools call.

- **`filigree show --with-files` flag.** Phase E5 aligns `filigree
  show <id>` with `get_issue.include_files=False` (D4 default): file
  associations are omitted unless `--with-files` is passed.

- **`--json` envelopes unified.** Phase E1 aligned remaining CLI
  `--json` outputs with the loom/MCP shapes: list commands wrap items
  in `ListResponse[T]` (`{items, has_more}`), slim-issue projections
  use `issue_id`, batch commands emit `{succeeded, failed}`.

- **`add-label` arg-order alignment (BREAKING).** Phase E6 flips
  `filigree add-label` to `<label> <issue_id>` order, matching
  `batch-add-label`'s existing order. Scripts using the old
  `<issue_id> <label>` positional order must update.

- **CLI↔MCP↔HTTP parity battery extended.** `tests/util/
  test_cross_surface_parity.py` now includes Phase E parity tests:
  `list-observations` CLI↔MCP, `list-files` CLI↔loom-HTTP,
  `start-work` CLI↔MCP (error and success shapes). The Phase D gate
  ("MCP↔HTTP parity") expands to "CLI↔MCP↔HTTP parity".

- **Classic HTTP unchanged.** The C2 `test_container_key_parity`
  strict xfail remains strict-xfailed — Phase E did not touch classic.

**Forward-only.** CLI does not accept dual vocabularies. The loom
envelope shape (`--json` output) replaces legacy shapes outright;
clients pinning to old `--json` output re-pin against the new schema.

## When a contract evolves

**Non-breaking additions** (new optional response fields, new optional request parameters with safe defaults) may land in-place without a new generation. Fixtures are updated to reflect the new shape; the `_meta.updated` field moves.

**Breaking changes** introduce a new named generation — `loom-v2`, `loom-graph`, `loom-entities`, or an entirely new era name if the shift is foundational. The older generation is *not* mutated; it continues to serve the pre-break shape until retired per ADR-002 §8 (new ADR + 12-month deprecation + CLI/docs communication).

## Cross-references

- **ADR-002** (the naming + lifecycle rules): `docs/architecture/decisions/ADR-002-api-generations-and-federation-posture.md`.
- **2.0 work package** (the execution sequence): `docs/plans/2026-04-24-2.0-federation-work-package.md`.
- **ADR-017 audit** (verifies classic-generation semantics are preserved on the 2.0 branch): `docs/plans/2026-04-24-adr017-audit.md`.
- **Clarion ADR-004** (finding exchange format): `/home/john/clarion/docs/clarion/adr/ADR-004-finding-exchange-format.md`.
- **Clarion ADR-017** (severity + dedup): `/home/john/clarion/docs/clarion/adr/ADR-017-severity-and-dedup.md`.
