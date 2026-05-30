"""Static dashboard release date behavior checks."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node(script: str, *, tz: str | None = None) -> dict[str, object]:
    env = os.environ.copy()
    if tz:
        env["TZ"] = tz
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
        text=True,
    )
    return json.loads(result.stdout)


def test_date_only_release_targets_keep_local_calendar_day_in_negative_timezones() -> None:
    script = textwrap.dedent(
        """
        const { releaseTargetDateStart } = await import("./src/filigree/static/js/views/releases.js");

        const target = releaseTargetDateStart("2026-05-20");
        console.log(JSON.stringify({
          year: target.getFullYear(),
          month: target.getMonth() + 1,
          day: target.getDate(),
        }));
        """
    )

    assert _run_node(script, tz="America/Los_Angeles") == {
        "year": 2026,
        "month": 5,
        "day": 20,
    }


def test_release_target_renderer_uses_local_date_only_parser() -> None:
    text = (REPO_ROOT / "src/filigree/static/js/views/releases.js").read_text()

    assert "const target = releaseTargetDateStart(isoDate);" in text
