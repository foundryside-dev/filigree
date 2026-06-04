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
            approve_execution=True,
        )

    assert exc_info.value.code == ErrorCode.IO
    assert exc_info.value.details == {
        "scanner": "test-scanner",
        "scan_log_dir": str(scan_log_dir),
    }
    assert "Failed to create scan log directory" in str(exc_info.value)
    popen.assert_not_called()


def test_spawn_scan_runs_controlled_subprocess_and_captures_log(tmp_path) -> None:
    project_root = tmp_path
    filigree_dir = project_root / ".filigree"
    filigree_dir.mkdir()
    (project_root / "target.py").write_text("print('target')\n")
    cfg = ScannerConfig(
        name="real-subprocess",
        description="Tiny real subprocess scanner",
        command=sys.executable,
        args=(
            "-c",
            "import pathlib, sys; print(f'scanned={sys.argv[1]} cwd={pathlib.Path.cwd().name}', file=sys.stderr)",
            "{file}",
        ),
        file_types=("py",),
    )

    result = _spawn_scan(
        cfg=cfg,
        canonical_path="target.py",
        api_url="http://localhost:8377",
        project_root=project_root,
        scan_run_id="scan-real",
        filigree_dir=filigree_dir,
        approve_execution=True,
    )

    proc = result["proc"]
    assert proc.wait(timeout=5) == 0
    log_path = result["scan_log_path"]
    assert log_path.read_text() == f"scanned=target.py cwd={project_root.name}\n"
