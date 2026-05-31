"""Static dashboard kanban drag-target computation checks.

Regression for the null-transitions crash: fetchTransitions returns null on
HTTP/network failure, and the drag-start handler iterated that null directly
(`for (const t of transitions)`), throwing a TypeError and breaking drag
affordances. computeDragTargets must tolerate a null/absent list.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node(script: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        cwd=REPO_ROOT,
        text=True,
    )
    return json.loads(result.stdout)


def test_compute_drag_targets_tolerates_null() -> None:
    script = textwrap.dedent(
        """
        const { computeDragTargets } = await import(
          "./src/filigree/static/js/views/kanban.js"
        );

        // null (fetch failure) must not throw and yields empty target sets
        const nullCase = computeDragTargets(null);

        // a real list keeps only ready transitions
        const listCase = computeDragTargets([
          { ready: true, to: "done", category: "closed" },
          { ready: false, to: "blocked", category: "open" },
        ]);

        process.stdout.write(JSON.stringify({
          nullEmpty:
            nullCase.validStatuses.size === 0 &&
            nullCase.validCategories.size === 0,
          listStatuses: [...listCase.validStatuses],
          listCategories: [...listCase.validCategories],
        }));
        """
    )
    result = _run_node(script)
    assert result["nullEmpty"] is True, result
    assert result["listStatuses"] == ["done"], result
    assert result["listCategories"] == ["closed"], result
