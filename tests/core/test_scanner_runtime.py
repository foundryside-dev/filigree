from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from filigree.scanner_runtime import ScannerSpawnError, _spawn_scan
from filigree.scanners import ScannerConfig
from filigree.types.api import ErrorCode


def test_spawn_scan_wraps_log_directory_creation_failure(tmp_path) -> None:
    project_root = tmp_path
    filigree_dir = project_root / ".filigree"
    filigree_dir.mkdir()
    scan_log_dir = filigree_dir / "scans"
    scan_log_dir.write_text("not a directory\n")
    cfg = ScannerConfig(
        name="test-scanner",
        description="Test scanner",
        command=sys.executable,
        args=("-c", "pass"),
        file_types=("py",),
    )

    with (
        patch("filigree.scanner_runtime.subprocess.Popen") as popen,
        pytest.raises(ScannerSpawnError) as exc_info,
    ):
        _spawn_scan(
            cfg=cfg,
            canonical_path="target.py",
            api_url="http://localhost:8377",
            project_root=project_root,
            scan_run_id="scan-1",
            filigree_dir=filigree_dir,
        )

    assert exc_info.value.code == ErrorCode.IO
    assert exc_info.value.details == {
        "scanner": "test-scanner",
        "scan_log_dir": str(scan_log_dir),
    }
    assert "Failed to create scan log directory" in str(exc_info.value)
    popen.assert_not_called()
