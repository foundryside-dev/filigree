# Investigation — filigree-bcbd4d66fd: multi-owner block contract

**Issue C:** `inject_instructions` has no foreign-owner concept; its malformed-marker
branch can delete a sibling tool's (wardline/legis) block in a shared
CLAUDE.md/AGENTS.md. Filed P2 (feature).

**Verdict:** Real, confirmed, and **under-prioritised** — there is a *live on-disk*
exposure today (lacuna), so this is **release-blocking for 3.0.0**, not a P2 feature.
The filigree-side fix is unilateral and was empirically verified to pass the full
suite. Legis carries the identical bug (track as a peer P0). Wardline is safe.

Method: 8-agent investigation workflow (evidence fan-out across filigree / wardline /
legis / weft-convention → design synthesis → 3 adversarial verification lenses, one of
which *applied the fix to the real tree and ran the tests*).

---

## 1. Confirmed root cause

`_inject_instructions_locked` (`src/filigree/install.py:267-297`) identifies its block
by a hard-coded substring prefix `FILIGREE_INSTRUCTIONS_MARKER = "<!-- filigree:instructions"`
(`install_support/__init__.py:7`) and end token `_END_MARKER = "<!-- /filigree:instructions -->"`
(`install.py:119`). **No foreign-owner concept exists.** Two branches delete content
filigree does not own:

1. **Malformed branch (the one the issue names), `install.py:285`:**
   `content = content[:start] + FILIGREE_INSTRUCTIONS` — when filigree's own end marker
   is absent, truncates from filigree's start marker through **EOF**. Any sibling block
   physically after an unclosed filigree block is matched by neither marker and is
   silently destroyed. (This truncate-to-EOF was a deliberate issue-A-era change to kill
   an orphan-tail bug — any fix must preserve that idempotency invariant,
   `test_malformed_block_repair_is_idempotent`.)

2. **Well-formed replace branch, `install.py:274-276` ("Shape 2"):**
   `content.find(_END_MARKER, start)` returns the *first* filigree close at-or-after
   `start`. If the first filigree start is unclosed, a foreign block sits next, and a
   *later* filigree block is closed, `find()` jumps to the later close and the splice
   eats the sandwiched foreign block.

**Reachable on every routine path:** `filigree install`; `doctor --fix`
(`_apply_doctor_fixes`, `admin.py:497/501`); and **automatically on SessionStart**
freshness repair (`hooks.py:178 → 200`) whenever the embedded marker hash is stale — so a
co-resident wardline block can be wiped with **no user action**. The `admin.py:451-452`
comment ("non-destructive: manages only its own marked block") is false in these branches.

### Reproduction (by inspection — do NOT run `filigree install` against a real co-resident file)
```
Before user preamble.
<!-- filigree:instructions:v2.9.0:deadbeef -->
filigree workflow body line A
<!-- wardline:instructions:v1:abcd1234 -->
body
<!-- /wardline:instructions -->
```
Filigree marker present → replace path; `find(_END_MARKER, start) == -1` (only wardline's
close follows) → malformed branch → output is `"Before user preamble.\n"` + fresh filigree
block. **The entire wardline block is gone.**

---

## 2. Live exposure (why this is release-blocking)

`~/lacuna/CLAUDE.md` **and** `~/lacuna/AGENTS.md` carry a real co-resident layout:
filigree block (lines 1–119) immediately followed by a wardline block (lines 121–123).
Today both are well-formed, so the replace path + SessionStart refresh preserve wardline.
The loss fires the moment filigree's block becomes unclosed (interrupted write, stale
format, manual edit), at which point `install.py:285` deletes lines 1–EOF including the
whole wardline block — and **filigree's own SessionStart auto-repair is the deleter.**

This also intersects the still-open **weft campaign `weft-eb3dee402f`**: a lacuna
0-byte-CLAUDE.md emptying that no member's code reproduces. This fix removes filigree's
*foreign-deletion* vector but does **not** by itself explain a full 0-byte emptying — keep
that root-cause gate live.

---

## 3. Owner survey

| Owner | Mechanism | Missing-end handling | Foreign-safe? |
|---|---|---|---|
| **filigree** | substring `index(start)` + `find(end, start)`, splice | **truncate-to-EOF** (`install.py:285`) | **NO** — both malformed + Shape-2 |
| **wardline** | namespaced non-greedy `_FENCE_RE` (`block.py:22-25`), canonicalise own dups | **append** (no match → append) | **YES** (reachable orderings; theoretical residual: a foreign block strictly *between two* wardline fences) |
| **legis** | substring `index`+`find`, splice (`install.py:178-216`) — **identical shape to filigree** | **truncate-to-EOF** (`install.py:208`) | **NO** — malformed + Shape-2; auto-fires on drift refresh (`hooks.py:67-69`). Currently *latent* (zero legis blocks on disk) |

De-facto convention (live in `weft/CLAUDE.md`, `weft/AGENTS.md`): namespaced HTML-comment
fences `<!-- <ns>:instructions:vN:hash --> … <!-- /<ns>:instructions -->`. The only
*normative* statement is `weft/conventions.md` **C-4** (idempotent + multi-owner +
never-empty + single-command-restore); "only-touch-your-namespace", "append-if-absent",
**bounded recovery**, and **canonicalise-own-duplicates** are unspecified. No member owns
the cross-tool contract; enforcement is per-member by weft doctrine §5/§6.

---

## 4. Recommended filigree fix (unilateral; verified, with refinements folded in)

Replace the two divergent branches with one **bounded scan**: filigree's writable region
runs from its start marker to the first of (a) its own close *if that close precedes any
foreign fence* → normal replace; (b) the next **foreign-namespace** fence → bounded
recovery; (c) EOF. Own-namespace fences are **absorbed** (never boundaries), so
duplicate/unclosed filigree blocks still collapse to one clean block (preserves the
orphan-tail invariant). The lock, symlink reject, `_atomic_write_text` + refuse-to-empty
guard, append, and create branches are untouched. **Monotonic safety property: in every
branch `bound_new ≤ bound_old`, so the fix can only *preserve* bytes the old code deleted,
never delete bytes it kept.**

```python
import re
# Case-INSENSITIVE namespace class (refinement 1).
_INSTR_FENCE_RE = re.compile(r"<!--\s*/?([A-Za-z0-9_-]+):instructions")

def _first_foreign_fence_pos(content: str, search_from: int) -> int:
    for m in _INSTR_FENCE_RE.finditer(content, search_from):
        if m.group(1).lower() != "filigree":   # own fences absorbed
            return m.start()
    return len(content)                          # EOF fallback

# marker-present case inside _inject_instructions_locked:
start   = content.index(FILIGREE_INSTRUCTIONS_MARKER)
fil_end = content.find(_END_MARKER, start)                       # -1 if no own close
foreign = _first_foreign_fence_pos(content, start + len(FILIGREE_INSTRUCTIONS_MARKER))
if fil_end != -1 and fil_end < foreign:
    bound = fil_end + len(_END_MARKER)                           # genuine own block → replace
else:
    bound = foreign                                             # malformed / own-close-past-foreign
tail = content[bound:]
# Refinement 3: re-insert a separating newline when we bounded at a foreign fence,
# so we never glue "...--><!-- wardline..." mid-line (sibling-detector independence).
sep = "\n" if (bound < len(content) and tail[:1] != "\n") else ""
content = content[:start] + FILIGREE_INSTRUCTIONS + sep + tail
_atomic_write_text(file_path, content)
```

### Refinements that MUST land with the fix (from adversarial verification)

1. **Case-insensitive namespace class** (`[A-Za-z0-9_-]+` + `.lower()`). Otherwise an
   uppercase-namespaced sibling (`<!-- Wardline:instructions -->`) is *not* recognised as a
   boundary and is truncated-to-EOF exactly as today — the same bug, latent. *(Verifier
   empirically deleted a `Wardline:instructions` block against the real shipped block.)*
2. **Guard that `instructions.md` (the filigree body) contains no `:instructions` fence
   token.** The scan runs from `start+len(marker)`, i.e. across filigree's own body. If the
   body ever mentions `<ns>:instructions` (a doc example, a cross-reference), `foreign`
   lands inside the body and misroutes the **common** well-formed path into bounded
   recovery → duplicated close marker / non-idempotent growth. Clean today (grep returns
   nothing); add a test pinning it so a future body edit can't regress it. *(This is the
   correct reconciliation: scanning "only after `fil_end`" would re-open the Shape-2 hole,
   so guard the body instead.)*
3. **Separating newline on bounded recovery** (in the snippet above). Avoids gluing
   filigree's close to a trailing foreign fence; removes a latent dependency on every
   sibling's detector being non-line-anchored (wardline verified safe; legis unverified).
   Cannot regress the named idempotency tests — their inputs have no foreign fence so
   `bound == EOF` and `sep == ""`.
4. **Surface the stale-duplicate split-brain.** A second filigree block *beyond* a foreign
   fence is intentionally left in place (foreign-safety > own-dedup) — but it is **stale,
   conflicting** instructions that never update, not a harmless dup. Emit a warning when a
   second filigree start marker is detected beyond `bound`, or document it normatively;
   do not ship silently.

### New tests required
- Foreign (wardline) block **survives** the named malformed repro (currently untested).
- Shape-2 sandwich: wardline block between unclosed-first + closed-later filigree survives.
- Uppercase-namespace sibling block survives (refinement 1).
- `instructions.md` contains no foreign `:instructions` fence (refinement 2).
- Bounded recovery is idempotent *with* the inserted separator (refinement 3).

### Verified to NOT regress
Verifier applied the exact pseudocode to `src/filigree/install.py` and ran:
`tests/install/test_install.py` = **216 passed**; doctor + admin + symlink + hooks =
**332 passed**. All four named invariant tests + issue-A empty-guard/lock tests pass.
All **90** interleavings of filigree×2 / wardline×2 / legis×2 converge to one block per
tool with the preamble intact. `_END_MARKER` can never collide with a wardline/legis close
(literal string compare). Foreign-safety holds **even against buggy-sibling inputs** (does
not depend on siblings being correct).

---

## 5. Cross-product coordination (do NOT defer — `feedback_no_self_deferring_cross_product_work`)

1. **Legis peer P0 (not deferred).** Legis has the *identical* confirmed bug
   (`legis/src/legis/install.py:202` Shape-2, `:208` truncate-to-EOF) and auto-fires on
   drift refresh (`hooks.py:67-69`). Currently latent (no legis blocks on disk) — that
   lowers priority, **not** whether it is tracked. File against legis with the same
   bounded-scan fix + harden its refresh path; release-blocking label, cross-linked to this
   issue. The filigree fix is genuinely unilateral and does not wait on it.

2. **weft C-4 scorecard is self-contradictory.** `weft/conventions.md:128` certifies
   filigree "conforms" and `:131` certifies legis "conforms" to the very multi-owner rule
   they violate (matrix `:194`). Promoting bounded-recovery to normative **must** downgrade
   both verdicts in the *same* change unit — don't certify the violators as compliant
   against the new rule. weft remains the authoritative doctrine/scorecard home (live
   commits today; doctrine §6 forbids shared infra, so per-member enforcement is correct) —
   the memory "stale relay" note applies to runtime/rename authority, not the scorecard.

3. **Promote the contract to normative** in `weft/conventions.md` C-4: add
   only-touch-your-namespace, **bounded recovery at foreign fences**, append-on-missing-end,
   canonicalise-own-duplicates, never-reorder-foreign, atomic-non-empty-write, plus the
   namespace charset/case rule (refinement 1). Soften the wardline "never selects a foreign
   block" line to "for all reachable orderings" (the between-two-wardline-fences residual).

---

## 6. Open questions / residuals (intentional)
- A duplicate filigree block *after* a foreign block can't be canonicalised without
  reaching across foreign content — left in place (foreign-safety > own-dedup), surfaced
  per refinement 4. Same for an orphan *close* marker stranded in the tail.
- A "who-emptied/who-truncated" cross-tool diagnostic (C-4's single-command restore) is
  unowned — out of scope here.
- CRLF: injected block stays LF even on CRLF files (pre-existing; not introduced).
- lacuna 0-byte emptying (`weft-eb3dee402f`) remains unexplained by any member's code —
  keep the gate live.

---
*Source: investigation workflow `wf_164ce359-ad0` (8 agents). Tracker note: filigree MCP
is on schema v26 vs project v27 (SCHEMA_MISMATCH), so issue C could not be annotated
in-tracker from this session.*
