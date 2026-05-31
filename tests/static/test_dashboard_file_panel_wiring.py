"""Static dashboard file-detail panel wiring checks.

These pin source-level contracts for the shared side panel (issue + file
detail). They assert wiring, not rendered behavior — the live behavior is
verified on the running dashboard.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text()


def test_escape_closes_file_detail_panel() -> None:
    """The global Escape handler must close a file detail panel (selectedFile),
    not only an issue detail panel (selectedIssue)."""
    app = _read("src/filigree/static/js/app.js")
    # The Escape branch must dispatch on selectedFile -> closeFileDetail().
    assert "else if (state.selectedFile) closeFileDetail();" in app


def test_open_file_detail_clears_shared_issue_header() -> None:
    """openFileDetail must clear #detailHeader (written with issue markup by
    detail.js) so the file panel never shows a stale issue header."""
    files = _read("src/filigree/static/js/views/files.js")
    # Locate the openFileDetail body and confirm it clears the header.
    start = files.index("export async function openFileDetail(")
    end = files.index("function renderFileDetail(", start)
    body = files[start:end]
    assert 'getElementById("detailHeader")' in body
    assert 'header.innerHTML = ""' in body
