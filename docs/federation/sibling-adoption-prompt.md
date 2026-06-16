# Drop-in for wardline / loomweave / legis — filigree did the weft.toml config cutover

> Paste this into the other member repos. It's a heads-up + an adoption contract,
> not a fire drill. Most of it is "here's what converged on the filigree side; pick
> it up on your own cadence." One item is a handoff the operator blessed
> (loomweave — that's the shared-schema merge). Precise and pre-verified.

## What filigree just did (so you can reason about us)

1. **Migrated our own identity off `.filigree.conf` into `.weft/filigree/config.json`**
   — the subtree we're the sole writer of. `filigree init` imports a legacy conf
   into config.json (conf-wins on the fields that were authoritative) and **retires
   it** (`.filigree.conf` → `.filigree.conf.imported`, an audit breadcrumb),
   crash-convergently. Fresh installs are **born confless**. Our project anchor is
   now the **presence of `.weft/filigree/`** — not the conf, not `weft.toml`.
2. **We NEVER write `weft.toml`.** Not init, not install, not doctor. It is
   operator-authored, read-only for us (C-9b / the C-4 multi-writer truncation
   lesson). We read only `[filigree].store_dir` today, and `[<sibling>].url` later.
3. **We boot with no `weft.toml`** (the C-9c deletion test) and treat a malformed
   `weft.toml` as **absent** on every discovery/runtime path. The one strict spot
   is the mutating `init` path, which refuses a present-but-unreadable file rather
   than auto-migrating over a config that may pin an operator store_dir.

Delete our `.weft/filigree/` and you break nothing of yours; we treat yours the same.

## The reader contract to share (you mostly already ship it)

Language-agnostic, byte-compatible with what legis (`config.py`) and loomweave
(`store.rs`) already ship. Any new `weft.toml` read should mirror these 7 steps:

1. Resolve `weft.toml` at `project_root / "weft.toml"` — **no independent walk-up**.
2. **Absent** → keys unset, defaults. Never an error.
3. **Malformed** (syntax / non-UTF-8 / OS read error) → **treat as ABSENT** (C-9c).
   Warn at most; **never hard-fail** (that would split the federation on the file).
4. Missing `[<member>]` table / key → unset → next rung / default.
5. Wrong-type / empty value → ignore (warn), fall through.
6. Unknown keys/tables → silently ignored (forward-compat).
7. **Strictly read-only** — never write/create/rewrite `weft.toml`.

One member-local exception: a *mutating* install/init/migrate path MAY use a strict
variant (absent → proceed; malformed → refuse), because silently treating
broken-as-absent there can relocate an operator-pinned store. Keep strictness on
write paths only.

## Three disjoint precedence ladders — don't collapse them

- **Identity** (name/prefix/mode/registry/…): your own member config wins;
  `weft.toml` does **not** override identity. No env tier for store/identity.
- **Store location**: `weft.toml [<member>].store_dir` (project-relative, under-root)
  > your default. The only thing `weft.toml` overrides for the store.
- **Shared sibling keys** (`[<member>].url`, reserved `enabled`): the 5-rung ladder
  loomweave ships — `flag › env WEFT_<X>_URL › weft.toml [X].url › .weft/<X>/ephemeral.port › default`.

## Shared-key schema — loomweave, here's the reassignment (operator-blessed)

**loomweave:** the operator reassigned the lead of the shared `weft.toml` schema
proposal **`weft-a2f4cf95c7`** from you to filigree. This is **not** a land-grab and
**not** filigree shipping a federation contract solo:

- Your draft **`docs/loomweave/proposals/C-9-shared-weft-toml-schema.md` ("009") is
  real and is the basis** — filigree wrote its **half**
  (`filigree/docs/federation/weft-toml-schema.md`), which **agrees with 009** and
  closes its four §5 open questions. The operator will **merge 009 ⊕ filigree's half**
  at the weft hub into one blessed schema. Your shipped reader code
  (`store.rs::sibling_url`, `filigree_url.rs`) is the reference; we **ratify** your
  rung-order and `WEFT_<X>_URL` spelling, we don't override them.
- If the reassignment isn't what you expected, **say so** — that's a coordination
  signal, not a steamroll.

The agreed shape (one fact, one home — each member's own top-level table; no nested
`[a.b]`, no `[federation]` mega-table):

```toml
[filigree]
store_dir = ".weft/filigree"
url       = "http://127.0.0.1:8377"   # SHARED: read by siblings
# enabled = true                       # RESERVED — tolerate, don't act on yet

[loomweave]
url = "http://127.0.0.1:9000"
[legis]
store_dir = ".weft/legis"
url       = "http://127.0.0.1:9100"
[wardline]
url = "http://127.0.0.1:9200"
```

**Not yet shipped:** filigree does **not** bake a sibling-`url` reader this pass —
we reserve the surface and publish our own endpoint at `.weft/filigree/ephemeral.port`
(so you can already discover us). Nobody bakes the cross-read until the **hub pins**
the rung order + env spelling (C-9d).

## Who this asks what

- **wardline:** you're the real multi-sibling consumer (you already retired
  `[wardline.*].url` — thank you). When the merged schema is hub-pinned, your
  re-integration against `[<member>].url` is a live cross-member constraint we'll
  align on *before* you wire it — ping us.
- **loomweave:** the `weft-a2f4cf95c7` reassignment above. Your 009 + code are the
  reference; filigree drives the doc + hub pin.
- **legis:** you're the member-private-form reference (`[legis].store_dir`) and we
  copied your fail-soft semantics. Nothing required beyond knowing your shared home
  is `[legis].url` (operator-written) if/when you expose an endpoint.

That's it — the filigree half is a no-op for your stores. Questions → the filigree side.
