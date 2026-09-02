"""Layer 2 — registry-driven sibling drift recheck for every vendored contract.

Each federation oracle pins its vendored golden by git-blob sha (Layer 1, runs
everywhere) and drives Filigree's real code over it (the non-circular core).
This module is the third layer for ALL of them at once: every entry of
``DRIFT_REGISTRY`` (``tests/federation/_oracle.py``) names a vendored copy, the
sibling repo that holds the other copy, and which side is the authority. The
two must be BYTE-identical.

Skip-clean by default: neither CI job checks out a sibling repo, so the
parametrized test skips there and Layer 1 + the per-oracle core still gate the
PR. On a machine with the siblings next to this checkout (or pointed at by
``LOOMWEAVE_REPO`` / ``WARDLINE_REPO`` / ``LEGIS_REPO``) it runs for real, and
``FILIGREE_REQUIRE_<SIBLING>_REPO=1`` turns an absent sibling into a hard
failure so a release-gate run can never silently skip the cross-repo recheck.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.federation._oracle import (
    DRIFT_REGISTRY,
    DriftEntry,
    require_sibling_source,
)

pytestmark = pytest.mark.federation_contract


def _drift_message(entry: DriftEntry) -> str:
    if entry.direction == "upstream":
        return (
            f"Vendored {entry.name} ({entry.vendored.name}) has drifted from {entry.sibling}'s authority source "
            f"({entry.relative}); re-vendor it byte-identical ({entry.sibling} is the producer)."
        )
    return (
        f"{entry.sibling}'s vendored copy of {entry.name} ({entry.relative}) has drifted from Filigree's authority "
        f"golden ({entry.vendored.name}); re-sync it on the {entry.sibling} side (Filigree is the producer)."
    )


@pytest.mark.parametrize("entry", DRIFT_REGISTRY, ids=[e.name for e in DRIFT_REGISTRY])
def test_vendored_copy_matches_sibling(entry: DriftEntry) -> None:
    """The vendored copy and the sibling's copy are BYTE-identical (whichever
    side is the authority). Skips when the sibling checkout is absent unless
    ``FILIGREE_REQUIRE_<SIBLING>_REPO`` arms it."""
    source = require_sibling_source(entry)
    assert entry.vendored.read_bytes() == source.read_bytes(), _drift_message(entry)


# ---------------------------------------------------------------------------
# Behavioural pins for the skip / fail / drift branches (hermetic via tmp_path)
# ---------------------------------------------------------------------------

_WARDLINE_ENTRY = next(e for e in DRIFT_REGISTRY if e.sibling == "wardline")
_DOWNSTREAM_ENTRY = next(e for e in DRIFT_REGISTRY if e.direction == "downstream")


def test_armed_env_with_missing_sibling_fails_not_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Armed + absent sibling = hard failure naming the arming env."""
    monkeypatch.setenv("WARDLINE_REPO", str(tmp_path))  # a checkout with no fixtures
    monkeypatch.setenv("FILIGREE_REQUIRE_WARDLINE_REPO", "1")
    with pytest.raises(pytest.fail.Exception, match="FILIGREE_REQUIRE_WARDLINE_REPO is armed"):
        test_vendored_copy_matches_sibling(_WARDLINE_ENTRY)


def test_unarmed_env_with_missing_sibling_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARDLINE_REPO", str(tmp_path))
    monkeypatch.setenv("FILIGREE_REQUIRE_WARDLINE_REPO", "0")
    with pytest.raises(pytest.skip.Exception):
        test_vendored_copy_matches_sibling(_WARDLINE_ENTRY)


@pytest.mark.parametrize("entry", [_WARDLINE_ENTRY, _DOWNSTREAM_ENTRY], ids=["upstream", "downstream"])
def test_divergent_sibling_bytes_fail_with_drift_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: DriftEntry) -> None:
    """Present-but-divergent sibling bytes red with a direction-aware message."""
    sibling_copy = tmp_path / entry.relative
    sibling_copy.parent.mkdir(parents=True)
    sibling_copy.write_bytes(entry.vendored.read_bytes() + b"\n")
    monkeypatch.setenv(f"{entry.sibling.upper()}_REPO", str(tmp_path))
    with pytest.raises(AssertionError, match="drifted") as excinfo:
        test_vendored_copy_matches_sibling(entry)
    expected_side = "re-vendor it" if entry.direction == "upstream" else f"re-sync it on the {entry.sibling} side"
    assert expected_side in str(excinfo.value)


def test_identical_sibling_bytes_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sibling_copy = tmp_path / _WARDLINE_ENTRY.relative
    sibling_copy.parent.mkdir(parents=True)
    sibling_copy.write_bytes(_WARDLINE_ENTRY.vendored.read_bytes())
    monkeypatch.setenv("WARDLINE_REPO", str(tmp_path))
    test_vendored_copy_matches_sibling(_WARDLINE_ENTRY)
