"""Static dashboard detail-readiness behavior checks."""

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


def test_detail_readiness_badge_uses_dependency_details_before_issue_cache_loads() -> None:
    script = textwrap.dedent(
        """
        import { detailReadinessBadgeHtml } from "./src/filigree/static/js/views/detail.js";

        const issueMap = {};
        const openFromDetail = detailReadinessBadgeHtml(
          {
            status_category: "open",
            blocked_by: ["blocker-a"],
            dep_details: { "blocker-a": { status_category: "open" } },
          },
          issueMap,
        );
        const unknownUntilListLoads = detailReadinessBadgeHtml(
          { status_category: "open", blocked_by: ["blocker-b"], dep_details: {} },
          issueMap,
        );
        const resolvedFromDetail = detailReadinessBadgeHtml(
          {
            status_category: "open",
            blocked_by: ["blocker-c"],
            dep_details: { "blocker-c": { status_category: "done" } },
          },
          issueMap,
        );

        console.log(JSON.stringify({ openFromDetail, unknownUntilListLoads, resolvedFromDetail }));
        """
    )

    result = _run_node(script)

    assert "Blocked by 1" in result["openFromDetail"]
    assert "Ready" not in result["openFromDetail"]
    assert "Blocked by 1" in result["unknownUntilListLoads"]
    assert "Ready" not in result["unknownUntilListLoads"]
    assert "Ready" in result["resolvedFromDetail"]
    assert "Blocked" not in result["resolvedFromDetail"]
