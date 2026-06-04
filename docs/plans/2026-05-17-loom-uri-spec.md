# Loom URI Scheme Specification

**Status:** Draft (2026-05-17) — **superseded; see status note below.**

> **Federation status (added 2026-06-05).** The federation-wide status of this
> `loom://` URI scheme is now tracked authoritatively at `~/loom/uri-scheme.md`.
> In short: the `loom://` registry + `/api/loom/multi-fetch` apparatus was
> **never implemented and is formally closed** — the stable cross-tool identity
> it reached for is now delivered by **SEI** (`~/loom/sei-standard.md`), which
> lists this scheme as out of scope. The thin `shuttle://…` milestone-reference
> convenience (used by the planning-deprecation plan) is **unresolved and
> low-stakes** because **Shuttle is a roadmap thought-bubble with no repo** —
> there is nothing for a `shuttle://` URI to resolve to. This spec is retained
> for historical context; do not build on `loom://` or `shuttle://`. See
> `~/loom/uri-scheme.md` and `~/loom/contracts-index.md`.

**Scope:** Canonical form, registration, resolution, and authorization for Loom URIs — the cross-component reference primitive in the Loom federation
**Sibling documents:**
- `2026-05-17-shuttle-design.md` — Shuttle (consumer)
- `2026-05-17-filigree-planning-deprecation.md` — filigree migration (consumer)

---

## 1. Purpose

Loom federation components (Filigree, Shuttle, Clarion, Wardline)
reference each other's objects without sharing storage. A Loom URI is
a stable, canonical, parseable handle that any component can use to
identify an object in any other component.

This specification covers:

- The URI grammar and stability guarantees.
- The component registration model (how each component publishes its
  URI scheme to the federation).
- The resolution model (how a holder of a URI fetches the referenced
  object).
- The authorization model (who can mint, attach, and resolve URIs).
- Failure modes and what consumers must handle.

This is a foundational document. **Shuttle Batch 3 cannot ship until
this spec is ratified and the URI scheme is registered.** Any URI
written to storage before ratification carries change-risk; this is
the principal motivation for keeping pre-ratification Shuttle work
free of URI references in stored rows.

---

## 2. Federation reality

As of 2026-05-17:

- **Filigree** runs and exposes `/api/loom/*` (issues, files,
  analytics, releases routers).
- **Clarion** exists as a sibling project but federation integration
  is in-flight.
- **Shuttle** does not exist (this design package is the founding
  artifact).
- **Wardline** does not exist; named only.
- **Loom itself is not a running service.** It is a federation
  *convention*. There is no broker, no router daemon, no central
  registry. Components register their URI schemes with each other
  pairwise via configuration. The spec below treats "the federation
  registry" as a logical concept; v1 implementation is a flat
  configuration file shared across components.

---

## 3. URI grammar

```
loom-uri   = component "://" authority "/" object-kind "/" object-id

component  = component-name              ; lowercase, ASCII letters, digits, hyphen
authority  = project-prefix              ; per-project namespace
object-kind = kind-name                  ; lowercase, ASCII letters, underscore
object-id  = component-defined-id        ; opaque to other components
```

### 3.1 Examples

```
filigree://elspeth/issue/14f4e50d00
filigree://filigree/issue/c2009921cf
shuttle://elspeth/plan/9a3f8c2b1d
shuttle://elspeth/step/0b1e7f4a89
clarion://elspeth/review/4d2e1a90c7
coordination://elspeth/milestone/2026-q3-launch       ; filigree-coordination pack post-rename
wardline://elspeth/gate/tier3-trust-boundary          ; after Wardline ships
```

### 3.2 Component name registry

The following component names are reserved by this specification:

| Component | Status | Notes |
|---|---|---|
| `filigree` | active | URI form unchanged from current `/api/loom/*` routing. Implemented kinds: `issue`, `observation`, `annotation`, `file`, `finding`, `comment`. **Pending kinds:** `release`, `release_item` — the filigree releases Loom router does not yet expose `GET /api/loom/filigree/releases/{id}`; the kind is reserved here but unresolvable until that router lands. Tracked separately as filigree implementation work. |
| `shuttle` | reserved (Shuttle Batch 3) | |
| `clarion` | reserved | active integration partner |
| `wardline` | reserved | named only; URI form pre-allocated |
| `coordination` | reserved | filigree's renamed planning-adjacent pack (per deprecation plan) |

New component names go through a registration request (Section 5).
Component names are lowercase ASCII; hyphens permitted (`agent-systems`);
underscores forbidden in the component slot (reserved for object-kind).

### 3.3 Authority (project-prefix)

The authority is the project prefix. Each federation peer scopes its
data to a project; the URI authority disambiguates which project's
objects are being referenced.

Examples: `elspeth`, `filigree`, `clarion-dev`, `mycompany-platform`.

Authorities are lowercase ASCII, digits, and hyphens. Maximum 64
characters. Authorities are scoped per-component — `filigree://elspeth`
and `shuttle://elspeth` reference the same project across two
components.

### 3.4 Object kind

The object kind is component-defined and meaningful only to that
component. Filigree publishes kinds (`issue`, `observation`,
`annotation`, `release`, etc.); Shuttle publishes its own
(`plan`, `stage`, `step`). Cross-component kind reuse is allowed but
not coordinated — a `filigree://x/release/...` and a hypothetical
`shuttle://x/release/...` are unrelated objects.

Object kinds are lowercase ASCII letters and underscores. Maximum 64
characters.

### 3.5 Object ID

The object ID is opaque to the rest of the federation. Each component
chooses its own ID scheme (ULID, UUID, hash, sequence). Other
components do not parse, sort, or interpret object IDs — they pass
them back to the owning component for resolution.

Object IDs are URL-safe characters: `[A-Za-z0-9_.-]`. Maximum 128
characters.

### 3.6 Reserved patterns

The following are reserved for federation use:

- `loom://` (no component) — reserved for future federation-level
  resources (e.g., `loom://registry`). Not currently used.
- Object IDs starting with `__` (double underscore) — reserved for
  component-internal sentinels (e.g., `__migration__`,
  `__system__`). Cross-component callers should treat these as
  opaque but be aware they exist.

### 3.7 Canonicalization

URIs are case-sensitive in `object-id` (because some components use
case-sensitive identifiers). All other slots are lowercased on parse:
`Filigree://Elspeth/Issue/abc` → `filigree://elspeth/issue/abc`.
Trailing slashes are stripped. Query strings and fragments are
unsupported in v1 (reserved for future use).

---

## 4. Stability guarantees

A Loom URI is a contract. Once a URI is stored in any federation
peer's data, its components are bound to the following:

| Slot | Stability |
|---|---|
| `component` | **Stable.** Never renamed; never repurposed. Reserved names cannot be reassigned. |
| `authority` (project prefix) | **Stable per project.** A project does not change its prefix once federation references exist. |
| `object-kind` | **Stable.** Components do not rename kinds. Adding new kinds is allowed; removing is a deprecation event with the same workflow as filigree's planning-pack deprecation. |
| `object-id` | **Stable for the object's lifetime.** A component does not re-issue an ID after deletion. |

### 4.1 What "stable" rules out

- Renaming `filigree` → `filigree-classic` post-Shuttle: forbidden.
  Existing URIs would break.
- Reordering URI slots: forbidden. Anyone storing `filigree://x/issue/y`
  in their data must continue to be able to resolve that exact string.
- Adding required URI components (e.g., a version slot): forbidden in
  v1. A future v2 grammar may add slots but must remain
  backward-compatible with v1 URIs.

### 4.2 What "stable" permits

- Adding *new* components, authorities, object kinds: encouraged.
- Renaming the *display name* of a component or kind: allowed (it's
  user-facing copy, not the URI).
- Changing how a URI resolves (different HTTP path, different format
  returned) without changing the URI itself.

---

## 5. Registration

The federation registry, in v1, is a JSON file shared across all
peers:

```
loom-registry.json
{
  "version": "1",
  "components": [
    {
      "name": "filigree",
      "base_url": "http://localhost:8377",
      "loom_root": "/api/loom",
      "object_kinds": ["issue", "observation", "annotation", "release",
                        "release_item", "file", "comment", "finding"],
      "identity_token_hint": "FILIGREE_LOOM_TOKEN"
    },
    {
      "name": "shuttle",
      "base_url": "http://localhost:8378",
      "loom_root": "/api/loom",
      "object_kinds": ["plan", "stage", "step"],
      "identity_token_hint": "SHUTTLE_LOOM_TOKEN"
    },
    ...
  ]
}
```

### 5.1 Registry distribution

Each project carries its registry at `.loom/registry.json`. Local
federation peers read it on startup. New components join by adding an
entry and propagating the file to each peer.

A more dynamic registry (gossip, service discovery, central broker)
is a v2 concern. v1 ships flat-file.

#### 5.1.1 Multi-project deployment topology (v1)

A single Shuttle (or Filigree, Clarion) process may serve multiple
projects. Each project carries its own `.loom/registry.json`. The v1
rule is:

- **Each component process maintains a per-project view of the
  registry.** When a request lands carrying the authority slot
  (`shuttle://elspeth/...`), the resolver reads
  `<project-root-for-elspeth>/.loom/registry.json` to look up peer
  base_urls.
- **Authorities are resolved through the calling component's project
  context, not the receiving component's.** A request from
  filigree-elspeth-process resolves URIs via elspeth's registry; a
  request from filigree-acme-process resolves via acme's. The two
  processes can hold conflicting views without colliding, because
  cross-project URIs are rare and explicit.
- **Cross-project resolution is allowed but requires both registries
  to list the same peer base_urls for that authority.** If
  elspeth's registry lists `shuttle://acme` resolution at
  `http://shuttle-acme:8378` and acme's registry lists it at
  `http://shuttle-acme-internal:8378`, the resolver picks the local
  project's view (elspeth's). This is documented as "perspective —
  authority resolution is asymmetric in v1."

A pure single-process Shuttle deployment that hosts one project is the
default and simplest case; multi-project is opt-in.

This resolves the question raised at §11.4 (which now records the
agreed default rather than enumerating options).

#### 5.1.2 Registry divergence detection

Flat-file registries propagated manually drift. v1 mitigation is
detection, not prevention:

- Every Loom response carries an `X-Loom-Registry-Version` header
  whose value is the SHA-256 of the receiving component's
  registry.json (computed at startup, cached).
- The calling component compares against the version header it
  expected (its own registry's SHA of the receiving peer's entry —
  see §5.1.3 for the SHA scope). Mismatch increments a
  `loom_registry_divergence_total{peer=<name>}` counter and logs at
  WARN with both hashes.
- The call still completes; the metric is the operator's signal that
  manual reconciliation is needed.

#### 5.1.3 Registry-hash scope

The SHA-256 in `X-Loom-Registry-Version` covers the *receiving peer's
own registry entry* — name, base_url, loom_root, object_kinds list —
sorted canonically. It does **not** cover the entire registry (other
peers' entries are not relevant to this peer's identity contract).
This keeps the hash stable across unrelated registry edits.

### 5.2 Collision protection

When a component starts up, it validates that:

- Its declared component name appears in the registry.
- Its declared object kinds match the registry entry.
- No other entry claims the same `(name, base_url)` pair.

Mismatch refuses startup with a clear error. This prevents the
"rogue component registers `filigree://` and shadows the real
filigree" failure mode the panel review flagged.

### 5.3 Component identity tokens

Each component holds a *component identity token* (HMAC secret)
issued at federation registration. Cross-component calls carry the
token in an `X-Loom-Component` header containing
`<component-name>:<hmac-of-request-body>`. Receiving peers verify
the HMAC against the registered secret.

**v1 simplification:** tokens are static, pre-shared via the
registry file. Token rotation is a v2 feature (signed token bundles
distributed by the registry).

Tokens scope **identity**, not **authorization** to specific
operations. A registered Clarion can call any `/api/loom/*`
endpoint on Shuttle; what it's allowed to *do* on those endpoints is
the receiving component's policy (e.g., Shuttle accepts
`clarion_review` attachments but not `filigree_issue` attachments
from a Clarion-identified caller — see sibling doc 3.3).

---

## 6. Resolution

A URI holder fetches the referenced object by:

1. Parse the URI; extract `component`, `authority`, `kind`, `id`.
2. Look up `component` in the registry; get `base_url` and `loom_root`.
3. Construct the resolution URL:
   `{base_url}{loom_root}/{component}/{kind_endpoint}/{id}`
   where `kind_endpoint` is component-defined (typically pluralized:
   `issue` → `issues`, `plan` → `plans`).
4. Issue an authenticated GET with `X-Loom-Component` header.
5. Receive a slimmed object payload (per the receiving component's
   slim contract).

### 6.1 Resolution contract per kind

Each component publishes its `kind_endpoint` mapping and slim payload
schema in its own loom-contracts directory:

- Filigree: `/home/john/filigree/docs/federation/contracts.md` (exists).
- Shuttle: `/home/john/shuttle/docs/federation/contracts.md` (to be
  created with Batch 3).
- Clarion: documented in `/home/john/clarion/docs/...`.

Slim payloads are deliberately limited — the kind-endpoint returns
just enough for the consumer to display a one-line summary. Deep
inspection requires calling the component's native API (out of scope
for this spec).

### 6.2 Batched resolution

`POST /api/loom/multi-fetch` is a federation-standard endpoint each
component exposes:

```
Request:
{
  "uris": [
    "filigree://elspeth/issue/abc",
    "filigree://elspeth/observation/xyz",
    "clarion://elspeth/review/123"
  ]
}

Response:
{
  "results": [
    {
      "uri": "filigree://elspeth/issue/abc",
      "status": "resolved",
      "payload": { ... slim issue ... }
    },
    {
      "uri": "filigree://elspeth/observation/xyz",
      "status": "not_found",
      "error": { "code": "NOT_FOUND", "message": "Observation xyz does not exist" }
    },
    {
      "uri": "clarion://elspeth/review/123",
      "status": "timeout",
      "error": { "code": "TIMEOUT", "message": "clarion (timeout 5s)", "retryable": true }
    }
  ]
}
```

Per-URI status is one of:

| Status | Meaning | Retryable |
|---|---|---|
| `resolved` | Object found and slim payload returned | n/a |
| `not_found` | Object does not exist (404 from owning component) | no |
| `unauthorized` | Caller's component identity rejected | no |
| `unavailable` | Owning component returned 5xx | yes |
| `timeout` | Owning component did not respond within configured timeout | yes |
| `unregistered` | URI's component is not in the registry | no |
| `malformed` | URI failed grammar parse | no |

The receiving component's `multi-fetch` implementation MUST return
per-URI status; it MUST NOT fail the entire request because one URI
is unresolvable.

### 6.3 Timeouts

Default timeout for outbound Loom calls is 5 seconds. Tunable per
peer via the registry entry. Long-tail timeouts (> 30s) are an error
condition; the calling component logs WARN at 5s and ERROR at 30s.

### 6.4 Caching

Components MAY cache slim payloads from other components with a TTL
defined per-kind. Default TTL is 60 seconds; ephemeral kinds
(observations, which expire in 14 days) get 30s; durable kinds
(release, milestone) get 5 minutes. Cache invalidation is
TTL-based only in v1 — no push-based invalidation.

Cached results carry an `as_of` timestamp; consumers can decide
whether the cached value is fresh enough for their use case.

---

## 7. Authorization

### 7.1 Mint authorization (who can create URIs)

Any component can mint URIs for its own objects. There is no
federation-level mint permission — the URI is just the object's
public identifier. Privacy and access control are receiving-side
concerns at resolution time.

### 7.2 Attach authorization (who can attach a URI to another component's object)

This is the principal authorization concern. Sibling doc
`2026-05-17-shuttle-design.md` Section 3.3 specifies:

- Each component's `POST /api/loom/<component>/<kind>/{id}/attach`
  endpoint enforces:
  - Caller identity verified via `X-Loom-Component` HMAC.
  - The caller's component matches the URI scheme of the attached URI
    (a Clarion-identified caller can attach `clarion://...` URIs but
    not `filigree://...` URIs).
  - The receiving component's policy on which attachment_kinds it
    accepts from which callers (Shuttle: accepts `clarion_review`
    from Clarion only; `filigree_issue` from Filigree only).
  - Rate limiting per component identity (default 100/min).

### 7.3 Resolve authorization (who can fetch a URI's content)

Receiving components MAY filter resolution results based on caller
identity. Filigree currently does not — Loom is internal-only and
trusted. Wardline (when it ships) introduces per-caller read gates
for sensitive content.

### 7.4 Impersonation prevention

The HMAC on `X-Loom-Component` prevents a misbehaving component from
posing as another. Token leakage is the residual risk; tokens are
treated as secrets and stored in the registry's
`identity_token_hint`-named environment variable, not the registry
file itself.

---

## 8. Failure modes and consumer obligations

Every Loom-consuming component MUST handle:

| Failure | Required behavior |
|---|---|
| Registry missing | Refuse to start. Federation cannot operate without registry. |
| Peer component down at startup | Start anyway; mark peer as unavailable; emit metric. |
| Peer component down at call time | Surface to caller as `unavailable` status; do NOT fail the caller's operation if it can degrade gracefully. |
| URI in storage points at deleted object | Surface as `not_found`; UI displays "(unresolvable)"; cleanup is operator-driven, not automatic. |
| URI in storage is malformed | Surface as `malformed`; log WARN with the URI string and source. |
| Authentication failure | Surface as `unauthorized`; log ERROR (potential token leak or misconfiguration). |
| Resolution exceeds timeout | Surface as `timeout`; retry with exponential backoff up to 3 attempts; then fail with retryable=true. |

### 8.1 Graceful degradation

The Shuttle design explicitly treats "Loom is degraded" as a normal
operating mode. `prepare_step` returns a partial brief with per-attachment
status; agents decide whether to proceed. This pattern is the
federation-recommended default. Components SHOULD NOT block their own
work on the availability of every peer.

---

## 9. Versioning

This is the v1 specification. Backward-incompatible changes require
v2. v1 → v2 migration would:

- Allow both URI grammars during a transition window.
- Provide a federation-wide migration tool that rewrites stored URIs
  in each component.
- Document v1 deprecation in the registry's `version` field.

v1 is expected to be stable for the lifetime of the current
federation; v2 is purely hypothetical and named here to clarify the
upgrade path.

---

## 10. Test strategy

### 10.1 Grammar tests

| Surface | Test |
|---|---|
| URI parser | round-trip for canonical examples; canonicalization (lowercase component/authority/kind); reject malformed inputs |
| URI builder | matches parser for every valid input |
| Reserved patterns | refuse minting URIs with `__` prefix (component should refuse internally) |

### 10.2 Registration tests

| Surface | Test |
|---|---|
| Registry load | parse registry.json; reject missing fields; reject duplicate `(name, base_url)` |
| Collision detection | component starts with a name claimed by another base_url → refuse |
| Token verification | HMAC roundtrip; mismatch refused |

### 10.3 Resolution tests

| Surface | Test |
|---|---|
| `multi-fetch` happy path | mixed URIs across components resolve correctly |
| `multi-fetch` partial failure | one peer down → that peer's URIs return `unavailable`; others return `resolved` |
| Timeout handling | peer hangs → request returns `timeout`; metric incremented |
| Retry behavior | retryable status retried up to 3 times with backoff |
| Cache TTL | cached entry returned within TTL; expired entry triggers re-fetch |

### 10.4 Contract tests

The fixtures published with this spec live at (to be created — the
directory does not yet exist; creation is a Batch 0 deliverable of
the Shuttle workstream alongside the loom-core extraction, owned by
the same team since loom-core is the natural fixture-publishing peer):

```
docs/federation/loom-uri/fixtures/
  ├── valid-uris.json         # canonical examples each component must parse identically
  ├── malformed-uris.json     # examples that must fail parse identically
  ├── multi-fetch-request.json
  ├── multi-fetch-response.json
  └── registry-example.json
```

#### 10.4.1 Fixture repository and ownership

- **Canonical repository:** the fixtures live in the filigree
  repository at `docs/federation/loom-uri/fixtures/` and are
  versioned alongside the URI spec doc. Filigree is chosen because it
  is the only federation peer that exists today and because
  `loom-core` (Shuttle Batch 0) will extract from filigree —
  co-locating fixtures keeps them in the same review surface.
- **Owner:** the filigree maintainers, with PR review from any
  consuming-component team for fixture additions.
- **Versioning:** fixtures are tagged with the spec version
  (`v1.0.0`, `v1.1.0`, …). Each tag is immutable; consumers pin to a
  tag.
- **Spec/fixture coupling:** every spec change that affects the
  parsing or wire-format surface of the contract MUST ship a fixture
  update in the same PR; a CI check in the filigree repo refuses
  spec edits without a corresponding fixtures bump.

#### 10.4.2 CI consumption mechanism

Consuming components pull fixtures via a **pinned commit SHA**, not a
moving branch. Two supported integration modes:

1. **Git submodule** at `tests/contract/loom-fixtures/` pointing at
   the filigree repo, pinned to the SHA matching the spec version
   the component implements. CI checks the submodule SHA matches the
   pinned value in `pyproject.toml`'s `[tool.loom-fixtures]`
   section.
2. **Sparse download** at install / CI start: a `scripts/fetch-loom-fixtures.sh`
   pulls just the fixtures directory via `git archive` at the pinned
   SHA. Used when the consumer does not want a submodule.

Either way the CI consumption is:

```
# pyproject.toml
[tool.loom-fixtures]
spec_version = "1.0.0"
pinned_sha = "abc123def456..."

# CI:
uv run pytest tests/contract/ --tb=short
```

The Shuttle design §8.5's `tests/contract/` gate references this
exact mechanism — no abstract "fixtures published" condition; the
gate is "submodule/sparse-checkout SHA matches `pinned_sha`, then run
contract tests."

Every federation component's test suite loads these fixtures and
verifies its parser/builder/resolver produces the documented behavior.
Adding a new component to the federation requires passing the fixture
suite.

---

## 11. Open questions

### 11.1 Should `authority` be globally unique or per-component scoped?

The spec currently scopes authority per-component (`filigree://elspeth`
and `shuttle://elspeth` are independent). The alternative is
federation-wide unique authority (one Elspeth project, registered once
in the registry, referenced by all peers).

**Recommendation:** per-component scope for v1. It's simpler and
matches how each component already names its data (each has its own
project prefix). Federation-wide authority is a v2 concern when there
are >2 federated peers in real use.

### 11.2 Should component identity tokens rotate?

v1 spec says static pre-shared. Rotation is v2.

**Recommendation:** static for v1. Until tokens leak in practice, the
operational cost of rotation (registry redistribution, runtime
re-load) exceeds the security gain.

### 11.3 Is the registry a federation-level resource (`loom://registry`)?

The reserved `loom://` scheme (Section 3.6) is allocated for this. v1
ships flat-file; v2 might serve the registry over the federation
itself.

**Recommendation:** keep `loom://` reserved but unused in v1. Flat
file is sufficient.

### 11.4 Cross-authority resolution (resolved in §5.1.1)

A `filigree://project-a/issue/abc` URI stored in a project-B Shuttle
plan: should it resolve?

**Decision (v1):** Resolution succeeds across authorities when both
projects' registries list compatible peer base_urls. Asymmetric views
are tolerated; the calling component's perspective wins (its registry
is authoritative for its own outbound calls). Consumers display the
authority in the UI for context. Registry divergence is detected via
the `X-Loom-Registry-Version` header (§5.1.2) and surfaced as a
metric.

If cross-project leakage proves to be a real concern in practice, v2
adds authority-scoped access policies.

---

## 12. Acceptance criteria

This spec is *ratified* when:

- All four reserved component names appear in `loom-registry.json` with
  base URLs (even if some are placeholder URLs for not-yet-shipped
  components).
- Filigree's existing `/api/loom/*` surface validates against the v1
  spec: URIs parse, `multi-fetch` works, identity tokens function. Any
  divergence is captured as filigree-side migration work, sequenced
  before Shuttle Batch 3.
- Fixture suite under `docs/federation/loom-uri/fixtures/` is
  populated.
- Filigree's contract tests pass against the fixtures.

Shuttle's Batch 3 work begins after ratification.
