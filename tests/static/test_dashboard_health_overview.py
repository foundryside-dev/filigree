"""Static dashboard health-overview behavior checks."""

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


def test_health_overview_treats_partial_api_failure_as_unavailable() -> None:
    script = textwrap.dedent(
        """
        const { healthOverviewUnavailableReason } = await import("./src/filigree/static/js/views/health.js");

        const payload = {
          statsFailure: healthOverviewUnavailableReason({
            hotspots: [],
            fileData: { total: 2 },
            stats: null,
            scanRunData: { scan_runs: [] },
          }),
          filesFailure: healthOverviewUnavailableReason({
            hotspots: [],
            fileData: null,
            stats: { critical: 0, high: 0, medium: 0, low: 0, info: 0, files_with_findings: 0 },
            scanRunData: { scan_runs: [] },
          }),
          complete: healthOverviewUnavailableReason({
            hotspots: [],
            fileData: { total: 2 },
            stats: { critical: 0, high: 0, medium: 0, low: 0, info: 0, files_with_findings: 0 },
            scanRunData: { scan_runs: [] },
          }),
        };
        console.log(JSON.stringify(payload));
        """
    )

    result = _run_node(script)

    assert result == {
        "statsFailure": "Code quality statistics are unavailable.",
        "filesFailure": "Tracked file counts are unavailable.",
        "complete": "",
    }
