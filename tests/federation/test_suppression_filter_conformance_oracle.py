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
- **Layer 2 — drift recheck (release-gate, skip-clean).** Lives in
  ``test_sibling_drift.py`` (registry entry ``wardline_suppression_filter``):
  byte-compares the vendored copy against Wardline's authority source
  (``tests/conformance/filigree_suppression_filter_contract.json`` in the sibling
  checkout located by ``_oracle.sibling_source``). Skips cleanly when the sibling
  is absent unless ``FILIGREE_REQUIRE_WARDLINE_REPO`` arms it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from filigree.db_files import VALID_SUPPRESSION_FILTERS, WARDLINE_SUPPRESSION_STATES
from tests.federation._oracle import blob_sha, load_golden

pytestmark = pytest.mark.federation_contract

# The vendored consumer copy of Wardline's authoritative contract.
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "wardline-suppression-filter-contract.json"

# Git-blob sha1 of the vendored contract (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_vendored_contract_byte_pin`` so any edit to the
# fixture reds in the default suite, on every CI run.
UPSTREAM_BLOB_SHA = "7bcb6993553e920438fe3854a8a62409362accb9"


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_contract_byte_pin() -> None:
    """The vendored contract's bytes hash to the pinned git-blob sha.

    A single-byte edit to the fixture changes the sha and reds this test in the
    default suite — the cheapest possible drift tripwire, no sibling repo needed.
    """
    assert blob_sha(CONTRACT_PATH.read_bytes()) == UPSTREAM_BLOB_SHA


# ---------------------------------------------------------------------------
# Consumer conformance oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


def test_filigree_owned_vocab_matches_contract() -> None:
    """Filigree's consumed ``WARDLINE_SUPPRESSION_STATES`` (the real constant from
    ``filigree.db_files``) equals exactly the contract's ``suppression_states``.

    This ties the vocabulary Filigree's runtime carries to Wardline's owned set:
    if Filigree adds/drops/renames a suppression state, it diverges from the
    authoritative contract and reds here."""
    contract = load_golden(CONTRACT_PATH)
    assert set(contract["suppression_states"]) == WARDLINE_SUPPRESSION_STATES


def test_filigree_filter_grammar_is_vocab_plus_only_the_sentinel() -> None:
    """Filigree's REAL accepted filter grammar (``VALID_SUPPRESSION_FILTERS``,
    read from ``filigree.db_files``) is exactly the contract's suppression_states
    plus the one local sentinel — and nothing else.

    Filigree is the consumer: it may add ONLY the ``all`` no-filter sentinel; every
    other accepted value must come from Wardline's vocabulary. If Filigree's grammar
    grows a value the contract doesn't sanction (or the sentinel name drifts), this
    reds."""
    contract = load_golden(CONTRACT_PATH)
    expected = set(contract["suppression_states"]) | {contract["filigree_filter_sentinel"]}
    assert expected == VALID_SUPPRESSION_FILTERS


def test_contract_precomputed_filter_values_match_constructed_union() -> None:
    """The contract's own precomputed ``filigree_filter_values`` equals the union
    we construct — a guard against a typo in that convenience field."""
    contract = load_golden(CONTRACT_PATH)
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
    contract = load_golden(CONTRACT_PATH)
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
