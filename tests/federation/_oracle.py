"""Shared helpers for the federation wire/contract oracles.

Every oracle under ``tests/federation/`` pins a vendored copy of a cross-repo
contract fixture in three layers:

1. **Layer 1 — byte-pin (default suite).** The module pins the git-blob sha of
   its vendored golden and recomputes it from bytes on every run (``blob_sha``).
2. **The non-circular core (default suite).** The module drives Filigree's REAL
   producer/consumer code path over the golden (``load_golden``).
3. **Layer 2 — sibling drift recheck.** The vendored copy must be byte-identical
   to the sibling repo's authority (or, when Filigree is the producer, the
   sibling's vendored copy must match ours). That check used to be copy-pasted
   into every oracle with a ``/home/john/<sibling>`` default; it now runs ONCE,
   registry-driven, in ``test_sibling_drift.py`` over ``DRIFT_REGISTRY``.

Sibling checkouts are located by ``sibling_source``: an explicit per-sibling env
override (``LOOMWEAVE_REPO`` / ``WARDLINE_REPO`` / ``LEGIS_REPO``; the legacy
``CLARION_REPO`` alias still works for loomweave) wins outright, otherwise the
sibling is expected next to this checkout (``<parent-of-filigree>/<sibling>``).
Absent siblings SKIP the drift check unless the per-sibling arming env
``FILIGREE_REQUIRE_<SIBLING>_REPO`` is set (``arming_requested``): ``1`` /
``true`` / ``yes`` / ``on`` arm it, ``0`` / ``false`` / ``no`` / ``off`` / empty
do not, anything else raises. The per-PR CI jobs check out no sibling, so
they never arm it; the scheduled ``federation-drift`` lane in ci.yml checks
out all three siblings, points the ``<SIBLING>_REPO`` overrides at them and
arms all three, so the drift params execute for real there.

This module is deliberately pytest-fixture-free; ``require_sibling_source`` is
the only place it touches pytest (to centralise the skip-vs-fail decision).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

# ``tests/fixtures/contracts`` — the vendored cross-repo contract goldens.
CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"
# ``tests/federation/fixtures`` — the SEI conformance oracle fixture.
FEDERATION_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
# Sibling checkout: <home>/<sibling> next to <home>/filigree (parents[3] of
# tests/federation/_oracle.py is the directory holding this checkout).
SIBLINGS_ROOT = Path(__file__).resolve().parents[3]

# Per-sibling checkout override env names, highest priority first. ``CLARION_REPO``
# is the SEI oracle's documented legacy override for the (then-Clarion) loomweave
# sibling and is kept as a lower-priority alias.
SIBLING_REPO_ENV: dict[str, tuple[str, ...]] = {
    "loomweave": ("LOOMWEAVE_REPO", "CLARION_REPO"),
    "wardline": ("WARDLINE_REPO",),
    "legis": ("LEGIS_REPO",),
}

_ARM_TRUE = frozenset({"1", "true", "yes", "on"})
_ARM_FALSE = frozenset({"", "0", "false", "no", "off"})


def blob_sha(data: bytes) -> str:
    """git's blob object id for ``data``: ``sha1(b"blob <len>\\0" + data)``.

    ``usedforsecurity=False`` is honest — this is content addressing (git's own
    object-id scheme), not a security primitive — and keeps ruff's S324 quiet
    without a per-line suppression.
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


@functools.cache
def _golden_bytes(path: Path) -> bytes:
    """One disk read per golden per session. Caches BYTES only — the parsed
    object is rebuilt per call so in-place mutation by an ingest path (e.g.
    ``process_scan_results`` normalising finding dicts) never leaks between
    tests."""
    return path.read_bytes()


def load_golden(path: Path) -> dict[str, Any]:
    """Parse the golden at ``path`` into a FRESH ``dict`` (bytes are cached)."""
    golden: dict[str, Any] = json.loads(_golden_bytes(path))
    return golden


def sibling_source(sibling: str, relative: str, *, siblings_root: Path = SIBLINGS_ROOT) -> Path | None:
    """Locate ``relative`` inside the ``sibling`` checkout, or ``None`` if absent.

    The first set, non-empty env in ``SIBLING_REPO_ENV[sibling]`` names the
    checkout outright (no fallthrough — a wrong override must surface as an
    absent source, not be papered over by a different checkout). Otherwise the
    sibling is expected at ``siblings_root / sibling``.

    Raises ``KeyError`` for an unknown sibling name.
    """
    env_names = SIBLING_REPO_ENV[sibling]
    repo: Path | None = None
    for name in env_names:
        value = os.environ.get(name, "")
        if value:
            repo = Path(value)
            break
    if repo is None:
        repo = siblings_root / sibling
    source = repo / relative
    return source if source.is_file() else None


def arming_env_name(sibling: str) -> str:
    """``FILIGREE_REQUIRE_<SIBLING>_REPO`` — the per-sibling arming env."""
    if sibling not in SIBLING_REPO_ENV:
        raise KeyError(sibling)
    return f"FILIGREE_REQUIRE_{sibling.upper()}_REPO"


def arming_requested(sibling: str) -> bool:
    """The one documented parse of the arming env.

    ``1`` / ``true`` / ``yes`` / ``on`` (any case, surrounding whitespace
    ignored) arm the drift check so an absent sibling is a hard failure;
    unset / empty / ``0`` / ``false`` / ``no`` / ``off`` leave it skip-clean.
    Any other value raises ``ValueError`` so a typo cannot silently disarm.
    """
    name = arming_env_name(sibling)
    raw = os.environ.get(name, "")
    value = raw.strip().lower()
    if value in _ARM_TRUE:
        return True
    if value in _ARM_FALSE:
        return False
    raise ValueError(f"{name}={raw!r} is not a recognised arming value (use 1/true/yes/on or 0/false/no/off)")


@dataclass(frozen=True)
class DriftEntry:
    """One vendored-copy ↔ sibling-copy pair the Layer-2 drift test compares.

    ``direction`` says who the authority is:

    * ``upstream`` — the sibling PRODUCES the wire; our vendored copy must match
      its authority source (re-vendor on our side when it drifts).
    * ``downstream`` — Filigree PRODUCES the wire; the sibling's vendored copy
      must match our authority golden (re-sync on the sibling side).

    Either way the comparison is byte-for-byte; direction only changes which
    side the failure message tells you to fix.
    """

    name: str
    vendored: Path
    sibling: str
    relative: str
    direction: Literal["upstream", "downstream"]


DRIFT_REGISTRY: tuple[DriftEntry, ...] = (
    DriftEntry(
        "capabilities",
        CONTRACTS_DIR / "get-api-v1-capabilities.json",
        "loomweave",
        "docs/federation/fixtures/get-api-v1-capabilities.json",
        "upstream",
    ),
    DriftEntry(
        "loomweave_scan_results",
        CONTRACTS_DIR / "loomweave-scan-results-wire.golden.json",
        "loomweave",
        "docs/federation/fixtures/loomweave-scan-results-wire.golden.json",
        "upstream",
    ),
    DriftEntry(
        "sei_conformance_oracle",
        FEDERATION_FIXTURES_DIR / "sei-conformance-oracle.json",
        "loomweave",
        "docs/federation/fixtures/sei-conformance-oracle.json",
        "upstream",
    ),
    DriftEntry(
        "entity_associations",
        CONTRACTS_DIR / "entity-associations-response.json",
        "loomweave",
        "docs/federation/fixtures/filigree-entity-associations-response.json",
        "downstream",
    ),
    DriftEntry(
        "weft_issue_detail",
        CONTRACTS_DIR / "weft" / "issues-get.json",
        "loomweave",
        "docs/federation/fixtures/filigree-issues-get.json",
        "downstream",
    ),
    DriftEntry(
        "wardline_finding_identity",
        CONTRACTS_DIR / "wardline-finding-identity-wire.golden.json",
        "wardline",
        "tests/conformance/fixtures/wardline-finding-identity-wire.golden.json",
        "upstream",
    ),
    DriftEntry(
        "wardline_scan_results",
        CONTRACTS_DIR / "wardline-scan-results-wire.golden.json",
        "wardline",
        "tests/conformance/fixtures/wardline-scan-results-wire.golden.json",
        "upstream",
    ),
    DriftEntry(
        "wardline_suppression_filter",
        CONTRACTS_DIR / "wardline-suppression-filter-contract.json",
        "wardline",
        "tests/conformance/filigree_suppression_filter_contract.json",
        "upstream",
    ),
    DriftEntry(
        "legis_signoff_binding",
        CONTRACTS_DIR / "legis-signoff-binding-request.json",
        "legis",
        "tests/contract/weft/vectors/signoff_binding.v1.json",
        "upstream",
    ),
)


def require_sibling_source(entry: DriftEntry) -> Path:
    """Resolve ``entry``'s sibling-side file, or skip/fail per the arming env.

    Absent sibling + armed → ``pytest.fail`` (the release gate demanded the
    cross-repo recheck actually execute); absent + not armed → ``pytest.skip``.
    """
    source = sibling_source(entry.sibling, entry.relative)
    if source is not None:
        return source
    override = SIBLING_REPO_ENV[entry.sibling][0]
    if arming_requested(entry.sibling):
        pytest.fail(
            f"{arming_env_name(entry.sibling)} is armed but {entry.sibling}'s {entry.relative} was not found "
            f"(set {override} to the sibling checkout so the byte-drift check can run)"
        )
    pytest.skip(f"{entry.sibling} checkout not found (set {override} to enable the byte-drift check for {entry.name})")
