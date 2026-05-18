"""CLI commands for file tracking, associations, and finding triage.

Mirrors the MCP file-domain tools:
  list-files, get-file, get-file-timeline, get-issue-files,
  add-file-association, register-file, delete-file-record,
  list-findings, get-finding, update-finding,
  promote-finding, dismiss-finding, batch-update-findings.
"""

from __future__ import annotations

import json as json_mod
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import click

from filigree.cli_common import get_db, refresh_summary
from filigree.core import VALID_ASSOC_TYPES, VALID_FINDING_STATUSES, VALID_SEVERITIES, find_filigree_anchor
from filigree.issue_payloads import issue_to_public
from filigree.mcp_tools.payloads import (
    file_assoc_to_mcp,
    file_detail_to_mcp,
    file_record_to_mcp,
    finding_to_mcp,
    timeline_entry_to_mcp,
)
from filigree.paths import safe_path
from filigree.registry import clarion_file_read_url
from filigree.types.api import BatchFailure, ErrorCode
from filigree.types.core import AssocType, FindingStatus
from filigree.validation import sanitize_actor

_DISMISS_FINDING_STATUSES = ("acknowledged", "false_positive", "fixed", "unseen_in_latest")
_MAX_SQLITE_OFFSET = 9_223_372_036_854_775_807
_MAX_SQLITE_LIMIT = _MAX_SQLITE_OFFSET - 1
_UNLIMITED_LIST_LIMIT = 10_000_000
_REGISTRY_MIGRATION_ACTOR = "registry-migration"
_FILE_ID_REFERENCE_UPDATES = (
    "UPDATE scan_findings SET file_id = ? WHERE file_id = ?",
    "UPDATE file_associations SET file_id = ? WHERE file_id = ?",
    "UPDATE file_events SET file_id = ? WHERE file_id = ?",
    "UPDATE observations SET file_id = ? WHERE file_id = ?",
    "UPDATE observation_links SET file_id = ? WHERE file_id = ?",
    "UPDATE annotations SET file_id = ? WHERE file_id = ?",
)


def _emit_validation_error(msg: str, *, as_json: bool) -> None:
    if as_json:
        click.echo(json_mod.dumps({"error": msg, "code": ErrorCode.VALIDATION}))
    else:
        click.echo(f"Error: {msg}", err=True)
    sys.exit(1)


def _validate_int_range(value: int, name: str, *, min_val: int, max_val: int, as_json: bool) -> None:
    if value < min_val or value > max_val:
        _emit_validation_error(f"{name} must be between {min_val} and {max_val}, got {value}", as_json=as_json)


# ---------------------------------------------------------------------------
# File commands
# ---------------------------------------------------------------------------


@click.command("list-files")
@click.option("--limit", default=100, type=int, help="Max results (default 100)")
@click.option("--offset", default=0, type=int, help="Skip first N results")
@click.option("--no-limit", "no_limit", is_flag=True, help="Return all results without cap")
@click.option("--language", default=None, help="Filter by language")
@click.option("--path-prefix", default=None, help="Filter by substring in file path")
@click.option(
    "--min-findings",
    default=None,
    type=int,
    help="Minimum open findings count",
)
@click.option(
    "--has-severity",
    default=None,
    type=click.Choice(sorted(VALID_SEVERITIES)),
    help="Require at least one open finding at this severity",
)
@click.option("--scan-source", default=None, help="Filter files by finding source")
@click.option(
    "--sort",
    default="updated_at",
    type=click.Choice(["updated_at", "first_seen", "path", "language"]),
    help="Sort field (default: updated_at)",
)
@click.option(
    "--direction",
    default=None,
    type=click.Choice(["asc", "desc"]),
    help="Sort direction",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_files_cmd(
    limit: int,
    offset: int,
    no_limit: bool,
    language: str | None,
    path_prefix: str | None,
    min_findings: int | None,
    has_severity: str | None,
    scan_source: str | None,
    sort: str,
    direction: str | None,
    as_json: bool,
) -> None:
    """List tracked files with filtering, sorting, and pagination."""
    _validate_int_range(limit, "limit", min_val=1, max_val=_MAX_SQLITE_LIMIT, as_json=as_json)
    _validate_int_range(offset, "offset", min_val=0, max_val=_MAX_SQLITE_OFFSET, as_json=as_json)
    if min_findings is not None:
        _validate_int_range(min_findings, "min_findings", min_val=0, max_val=_MAX_SQLITE_LIMIT, as_json=as_json)
    with get_db() as db:
        effective_limit = limit if not no_limit else _UNLIMITED_LIST_LIMIT
        try:
            result = db.list_files_paginated(
                limit=effective_limit,
                offset=offset,
                language=language,
                path_prefix=path_prefix,
                min_findings=min_findings,
                has_severity=has_severity,
                scan_source=scan_source,
                sort=sort,
                direction=direction,
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        items = list(result["results"])
        has_more = bool(result["has_more"])
        next_offset = offset + len(items) if has_more else None

        if as_json:
            payload: dict[str, Any] = {"items": [file_record_to_mcp(item) for item in items], "has_more": has_more}
            if has_more and next_offset is not None:
                payload["next_offset"] = next_offset
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            return

        if not items:
            click.echo("No files found.")
            return
        for f in items:
            lang = f.get("language") or ""
            click.echo(f"{f['id']}  {f['path']}" + (f"  [{lang}]" if lang else ""))
        click.echo(f"\n{len(items)} file(s)")


@click.command("get-file")
@click.argument("file_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_file_cmd(file_id: str, as_json: bool) -> None:
    """Get file details, linked issues, recent findings, and summary."""
    with get_db() as db:
        try:
            data = db.get_file_detail(file_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"File not found: {file_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: File not found: {file_id}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(file_detail_to_mcp(data), indent=2, default=str))
            return

        f = data["file"]
        click.echo(f"File: {f['id']}  {f['path']}")
        if f.get("language"):
            click.echo(f"  Language: {f['language']}")
        summary = data.get("summary", {})
        click.echo(f"  Open findings: {summary.get('open_findings', 0)}")
        assoc = data.get("associations", [])
        if assoc:
            click.echo(f"  Associations: {len(assoc)}")


@click.command("get-file-timeline")
@click.argument("file_id")
@click.option("--limit", default=50, type=click.IntRange(min=1), help="Max results (default 50)")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Skip first N results")
@click.option("--no-limit", "no_limit", is_flag=True, help="Return all results without cap")
@click.option(
    "--event-type",
    default=None,
    type=click.Choice(["finding", "association", "file_metadata_update", "issue_event"]),
    help="Optional event type filter",
)
@click.option("--include-issue-events", is_flag=True, help="Merge events from associated issues")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_file_timeline_cmd(
    file_id: str,
    limit: int,
    offset: int,
    no_limit: bool,
    event_type: str | None,
    include_issue_events: bool,
    as_json: bool,
) -> None:
    """Get merged timeline events for a file (findings, associations, metadata updates)."""
    with get_db() as db:
        effective_limit = limit if not no_limit else 10_000_000
        try:
            result = db.get_file_timeline(
                file_id,
                limit=effective_limit,
                offset=offset,
                event_type=event_type,
                include_issue_events=include_issue_events,
            )
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"File not found: {file_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: File not found: {file_id}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        # Normalize PaginatedResult → ListResponse (CLI surface normalization)
        items = list(result["results"])
        has_more = bool(result["has_more"])
        next_offset = offset + len(items) if has_more else None

        if as_json:
            payload: dict[str, Any] = {"items": [timeline_entry_to_mcp(item) for item in items], "has_more": has_more}
            if has_more and next_offset is not None:
                payload["next_offset"] = next_offset
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            return

        if not items:
            click.echo("No timeline events.")
            return
        for entry in items:
            click.echo(f"{entry.get('timestamp', '')}  {entry.get('type', '')}  {entry.get('source_id', '')}")
        click.echo(f"\n{len(items)} event(s)")


@click.command("get-issue-files")
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_issue_files_cmd(issue_id: str, as_json: bool) -> None:
    """List files associated with an issue."""
    with get_db() as db:
        try:
            db.get_issue(issue_id)
            items = db.get_issue_files(issue_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Issue not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: Issue not found: {issue_id}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        # Normalize raw list → ListResponse (CLI surface normalization; MCP returns raw list)
        if as_json:
            payload: dict[str, Any] = {"items": [file_assoc_to_mcp(item) for item in items], "has_more": False}
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            return

        if not items:
            click.echo("No files associated with this issue.")
            return
        for assoc in items:
            click.echo(f"{assoc['file_id']}  {assoc.get('file_path', '')}  [{assoc.get('assoc_type', '')}]")
        click.echo(f"\n{len(items)} association(s)")


@click.command("add-file-association")
@click.argument("file_id")
@click.argument("issue_id")
@click.argument("assoc_type", type=click.Choice(sorted(VALID_ASSOC_TYPES)))
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def add_file_association_cmd(
    ctx: click.Context,
    file_id: str,
    issue_id: str,
    assoc_type: str,
    as_json: bool,
) -> None:
    """Create a file<->issue association (idempotent for duplicate tuples)."""
    with get_db() as db:
        # Validate file exists
        try:
            db.get_file(file_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"File not found: {file_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: File not found: {file_id}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        # Validate issue exists
        try:
            db.get_issue(issue_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Issue not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: Issue not found: {issue_id}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        try:
            db.add_file_association(file_id, issue_id, cast(AssocType, assoc_type), actor=ctx.obj["actor"])
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps({"status": "created"}, indent=2))
        else:
            click.echo(f"Associated {file_id} with {issue_id} as {assoc_type}")


@click.command("register-file")
@click.argument("path")
@click.option("--language", default=None, help="Optional language hint")
@click.option("--file-type", default=None, help="Optional file type tag")
@click.option("--metadata", default=None, help="Optional metadata as JSON object string")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def register_file_cmd(
    ctx: click.Context,
    path: str,
    language: str | None,
    file_type: str | None,
    metadata: str | None,
    as_json: bool,
) -> None:
    """Register or fetch a file record by project-relative path."""
    parsed_metadata: dict[str, Any] | None = None
    if metadata is not None:
        try:
            parsed_metadata = json_mod.loads(metadata)
            if not isinstance(parsed_metadata, dict):
                raise ValueError("metadata must be a JSON object")
        except (json_mod.JSONDecodeError, ValueError) as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Invalid metadata JSON: {e}", "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: Invalid metadata JSON: {e}", err=True)
            sys.exit(1)

    # Validate the path before opening the DB: reject absolute paths and traversals.
    try:
        project_root, _ = find_filigree_anchor()
    except Exception:
        # Let get_db() surface the proper error below.
        project_root = None

    if project_root is not None:
        try:
            resolved = safe_path(path, project_root)
            canonical_path = str(resolved.relative_to(project_root.resolve()))
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
    else:
        canonical_path = path

    with get_db() as db:
        if db.registry_backend == "clarion":
            base_url = str(db.clarion_config.get("base_url", "http://localhost:9111"))
            read_url = clarion_file_read_url(base_url, canonical_path, language=language or "")
            msg = (
                "File registration is displaced to Clarion for this project. "
                f"Use Clarion's read API instead: {read_url} (path: {canonical_path})"
            )
            if as_json:
                click.echo(json_mod.dumps({"error": msg, "code": ErrorCode.FILIGREE_FILE_REGISTRY_DISPLACED}))
            else:
                click.echo(f"Error: {msg}", err=True)
            sys.exit(1)
        try:
            file_record = db.register_file(
                canonical_path,
                language=language or "",
                file_type=file_type or "",
                metadata=parsed_metadata,
                actor=ctx.obj["actor"],
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(file_record_to_mcp(file_record.to_dict()), indent=2, default=str))
        else:
            click.echo(f"Registered {file_record.id}: {file_record.path}")


def _registry_migration_plan(db: Any, *, target_backend: str) -> dict[str, Any]:
    planned: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    rows = db.conn.execute("SELECT * FROM file_records ORDER BY path").fetchall()
    for row in rows:
        old_file_id = row["id"]
        try:
            resolved = db.registry.resolve_file(
                row["path"],
                language=row["language"] or "",
                actor=_REGISTRY_MIGRATION_ACTOR,
            )
        except Exception as exc:
            unresolved.append({"file_id": old_file_id, "path": row["path"], "error": str(exc)})
            continue

        planned.append(
            {
                "old_file_id": old_file_id,
                "new_file_id": resolved["file_id"],
                "old_path": row["path"],
                "new_path": resolved["canonical_path"],
                "old_language": row["language"] or "",
                "new_language": resolved["language"] or row["language"] or "",
                "old_content_hash": row["content_hash"] or "",
                "new_content_hash": resolved["content_hash"],
                "old_registry_backend": row["registry_backend"] or "local",
                "new_registry_backend": target_backend,
            }
        )
    return {
        "version": 1,
        "to": target_backend,
        "created_at": datetime.now(UTC).isoformat(),
        "planned": planned,
        "unresolved": unresolved,
    }


def _rewrite_scan_run_file_ids(conn: sqlite3.Connection, old_file_id: str, new_file_id: str) -> None:
    rows = conn.execute("SELECT id, file_ids FROM scan_runs WHERE file_ids LIKE ?", (f'%"{old_file_id}"%',)).fetchall()
    for row in rows:
        try:
            file_ids = json_mod.loads(row["file_ids"] or "[]")
        except json_mod.JSONDecodeError:
            continue
        if not isinstance(file_ids, list):
            continue
        rewritten = [new_file_id if item == old_file_id else item for item in file_ids]
        if rewritten != file_ids:
            conn.execute("UPDATE scan_runs SET file_ids = ? WHERE id = ?", (json_mod.dumps(rewritten), row["id"]))


def _apply_registry_migration(db: Any, entries: list[dict[str, Any]], *, reverse: bool = False) -> None:
    db.conn.commit()
    original_fk = int(db.conn.execute("PRAGMA foreign_keys").fetchone()[0])
    db.conn.execute("PRAGMA foreign_keys=OFF")
    try:
        db.conn.execute("BEGIN")
        for entry in entries:
            old_file_id = entry["new_file_id"] if reverse else entry["old_file_id"]
            new_file_id = entry["old_file_id"] if reverse else entry["new_file_id"]
            new_path = entry["old_path"] if reverse else entry["new_path"]
            new_language = entry["old_language"] if reverse else entry["new_language"]
            new_content_hash = entry["old_content_hash"] if reverse else entry["new_content_hash"]
            new_registry_backend = entry["old_registry_backend"] if reverse else entry["new_registry_backend"]

            conflict = db.conn.execute(
                "SELECT id FROM file_records WHERE id = ? AND id != ?",
                (new_file_id, old_file_id),
            ).fetchone()
            if conflict is not None:
                msg = f"Cannot rewrite {old_file_id} to {new_file_id}: target file_id already exists"
                raise ValueError(msg)

            updated = db.conn.execute(
                "UPDATE file_records SET id = ?, path = ?, language = ?, content_hash = ?, registry_backend = ? WHERE id = ?",
                (new_file_id, new_path, new_language, new_content_hash, new_registry_backend, old_file_id),
            ).rowcount
            if updated != 1:
                msg = f"File record not found for registry migration: {old_file_id}"
                raise KeyError(msg)
            for update_sql in _FILE_ID_REFERENCE_UPDATES:
                db.conn.execute(update_sql, (new_file_id, old_file_id))
            _rewrite_scan_run_file_ids(db.conn, old_file_id, new_file_id)

        violations = db.conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            msg = f"Registry migration would leave foreign-key violations: {len(violations)}"
            raise sqlite3.IntegrityError(msg)
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        db.conn.execute(f"PRAGMA foreign_keys={original_fk}")


@click.command("migrate-registry")
@click.option("--to", "target_backend", type=click.Choice(["clarion"]), default=None, help="Target registry backend")
@click.option("--dry-run", "dry_run", is_flag=True, help="Plan the migration without changing the database")
@click.option("--execute", "execute", is_flag=True, help="Apply the migration and write a rollback manifest")
@click.option("--rollback", "rollback_manifest", type=click.Path(path_type=Path), default=None, help="Rollback using a manifest")
@click.option("--manifest", "manifest_path", type=click.Path(path_type=Path), default=None, help="Manifest path for execute")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def migrate_registry_cmd(
    target_backend: str | None,
    dry_run: bool,
    execute: bool,
    rollback_manifest: Path | None,
    manifest_path: Path | None,
    as_json: bool,
) -> None:
    """Migrate file_records IDs to another registry backend, or rollback."""
    if rollback_manifest is not None and (target_backend is not None or dry_run or execute):
        _emit_validation_error("--rollback cannot be combined with --to, --dry-run, or --execute", as_json=as_json)
    if rollback_manifest is None and target_backend is None:
        _emit_validation_error("--to is required unless --rollback is used", as_json=as_json)
    if dry_run and execute:
        _emit_validation_error("--dry-run and --execute are mutually exclusive", as_json=as_json)
    if not dry_run and not execute and rollback_manifest is None:
        dry_run = True

    payload: dict[str, Any]
    with get_db() as db:
        try:
            if rollback_manifest is not None:
                manifest = json_mod.loads(rollback_manifest.read_text())
                entries = list(manifest.get("planned", []))
                _apply_registry_migration(db, entries, reverse=True)
                payload = {
                    "mode": "rollback",
                    "rolled_back": len(entries),
                    "manifest_path": str(rollback_manifest),
                }
            else:
                if target_backend is None:
                    msg = "--to is required unless --rollback is used"
                    raise ValueError(msg)
                if db.registry_backend != target_backend:
                    msg = f"Project registry_backend is {db.registry_backend!r}; set it to {target_backend!r} before migration"
                    raise ValueError(msg)
                manifest = _registry_migration_plan(db, target_backend=target_backend)
                if manifest["unresolved"]:
                    payload = {"mode": "dry-run" if dry_run else "execute", **manifest}
                    if execute:
                        msg = f"Cannot execute registry migration with {len(manifest['unresolved'])} unresolved file(s)"
                        raise ValueError(msg)
                if execute:
                    if manifest_path is None:
                        manifest_path = Path(f"registry-migration-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
                    _apply_registry_migration(db, list(manifest["planned"]), reverse=False)
                    manifest_path.write_text(json_mod.dumps(manifest, indent=2, default=str) + "\n")
                    payload = {"mode": "execute", "migrated": len(manifest["planned"]), "manifest_path": str(manifest_path), **manifest}
                else:
                    payload = {"mode": "dry-run", **manifest}
        except (OSError, json_mod.JSONDecodeError, KeyError, ValueError, sqlite3.Error) as e:
            if as_json:
                code = ErrorCode.IO if isinstance(e, sqlite3.Error) else ErrorCode.VALIDATION
                click.echo(json_mod.dumps({"error": str(e), "code": code}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    if as_json:
        click.echo(json_mod.dumps(payload, indent=2, default=str))
        return
    if payload["mode"] == "dry-run":
        click.echo(f"Planned {len(payload['planned'])} file registry rewrite(s); unresolved: {len(payload['unresolved'])}")
    elif payload["mode"] == "execute":
        click.echo(f"Migrated {payload['migrated']} file record(s). Manifest: {payload['manifest_path']}")
    else:
        click.echo(f"Rolled back {payload['rolled_back']} file record(s).")


@click.command("delete-file-record")
@click.argument("file_id")
@click.option("--force", is_flag=True, help="Cascade associations and open findings")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def delete_file_record_cmd(ctx: click.Context, file_id: str, force: bool, as_json: bool) -> None:
    """Delete a file record. Refuses linked/open-finding records unless --force is passed."""
    with get_db() as db:
        try:
            result = db.delete_file_record(file_id, force=force, actor=ctx.obj["actor"])
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"File not found: {file_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: File not found: {file_id}", err=True)
            sys.exit(1)
        except ValueError as e:
            code = ErrorCode.CONFLICT if "Cannot delete file record" in str(e) else ErrorCode.VALIDATION
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": code}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(result, indent=2, default=str))
        else:
            click.echo(f"Deleted {file_id} ({result['deleted_findings']} finding(s), {result['deleted_associations']} association(s))")


# ---------------------------------------------------------------------------
# Finding commands
# ---------------------------------------------------------------------------


@click.command("list-findings")
@click.option("--limit", default=100, type=click.IntRange(min=1), help="Max results (default 100)")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Skip first N results")
@click.option("--no-limit", "no_limit", is_flag=True, help="Return all results without cap")
@click.option(
    "--severity",
    default=None,
    type=click.Choice(sorted(VALID_SEVERITIES)),
    help="Filter by severity",
)
@click.option(
    "--status",
    default=None,
    type=click.Choice(sorted(VALID_FINDING_STATUSES)),
    help="Filter by finding status",
)
@click.option("--scan-source", default=None, help="Filter by scan source")
@click.option("--scan-run-id", default=None, help="Filter by scan run ID")
@click.option("--file-id", default=None, help="Filter by file ID")
@click.option("--issue-id", default=None, help="Filter by linked issue ID")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_findings_cmd(
    limit: int,
    offset: int,
    no_limit: bool,
    severity: str | None,
    status: str | None,
    scan_source: str | None,
    scan_run_id: str | None,
    file_id: str | None,
    issue_id: str | None,
    as_json: bool,
) -> None:
    """List scan findings across all files with optional filters."""
    with get_db() as db:
        effective_limit = limit if not no_limit else 10_000_000
        try:
            result = db.list_findings_global(
                limit=effective_limit,
                offset=offset,
                severity=severity,
                status=status,
                scan_source=scan_source,
                scan_run_id=scan_run_id,
                file_id=file_id,
                issue_id=issue_id,
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        findings = list(result["findings"])
        total = int(result["total"])
        has_more = (offset + len(findings)) < total
        next_offset = offset + len(findings) if has_more else None

        if as_json:
            payload: dict[str, Any] = {"items": [finding_to_mcp(item) for item in findings], "has_more": has_more}
            if has_more and next_offset is not None:
                payload["next_offset"] = next_offset
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            return

        if not findings:
            click.echo("No findings.")
            return
        for finding in findings:
            click.echo(
                f"{finding['id']}  [{finding.get('severity', '')}]  {finding.get('scan_source', '')}  {finding.get('message', '')[:60]}"
            )
        click.echo(f"\n{len(findings)} finding(s)")


@click.command("get-finding")
@click.argument("finding_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_finding_cmd(finding_id: str, as_json: bool) -> None:
    """Get a single scan finding by ID."""
    with get_db() as db:
        try:
            finding = db.get_finding(finding_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Finding not found: {finding_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: Finding not found: {finding_id}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(finding_to_mcp(finding), indent=2, default=str))
            return

        click.echo(f"Finding: {finding['id']}")
        click.echo(f"  Severity: {finding.get('severity', '')}")
        click.echo(f"  Status: {finding.get('status', '')}")
        click.echo(f"  Source: {finding.get('scan_source', '')}")
        click.echo(f"  Rule: {finding.get('rule_id', '')}")
        click.echo(f"  Message: {finding.get('message', '')}")


@click.command("update-finding")
@click.argument("finding_id")
@click.option(
    "--status",
    default=None,
    type=click.Choice(sorted(VALID_FINDING_STATUSES)),
    help="New finding status",
)
@click.option("--issue-id", default=None, help="Issue ID to link")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def update_finding_cmd(
    ctx: click.Context,
    finding_id: str,
    status: str | None,
    issue_id: str | None,
    as_json: bool,
) -> None:
    """Update a finding's status or linked issue."""
    if status is None and issue_id is None:
        if as_json:
            click.echo(json_mod.dumps({"error": "At least one of --status or --issue-id must be provided", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: At least one of --status or --issue-id must be provided", err=True)
        sys.exit(1)

    with get_db() as db:
        try:
            updated = db.update_finding(
                finding_id,
                status=cast(FindingStatus, status) if status is not None else None,
                issue_id=issue_id,
                actor=ctx.obj["actor"],
            )
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Finding not found: {finding_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: Finding not found: {finding_id}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(finding_to_mcp(updated), indent=2, default=str))
        else:
            click.echo(f"Updated finding {finding_id}: status={updated.get('status', '')}")
        refresh_summary(db)


@click.command("promote-finding")
@click.argument("finding_id")
@click.option(
    "--priority",
    default=None,
    type=click.IntRange(0, 4),
    help="Override priority (default: inferred from severity)",
)
@click.option("--actor", default=None, help="Actor identity (defaults to global --actor)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def promote_finding_cmd(
    ctx: click.Context,
    finding_id: str,
    priority: int | None,
    actor: str | None,
    as_json: bool,
) -> None:
    """Promote a scan finding directly to a tracked issue."""
    if actor is None:
        resolved_actor = ctx.obj["actor"]
    else:
        cleaned, err = sanitize_actor(actor)
        if err:
            if as_json:
                click.echo(json_mod.dumps({"error": err, "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {err}", err=True)
            sys.exit(1)
        resolved_actor = cleaned
    with get_db() as db:
        try:
            promoted = db.promote_finding_to_issue(finding_id, priority=priority, actor=resolved_actor)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Finding not found: {finding_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: Finding not found: {finding_id}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Failed to promote finding: {e}", "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: Failed to promote finding: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error promoting finding: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            payload: dict[str, Any] = dict(issue_to_public(promoted["issue"]))
            if promoted.get("warnings"):
                payload["warnings"] = promoted["warnings"]
            click.echo(json_mod.dumps(payload, indent=2, default=str))
        else:
            issue = promoted["issue"]
            click.echo(f"Promoted finding {finding_id} → issue {issue.id}: {issue.title}")
        refresh_summary(db)


@click.command("dismiss-finding")
@click.argument("finding_id")
@click.option(
    "--status",
    type=click.Choice(_DISMISS_FINDING_STATUSES),
    default="false_positive",
    show_default=True,
    help="Dismissal status to apply",
)
@click.option("--reason", default=None, help="Optional reason for dismissal")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def dismiss_finding_cmd(ctx: click.Context, finding_id: str, status: FindingStatus, reason: str | None, as_json: bool) -> None:
    """Dismiss a finding by marking it with a triage status."""
    with get_db() as db:
        try:
            updated = db.update_finding(
                finding_id,
                status=status,
                dismiss_reason=reason or None,
                actor=ctx.obj["actor"],
            )
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Finding not found: {finding_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: Finding not found: {finding_id}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error dismissing finding: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(finding_to_mcp(updated), indent=2, default=str))
        else:
            click.echo(f"Dismissed finding {finding_id}")
        refresh_summary(db)


@click.command("batch-update-findings")
@click.argument("finding_ids", nargs=-1, required=True)
@click.option(
    "--status",
    required=True,
    type=click.Choice(sorted(VALID_FINDING_STATUSES)),
    help="New status for all findings",
)
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, finding ID strings) or 'full' (ScanFindingDict records).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_update_findings_cmd(
    ctx: click.Context,
    finding_ids: tuple[str, ...],
    status: str,
    response_detail: str,
    as_json: bool,
) -> None:
    """Update the status of multiple findings at once."""
    with get_db() as db:
        raw_ids = list(finding_ids)
        updated_ids: list[str] = []
        updated_records: list[dict[str, Any]] = []
        errors: list[BatchFailure] = []

        for fid in raw_ids:
            try:
                record = db.update_finding(fid, status=cast(FindingStatus, status), actor=ctx.obj["actor"])
                updated_ids.append(fid)
                if response_detail == "full":
                    updated_records.append(finding_to_mcp(record))
            except KeyError as e:
                errors.append(BatchFailure(id=fid, error=str(e), code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                errors.append(BatchFailure(id=fid, error=str(e), code=ErrorCode.VALIDATION))
            except sqlite3.Error as e:
                errors.append(BatchFailure(id=fid, error=f"Database error: {e}", code=ErrorCode.IO))

        # Mirror MCP: all-failed → ErrorResponse. Derive the envelope code
        # from per-item codes so callers can apply the right retry policy:
        # IO wins (it's retryable); else a homogeneous code is preserved;
        # else fall back to VALIDATION for genuinely mixed failures.
        if not updated_ids and errors:
            err_codes = {f["code"] for f in errors}
            if ErrorCode.IO in err_codes:
                envelope_code = ErrorCode.IO
            elif len(err_codes) == 1:
                envelope_code = next(iter(err_codes))
            else:
                envelope_code = ErrorCode.VALIDATION
            if as_json:
                click.echo(
                    json_mod.dumps(
                        {
                            "error": f"All {len(errors)} finding update(s) failed",
                            "code": envelope_code,
                        }
                    )
                )
            else:
                click.echo(f"Error: All {len(errors)} finding update(s) failed", err=True)
                for f_item in errors:
                    click.echo(f"  {f_item['id']}: {f_item['error']}", err=True)
            sys.exit(1)

        if as_json:
            succeeded_payload: list[Any] = updated_records if response_detail == "full" else updated_ids
            click.echo(
                json_mod.dumps(
                    {"succeeded": succeeded_payload, "failed": list(errors)},
                    indent=2,
                    default=str,
                )
            )
        else:
            for fid in updated_ids:
                click.echo(f"  Updated {fid}")
            for f_item in errors:
                click.echo(f"  Error {f_item['id']}: {f_item['error']}", err=True)
            click.echo(f"Updated {len(updated_ids)}/{len(raw_ids)} findings")

        if updated_ids:
            refresh_summary(db)

        if errors:
            sys.exit(1)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(cli: click.Group) -> None:
    """Register file and finding commands with the CLI group."""
    cli.add_command(list_files_cmd)
    cli.add_command(get_file_cmd)
    cli.add_command(get_file_timeline_cmd)
    cli.add_command(get_issue_files_cmd)
    cli.add_command(add_file_association_cmd)
    cli.add_command(register_file_cmd)
    cli.add_command(migrate_registry_cmd)
    cli.add_command(delete_file_record_cmd)
    cli.add_command(list_findings_cmd)
    cli.add_command(get_finding_cmd)
    cli.add_command(update_finding_cmd)
    cli.add_command(promote_finding_cmd)
    cli.add_command(dismiss_finding_cmd)
    cli.add_command(batch_update_findings_cmd)
