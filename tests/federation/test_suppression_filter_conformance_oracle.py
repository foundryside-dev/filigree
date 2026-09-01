"""Consumer-side suppression-filter conformance oracle.

Wardline OWNS the ``suppression_state`` vocabulary (``active`` / ``baselined`` /
``waived`` / ``judged``). Filigree CONSUMES it as its finding-list suppression
*filter* grammar and adds only the local ``all`` no-filter sentinel. The shared
contract is vendored byte-identical in both repos; this module is the missing
*consumer* half of "both peers load the shared corpus": it proves Filigree
genuinely loads, conforms to, and drift-checks Wardline's authoritative contract.

Three layers, mirroring the SEI oracle (``test_sei_conformance_oracle.py``):

- **Layer 1 — byte-pin (default suite).** ``UPSTREAM_BLOB_SHA`` is the git-blob
  sha of the vendored contract; an unmarked test recomputes it from bytes. Any
  edit to the vendored fixture reds this immediately, in every CI run.
- **Consumer conformance oracle (the non-circular core, default suite).** Reads
  Filigree's REAL suppression-filter grammar out of ``filigree.db_files`` —
  ``WARDLINE_SUPPRESSION_STATES`` and ``VALID_SUPPRESSION_FILTERS`` — and asserts
  it equals exactly the union of the contract's suppression_states and its single
  ``all`` sentinel. Then
  it PROBES the live validator (``FiligreeDB.list_findings_global``, the single
  gate every consumer surface — MCP ``finding_list``, CLI ``list-findings``, and
  the ``/api/weft/findings`` route — funnels through): every contract value is
  accepted, and an off-vocab value is rejected with ``ValueError``. If Filigree's
  grammar drifts from Wardline's vocab — a value added, dropped, or renamed — this
  reds. It reads the real grammar, not a restatement of the contract.
- **Layer 2 — drift recheck (release-gate, skip-clean).** Byte-compares the
  vendored copy against Wardline's authority source
  (``$WARDLINE_REPO/tests/conformance/filigree_suppression_filter_contract.json``,
  default ``/home/john/wardline``). Skips cleanly when the sibling repo is absent
  (e.g. CI); fails closed on any byte divergence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from filigree.core import FiligreeDB
from filigree.db_files import VALID_SUPPRESSION_FILTERS, WARDLINE_SUPPRESSION_STATES

# The vendored consumer copy of Wardline's authoritative contract.
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "wardline-suppression-filter-contract.json"

# Git-blob sha1 of the vendored contract (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_vendored_contract_byte_pin`` so any edit to the
# fixture reds in the default suite, on every CI run.
UPSTREAM_BLOB_SHA = "7bcb6993553e920438fe3854a8a62409362accb9"


def _blob_sha(data: bytes) -> str:
    """git's blob object id for ``data``: ``sha1(b"blob <len>\\0" + data)``.

    ``usedforsecurity=False`` is honest — this is content addressing (git's own
    object-id scheme), not a security primitive — and keeps ruff's S324 quiet
    without a per-line suppression.
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _load_contract() -> dict[str, Any]:
    contract: dict[str, Any] = json.loads(CONTRACT_PATH.read_text())
    return contract


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_contract_byte_pin() -> None:
    """The vendored contract's bytes hash to the pinned git-blob sha.

    A single-byte edit to the fixture changes the sha and reds this test in the
    default suite — the cheapest possible drift tripwire, no sibling repo needed.
    """
    assert _blob_sha(CONTRACT_PATH.read_bytes()) == UPSTREAM_BLOB_SHA


# ---------------------------------------------------------------------------
# Consumer conformance oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


def test_filigree_owned_vocab_matches_contract() -> None:
    """Filigree's consumed ``WARDLINE_SUPPRESSION_STATES`` (the real constant from
    ``filigree.db_files``) equals exactly the contract's ``suppression_states``.

    This ties the vocabulary Filigree's runtime carries to Wardline's owned set:
    if Filigree adds/drops/renames a suppression state, it diverges from the
    authoritative contract and reds here."""
    contract = _load_contract()
    assert set(contract["suppression_states"]) == WARDLINE_SUPPRESSION_STATES


def test_filigree_filter_grammar_is_vocab_plus_only_the_sentinel() -> None:
    """Filigree's REAL accepted filter grammar (``VALID_SUPPRESSION_FILTERS``,
    read from ``filigree.db_files``) is exactly the contract's suppression_states
    plus the one local sentinel — and nothing else.

    Filigree is the consumer: it may add ONLY the ``all`` no-filter sentinel; every
    other accepted value must come from Wardline's vocabulary. If Filigree's grammar
    grows a value the contract doesn't sanction (or the sentinel name drifts), this
    reds."""
    contract = _load_contract()
    expected = set(contract["suppression_states"]) | {contract["filigree_filter_sentinel"]}
    assert expected == VALID_SUPPRESSION_FILTERS


def test_contract_precomputed_filter_values_match_constructed_union() -> None:
    """The contract's own precomputed ``filigree_filter_values`` equals the union
    we construct — a guard against a typo in that convenience field."""
    contract = _load_contract()
    constructed = set(contract["suppression_states"]) | {contract["filigree_filter_sentinel"]}
    assert set(contract["filigree_filter_values"]) == constructed


def test_real_validator_accepts_contract_values_and_rejects_off_vocab(tmp_path: Path) -> None:
    """Probe Filigree's REAL runtime gate, not a restatement of the contract.

    ``FiligreeDB.list_findings_global`` is the single validation point every
    consumer surface (MCP ``finding_list``, CLI ``list-findings``, the
    ``/api/weft/findings`` route) funnels its ``suppression`` filter through. Every
    contract-sanctioned value must be accepted; an off-vocabulary value must raise
    ``ValueError`` (the dashboard route maps that to a 400 VALIDATION error).

    This is the "add a bogus accepted value / remove a real one and it reds" proof:
    if the validator stopped accepting a contract value, or started accepting the
    bogus one, this test reds — the runtime consumer is tied to the contract.
    """
    contract = _load_contract()
    accepted = set(contract["suppression_states"]) | {contract["filigree_filter_sentinel"]}

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        # Every contract value is accepted by the real validator (validation runs
        # before any SQL, so an empty initialized schema is enough to exercise it).
        for value in sorted(accepted):
            db.list_findings_global(suppression=value, limit=1)
        # An off-vocabulary value is rejected at the same gate.
        with pytest.raises(ValueError, match="Invalid suppression filter"):
            db.list_findings_global(suppression="definitely-not-a-state", limit=1)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Layer 2 — drift recheck against Wardline's authority source (skip-clean)
# ---------------------------------------------------------------------------


def _wardline_authority_source() -> Path | None:
    """Locate Wardline's canonical contract, if the sibling repo is present.

    Honors a ``WARDLINE_REPO`` override; defaults to ``/home/john/wardline``.
    """
    repo = Path(os.environ.get("WARDLINE_REPO", "/home/john/wardline"))
    source = repo / "tests" / "conformance" / "filigree_suppression_filter_contract.json"
    return source if source.exists() else None


def test_vendored_contract_matches_wardline_authority_source() -> None:
    """The vendored consumer copy must be BYTE-identical to Wardline's authority
    source. Fails closed on any divergence; skips cleanly when the sibling repo
    is absent (e.g. CI), where Layer 1 + the consumer oracle still gate the PR."""
    source = _wardline_authority_source()
    if source is None:
        pytest.skip("Wardline repo not found (set WARDLINE_REPO to enable the byte-drift check)")
    assert CONTRACT_PATH.read_bytes() == source.read_bytes(), (
        "Vendored wardline-suppression-filter-contract.json has drifted from Wardline's authority source; re-vendor it byte-identical."
    )
