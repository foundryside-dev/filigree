"""Static dashboard release-tree fetch-classification checks.

Regression: fetchReleaseTree returns null on a non-OK response (it does not
throw), so the toggle handler cached null and the catch never fired — a load
failure rendered "No tree data available." instead of an error/retry state.
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


def test_classify_release_tree_fetch_distinguishes_failure() -> None:
    script = textwrap.dedent(
        """
        const { classifyReleaseTreeFetch } = await import(
          "./src/filigree/static/js/views/releases.js"
        );
        process.stdout.write(JSON.stringify({
          nullIsError: classifyReleaseTreeFetch(null),
          undefinedIsError: classifyReleaseTreeFetch(undefined),
          treeIsOk: classifyReleaseTreeFetch({ children: [] }),
          emptyTreeIsOk: classifyReleaseTreeFetch({}),
        }));
        """
    )
    result = _run_node(script)
    assert result["nullIsError"] == "error", result
    assert result["undefinedIsError"] == "error", result
    assert result["treeIsOk"] == "ok", result
    assert result["emptyTreeIsOk"] == "ok", result
