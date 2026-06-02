# Design: Opt-in bearer-token auth for the loom federation surface

**Date:** 2026-06-03
**Author:** Claude (opus-4.8), with John (project lead)
**Tracking issue:** `filigree-30cd35bcb9` (Reconcile inbound auth posture across Filigree↔Clarion — token sent, ignored)
**Related:** `filigree-81d3971467` (transport-bound *identity* — stays open), ADR-012 (actor-identity threat model), ADR-002 (API generations / federation posture)

## Problem

Clarion's Filigree HTTP client sends `Authorization: Bearer <FILIGREE_API_TOKEN>`
on its federation requests (scan-results ingest, `POST /api/loom/findings/clean-stale`),
but Filigree's HTTP API performs **no** inbound auth — it is loopback-only and
reads `actor` from the request body. Operators configure a token that does
nothing on the Filigree side. The contract is dishonest: one side believes it is
authenticating, the other discards the credential.

The project lead chose option (b) from the issue — **the loom routes actually
honour the bearer token** — over (a) document-and-ignore.

## Constraints

- **ADR-002 freezes the `loom` generation to wire-compatible changes only.**
  Making a token *mandatory* would break every consumer not yet sending one, so
  enforcement must be **opt-in**: active only when an operator configures a
  server-side token.
- **The bundled dashboard UI is a tokenless browser app** hitting `/api/*`.
  Scope must not break it.
- **`ErrorCode` is a closed enum** (ADR-002 §enum-closure). The solution must not
  introduce a new code value. `ErrorCode.PERMISSION` already maps 401/403
  (`dashboard.py:543-544`), so 401 responses reuse it.

## Decisions (settled with the project lead)

1. **Enforcement model — opt-in.** Auth is active only when `FILIGREE_API_TOKEN`
   is set (non-empty after strip). Unset → behaviour is exactly as today (the
   loopback boundary; zero overhead, middleware not even installed). Set → a
   loom-scoped request lacking a valid token returns 401.
2. **Scope — loom federation surface only.** Enforce on `/api/loom/*` and the
   living-surface federation aliases that route to loom (today: `POST
   /api/scan-results`). Classic (`/api/issue/…`, `/api/issues`, …), the root
   dashboard (`/`), and `/api/health` stay open. Clean human/machine split:
   humans use the dashboard over loopback; machines (Clarion/Wardline/Shuttle)
   use the loom generation and authenticate.
3. **Token source — env var `FILIGREE_API_TOKEN`.** The exact name operators
   already set and Clarion already sends. Single server-wide token (one
   federation trust domain). Compared constant-time via `hmac.compare_digest`.
   Nothing written to disk by Filigree.

## Architecture

A new isolated unit **`src/filigree/dashboard_auth.py`** holds all auth logic;
`dashboard.py` only wires it in `create_app`. This matches the existing
`BaseHTTPMiddleware` pattern (`ProjectMiddleware`, `IdleTrackingMiddleware`) and
keeps the already-902-line `dashboard.py` from absorbing a security concern
inline.

### Public surface of `dashboard_auth.py`

- `LIVING_FEDERATION_ALIASES: frozenset[str]` — the living-surface paths that
  route to loom and must be enforced. Initially `{"scan-results"}` (the trailing
  segment). A named constant so future aliases (per contracts.md Phase-C) are a
  one-line add.
- `is_loom_scoped_path(path: str) -> bool` — pure predicate. Returns True when,
  after stripping an optional `/api` root and optional `/p/{key}` server-mode
  segment, the remainder starts with `loom/` **or** equals one of
  `LIVING_FEDERATION_ALIASES`. Pure and unit-testable with no app.
- `build_auth_middleware(token: str)` — returns a `BaseHTTPMiddleware` subclass
  (or dispatch callable) closed over `token`. Only called when `token` is
  non-empty.

### Wiring in `create_app`

```
token = os.environ.get("FILIGREE_API_TOKEN", "").strip()
...
# after CORS middleware (CORS stays outermost so preflight is unaffected)
if token:
    app.add_middleware(build_auth_middleware(token))
```

The token is read **once at construction** and closed over — tokens do not
change at runtime, and this keeps the per-request path allocation-free. Tests
set the env var before calling `create_app`.

## Data flow (per request, when enforcement active)

1. `request.method == "OPTIONS"` → pass through (CORS preflight carries no auth).
2. `is_loom_scoped_path(request.url.path)` is False → pass through.
3. Else extract the bearer token from `Authorization`: header must be
   `Bearer <token>` (scheme case-insensitive, single space split). Missing
   header, wrong scheme, or `not hmac.compare_digest(provided, token)` →
   **401**.
4. Valid token → pass through to the route.

## Error handling

A rejected request returns the existing error envelope:

```json
{ "error": "Missing or invalid bearer token", "code": "PERMISSION" }
```

with `status_code=401` and a `WWW-Authenticate: Bearer` response header. No new
`ErrorCode` member (PERMISSION already pairs with 401 in `dashboard.py`'s
status→code map), so generation enum-closure and wire-compat are preserved.

## Explicit non-goals (YAGNI; keep the ADR-012 boundary clean)

- **No token→actor binding.** This gates *access* only; `actor` is still read
  from the request body. Binding a verified identity into the actor field is the
  separate verified-actor work in `filigree-81d3971467` and stays deferred.
- **No classic / MCP / CLI auth.** Out of scope by the transport boundary
  (MCP = stdio, CLI = shell, classic = human/loopback surface).
- **Single server-wide token.** Per-project tokens are future work, not part of
  this change.

## Documentation & ADR

- **New ADR-018** — "Opt-in bearer-token auth for the loom federation surface."
  Records the decision and that it **partially lifts** ADR-012 §5's deferral:
  the access-gate half lands now; verified *identity* (token→actor) stays
  deferred.
- **ADR-012** — add a cross-reference noting the partial lift and pointing at
  ADR-018; the existing cross-host trigger bullet (added 2026-06-03) stays.
- **`docs/federation/contracts.md`** — a short "Authentication" subsection:
  the opt-in env var, the loom-only scope, the 401 envelope, and that the
  default (unset) behaviour is unchanged.

## Testing (`tests/api/test_loom_auth.py`, FastAPI `TestClient`)

Back-compat guarantee (load-bearing):
- token **unset** → a loom route (`GET /api/loom/issues`) works with no auth.

Enforcement (token set):
- correct `Authorization: Bearer <token>` → 200.
- wrong token → 401 `{code: PERMISSION}` + `WWW-Authenticate: Bearer`.
- absent header → 401.
- malformed header (`Authorization: <token>` without `Bearer`) → 401.

Scope boundary (token set):
- classic route `GET /api/issue/{id}` → works without token.
- `GET /api/health` and `GET /` → open.
- living alias `POST /api/scan-results` → enforced.
- server-mode `/api/p/{key}/loom/…` → enforced.
- `OPTIONS` preflight on a loom path → not blocked.

Unit (`is_loom_scoped_path`):
- True: `/api/loom/issues`, `/api/p/acme/loom/issues`, `/api/scan-results`,
  `/api/p/acme/scan-results`.
- False: `/api/issue/x`, `/api/issues`, `/api/health`, `/`, `/api/v1/scan-results`
  (classic outlier — NOT loom-scoped).

## Files

| File | Change |
|------|--------|
| `src/filigree/dashboard_auth.py` | **new** — predicate, alias set, middleware factory |
| `src/filigree/dashboard.py` | wire token read + conditional middleware in `create_app` |
| `docs/architecture/decisions/ADR-018-loom-bearer-token-auth.md` | **new** |
| `docs/architecture/decisions/ADR-012-actor-identity-threat-model.md` | cross-ref to ADR-018 |
| `docs/federation/contracts.md` | "Authentication" subsection |
| `tests/api/test_loom_auth.py` | **new** — full battery above |

## Issue outcome

- `filigree-30cd35bcb9` → **closed** on landing (option (b) implemented).
- `filigree-81d3971467` → stays open (verified-actor / identity half).

## Out-of-scope risks noted

- `/api/v1/scan-results` (classic outlier) is **not** enforced — a federation
  producer posting to the classic path bypasses auth. Acceptable: contracts
  direct federation consumers to the loom/living path, and classic is the frozen
  human-compat surface. Documented in ADR-018 so it is a known boundary, not a
  silent gap.
