# Proposal (filigree half): shared `weft.toml` key layout

**Status**: DRAFT — filigree authors; **to be merged at the weft hub with
loomweave's `C-9-shared-weft-toml-schema.md` ("009")** into one hub-blessed
schema. (weft-a2f4cf95c7, reassigned loomweave → filigree by the operator,
2026-06-10.)
**Author**: filigree
**Merges with**: `loomweave/docs/loomweave/proposals/C-9-shared-weft-toml-schema.md`
**Tracks**: weft `conventions.md` C-9(d), conflict-register §A-14, glossary §8.
**Reference readers (shipped)**: legis `src/legis/config.py` (`[legis].store_dir`);
loomweave `crates/loomweave-core/src/store.rs` (`sibling_url`, `[<member>].url`);
filigree `src/filigree/core.py` (`resolve_store_dir`, `[filigree].store_dir`).

---

## 0. Relationship to loomweave-009

This is **not a competing design.** loomweave-009 already pins the shared layer
well, and filigree **agrees with it** on the load-bearing decisions. This document
exists to (a) close 009's four open questions with filigree's vote — filigree is
the most-read endpoint (`[filigree].url` is consumed by wardline, loomweave *and*
legis), so it has the strongest stake — and (b) record the one thing 009 cannot
see from loomweave's seat: how filigree's **own config** relates to its
`[filigree]` table after the config-anchor cutover (filigree-4bf16e64b6).

**Provenance note (correcting a draft error):** loomweave-009 is a real, shipped
draft (`C-9-shared-weft-toml-schema.md`) *and* loomweave has shipped the reference
reader code (`store.rs::sibling_url`, `filigree_url.rs::resolve_filigree_url`).
The merge is **doc-009 ⊕ this doc**, ratified against that shipped code — not a
greenfield contract.

## 1. Agreements with 009 (carried verbatim, not re-opened)

- **§2.1 home** — a shared fact about member *X* lives **once** at the top-level
  `[X]` table, read by any member; cross-read allowlist is `url` (+ reserved
  `enabled`); no `[wardline.filigree]`-style duplication (§8 clash rule).
- **§2.2 precedence** — `flag › env WEFT_<X>_URL › weft.toml [X].url ›
  .weft/<X>/ephemeral.port › default`, with `weft.toml` (rung 3) **above** on-disk
  discovery (rung 4) for the operator-declared remote-host case.
- **§2.3 invariants** — malformed = absent (NORMATIVE, never hard-fail);
  operator is sole writer; one fact one home; forward-compatible parse (unknown
  tables/keys ignored).

## 2. filigree's answers to 009 §5 (the four open questions)

| # | 009's question | filigree's vote |
|---|---|---|
| 1 | Member-table home (§2.1) vs a `[federation]` table (§4-B)? | **§2.1 member-table.** It keeps "everything about X" in one place and generalises to `enabled` without a parallel table. (Same as 009's recommendation.) |
| 2 | Confirm `weft.toml [X].url` (rung 3) **above** on-disk discovery (rung 4)? | **Confirm 3 > 4.** loomweave already **shipped** this (`filigree_url.rs`, in-code "Outranks on-disk discovery by design"); the hub is ratifying shipped behaviour, not choosing fresh. Local-only federations declare no `url` and are unaffected. |
| 3 | Pin `enabled` in v1, or `url`-only? | **`url`-only is live; `enabled` is RESERVED** (readers MUST tolerate its presence/absence but bake **no** semantics until the hub pins "absent = enabled?" and "advisory vs gating"). |
| 4 | Standardize `WEFT_<MEMBER>_URL` env spelling? | **Yes — `WEFT_<MEMBER>_URL`** (e.g. `WEFT_FILIGREE_URL`), matching loomweave's shipped `SOURCE_ENV`. Baking a *different* spelling would be the real compat break. Distinct from the inbound `WEFT_FEDERATION_TOKEN` and the registry `WEFT_TOKEN` (same `WEFT_*` family, different nouns — no collision). |

## 3. The filigree-specific config split (what 009 can't see)

filigree is the only member whose **identity** (project name, prefix, mode, db
location, registry backend, loomweave block) is non-trivial. After the
config-anchor cutover (filigree-4bf16e64b6):

- **Identity lives in `.weft/filigree/config.json`** — filigree's sole-writer
  subtree. It is NOT in `weft.toml` (the C-9c deletion test forbids putting
  identity where `rm weft.toml` would brick the member). The project **anchor**
  is the *presence of `.weft/filigree/`*, never `weft.toml`.
- **`weft.toml [filigree]` carries only operator overlays filigree READS:**
  `store_dir` (member-private relocation) and the shared `url` (everyone reads).

```toml
# weft.toml — operator-authored, project root, NEVER written by filigree
[filigree]
store_dir = ".weft/filigree"          # member-PRIVATE: read only by filigree (relocation)
url       = "http://127.0.0.1:8377"   # SHARED: filigree's endpoint, read by siblings
# enabled = true                       # SHARED (RESERVED — tolerate, don't act on)

[loomweave]
url = "http://127.0.0.1:9000"
[legis]
store_dir = ".weft/legis"
url       = "http://127.0.0.1:9100"
[wardline]
url = "http://127.0.0.1:9200"
```

**Three disjoint precedence ladders — do NOT collapse** (filigree's reader proves
they never overlap):

1. **Identity** (name/prefix/mode/db/registry/loomweave): `config.json` >
   built-in defaults. `weft.toml` does **not** override identity. No env tier.
2. **Store location**: `weft.toml [filigree].store_dir` (project-relative,
   under-root only) > `.weft/filigree/` > legacy `.filigree/` > default. The
   ONLY thing `weft.toml` overrides for filigree — and it is *relocation*, not
   identity.
3. **Shared sibling keys** (`[<member>].url`, reserved `enabled`): the 009 5-rung
   ladder above.

## 4. filigree's scope this pass (honest about what is and isn't shipped)

- **filigree READS** `[filigree].store_dir` today (`resolve_store_dir`).
- **filigree PUBLISHES** its own endpoint at `.weft/filigree/ephemeral.port`, so
  siblings can already discover it (C-9e).
- **filigree does NOT yet ship a sibling-`url` cross-reader.** The schema
  *reserves* that surface; filigree implements the cross-read only **after the
  hub pins** the rung order + env spelling (C-9d: "no member bakes until pinned").
  This proposal does the pinning *proposal*; it is not itself the pin.

## 5. Reader contract (language-agnostic; byte-compatible with legis + loomweave)

1. Resolve `weft.toml` at `project_root / "weft.toml"` — **no independent
   walk-up**; binary open; standards-compliant TOML parser.
2. Absent → all keys unset, defaults. Never an error.
3. Malformed (syntax / non-UTF-8 / OS read error) → **treat as ABSENT** (C-9c);
   warn at most; **never hard-fail**.
4. Missing `[<member>]` table / key → unset → next rung / default.
5. Wrong-type / empty value → ignore (warn), fall through.
6. Unknown keys/tables → silently ignored (forward-compat).
7. **Strictly read-only** — never write/create/rewrite `weft.toml`.

**One member-local exception**: a *mutating* install/init/migrate path MAY use a
strict variant that distinguishes absent (proceed) from malformed (refuse) —
because silently treating broken-as-absent there can relocate an operator-pinned
store. Confine strictness to write paths; all discovery/runtime stays
malformed=absent. (filigree `_load_weft_filigree_table` raises; consumed only by
`filigree init`.)

## 6. For the hub to decide at merge/bless

1. Adopt §2.1 member-table home (filigree + loomweave both recommend).
2. Ratify rung 3 > rung 4 (shipped by loomweave).
3. `enabled`: reserved-only in v1 (filigree's vote), or pin semantics now?
4. Standardize `WEFT_<MEMBER>_URL` (filigree + loomweave shipped agreement).
5. Confirm the §3 identity/overlay split is the canonical statement of how a
   member's authoritative config relates to its `weft.toml [<member>]` table
   (filigree is the worked example; legis already conforms with `store_dir`).
