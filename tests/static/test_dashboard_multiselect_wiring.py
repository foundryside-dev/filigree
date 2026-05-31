"""Static dashboard multi-select button wiring check.

Pins the source-level contract between the Select button markup and the
toggleMultiSelect() handler that switches its active/inactive styling by id.
Asserts wiring, not rendered behavior.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text()


def test_multiselect_button_has_id_the_handler_queries() -> None:
    """toggleMultiSelect() looks up #btnMultiSelect to switch active styling;
    the Select button must carry that id or the lookup returns null and the
    button never reflects multi-select state."""
    html = _read("src/filigree/static/dashboard.html")
    filters = _read("src/filigree/static/js/filters.js")
    assert 'getElementById("btnMultiSelect")' in filters
    # The id must sit on the toggleMultiSelect button specifically.
    assert 'id="btnMultiSelect" onclick="toggleMultiSelect()"' in html
