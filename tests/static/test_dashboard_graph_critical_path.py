"""Static dashboard graph critical-path behavior checks."""

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


def test_critical_path_edge_ids_exclude_shortcut_edges() -> None:
    script = textwrap.dedent(
        """
        const { criticalPathEdgeIds } = await import("./src/filigree/static/js/views/graph.js");

        const path = [{ id: "x1" }, { id: "x2" }, { id: "x3" }, { id: "x4" }];
        const edgeIds = [...criticalPathEdgeIds(path)].sort();
        console.log(JSON.stringify({ edgeIds, hasShortcut: edgeIds.includes("e-x1-x4") }));
        """
    )

    result = _run_node(script)

    assert result == {
        "edgeIds": ["e-x1-x2", "e-x2-x3", "e-x3-x4"],
        "hasShortcut": False,
    }


def test_empty_graph_reset_destroys_stale_cytoscape_instance() -> None:
    script = textwrap.dedent(
        """
        const { clearGraphForNoIssues } = await import("./src/filigree/static/js/views/graph.js");

        let destroyed = 0;
        const graphState = { cy: { destroy: () => { destroyed += 1; } } };
        const container = { innerHTML: "<div>stale graph</div>" };
        clearGraphForNoIssues(container, graphState);

        console.log(JSON.stringify({
          destroyed,
          cyCleared: graphState.cy === null,
          blankState: container.innerHTML.includes("data-graph-blank"),
          oldGraphGone: !container.innerHTML.includes("stale graph"),
        }));
        """
    )

    result = _run_node(script)

    assert result == {
        "destroyed": 1,
        "cyCleared": True,
        "blankState": True,
        "oldGraphGone": True,
    }
