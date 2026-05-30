"""Static dashboard activity-feed render-state checks.

Regression: fetchActivity returns null on a non-OK response and an array
(possibly empty) on success. The view conflated null (load failure) with an
empty list, both rendering "No recent activity." and hiding server problems.
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


def test_activity_render_state_distinguishes_failure_from_empty() -> None:
    script = textwrap.dedent(
        """
        const { activityRenderState } = await import(
          "./src/filigree/static/js/views/activity.js"
        );
        process.stdout.write(JSON.stringify({
          nullIsError: activityRenderState(null),
          undefinedIsError: activityRenderState(undefined),
          emptyIsEmpty: activityRenderState([]),
          listIsList: activityRenderState([{ id: 1 }]),
        }));
        """
    )
    result = _run_node(script)
    assert result["nullIsError"] == "error", result
    assert result["undefinedIsError"] == "error", result
    assert result["emptyIsEmpty"] == "empty", result
    assert result["listIsList"] == "list", result
