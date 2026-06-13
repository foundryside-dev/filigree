"""Consumer half of the Wardline -> Filigree suppression-filter contract."""

from __future__ import annotations

import json
from pathlib import Path

from filigree.db_files import VALID_SUPPRESSION_FILTERS, WARDLINE_SUPPRESSION_STATES

VECTOR_PATH = Path(__file__).parents[1] / "fixtures" / "contracts" / "wardline-suppression-filter-contract.json"


def _vector() -> dict:
    return json.loads(VECTOR_PATH.read_text(encoding="utf-8"))


def test_filigree_suppression_filters_match_wardline_vector() -> None:
    vector = _vector()

    assert vector["contract"] == "weft/wardline-filigree-suppression-filter"
    assert set(vector["suppression_states"]) == WARDLINE_SUPPRESSION_STATES
    assert set(vector["filigree_filter_values"]) == VALID_SUPPRESSION_FILTERS


def test_all_is_the_only_filigree_local_filter_value() -> None:
    vector = _vector()

    assert vector["filigree_filter_sentinel"] == "all"
    assert {"all"} == VALID_SUPPRESSION_FILTERS - WARDLINE_SUPPRESSION_STATES
