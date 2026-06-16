"""Conformance: filigree's weft-reason vocabulary is a subset of the canonical 11.

Locks filigree (a federation member, PDR-0023) to the closed weft-reason
vocabulary defined by the suite source of truth:

    contracts/weft-reason-vocab.json   (in the *weft* hub repo, version 1)

Members stay independent repos with no shared runtime dep, so the canonical
closed set is pinned here as ``CANONICAL_REASON_CLASSES`` (a verbatim copy of
that contract's ``reason_classes`` keys). The contract's own ``$comment`` states
each member conforms by a per-member conformance TEST that asserts its reason
surface against that list — this is filigree's.

Two invariants are enforced, and this test FAILS if the member ever drifts:

1. SUBSET — every ``reason_class`` literal filigree actually emits (discovered by
   AST-walking the producer modules, so a newly-introduced non-canonical literal
   is caught the moment it lands) is one of the canonical 11.
2. CARRIER — the ``WeftReason`` carrier requires ``reason_class``, ``cause`` and
   ``fix`` on every (non-clean) carrier; and the clean path omits ``cause`` +
   ``fix`` (filigree models "clean" as *no carrier emitted* — ``weft_reasons``
   is empty, and ``weft_reasons`` is ``NotRequired`` / absent on the wire).
"""

from __future__ import annotations

import ast
from pathlib import Path

from filigree.types.files import WeftReason

# Verbatim copy of contracts/weft-reason-vocab.json -> reason_classes (version 1,
# weft hub repo). The closed set: every non-clean carrier maps to exactly one of
# these; "clean" is represented in filigree by the *absence* of a carrier.
CANONICAL_REASON_CLASSES: frozenset[str] = frozenset(
    {
        "clean",
        "disabled",
        "unresolved_input",
        "rejected",
        "dead_path",
        "unreachable",
        "misrouted",
        "error",
        "scheme_mismatch",
        "stale",
        "partial",
    }
)

# Carrier rule from the same contract: every NON-clean result carries these three
# fields; "clean" omits cause + fix.
CARRIER_REQUIRED_FIELDS: frozenset[str] = frozenset({"reason_class", "cause", "fix"})
CARRIER_CLEAN_OMITS: frozenset[str] = frozenset({"cause", "fix"})

_SRC_ROOT = Path(__file__).parents[2] / "src" / "filigree"

# The modules that PRODUCE weft-reason carriers (emit ``reason_class=<literal>``).
# Listed explicitly so the test stays a tight, intentional surface rather than a
# whole-tree grep; if a new producer module is added it should be added here.
_PRODUCER_MODULES: tuple[Path, ...] = (
    _SRC_ROOT / "sei_backfill.py",
    _SRC_ROOT / "db_files.py",
)


def _emitted_reason_class_literals() -> set[str]:
    """Every string literal assigned to a ``reason_class=`` keyword, by AST.

    Walks each producer module's syntax tree and collects the constant string
    operands of every ``reason_class=...`` keyword argument — including both
    branches of a ternary (e.g. ``reason_class="unreachable" if x else "stale"``).
    A non-literal (computed) value is recorded as the sentinel ``"<dynamic>"`` so
    the test fails loudly rather than silently passing on an un-introspectable
    producer.
    """
    found: set[str] = set()
    for path in _PRODUCER_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "reason_class":
                continue
            for value in _literal_strings(node.value):
                found.add(value)
    return found


def _literal_strings(node: ast.expr) -> list[str]:
    """Resolve an expression to its possible string-literal values.

    Handles plain ``ast.Constant`` strings and ``ast.IfExp`` (ternary) by
    recursing into both branches. ``None`` constants (the "clean / resolved"
    sentinel on ``SymbolResolution``) are ignored. Anything else yields the
    ``"<dynamic>"`` sentinel.
    """
    if isinstance(node, ast.Constant):
        if node.value is None:
            return []
        if isinstance(node.value, str):
            return [node.value]
        return ["<dynamic>"]
    if isinstance(node, ast.IfExp):
        return _literal_strings(node.body) + _literal_strings(node.orelse)
    return ["<dynamic>"]


def test_emitted_reason_classes_are_subset_of_canonical() -> None:
    emitted = _emitted_reason_class_literals()

    # Guard: AST introspection must actually find the known producers, otherwise
    # a refactor that moves emission elsewhere would make this test vacuously
    # pass. We know at minimum scheme_mismatch (db_files) is emitted.
    assert "scheme_mismatch" in emitted, (
        "AST introspection found no scheme_mismatch carrier — producers may have moved; update _PRODUCER_MODULES."
    )
    assert "<dynamic>" not in emitted, (
        "A reason_class is assigned a non-literal value; it can no longer be "
        "statically conformance-checked. Emit a literal from the canonical set."
    )

    drift = emitted - CANONICAL_REASON_CLASSES
    assert not drift, (
        f"filigree emits non-canonical reason_class value(s): {sorted(drift)}. "
        f"Allowed set (contracts/weft-reason-vocab.json): "
        f"{sorted(CANONICAL_REASON_CLASSES)}"
    )


def test_carrier_requires_reason_class_cause_and_fix() -> None:
    # The WeftReason carrier (filigree.types.files) must require exactly the three
    # carrier fields the contract mandates on a non-clean result.
    assert set(WeftReason.__annotations__) == CARRIER_REQUIRED_FIELDS
    # WeftReason is total (every key required) — the carrier cannot drop cause/fix.
    assert getattr(WeftReason, "__total__", True) is True
    assert CARRIER_CLEAN_OMITS <= CARRIER_REQUIRED_FIELDS


def test_clean_path_emits_no_carrier() -> None:
    """filigree models "clean" as the absence of a carrier, not a carrier with
    reason_class="clean". A clean ingest leaves ``weft_reasons`` empty (and the
    wire shape omits the key entirely via NotRequired), satisfying the contract's
    "clean omits cause + fix" by omitting the whole carrier. This pins that
    modelling choice so a future change can't start emitting a bare clean carrier
    that carries cause/fix.
    """
    # "clean" is a canonical reason_class, but filigree must NOT emit it as a
    # carrier literal (it would then be forced to carry cause+fix, violating the
    # clean-omits rule).
    assert "clean" not in _emitted_reason_class_literals()
