# Execution prompt — Clarion→Loomweave / Loom→Weft rebrand

Hand this to a fresh agent (or paste as the opening prompt) to execute the
rebrand. It is self-contained except for the two reference docs it points at.

---

## PROMPT (copy from here)

You are executing a **federation rebrand** of the Filigree codebase, on branch
`release/3.0.0`. Two independent rename axes:

- **Clarion → Loomweave** (the registry/SEI sibling product)
- **Loom → Weft** (the federation; inside Filigree also the named API
  generation `/api/loom/*` + the `generations/loom/` module + the `loom://`
  URI scheme)

**Decision (fixed — do not relitigate):** this is a **hard wire-break** riding
the 3.0.0 train. **No compatibility aliases** for the new contracts. It
co-ships with the MCP-namespacing breaking change so consumers cut over once.

### Read first (do not skip)
1. `docs/plans/2026-06-05-clarion-loomweave-loom-weft-rebrand-inventory.md` —
   the full rub-point inventory: tiers, ownership tags, exact file:line fix
   targets, the **confirmed name table**, and coordination items. This is your
   spec.
2. Epic `filigree-1d08ffb493` and its subtasks (G0, T2A, T2B, T0, T1, T0b, T3,
   Parked) — each subtask is one unit of work with acceptance criteria. Run
   `filigree show <id>` per subtask. Claim with
   `filigree start-work <id> --assignee <you>`; close with `--reason`.

### Names — CONFIRMED vs PENDING (read the inventory's names table for status)
Use **only the CONFIRMED names** in code. The rest are proposals G0 must lock —
**do not flip a PENDING wire/data value on a guess** (that is what G0 is for; a
wrong-but-confident contract name is worse than a blank).

- **CONFIRMED (safe to apply):** `Clarion`→`Loomweave` · `Loom`→`Weft` ·
  `/api/loom/*`→`/api/weft/*` (gen token `"loom"`→`"weft"`) ·
  `clarion_entity_id`→`loomweave_entity_id` · `CLA-`→`LMWV-`.
- **PENDING G0 (do NOT apply until G0 closes):** `CLARION_LOOM_TOKEN`→? ·
  audience `"loom"`→? · error codes `CLARION_*`→? · `clarion:eid:`→? ·
  `registry_backend "clarion"`/`[clarion]`→? · `loom://`→?. The inventory lists
  this author's *proposed* targets, but they are unconfirmed.
- **BLOCKED:** Legis's new name is unknown — subtask "Parked" stays blocked
  until the hub publishes it; do not guess.

Note the CONFIRMED set is exactly enough to fully execute **T2A and T2B** (the
product-name and module renames) without touching any pending contract value.

### Hard guardrails
- 🚫 **Never blind-`sed` "loom".** It is a substring of `bloom`/`gloom` and
  appears in prose. Axis B is done **by identifier** (rename symbols, move the
  module, fix imports) — use LSP/grep-then-edit, not text replace. `clarion`
  is safe to bulk-replace; `loom` is not.
- 🚫 **Never create or switch branches.** Work on `release/3.0.0` as-is. (User
  rule: no branch changes without explicit approval; no worktrees.)
- 🔴 **G0 gates the wire + data tiers.** T0, T1, T0b, and Parked are blocked by
  G0 (the hub locking names + the Legis re-sign protocol). `filigree ready`
  will show them as not-startable until G0 closes. **Do not start a blocked
  tier** — `filigree blocked` to confirm.
- 🤝 **Lockstep items are JOINT/SIBLING** (see ownership tags). The emitted
  entity key, SEI prefix, `CLA-` rule prefix, audience claim, and env var must
  match what Loomweave/the hub emit. If the sibling side isn't confirmed for an
  item, leave it and comment on the subtask — don't unilaterally flip a
  contract value.

### Execution order (honor the dependency graph)
1. **Startable now (Filigree-owned, no gate):**
   - **T2A** `filigree-0d403dc684` — Clarion→Loomweave internal code (registry/SEI
     symbols, constants, attrs). Leave wire/data *values* (`CLARION_LOOM_TOKEN`,
     `clarion:eid:`, error-code strings) untouched — those are T1/T0.
   - **T2B** `filigree-cda5448d48` — move `generations/loom/`→`generations/weft/`,
     rename `*Loom` DTOs, `*_to_loom` adapters, `create_loom_router`. **By
     identifier.** WIRE-visible bits (route prefix, gen token) flip in T1.
   These two are independent — can run in parallel by different agents.
2. **After G0 closes:**
   - **T0** `filigree-e0896844cd` — data migrations (column→`loomweave_entity_id`,
     `registry_backend` literal, `[clarion]` section, SEI prefix, finding
     `rule_id` `CLA-`→`LMWV-`). Each needs a schema migration + back-compat read
     during rewrite. **Gate the SEI-prefix/key rewrite on the Legis re-sign
     protocol (T0b).**
   - **T1** `filigree-648e6460d4` — wire flip (routes, gen token, audience, env
     var, error codes, remediation string, capability probe, URI scheme). Ship
     in the SAME 3.0.0 cut as MCP namespacing.
3. **T0b** `filigree-2cf022fff2` — coordinate the Legis HMAC re-sign over renamed
   `entity_id`s (sibling-driven; Filigree provides the re-import path). Stored
   signatures are stale-by-design until Legis re-cuts — expected, not corruption.
4. **T3** `filigree-44a56a8912` — docs, ADRs, CHANGELOG (`[3.0.0]` only; keep
   shipped history), CI, test fixtures. Last, for coherence.
5. **Parked** `filigree-58ccd105b7` — Legis surface rename. **Blocked on Legis's
   new name from the hub.**

### Verify before every close (no exceptions)
Run the full project CI gate and paste real output into the close `--reason`:
```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/filigree/
uv run pytest --tb=short
```
If you touched JS under `src/filigree/static/js/`, also run the biome gate:
`npx biome lint <file>` and `npx biome format <file>`.

Migrations (T0): write a forward migration with a bumped schema version, prove
round-trip on a copy of a real `.db`, and confirm reads of pre-migration rows
still work during the rewrite window. Do not ship a rename that breaks reads.

Wire (T1): grep the repo for the OLD token after the change
(`git grep -n 'api/loom\|CLARION_LOOM_TOKEN\|clarion:eid:\|"loom"'`) and confirm
zero live-code hits remain (docs/CHANGELOG history excepted).

### When blocked or uncertain on a contract value
Comment on the subtask with what you need from the sibling/hub, add the
dependency, and move to another startable item. Do not invent a name or flip a
shared value on a guess. Surface genuinely ambiguous design calls (e.g. whether
the entity key should be opaque `entity_id` vs branded `loomweave_entity_id`)
to the user rather than deciding silently.

## (end prompt)
