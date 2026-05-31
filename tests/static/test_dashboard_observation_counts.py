"""Static dashboard metrics observation-count rendering checks.

Regression for the escHtml(0) falsy-guard bug: numeric zero counts must
render as an explicit "0", not a blank cell.
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


def test_observation_stats_render_numeric_zero_counts() -> None:
    """stale_count / expiring_soon_count of 0 must render as "0", not blank."""
    script = textwrap.dedent(
        """
        const { renderObservationStats } = await import(
          "./src/filigree/static/js/views/metrics.js"
        );

        const container = { innerHTML: "" };
        renderObservationStats(container, {
          count: 3,
          stale_count: 0,
          expiring_soon_count: 0,
          oldest_hours: null,
        });

        const html = container.innerHTML;
        process.stdout.write(JSON.stringify({
          // stale uses emerald when 0; the count cell must show "0"
          staleZero: html.includes('text-emerald-400">0</div>'),
          // expiring uses emerald when 0; the count cell must show "0"
          expiringZero: html.includes('text-emerald-400">0</div>'),
          // the pending count (3) must still render
          pendingThree: html.includes('>3</div>'),
          // no blank count cell should be emitted
          noBlankCount: !html.includes('font-bold ' + 'text-emerald-400' + '"></div>'),
        }));
        """
    )
    result = _run_node(script)
    assert result["pendingThree"] is True, result
    assert result["staleZero"] is True, result
    assert result["expiringZero"] is True, result
    assert result["noBlankCount"] is True, result
