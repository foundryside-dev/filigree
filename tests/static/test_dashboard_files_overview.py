"""Static dashboard files-overview behavior checks."""

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


def test_files_overview_is_recreated_when_project_key_changes() -> None:
    script = textwrap.dedent(
        """
        globalThis.window = {};
        const { shouldRecreateFilesOverview } = await import("./src/filigree/static/js/views/files.js");

        const payload = {
          sameProject: shouldRecreateFilesOverview({ dataset: { projectKey: "alpha" } }, "alpha"),
          newProject: shouldRecreateFilesOverview({ dataset: { projectKey: "alpha" } }, "bravo"),
          missingMarker: shouldRecreateFilesOverview({ dataset: {} }, "alpha"),
          missingOverview: shouldRecreateFilesOverview(null, "alpha"),
        };
        console.log(JSON.stringify(payload));
        """
    )

    result = _run_node(script)

    assert result == {
        "sameProject": False,
        "newProject": True,
        "missingMarker": True,
        "missingOverview": False,
    }
