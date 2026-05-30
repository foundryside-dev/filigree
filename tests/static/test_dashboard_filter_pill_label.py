"""Static dashboard filter-pill label ordering check.

Regression for filigree-c92966fa4c: applyFilterState() called syncPillUI()
*before* restoring the #doneTimeBound dropdown value. syncPillUI() reads that
dropdown to render the "Done: Xd" pill label, so the label reflected the
previous/default window instead of the restored one.

This pins the ordering contract at the source level: within applyFilterState,
the doneTimeBound dropdown assignment must appear before the syncPillUI() call.
A DOM-stubbed behavior test is avoided because applyFilterState pulls in
render/persistence/router collaborators that would require brittle stubbing;
the defect is purely statement ordering, asserted directly here.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_done_time_bound_restored_before_pill_sync() -> None:
    lines = (REPO_ROOT / "src/filigree/static/js/filters.js").read_text().splitlines()

    start = next(i for i, ln in enumerate(lines) if ln.startswith("export function applyFilterState("))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("export function"))
    # Code lines only — skip comment lines so the comment mentioning syncPillUI()
    # doesn't shadow the actual call.
    body = [ln for ln in lines[start:end] if not ln.lstrip().startswith("//")]

    done_line = next(i for i, ln in enumerate(body) if "doneEl.value = normalized.doneTimeBound" in ln)
    sync_line = next(i for i, ln in enumerate(body) if "syncPillUI()" in ln)

    assert done_line < sync_line, (
        "applyFilterState must restore the #doneTimeBound dropdown before syncPillUI(), which reads it to render the Done pill label"
    )
