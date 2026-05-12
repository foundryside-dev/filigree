"""CLI commands for scanner lifecycle — list, trigger, batch trigger, status, preview, report.

Mirrors the MCP scanner-domain tools:
  list-scanners, trigger-scan, trigger-scan-batch,
  get-scan-status, preview-scan, report-finding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as json_mod
import logging
import secrets
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Any

import click

from filigree.cli_common import get_db
from filigree.core import FILIGREE_DIR_NAME, VALID_SEVERITIES, ProjectNotInitialisedError, find_filigree_anchor
from filigree.mcp_tools.scanners import _load_scanner_or_error, _report_finding_observation_ids, _validate_localhost_url
from filigree.paths import safe_path
from filigree.scanner_runtime import ScannerSpawnError, _spawn_scan
from filigree.scanners import list_scanners as _list_scanners
from filigree.scanners import validate_scanner_command
from filigree.types.api import ErrorCode

_logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "http://localhost:8377"


def _get_filigree_dir() -> Path:
    """Discover .filigree/ directory.

    Raises ``ProjectNotInitialisedError`` (including its
    ``ForeignDatabaseError`` subclass) so callers can surface the rich
    diagnostic with ``str(exc)``. Don't broaden the catch to ``Exception``
    here — silently turning a foreign-database refusal into a generic
    "not initialized" message regresses the contract asserted in
    ``tests/test_doctor.py::test_foreign_database_is_reported_with_specific_message``.
    """
    project_root, _ = find_filigree_anchor()
    return project_root / FILIGREE_DIR_NAME


def _resolve_filigree_dir_or_die(as_json: bool) -> Path:
    """Resolve .filigree/ or emit a NOT_INITIALIZED error envelope and exit.

    Surfaces ``str(exc)`` so ``ForeignDatabaseError``'s rich message
    ("Refusing to latch onto another project's filigree database…") reaches
    the user instead of being collapsed into a generic line.
    """
    try:
        return _get_filigree_dir()
    except ProjectNotInitialisedError as exc:
        _emit_error(str(exc), ErrorCode.NOT_INITIALIZED, as_json=as_json)
        raise AssertionError("unreachable: _emit_error calls sys.exit(1)") from None  # pragma: no cover


def _emit_error(msg: str, code: Any, *, as_json: bool, details: dict[str, Any] | None = None) -> None:
    """Print an error envelope and exit 1."""
    if as_json:
        envelope: dict[str, Any] = {"error": msg, "code": code}
        if details:
            envelope["details"] = details
        click.echo(json_mod.dumps(envelope))
    else:
        click.echo(f"Error: {msg}", err=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# list-scanners
# ---------------------------------------------------------------------------


@click.command("list-scanners")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_scanners_cmd(as_json: bool) -> None:
    """List registered scanners from .filigree/scanners/*.toml."""
    filigree_dir = _resolve_filigree_dir_or_die(as_json)
    scanners_dir = filigree_dir / "scanners"
    load_errors: list[str] = []
    scanners = _list_scanners(scanners_dir, errors=load_errors)
    if load_errors:
        for msg in load_errors:
            _logger.warning("list_scanners load error: %s", msg)

    items = [s.to_dict() for s in scanners]

    if as_json:
        click.echo(json_mod.dumps({"items": items, "has_more": False}, indent=2, default=str))
        return

    if not items:
        click.echo("No scanners configured.")
        return
    for sc in items:
        click.echo(f"{sc['name']}  {sc.get('description', '')}")
    click.echo(f"\n{len(items)} scanner(s)")


# ---------------------------------------------------------------------------
# trigger-scan
# ---------------------------------------------------------------------------


@click.command("trigger-scan")
@click.argument("scanner")
@click.argument("file_path")
@click.option("--api-url", default=_DEFAULT_API_URL, help="Dashboard URL for scan result callbacks")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def trigger_scan_cmd(scanner: str, file_path: str, api_url: str, as_json: bool) -> None:
    """Trigger an async scan on a single file. Returns immediately with a scan_run_id."""
    from datetime import UTC, datetime

    filigree_dir = _resolve_filigree_dir_or_die(as_json)

    url_err = _validate_localhost_url(api_url)
    if url_err is not None:
        _emit_error(url_err["error"], url_err["code"], as_json=as_json)

    project_root = filigree_dir.parent
    try:
        target = safe_path(file_path, project_root)
    except ValueError as e:
        _emit_error(str(e), ErrorCode.VALIDATION, as_json=as_json)
        return  # unreachable — _emit_error exits

    cfg, err = _load_scanner_or_error(filigree_dir, scanner)
    if err is not None:
        _emit_error(err["error"], err["code"], as_json=as_json, details=err.get("details"))
        return

    assert cfg is not None  # noqa: S101

    if not target.is_file():
        _emit_error(f"File not found: {file_path}", ErrorCode.NOT_FOUND, as_json=as_json)
        return

    file_type_warning = ""
    if cfg.file_types:
        ext = Path(file_path).suffix.lstrip(".")
        if ext and ext not in cfg.file_types:
            file_type_warning = f"Warning: file extension {ext!r} not in scanner's declared file_types {cfg.file_types}. Proceeding anyway."

    canonical_path = str(target.relative_to(project_root.resolve()))

    with get_db() as tracker:
        file_record = tracker.register_file(canonical_path)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        scan_run_id = f"{scanner}-{ts}-{secrets.token_hex(3)}"

        try:
            created, blocking_run = tracker.reserve_scan_run(
                scan_run_id=scan_run_id,
                scanner_name=scanner,
                scan_source=scanner,
                file_path=canonical_path,
                file_id=file_record.id,
                api_url=api_url,
            )
        except (sqlite3.Error, ValueError) as exc:
            _emit_error(f"Failed to reserve scan run: {exc}", ErrorCode.IO, as_json=as_json)
            return

        if blocking_run is not None:
            _emit_error(
                f"Scanner {scanner!r} was already triggered for {file_path!r} recently. Retry after the blocking run completes.",
                ErrorCode.CONFLICT,
                as_json=as_json,
                details={"blocking_run_id": blocking_run["id"]},
            )
            return

        assert created is not None  # noqa: S101

        try:
            spawn_result = _spawn_scan(
                cfg=cfg,
                canonical_path=canonical_path,
                api_url=api_url,
                project_root=project_root,
                scan_run_id=scan_run_id,
                filigree_dir=filigree_dir,
            )
        except ScannerSpawnError as exc:
            with contextlib.suppress(sqlite3.Error, KeyError, ValueError):
                tracker.update_scan_run_status(scan_run_id, "failed", error_message="Scanner process failed to spawn")
            _emit_error(str(exc), exc.code, as_json=as_json, details=exc.details or None)
            return

        proc = spawn_result["proc"]
        scan_log_path = spawn_result["scan_log_path"]
        log_rel = str(scan_log_path.relative_to(project_root))

        try:
            tracker.set_scan_run_spawn_info(scan_run_id, pid=proc.pid, log_path=log_rel)
            tracker.update_scan_run_status(scan_run_id, "running")
        except (sqlite3.Error, KeyError, ValueError) as exc:
            with contextlib.suppress(OSError):
                proc.kill()
            _emit_error(
                f"Scan process spawned but DB tracking failed: {exc}. Process (pid={proc.pid}) terminated.",
                ErrorCode.IO,
                as_json=as_json,
            )
            return

        # Quick poll: did the process exit immediately?
        asyncio.run(asyncio.sleep(0.2))
        exit_code = proc.poll()
        if exit_code is not None and exit_code != 0:
            tracker.update_scan_run_status(
                scan_run_id,
                "failed",
                exit_code=exit_code,
                error_message=f"Scanner exited immediately with code {exit_code}",
            )
            log_hint = ""
            if scan_log_path.exists() and scan_log_path.stat().st_size > 0:
                log_hint = f" Check log: {log_rel}"
            elif spawn_result.get("log_warning"):
                log_hint = f" Note: {spawn_result['log_warning']}"
            _emit_error(
                f"Scanner process exited immediately with code {exit_code}.{log_hint}",
                ErrorCode.IO,
                as_json=as_json,
                details={
                    "scanner": scanner,
                    "file_id": file_record.id,
                    "scan_run_id": scan_run_id,
                    "exit_code": exit_code,
                    "log_path": log_rel,
                },
            )
            return

        scan_result: dict[str, Any] = {
            "status": "triggered",
            "scanner": scanner,
            "file_path": file_path,
            "file_id": file_record.id,
            "scan_run_id": scan_run_id,
            "pid": proc.pid,
            "log_path": log_rel,
            "message": (
                f"Scan triggered with run_id={scan_run_id!r}. "
                f"Results will be POSTed to {api_url}. "
                f"Poll findings via file_id={file_record.id!r} or status via get_scan_status. "
                f"Scanner log: {log_rel}"
            ),
        }
        warnings: list[str] = []
        if file_type_warning:
            warnings.append(file_type_warning)
        if spawn_result.get("log_warning"):
            warnings.append(spawn_result["log_warning"])
        if warnings:
            scan_result["warnings"] = warnings

    if as_json:
        click.echo(json_mod.dumps(scan_result, indent=2, default=str))
    else:
        click.echo(f"Triggered: {scanner} → {file_path} (run_id={scan_run_id}, pid={proc.pid})")
        click.echo(f"  Log: {log_rel}")


# ---------------------------------------------------------------------------
# trigger-scan-batch
# ---------------------------------------------------------------------------


@click.command("trigger-scan-batch")
@click.argument("scanner")
@click.argument("file_paths", nargs=-1)
@click.option("--api-url", default=_DEFAULT_API_URL, help="Dashboard URL for scan result callbacks")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def trigger_scan_batch_cmd(scanner: str, file_paths: tuple[str, ...], api_url: str, as_json: bool) -> None:
    """Trigger a scanner on multiple files. Returns batch_id and per-file scan_run_ids."""
    from datetime import UTC, datetime

    filigree_dir = _resolve_filigree_dir_or_die(as_json)

    fp_list = list(file_paths)
    if not fp_list:
        _emit_error("file_paths must be a non-empty list", ErrorCode.VALIDATION, as_json=as_json)
        return

    max_batch_size = 500
    if len(fp_list) > max_batch_size:
        _emit_error(
            f"file_paths length {len(fp_list)} exceeds maximum of {max_batch_size}",
            ErrorCode.VALIDATION,
            as_json=as_json,
        )
        return

    url_err = _validate_localhost_url(api_url)
    if url_err is not None:
        _emit_error(url_err["error"], url_err["code"], as_json=as_json)
        return

    cfg, err = _load_scanner_or_error(filigree_dir, scanner)
    if err is not None:
        _emit_error(err["error"], err["code"], as_json=as_json, details=err.get("details"))
        return
    assert cfg is not None  # noqa: S101

    project_root = filigree_dir.parent

    with get_db() as tracker:
        canonical_paths: list[str] = []
        file_ids: list[str] = []
        skipped: list[dict[str, str]] = []
        seen_canonical: set[str] = set()
        for fp in fp_list:
            try:
                target = safe_path(fp, project_root)
            except ValueError as e:
                skipped.append({"file_path": fp, "reason": str(e)})
                continue
            if not target.is_file():
                skipped.append({"file_path": fp, "reason": "File not found"})
                continue
            cp = str(target.relative_to(project_root.resolve()))
            if cp in seen_canonical:
                skipped.append({"file_path": fp, "reason": "duplicate"})
                continue
            seen_canonical.add(cp)
            file_record = tracker.register_file(cp)
            canonical_paths.append(cp)
            file_ids.append(file_record.id)

        if not canonical_paths:
            _emit_error(
                "No files eligible for scanning",
                ErrorCode.VALIDATION,
                as_json=as_json,
                details={"skipped": skipped},
            )
            return

        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        batch_id = f"{scanner}-batch-{ts}-{secrets.token_hex(3)}"

        reserved: list[dict[str, Any]] = []
        for i, (cp, fid) in enumerate(zip(canonical_paths, file_ids, strict=True)):
            child_run_id = f"{batch_id}-{i}"
            try:
                created, blocking = tracker.reserve_scan_run(
                    scan_run_id=child_run_id,
                    scanner_name=scanner,
                    scan_source=scanner,
                    file_path=cp,
                    file_id=fid,
                    api_url=api_url,
                )
            except (sqlite3.Error, ValueError) as exc:
                _logger.warning("reserve_scan_run failed for %s: %s", cp, exc)
                skipped.append({"file_path": cp, "reason": f"reservation_failed: {exc}"})
                continue
            if blocking is not None:
                skipped.append({"file_path": cp, "reason": "rate_limited"})
                continue
            assert created is not None  # noqa: S101
            reserved.append({"scan_run_id": child_run_id, "canonical_path": cp, "file_id": fid, "index": i})

        if not reserved:
            _emit_error(
                "No files eligible for scanning",
                ErrorCode.VALIDATION,
                as_json=as_json,
                details={"skipped": skipped},
            )
            return

        spawned: list[dict[str, Any]] = []
        spawn_errors: list[dict[str, str]] = []
        for entry in reserved:
            cp = entry["canonical_path"]
            child_run_id = entry["scan_run_id"]
            try:
                spawn_result = _spawn_scan(
                    cfg=cfg,
                    canonical_path=cp,
                    api_url=api_url,
                    project_root=project_root,
                    scan_run_id=child_run_id,
                    filigree_dir=filigree_dir,
                    log_suffix=f"-{entry['index']}",
                )
            except ScannerSpawnError as exc:
                reason = str(exc)
                spawn_errors.append({"file_path": cp, "reason": reason})
                with contextlib.suppress(sqlite3.Error, KeyError, ValueError):
                    tracker.update_scan_run_status(
                        child_run_id,
                        "failed",
                        error_message=f"Scanner process failed to spawn: {reason}",
                    )
                continue
            entry["spawn_result"] = spawn_result
            spawned.append(entry)

        if not spawned:
            _emit_error(
                "All scanner processes failed to spawn",
                ErrorCode.IO,
                as_json=as_json,
                details={"spawn_errors": spawn_errors, "skipped": skipped, "batch_id": batch_id},
            )
            return

        finalized: list[dict[str, Any]] = []
        for entry in spawned:
            spawn_result = entry["spawn_result"]
            proc = spawn_result["proc"]
            scan_log_path = spawn_result["scan_log_path"]
            log_rel = str(scan_log_path.relative_to(project_root))
            try:
                tracker.set_scan_run_spawn_info(entry["scan_run_id"], pid=proc.pid, log_path=log_rel)
                tracker.update_scan_run_status(entry["scan_run_id"], "running")
            except (sqlite3.Error, KeyError, ValueError) as exc:
                with contextlib.suppress(OSError):
                    proc.kill()
                spawn_errors.append({"file_path": entry["canonical_path"], "reason": f"db_tracking_failed: {exc}"})
                continue
            entry["log_rel"] = log_rel
            entry["pid"] = proc.pid
            finalized.append(entry)

        if not finalized:
            _emit_error(
                "All scanner processes spawned but DB tracking failed",
                ErrorCode.IO,
                as_json=as_json,
                details={"spawn_errors": spawn_errors, "skipped": skipped, "batch_id": batch_id},
            )
            return

        asyncio.run(asyncio.sleep(0.2))
        immediate_failures = 0
        for entry in finalized:
            proc = entry["spawn_result"]["proc"]
            ec = proc.poll()
            if ec is not None and ec != 0:
                immediate_failures += 1
                with contextlib.suppress(sqlite3.Error, KeyError, ValueError):
                    tracker.update_scan_run_status(
                        entry["scan_run_id"],
                        "failed",
                        exit_code=ec,
                        error_message="Scanner exited immediately",
                    )

        scan_run_ids = [entry["scan_run_id"] for entry in finalized]
        per_file = [
            {
                "scan_run_id": entry["scan_run_id"],
                "file_path": entry["canonical_path"],
                "file_id": entry["file_id"],
                "pid": entry["pid"],
                "log_path": entry["log_rel"],
            }
            for entry in finalized
        ]

        if immediate_failures == len(finalized):
            _emit_error(
                f"All {len(finalized)} scanner processes exited immediately.",
                ErrorCode.IO,
                as_json=as_json,
                details={"batch_id": batch_id, "scan_run_ids": scan_run_ids, "per_file": per_file},
            )
            return

        result: dict[str, Any] = {
            "status": "triggered",
            "scanner": scanner,
            "file_count": len(finalized),
            "processes_spawned": len(finalized),
            "batch_id": batch_id,
            "scan_run_ids": scan_run_ids,
            "per_file": per_file,
        }
        if spawn_errors:
            result["spawn_errors"] = spawn_errors
        if skipped:
            result["skipped"] = skipped
        if immediate_failures:
            result["immediate_failures"] = immediate_failures
        log_warnings = [entry["spawn_result"]["log_warning"] for entry in finalized if entry["spawn_result"].get("log_warning")]
        if log_warnings:
            result["warnings"] = log_warnings

    if as_json:
        click.echo(json_mod.dumps(result, indent=2, default=str))
    else:
        click.echo(f"Triggered: {scanner} batch on {len(finalized)} file(s) (batch_id={batch_id})")
        for entry in finalized:
            click.echo(f"  {entry['canonical_path']}  run_id={entry['scan_run_id']}")


# ---------------------------------------------------------------------------
# get-scan-status
# ---------------------------------------------------------------------------


@click.command("get-scan-status")
@click.argument("scan_run_id")
@click.option("--log-lines", default=50, type=click.IntRange(min=1, max=500), help="Number of log lines to tail (1-500)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_scan_status_cmd(scan_run_id: str, log_lines: int, as_json: bool) -> None:
    """Get the status of a scan run by ID, including live PID check and log tail."""
    if not scan_run_id.strip():
        _emit_error("scan_run_id is required", ErrorCode.VALIDATION, as_json=as_json)
        return

    with get_db() as tracker:
        try:
            status = tracker.get_scan_status(scan_run_id, log_lines=log_lines)
        except KeyError:
            _emit_error(f"Scan run not found: {scan_run_id}", ErrorCode.NOT_FOUND, as_json=as_json)
            return
        except sqlite3.Error as exc:
            _emit_error(f"Database error: {exc}", ErrorCode.IO, as_json=as_json)
            return

    if as_json:
        click.echo(json_mod.dumps(status, indent=2, default=str))
        return

    click.echo(f"Scan run: {status['id']}")
    click.echo(f"  Status: {status['status']}")
    click.echo(f"  Scanner: {status.get('scanner_name', '')}")
    click.echo(f"  Process alive: {status.get('process_alive', False)}")
    if status.get("log_tail"):
        click.echo(f"  Log ({len(status['log_tail'])} lines):")
        for line in status["log_tail"]:
            click.echo(f"    {line}")


# ---------------------------------------------------------------------------
# preview-scan
# ---------------------------------------------------------------------------


@click.command("preview-scan")
@click.argument("scanner")
@click.argument("file_path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def preview_scan_cmd(scanner: str, file_path: str, as_json: bool) -> None:
    """Preview the command that would be executed for a scan, without spawning a process."""
    filigree_dir = _resolve_filigree_dir_or_die(as_json)
    project_root = filigree_dir.parent
    try:
        target = safe_path(file_path, project_root)
    except ValueError as e:
        _emit_error(str(e), ErrorCode.VALIDATION, as_json=as_json)
        return

    cfg, err = _load_scanner_or_error(filigree_dir, scanner)
    if err is not None:
        _emit_error(err["error"], err["code"], as_json=as_json, details=err.get("details"))
        return
    assert cfg is not None  # noqa: S101

    canonical_path = str(target.relative_to(project_root.resolve()))
    try:
        cmd = cfg.build_command(
            file_path=canonical_path,
            api_url=_DEFAULT_API_URL,
            project_root=str(project_root),
            scan_run_id="preview-dry-run",
        )
    except ValueError as e:
        _emit_error(str(e), ErrorCode.VALIDATION, as_json=as_json)
        return

    cmd_err = validate_scanner_command(cmd, project_root=project_root)

    preview: dict[str, Any] = {
        "scanner": scanner,
        "file_path": file_path,
        "command": cmd,
        "command_string": shlex.join(cmd),
        "valid": cmd_err is None,
        "validation_error": cmd_err,
    }

    if as_json:
        click.echo(json_mod.dumps(preview, indent=2, default=str))
        return

    click.echo(f"Scanner: {scanner}")
    click.echo(f"File: {file_path}")
    click.echo(f"Command: {preview['command_string']}")
    click.echo(f"Valid: {preview['valid']}")
    if cmd_err:
        click.echo(f"Validation error: {cmd_err}", err=True)


# ---------------------------------------------------------------------------
# report-finding
# ---------------------------------------------------------------------------


@click.command("report-finding")
@click.option("--file", "file_path", default=None, help="Path to JSON file with finding (default: stdin)")
@click.option(
    "--actor",
    default="",
    help="Agent identity for the paired observation (default: scanner:agent)",
)
@click.option(
    "--no-observation",
    "no_observation",
    is_flag=True,
    help="Skip auto-creating a paired observation for triage.",
)
@click.option(
    "--response-detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="'slim' (default) drops batch-ingest stats; 'full' keeps the legacy keys.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def report_finding_cmd(file_path: str | None, actor: str, no_observation: bool, response_detail: str, as_json: bool) -> None:
    """Report a single code finding (bug, smell, security issue) from JSON via stdin or --file.

    The JSON must be an object with at minimum: path, rule_id, message.
    Optional fields: severity (default: info), line_start, line_end, category.

    By default a paired observation is auto-created so the finding shows up in
    list-observations for triage. Pass ``--no-observation`` to skip. Pass
    ``--actor`` to attribute the report to a specific agent identity.
    """
    # Resolve the project up front so a foreign-database refusal surfaces as
    # NOT_INITIALIZED rather than getting masked by a downstream --file path
    # error. This matches the rest of this module (list-scanners, trigger-scan,
    # trigger-scan-batch, preview-scan).
    filigree_dir = _resolve_filigree_dir_or_die(as_json)
    project_root: Path = filigree_dir.parent

    # Read input
    if file_path is not None:
        try:
            resolved = safe_path(file_path, project_root)
            raw = resolved.read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            _emit_error(str(e), ErrorCode.VALIDATION, as_json=as_json)
            return
    else:
        raw = click.get_text_stream("stdin").read()

    # Parse JSON
    try:
        finding = json_mod.loads(raw)
    except json_mod.JSONDecodeError as e:
        _emit_error(f"Invalid JSON: {e}", ErrorCode.VALIDATION, as_json=as_json)
        return

    if not isinstance(finding, dict):
        _emit_error("Finding must be a JSON object", ErrorCode.VALIDATION, as_json=as_json)
        return

    # Validate types up front. Without isinstance checks, an unhashable value
    # (`"severity": []`) would crash the membership test below with a raw
    # TypeError, and non-string-but-truthy values (`"path": [1, 2]`) would slip
    # past a bare falsy guard and only fail in the DB layer — where the CLI
    # would mismap the resulting ValueError to ErrorCode.IO.
    path = finding.get("path")
    if path is None:
        path = finding.get("file_path")
    for field_name, value in (("path (or file_path)", path), ("rule_id", finding.get("rule_id")), ("message", finding.get("message"))):
        if not isinstance(value, str):
            _emit_error(
                f"{field_name} must be a non-empty string, got {type(value).__name__}",
                ErrorCode.VALIDATION,
                as_json=as_json,
            )
            return
        if not value:
            _emit_error(
                f"{field_name} is required",
                ErrorCode.VALIDATION,
                as_json=as_json,
            )
            return
    rule_id = finding["rule_id"]
    message = finding["message"]

    severity = finding.get("severity", "info")
    if not isinstance(severity, str):
        _emit_error(
            f"severity must be a string, got {type(severity).__name__}",
            ErrorCode.VALIDATION,
            as_json=as_json,
        )
        return
    if severity not in VALID_SEVERITIES:
        _emit_error(
            f"Invalid severity: {severity!r}. Valid: {', '.join(sorted(VALID_SEVERITIES))}",
            ErrorCode.VALIDATION,
            as_json=as_json,
        )
        return

    line_start = finding.get("line_start")
    if line_start is not None and (isinstance(line_start, bool) or not isinstance(line_start, int)):
        _emit_error(
            f"line_start must be an integer or null, got {type(line_start).__name__}",
            ErrorCode.VALIDATION,
            as_json=as_json,
        )
        return
    line_end = finding.get("line_end")
    if line_end is not None and (isinstance(line_end, bool) or not isinstance(line_end, int)):
        _emit_error(
            f"line_end must be an integer or null, got {type(line_end).__name__}",
            ErrorCode.VALIDATION,
            as_json=as_json,
        )
        return

    # Build the finding dict for process_scan_results
    finding_record: dict[str, Any] = {
        "path": path,
        "rule_id": rule_id,
        "message": message,
        "severity": severity,
    }
    if line_start is not None:
        finding_record["line_start"] = line_start
    if line_end is not None:
        finding_record["line_end"] = line_end
    if finding.get("category"):
        finding_record["metadata"] = {"category": finding["category"]}

    observation_ids: list[str] = []
    create_observation = not no_observation
    with get_db() as tracker:
        try:
            result = tracker.process_scan_results(
                scan_source="agent",
                findings=[finding_record],
                scan_run_id="",
                create_observations=create_observation,
                observation_actor=actor.strip(),
            )
        except ValueError as exc:
            # Mirrors the HTTP route at dashboard_routes/files.py: a ValueError
            # from process_scan_results is caller-side malformed-input, not a
            # storage failure.
            _logger.warning("report_finding validation failed: %s", exc)
            _emit_error(f"Failed to report finding: {exc}", ErrorCode.VALIDATION, as_json=as_json)
            return
        except sqlite3.Error as exc:
            _logger.error("report_finding storage failure: %s", exc)
            _emit_error(f"Failed to report finding: {exc}", ErrorCode.IO, as_json=as_json)
            return
        file_record = tracker.register_file(finding_record["path"])
        if create_observation and result["new_finding_ids"]:
            observation_ids = _report_finding_observation_ids(
                tracker,
                file_id=file_record.id,
                finding_id=result["new_finding_ids"][0],
            )

    response: dict[str, Any] = {
        "status": "created" if result["findings_created"] else "updated",
    }
    if result["new_finding_ids"]:
        response["finding_id"] = result["new_finding_ids"][0]
    if observation_ids:
        response["observation_id"] = observation_ids[0]
    if response_detail == "full":
        # Legacy batch-style stats — useful when piping into scripts that
        # expect the ingest summary. Default slim drops these as noise for
        # the single-finding-write case (F3 — review-h).
        response["findings_created"] = result["findings_created"]
        response["findings_updated"] = result["findings_updated"]
        response["file_created"] = result["files_created"] > 0
        response["observations_created"] = result["observations_created"]
        response["observations_failed"] = result["observations_failed"]
        response["observation_ids"] = observation_ids
    if result.get("warnings"):
        response["warnings"] = result["warnings"]

    if as_json:
        click.echo(json_mod.dumps(response, indent=2, default=str))
    else:
        click.echo(f"{response['status']}: finding in {path}")
        if response.get("finding_id"):
            click.echo(f"  Finding ID: {response['finding_id']}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(cli: click.Group) -> None:
    """Register scanner commands with the CLI group."""
    cli.add_command(list_scanners_cmd)
    cli.add_command(trigger_scan_cmd)
    cli.add_command(trigger_scan_batch_cmd)
    cli.add_command(get_scan_status_cmd)
    cli.add_command(preview_scan_cmd)
    cli.add_command(report_finding_cmd)
