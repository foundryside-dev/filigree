"""Regression test for LEGACY_CODE_TO_ERRORCODE mapping (review finding #13).

The mapping dict in types/api.py is documentation for the Stage 2a rollout —
it records the 27-legacy-code collapse into the 11-member ErrorCode enum.
Nothing else in the codebase imports it, so it can drift silently if a
Stage 2a fix-up adds a new legacy code to one of the sweep modules but
forgets to add the mapping entry.

This test pins the dict by:
1. Asserting every value is an actual ErrorCode member.
2. Asserting every key is lowercase snake_case (the legacy wire shape), except
   the documented ADR-014 product-prefixed compatibility alias.
3. Asserting no legacy-style code strings remain in src/ outside the
   mapping dict itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from filigree.types.api import LEGACY_CODE_TO_ERRORCODE, ErrorCode

SRC_ROOT = Path(__file__).parents[2] / "src" / "filigree"
_DOCUMENTED_NON_SNAKE_LEGACY_CODES = {"FILIGREE_FILE_REGISTRY_DISPLACED"}


def test_all_values_are_real_errorcode_members() -> None:
    """Every mapping target must be a live ErrorCode member."""
    members = set(ErrorCode)
    for legacy, target in LEGACY_CODE_TO_ERRORCODE.items():
        assert target in members, f"LEGACY_CODE_TO_ERRORCODE[{legacy!r}] = {target!r} is not an ErrorCode member"


def test_all_keys_are_snake_case() -> None:
    """Legacy keys should match the pre-2.0 lowercase wire shape."""
    pattern = re.compile(r"^[a-z][a-z0-9_]*$")
    for legacy in LEGACY_CODE_TO_ERRORCODE:
        if legacy in _DOCUMENTED_NON_SNAKE_LEGACY_CODES:
            continue
        assert pattern.fullmatch(legacy), (
            f"Legacy key {legacy!r} is not snake_case — this dict documents the pre-2.0 wire shape, which was all lowercase."
        )


def test_no_duplicate_keys() -> None:
    """Dict literal syntax would silently drop duplicates; this asserts the
    invariant holds so future edits don't accidentally collapse two different
    legacy codes into one entry.
    """
    # A dict obviously can't have duplicate keys at runtime; this reads the
    # file source and counts the literal occurrences of each key.
    source = (SRC_ROOT / "types" / "api.py").read_text()
    # Grab lines that look like legacy-code mapping entries.
    entries = re.findall(r'^\s*"([a-z][a-z0-9_]*)":\s*ErrorCode\.[A-Z_]+', source, re.MULTILINE)
    assert len(entries) == len(set(entries)), (
        f"Duplicate legacy codes in LEGACY_CODE_TO_ERRORCODE source: {sorted({e for e in entries if entries.count(e) > 1})}"
    )


# Known legacy strings that should NOT appear as a hardcoded "code" value
# in src/ any more (they all live in LEGACY_CODE_TO_ERRORCODE now).
_LEGACY_CODES = set(LEGACY_CODE_TO_ERRORCODE.keys())

# Files and directories that are allowed to mention legacy codes.
# - types/api.py owns the mapping dict itself
_EXEMPT_FILES = {
    SRC_ROOT / "types" / "api.py",
}


def _iter_python_files() -> list[Path]:
    return [p for p in SRC_ROOT.rglob("*.py") if p not in _EXEMPT_FILES]


@pytest.mark.parametrize("legacy_code", sorted(_LEGACY_CODES))
def test_no_legacy_code_literals_in_source(legacy_code: str) -> None:
    """No file in src/ should return an error with a lowercase legacy code
    as a string literal — those all migrated to ErrorCode.<MEMBER>.
    """
    # Look for a quoted "code": "<legacy>" pattern — that's the exact
    # shape that the 2.0 sweep replaced. A bare mention of the word
    # (e.g. in a comment or an error message) is fine.
    pattern = re.compile(
        r'["\']code["\']\s*:\s*["\']' + re.escape(legacy_code) + r'["\']',
    )
    hits: list[str] = []
    for path in _iter_python_files():
        text = path.read_text()
        if pattern.search(text):
            hits.append(str(path.relative_to(SRC_ROOT)))
    assert not hits, (
        f'Legacy code {legacy_code!r} still appears as a literal "code" value in: '
        f"{hits}. Replace with the corresponding ErrorCode member "
        f"({LEGACY_CODE_TO_ERRORCODE[legacy_code].name})."
    )
