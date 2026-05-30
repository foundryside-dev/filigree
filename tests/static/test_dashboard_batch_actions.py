"""Static dashboard batch-action behavior checks."""

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


def test_batch_action_error_message_covers_http_and_partial_failures() -> None:
    script = textwrap.dedent(
        """
        import { batchActionErrorMessage } from "./src/filigree/static/js/ui.js";

        const payload = {
          http: batchActionErrorMessage({ ok: false, error: "Batch update failed" }, "Priority update", 2),
          partial: batchActionErrorMessage(
            { ok: true, data: { updated: ["issue-a"], errors: [{ id: "issue-b", error: "not found" }] } },
            "Priority update",
            2,
          ),
          batchResponse: batchActionErrorMessage(
            { ok: true, data: { succeeded: ["issue-a"], failed: [{ id: "issue-b", error: "not found" }] } },
            "Batch close",
            2,
          ),
          success: batchActionErrorMessage({ ok: true, data: { updated: ["issue-a"], errors: [] } }, "Priority update", 1),
        };
        console.log(JSON.stringify(payload));
        """
    )

    result = _run_node(script)

    assert result == {
        "http": "Batch update failed",
        "partial": "Priority update failed for 1 of 2 issues",
        "batchResponse": "Batch close failed for 1 of 2 issues",
        "success": "",
    }
