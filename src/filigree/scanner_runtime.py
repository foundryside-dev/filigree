"""Core scanner spawn logic shared by MCP tools and CLI commands.

Separating spawn logic from the MCP layer allows the CLI to call it
without depending on ``mcp.types``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from filigree.types.api import ErrorCode

_logger = logging.getLogger(__name__)


class ScannerSpawnError(Exception):
    """Raised when _spawn_scan cannot launch the scanner.

    code: an ErrorCode (VALIDATION, NOT_FOUND, IO, etc.)
    details: optional metadata dict
    """

    def __init__(self, message: str, *, code: ErrorCode, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _spawn_scan(
    *,
    cfg: Any,
    canonical_path: str,
    api_url: str,
    project_root: Path,
    scan_run_id: str,
    filigree_dir: Path,
    prompt: str = "bug-hunt",
    log_suffix: str = "",
    approve_execution: bool = False,
    approval_hint: str = "approve_execution=true",
) -> dict[str, Any]:
    """Build command, validate, and spawn scanner process.

    Returns ``{'proc': Popen, 'scan_log_path': Path, 'cmd': list[str],
    'log_warning'?: str}`` on success.

    Raises :exc:`ScannerSpawnError` on failure.

    *log_suffix* disambiguates log files when multiple processes share
    a scan_run_id (batch mode).
    """
    from filigree.scanners import scanner_execution_approval_error, scanner_requires_execution_approval, validate_scanner_command

    approval_error = scanner_execution_approval_error(cfg, approved=approve_execution, approval_hint=approval_hint)
    if approval_error is not None:
        raise ScannerSpawnError(
            approval_error,
            code=ErrorCode.VALIDATION,
            details={
                "scanner": cfg.name,
                "managed": not scanner_requires_execution_approval(cfg),
                "approval_argument": "approve_execution",
            },
        )

    try:
        cmd = cfg.build_command(
            file_path=canonical_path,
            api_url=api_url,
            project_root=str(project_root),
            scan_run_id=scan_run_id,
            prompt=prompt,
        )
    except ValueError as e:
        raise ScannerSpawnError(str(e), code=ErrorCode.VALIDATION) from e

    cmd_err = validate_scanner_command(cmd, project_root=project_root)
    if cmd_err is not None:
        raise ScannerSpawnError(cmd_err, code=ErrorCode.NOT_FOUND)

    scan_log_dir = filigree_dir / "scans"
    try:
        scan_log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ScannerSpawnError(
            f"Failed to create scan log directory {scan_log_dir}: {e}",
            code=ErrorCode.IO,
            details={"scanner": cfg.name, "scan_log_dir": str(scan_log_dir)},
        ) from e
    log_name = f"{scan_run_id}{log_suffix}.log"
    scan_log_path = scan_log_dir / log_name
    log_warning: str | None = None
    try:
        scan_log_fd = open(scan_log_path, "w")  # noqa: SIM115
    except OSError as log_err:
        scan_log_fd = None
        log_warning = f"Scan log could not be created at {scan_log_path}: {log_err}. Scanner stderr will be discarded."
        _logger.warning("Cannot open scan log %s: %s", scan_log_path, log_err)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=scan_log_fd if scan_log_fd is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError, TypeError) as e:
        raise ScannerSpawnError(
            f"Failed to spawn scanner process: {e}",
            code=ErrorCode.IO,
            details={"scanner": cfg.name},
        ) from e
    finally:
        if scan_log_fd is not None:
            scan_log_fd.close()

    result: dict[str, Any] = {
        "proc": proc,
        "scan_log_path": scan_log_path,
        "cmd": cmd,
    }
    if log_warning:
        result["log_warning"] = log_warning
    return result
