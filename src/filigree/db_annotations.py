"""AnnotationsMixin — durable shared file annotations with provenance."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args

from filigree.db_base import DBMixinProtocol, _begin_immediate, _now_iso
from filigree.db_files import _normalize_scan_path
from filigree.types.api import ErrorCode
from filigree.types.core import (
    AnnotationAnchorState,
    AnnotationIntent,
    AnnotationRelationship,
    AnnotationStatus,
    AnnotationTargetType,
)

logger = logging.getLogger(__name__)

MAX_SNIPPET_BYTES = 8 * 1024
MAX_FILE_DIFF_BYTES = 64 * 1024
MAX_WORKTREE_SUMMARY_BYTES = 32 * 1024
LARGE_FILE_BYTES = 1024 * 1024

VALID_ANNOTATION_INTENTS = frozenset(get_args(AnnotationIntent))
VALID_ANNOTATION_STATUSES = frozenset(get_args(AnnotationStatus))
VALID_ANNOTATION_TARGET_TYPES = frozenset(get_args(AnnotationTargetType))
VALID_ANNOTATION_RELATIONSHIPS = frozenset(get_args(AnnotationRelationship))

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^([+-]?\s*[^#\n]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)[^=\n:]*\s*(?:=|:)\s*).*$"
)


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _cap_text(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _redact_secrets(value: str) -> tuple[str, bool]:
    redacted = _SECRET_ASSIGNMENT_RE.sub("[REDACTED_SECRET_ASSIGNMENT]", value)
    return redacted, redacted != value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_generated_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = Path(normalized).name
    return normalized.startswith("docs/bugs/generated/") or "/generated/" in normalized or ".generated." in name


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _logical_line_count(snippet: str) -> int:
    if not snippet:
        return 0
    return len(snippet.splitlines()) or 1


class AnnotationsMixin(DBMixinProtocol):
    """Shared file annotation CRUD, provenance, links, and closeout warnings."""

    def _project_root_for_annotations(self) -> Path:
        if self.project_root is not None:
            return self.project_root.resolve()
        if self.db_path.parent.name == ".filigree":
            return self.db_path.parent.parent.resolve()
        return self.db_path.parent.resolve()

    def _resolve_annotation_file_path(self, file_path: str) -> tuple[str, Path]:
        if not isinstance(file_path, str) or not file_path.strip():
            msg = "file_path is required"
            raise ValueError(msg)
        normalized = _normalize_scan_path(file_path.strip())
        if not normalized:
            msg = "file_path cannot be empty after normalization"
            raise ValueError(msg)
        if Path(normalized).is_absolute():
            msg = "file_path must be project-relative"
            raise ValueError(msg)
        root = self._project_root_for_annotations()
        resolved = (root / normalized).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            msg = f"file_path escapes project root: {file_path!r}"
            raise ValueError(msg) from exc
        if not resolved.exists() or not resolved.is_file():
            msg = f"File not found: {normalized}"
            raise ValueError(msg)
        return normalized, resolved

    def _resolve_stored_annotation_file_path(self, file_path: Any) -> Path | None:
        if not isinstance(file_path, str) or not file_path.strip():
            return None
        normalized = _normalize_scan_path(file_path.strip())
        if not normalized or Path(normalized).is_absolute():
            return None
        root = self._project_root_for_annotations()
        resolved = (root / normalized).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    @staticmethod
    def _validate_annotation_line_range(line_start: int | None, line_end: int | None) -> tuple[int | None, int | None]:
        if line_start is None and line_end is not None:
            msg = "line_start is required when line_end is provided"
            raise ValueError(msg)
        if line_start is not None:
            if isinstance(line_start, bool) or not isinstance(line_start, int):
                msg = "line_start must be an integer"
                raise ValueError(msg)
            if line_start < 1:
                msg = "line_start must be >= 1"
                raise ValueError(msg)
        if line_end is not None and (isinstance(line_end, bool) or not isinstance(line_end, int)):
            msg = "line_end must be an integer"
            raise ValueError(msg)
        if line_start is not None and line_end is None:
            line_end = line_start
        if line_start is not None and line_end is not None and line_end < line_start:
            msg = "line_end must be >= line_start"
            raise ValueError(msg)
        return line_start, line_end

    @staticmethod
    def _read_annotation_text(data: bytes) -> tuple[str, bool]:
        if b"\0" in data[:4096]:
            return "", True
        try:
            return data.decode("utf-8"), False
        except UnicodeDecodeError:
            return "", True

    @staticmethod
    def _slice_anchor(
        text: str,
        *,
        line_start: int | None,
        line_end: int | None,
    ) -> tuple[str, str, str]:
        if line_start is None:
            return "", "", ""
        lines = text.splitlines(keepends=True)
        if line_start > len(lines):
            msg = f"line_start exceeds file length ({len(lines)} line(s))"
            raise ValueError(msg)
        if line_end is None:
            msg = "line_end is required when line_start is provided"
            raise ValueError(msg)
        if line_end > len(lines):
            msg = f"line_end exceeds file length ({len(lines)} line(s))"
            raise ValueError(msg)
        end = min(line_end, len(lines))
        snippet = "".join(lines[line_start - 1 : end])
        before = "".join(lines[max(0, line_start - 3) : line_start - 1])
        after = "".join(lines[end : min(len(lines), end + 2)])
        return snippet, before, after

    def _run_git(self, args: list[str], *, cwd: Path) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(cwd), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, ""
        return proc.returncode == 0, proc.stdout.strip()

    def _capture_annotation_provenance(
        self,
        *,
        file_path: str,
        absolute_path: Path,
        line_start: int | None,
        line_end: int | None,
    ) -> tuple[str, dict[str, Any]]:
        data = absolute_path.read_bytes()
        checksum = _sha256(data)
        stat = absolute_path.stat()
        file_mtime = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
        text, is_binary = self._read_annotation_text(data)

        flags: list[str] = []
        warnings: list[str] = []
        trust_level = "complete"
        if is_binary:
            flags.append("binary_file")
            warnings.append("binary file: snippet and raw diff omitted")
            trust_level = "partial"
        if stat.st_size > LARGE_FILE_BYTES:
            flags.append("oversized_file")
            warnings.append("large file: captured context may be capped")
        if _is_generated_path(file_path):
            flags.append("generated_file")

        snippet = ""
        before = ""
        after = ""
        if not is_binary:
            snippet, before, after = self._slice_anchor(text, line_start=line_start, line_end=line_end)
            snippet, snippet_capped = _cap_text(snippet, MAX_SNIPPET_BYTES)
            if snippet_capped:
                warnings.append("anchor snippet capped at 8 KiB")
            before, _ = _cap_text(before, MAX_SNIPPET_BYTES)
            after, _ = _cap_text(after, MAX_SNIPPET_BYTES)

        root = self._project_root_for_annotations()
        repo_ok, repo_root = self._run_git(["rev-parse", "--show-toplevel"], cwd=root)
        commit_ref = ""
        branch = ""
        git_state = "missing_git_metadata"
        worktree_dirty = False
        dirty_diff_hash = ""
        dirty_diff_summary = ""
        file_diff = ""
        worktree_summary = ""

        if not repo_ok:
            flags.extend(["missing_git_metadata", "commit_unavailable"])
            warnings.append("git metadata unavailable; provenance is partial")
            trust_level = "minimal"
            repo_root = ""
        else:
            commit_ok, commit_ref = self._run_git(["rev-parse", "HEAD"], cwd=root)
            if not commit_ok:
                commit_ref = ""
                flags.append("commit_unavailable")
                warnings.append("commit unavailable")
                trust_level = "partial"
            branch_ok, branch_out = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
            if branch_ok:
                if branch_out == "HEAD":
                    flags.append("detached_head")
                    branch = ""
                else:
                    branch = branch_out
            tracked_ok, _ = self._run_git(["ls-files", "--error-unmatch", "--", file_path], cwd=root)
            status_ok, file_status = self._run_git(["status", "--porcelain", "--", file_path], cwd=root)
            worktree_ok, worktree_status = self._run_git(["status", "--porcelain"], cwd=root)
            worktree_dirty = bool(worktree_ok and worktree_status)
            if worktree_dirty:
                flags.append("dirty_worktree")
            if not tracked_ok:
                flags.append("untracked_file")
                git_state = "untracked"
                trust_level = "partial"
            elif status_ok and file_status:
                git_state = "dirty"
            else:
                git_state = "clean"

            if not is_binary and tracked_ok:
                diff_parts: list[str] = []
                diff_ok, diff_out = self._run_git(["diff", "--", file_path], cwd=root)
                if diff_ok and diff_out:
                    diff_parts.append(diff_out)
                cached_diff_ok, cached_diff_out = self._run_git(["diff", "--cached", "--", file_path], cwd=root)
                if cached_diff_ok and cached_diff_out:
                    diff_parts.append(cached_diff_out)
                if diff_parts:
                    diff_out = "\n".join(diff_parts)
                    dirty_diff_hash = hashlib.sha256(diff_out.encode("utf-8")).hexdigest()
                    file_diff, redacted = _redact_secrets(diff_out)
                    if redacted:
                        flags.append("redacted")
                        warnings.append("secret-like assignment redacted from file diff")
                    file_diff, capped = _cap_text(file_diff, MAX_FILE_DIFF_BYTES)
                    if capped:
                        flags.append("oversized_diff")
                        warnings.append("file diff capped at 64 KiB")
                        dirty_diff_summary = f"file diff exceeded {MAX_FILE_DIFF_BYTES} bytes"
                        file_diff = ""
            if worktree_dirty:
                summary_ok, summary_out = self._run_git(["status", "--short"], cwd=root)
                if summary_ok:
                    worktree_summary, redacted = _redact_secrets(summary_out)
                    if redacted and "redacted" not in flags:
                        flags.append("redacted")
                        warnings.append("secret-like assignment redacted from worktree summary")
                    worktree_summary, capped = _cap_text(worktree_summary, MAX_WORKTREE_SUMMARY_BYTES)
                    if capped:
                        if "oversized_diff" not in flags:
                            flags.append("oversized_diff")
                        warnings.append("worktree summary capped at 32 KiB")

        provenance = {
            "commit_ref": commit_ref,
            "branch": branch,
            "repo_root": repo_root,
            "worktree_root": str(root),
            "git_state": git_state,
            "worktree_dirty": worktree_dirty,
            "file_checksum": checksum,
            "file_size": stat.st_size,
            "file_mtime": file_mtime,
            "dirty_diff_hash": dirty_diff_hash,
            "dirty_diff_summary": dirty_diff_summary,
            "file_diff": file_diff,
            "worktree_diff_summary": worktree_summary,
            "anchor_context_before": before,
            "anchor_context_after": after,
            "provenance_trust_level": trust_level,
            "provenance_flags": sorted(set(flags)),
            "provenance_warnings": warnings,
        }
        return snippet, provenance

    def _validate_annotation_values(
        self,
        *,
        intent: str,
        status: str = "active",
        critical: bool | None = None,
    ) -> None:
        if intent not in VALID_ANNOTATION_INTENTS:
            msg = f"intent must be one of: {', '.join(sorted(VALID_ANNOTATION_INTENTS))}"
            raise ValueError(msg)
        if status not in VALID_ANNOTATION_STATUSES:
            msg = f"status must be one of: {', '.join(sorted(VALID_ANNOTATION_STATUSES))}"
            raise ValueError(msg)
        if critical is not None and not isinstance(critical, bool):
            msg = "critical must be a boolean"
            raise ValueError(msg)

    def _get_annotation_row(self, annotation_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        if row is None:
            raise KeyError(annotation_id)
        return cast(sqlite3.Row, row)

    def _get_annotation_provenance_row(self, annotation_id: str) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM annotation_provenance WHERE annotation_id = ?", (annotation_id,)).fetchone()
        return None if row is None else cast(sqlite3.Row, row)

    def _annotation_commit_available(self, commit_ref: str) -> bool:
        if not commit_ref:
            return False
        ok, _ = self._run_git(["cat-file", "-e", f"{commit_ref}^{{commit}}"], cwd=self._project_root_for_annotations())
        return ok

    def _compute_annotation_anchor_state(
        self,
        annotation: sqlite3.Row,
        provenance: sqlite3.Row | None,
    ) -> dict[str, Any]:
        file_path = annotation["file_path"]
        commit_ref = provenance["commit_ref"] if provenance is not None else ""
        base = {
            "anchor_state": "file_missing",
            "anchor_match_confidence": 0.0,
            "anchor_match_count": 0,
            "current_line_start": None,
            "current_line_end": None,
            "commit_available": self._annotation_commit_available(commit_ref),
        }
        absolute_path = self._resolve_stored_annotation_file_path(file_path)
        if absolute_path is None:
            return base
        if not absolute_path.exists() or not absolute_path.is_file():
            return base
        data = absolute_path.read_bytes()
        current_checksum = _sha256(data)
        stored_checksum = provenance["file_checksum"] if provenance is not None else ""
        original_start = annotation["line_start"]
        original_end = annotation["line_end"]
        if stored_checksum and current_checksum == stored_checksum:
            base.update(
                {
                    "anchor_state": "current",
                    "anchor_match_confidence": 1.0,
                    "anchor_match_count": 1 if annotation["anchor_snippet"] else 0,
                    "current_line_start": original_start,
                    "current_line_end": original_end,
                }
            )
            return base
        text, is_binary = self._read_annotation_text(data)
        snippet = annotation["anchor_snippet"] or ""
        if is_binary or not snippet:
            base["anchor_state"] = "stale"
            return base
        if original_start is not None and original_end is not None:
            lines = text.splitlines(keepends=True)
            if original_start <= len(lines):
                candidate = "".join(lines[original_start - 1 : min(original_end, len(lines))])
                if candidate == snippet:
                    line_count = _logical_line_count(snippet)
                    base.update(
                        {
                            "anchor_state": "content_changed_anchor_found",
                            "anchor_match_confidence": 0.85,
                            "anchor_match_count": 1,
                            "current_line_start": original_start,
                            "current_line_end": original_start + line_count - 1,
                        }
                    )
                    return base
        offsets: list[int] = []
        cursor = 0
        while True:
            found = text.find(snippet, cursor)
            if found == -1:
                break
            offsets.append(found)
            cursor = found + max(len(snippet), 1)
        if len(offsets) == 1:
            line = _line_for_offset(text, offsets[0])
            line_count = _logical_line_count(snippet)
            base.update(
                {
                    "anchor_state": "line_drifted",
                    "anchor_match_confidence": 0.8,
                    "anchor_match_count": 1,
                    "current_line_start": line,
                    "current_line_end": line + line_count - 1,
                }
            )
            return base
        base.update({"anchor_state": "stale", "anchor_match_count": len(offsets)})
        return base

    def _annotation_links(self, annotation_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM annotation_links WHERE annotation_id = ? ORDER BY created_at, id",
            (annotation_id,),
        ).fetchall()
        return [
            {
                "annotation_link_id": row["id"],
                "annotation_id": row["annotation_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "relationship": row["relationship"],
                "actor": row["actor"] or "",
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _annotation_events(self, annotation_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM annotation_events WHERE annotation_id = ? ORDER BY created_at, id",
            (annotation_id,),
        ).fetchall()
        return [
            {
                "annotation_event_id": row["id"],
                "annotation_id": row["annotation_id"],
                "event_type": row["event_type"],
                "actor": row["actor"] or "",
                "reason": row["reason"] or "",
                "old_value": row["old_value"],
                "new_value": row["new_value"],
                "target_type": row["target_type"] or "",
                "target_id": row["target_id"] or "",
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _annotation_provenance_payload(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        return {
            "commit_ref": row["commit_ref"] or "",
            "branch": row["branch"] or "",
            "repo_root": row["repo_root"] or "",
            "worktree_root": row["worktree_root"] or "",
            "git_state": row["git_state"] or "",
            "worktree_dirty": bool(row["worktree_dirty"]),
            "file_checksum": row["file_checksum"] or "",
            "file_size": row["file_size"] or 0,
            "file_mtime": row["file_mtime"] or "",
            "dirty_diff_hash": row["dirty_diff_hash"] or "",
            "dirty_diff_summary": row["dirty_diff_summary"] or "",
            "file_diff": row["file_diff"] or "",
            "worktree_diff_summary": row["worktree_diff_summary"] or "",
            "anchor_context_before": row["anchor_context_before"] or "",
            "anchor_context_after": row["anchor_context_after"] or "",
            "provenance_trust_level": row["provenance_trust_level"] or "minimal",
            "provenance_flags": _json_list(row["provenance_flags"]),
            "provenance_warnings": _json_list(row["provenance_warnings"]),
        }

    def _build_annotation_payload(self, row: sqlite3.Row, *, response_detail: str = "summary") -> dict[str, Any]:
        provenance_row = self._get_annotation_provenance_row(row["id"])
        drift = self._compute_annotation_anchor_state(row, provenance_row)
        payload: dict[str, Any] = {
            "annotation_id": row["id"],
            "file_id": row["file_id"],
            "file_path": row["file_path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "anchor_snippet": row["anchor_snippet"] or "",
            "note": row["note"],
            "context_summary": row["context_summary"] or "",
            "intent": row["intent"],
            "critical": bool(row["critical"]),
            "status": row["status"],
            "actor": row["actor"] or "",
            "session_ref": row["session_ref"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
            **drift,
        }
        if response_detail == "full":
            payload["provenance"] = self._annotation_provenance_payload(provenance_row)
            payload["links"] = self._annotation_links(row["id"])
            payload["events"] = self._annotation_events(row["id"])
        return payload

    def _record_annotation_event(
        self,
        annotation_id: str,
        event_type: str,
        *,
        actor: str = "",
        reason: str = "",
        old_value: Any = None,
        new_value: Any = None,
        target_type: str = "",
        target_id: str = "",
    ) -> dict[str, Any]:
        event_id = self._generate_unique_id("annotation_events", "annevent")
        now = _now_iso()
        old_text = None if old_value is None else json.dumps(old_value, default=str)
        new_text = None if new_value is None else json.dumps(new_value, default=str)
        self.conn.execute(
            "INSERT INTO annotation_events "
            "(id, annotation_id, event_type, actor, reason, old_value, new_value, target_type, target_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, annotation_id, event_type, actor, reason, old_text, new_text, target_type, target_id, now),
        )
        return {
            "annotation_event_id": event_id,
            "annotation_id": annotation_id,
            "event_type": event_type,
            "actor": actor,
            "reason": reason,
            "old_value": old_text,
            "new_value": new_text,
            "target_type": target_type,
            "target_id": target_id,
            "created_at": now,
        }

    def _validate_annotation_link_target(self, target_type: str, target_id: str) -> None:
        if target_type not in VALID_ANNOTATION_TARGET_TYPES:
            msg = f"target_type must be one of: {', '.join(sorted(VALID_ANNOTATION_TARGET_TYPES))}"
            raise ValueError(msg)
        if not isinstance(target_id, str) or not target_id.strip():
            msg = "target_id is required"
            raise ValueError(msg)
        if target_type == "issue":
            self._check_id_prefix(target_id)
            self.get_issue(target_id)
        elif target_type == "file":
            self.get_file(target_id)
        elif target_type == "finding":
            self.get_finding(target_id)
        elif target_type == "observation":
            row = self.conn.execute("SELECT 1 FROM observations WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise KeyError(target_id)

    def _insert_annotation_link(
        self,
        annotation_id: str,
        *,
        target_type: str,
        target_id: str,
        relationship: str,
        actor: str = "",
    ) -> dict[str, Any]:
        self._get_annotation_row(annotation_id)
        self._validate_annotation_link_target(target_type, target_id)
        if relationship not in VALID_ANNOTATION_RELATIONSHIPS:
            msg = f"relationship must be one of: {', '.join(sorted(VALID_ANNOTATION_RELATIONSHIPS))}"
            raise ValueError(msg)
        now = _now_iso()
        link_id = self._generate_unique_id("annotation_links", "annlink")
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO annotation_links "
            "(id, annotation_id, target_type, target_id, relationship, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (link_id, annotation_id, target_type, target_id, relationship, actor, now),
        )
        if cursor.rowcount == 0:
            row = self.conn.execute(
                "SELECT * FROM annotation_links WHERE annotation_id = ? AND target_type = ? AND target_id = ? AND relationship = ?",
                (annotation_id, target_type, target_id, relationship),
            ).fetchone()
            if row is None:
                msg = "annotation link insert failed and existing link was not found"
                raise RuntimeError(msg)
            link_id = row["id"]
            now = row["created_at"]
            actor = row["actor"] or actor
        return {
            "annotation_link_id": link_id,
            "annotation_id": annotation_id,
            "target_type": target_type,
            "target_id": target_id,
            "relationship": relationship,
            "actor": actor,
            "created_at": now,
        }

    def annotate_file(
        self,
        file_path: str,
        note: str,
        *,
        line_start: int | None = None,
        line_end: int | None = None,
        context_summary: str = "",
        intent: str = "breadcrumb",
        critical: bool = False,
        links: list[dict[str, str]] | None = None,
        actor: str = "",
        session_ref: str = "",
    ) -> dict[str, Any]:
        if not isinstance(note, str) or not note.strip():
            msg = "note cannot be empty"
            raise ValueError(msg)
        self._validate_annotation_values(intent=intent, critical=critical)
        line_start, line_end = self._validate_annotation_line_range(line_start, line_end)
        normalized, absolute_path = self._resolve_annotation_file_path(file_path)
        for link in links or []:
            self._validate_annotation_link_target(link.get("target_type", ""), link.get("target_id", ""))
            relationship = link.get("relationship", "")
            if relationship not in VALID_ANNOTATION_RELATIONSHIPS:
                msg = f"relationship must be one of: {', '.join(sorted(VALID_ANNOTATION_RELATIONSHIPS))}"
                raise ValueError(msg)

        annotation_id = self._generate_unique_id("annotations", "ann")
        now = _now_iso()
        anchor_snippet, provenance = self._capture_annotation_provenance(
            file_path=normalized,
            absolute_path=absolute_path,
            line_start=line_start,
            line_end=line_end,
        )
        file_record = self.register_file(normalized)
        try:
            self.conn.execute(
                "INSERT INTO annotations "
                "(id, file_id, file_path, line_start, line_end, anchor_snippet, note, context_summary, "
                "intent, critical, status, actor, session_ref, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    annotation_id,
                    file_record.id,
                    normalized,
                    line_start,
                    line_end,
                    anchor_snippet,
                    note.strip(),
                    context_summary,
                    intent,
                    int(critical),
                    actor,
                    session_ref,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO annotation_provenance "
                "(annotation_id, commit_ref, branch, repo_root, worktree_root, git_state, worktree_dirty, "
                "file_checksum, file_size, file_mtime, dirty_diff_hash, dirty_diff_summary, file_diff, "
                "worktree_diff_summary, anchor_context_before, anchor_context_after, provenance_trust_level, "
                "provenance_flags, provenance_warnings) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    annotation_id,
                    provenance["commit_ref"],
                    provenance["branch"],
                    provenance["repo_root"],
                    provenance["worktree_root"],
                    provenance["git_state"],
                    int(provenance["worktree_dirty"]),
                    provenance["file_checksum"],
                    provenance["file_size"],
                    provenance["file_mtime"],
                    provenance["dirty_diff_hash"],
                    provenance["dirty_diff_summary"],
                    provenance["file_diff"],
                    provenance["worktree_diff_summary"],
                    provenance["anchor_context_before"],
                    provenance["anchor_context_after"],
                    provenance["provenance_trust_level"],
                    json.dumps(provenance["provenance_flags"]),
                    json.dumps(provenance["provenance_warnings"]),
                ),
            )
            self._record_annotation_event(annotation_id, "created", actor=actor)
            for link in links or []:
                self._insert_annotation_link(
                    annotation_id,
                    target_type=link["target_type"],
                    target_id=link["target_id"],
                    relationship=link["relationship"],
                    actor=actor,
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_annotation(annotation_id)

    def get_annotation(self, annotation_id: str, *, response_detail: str = "full") -> dict[str, Any]:
        if response_detail not in {"summary", "full"}:
            msg = "response_detail must be 'summary' or 'full'"
            raise ValueError(msg)
        return self._build_annotation_payload(self._get_annotation_row(annotation_id), response_detail=response_detail)

    def list_annotations(
        self,
        *,
        file_path: str | None = None,
        file_id: str | None = None,
        issue_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        actor: str | None = None,
        intent: str | None = None,
        critical: bool | None = None,
        status: str | None = None,
        anchor_state: str | None = None,
        relationship: str | None = None,
        response_detail: str = "summary",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if response_detail not in {"summary", "full"}:
            msg = "response_detail must be 'summary' or 'full'"
            raise ValueError(msg)
        if anchor_state is not None and anchor_state not in get_args(AnnotationAnchorState):
            msg = "anchor_state must be one of: " + ", ".join(sorted(get_args(AnnotationAnchorState)))
            raise ValueError(msg)
        if intent is not None and intent not in VALID_ANNOTATION_INTENTS:
            msg = f"intent must be one of: {', '.join(sorted(VALID_ANNOTATION_INTENTS))}"
            raise ValueError(msg)
        if status is not None and status not in VALID_ANNOTATION_STATUSES:
            msg = f"status must be one of: {', '.join(sorted(VALID_ANNOTATION_STATUSES))}"
            raise ValueError(msg)
        if critical is not None and not isinstance(critical, bool):
            msg = "critical must be a boolean"
            raise ValueError(msg)
        if issue_id is not None:
            self._check_id_prefix(issue_id)
            self.get_issue(issue_id)
            target_type = "issue"
            target_id = issue_id
        if target_type is not None:
            if target_id is None:
                msg = "target_id is required when target_type is provided"
                raise ValueError(msg)
            self._validate_annotation_link_target(target_type, target_id)
        elif target_id is not None:
            msg = "target_type is required when target_id is provided"
            raise ValueError(msg)
        if relationship is not None and relationship not in VALID_ANNOTATION_RELATIONSHIPS:
            msg = f"relationship must be one of: {', '.join(sorted(VALID_ANNOTATION_RELATIONSHIPS))}"
            raise ValueError(msg)
        if isinstance(limit, bool) or limit < 1:
            msg = "limit must be >= 1"
            raise ValueError(msg)
        if isinstance(offset, bool) or offset < 0:
            msg = "offset must be >= 0"
            raise ValueError(msg)

        clauses: list[str] = []
        params: list[Any] = []
        join = ""
        if file_path is not None:
            normalized = _normalize_scan_path(file_path)
            clauses.append("a.file_path = ?")
            params.append(normalized)
        if file_id is not None:
            clauses.append("a.file_id = ?")
            params.append(file_id)
        if actor is not None:
            clauses.append("a.actor = ?")
            params.append(actor)
        if intent is not None:
            clauses.append("a.intent = ?")
            params.append(intent)
        if critical is not None:
            clauses.append("a.critical = ?")
            params.append(int(critical))
        if status is not None:
            clauses.append("a.status = ?")
            params.append(status)
        if target_type is not None and target_id is not None:
            join = "JOIN annotation_links l ON l.annotation_id = a.id"
            clauses.append("l.target_type = ?")
            params.append(target_type)
            clauses.append("l.target_id = ?")
            params.append(target_id)
        if relationship is not None:
            join = "JOIN annotation_links l ON l.annotation_id = a.id"
            clauses.append("l.relationship = ?")
            params.append(relationship)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT a.* FROM annotations a
            {join}
            {where}
            ORDER BY a.critical DESC,
                     CASE a.status WHEN 'active' THEN 0 ELSE 1 END,
                     a.created_at DESC,
                     a.id ASC
            """,
            params,
        ).fetchall()
        payloads = [self._build_annotation_payload(row, response_detail=response_detail) for row in rows]
        if anchor_state is not None:
            payloads = [item for item in payloads if item["anchor_state"] == anchor_state]
        page = payloads[offset : offset + limit]
        has_more = offset + limit < len(payloads)
        result: dict[str, Any] = {"items": page, "has_more": has_more}
        if has_more:
            result["next_offset"] = offset + len(page)
        return result

    def get_file_annotations(
        self,
        file_path: str,
        *,
        response_detail: str = "summary",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.list_annotations(file_path=file_path, response_detail=response_detail, limit=limit, offset=offset)

    def get_issue_annotations(
        self,
        issue_id: str,
        *,
        response_detail: str = "summary",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.list_annotations(issue_id=issue_id, response_detail=response_detail, limit=limit, offset=offset)

    def list_attention_annotations(
        self,
        *,
        target_id: str | None = None,
        file_path: str | None = None,
        critical: bool = True,
        status: str = "active",
        response_detail: str = "summary",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if target_id is not None:
            return self.list_annotations(
                target_type="issue",
                target_id=target_id,
                file_path=file_path,
                critical=critical,
                status=status,
                relationship="must_consider",
                response_detail=response_detail,
                limit=limit,
                offset=offset,
            )
        return self.list_annotations(
            file_path=file_path,
            critical=critical,
            status=status,
            response_detail=response_detail,
            limit=limit,
            offset=offset,
        )

    def update_annotation(
        self,
        annotation_id: str,
        *,
        note: str | None = None,
        context_summary: str | None = None,
        intent: str | None = None,
        critical: bool | None = None,
        status: str | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        row = self._get_annotation_row(annotation_id)
        new_intent = intent if intent is not None else row["intent"]
        new_status = status if status is not None else row["status"]
        self._validate_annotation_values(intent=new_intent, status=new_status, critical=critical)
        updates: list[str] = []
        params: list[Any] = []
        old_values: dict[str, Any] = {}
        new_values: dict[str, Any] = {}
        if note is not None:
            if not note.strip():
                msg = "note cannot be empty"
                raise ValueError(msg)
            updates.append("note = ?")
            params.append(note.strip())
            old_values["note"] = row["note"]
            new_values["note"] = note.strip()
        if context_summary is not None:
            updates.append("context_summary = ?")
            params.append(context_summary)
            old_values["context_summary"] = row["context_summary"] or ""
            new_values["context_summary"] = context_summary
        if intent is not None:
            updates.append("intent = ?")
            params.append(intent)
            old_values["intent"] = row["intent"]
            new_values["intent"] = intent
        if critical is not None:
            updates.append("critical = ?")
            params.append(int(critical))
            old_values["critical"] = bool(row["critical"])
            new_values["critical"] = critical
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            old_values["status"] = row["status"]
            new_values["status"] = status
            if status in {"resolved", "superseded", "promoted"}:
                updates.append("resolved_at = ?")
                params.append(_now_iso())
            elif status == "active":
                updates.append("resolved_at = NULL")
        if not updates:
            return self.get_annotation(annotation_id)
        updates.append("updated_at = ?")
        params.append(_now_iso())
        params.append(annotation_id)
        try:
            self.conn.execute(f"UPDATE annotations SET {', '.join(updates)} WHERE id = ?", params)
            self._record_annotation_event(annotation_id, "updated", actor=actor, old_value=old_values, new_value=new_values)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_annotation(annotation_id)

    def resolve_annotation(self, annotation_id: str, *, reason: str = "", actor: str = "") -> dict[str, Any]:
        self._get_annotation_row(annotation_id)
        now = _now_iso()
        try:
            self.conn.execute(
                "UPDATE annotations SET status = 'resolved', resolved_at = ?, updated_at = ? WHERE id = ?",
                (now, now, annotation_id),
            )
            self._record_annotation_event(annotation_id, "resolved", actor=actor, reason=reason)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_annotation(annotation_id)

    def supersede_annotation(
        self,
        annotation_id: str,
        *,
        replacement_annotation_id: str,
        reason: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        if annotation_id == replacement_annotation_id:
            msg = "replacement_annotation_id must be different from annotation_id"
            raise ValueError(msg)
        self._get_annotation_row(annotation_id)
        self._get_annotation_row(replacement_annotation_id)
        now = _now_iso()
        try:
            self.conn.execute(
                "UPDATE annotations SET status = 'superseded', resolved_at = ?, updated_at = ? WHERE id = ?",
                (now, now, annotation_id),
            )
            self._record_annotation_event(
                annotation_id,
                "superseded",
                actor=actor,
                reason=reason,
                target_type="annotation",
                target_id=replacement_annotation_id,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_annotation(annotation_id)

    def promote_annotation(
        self,
        annotation_id: str,
        *,
        target_type: str = "issue",
        title: str | None = None,
        reason: str = "",
        keep_active: bool = True,
        actor: str = "",
    ) -> dict[str, Any]:
        annotation = self.get_annotation(annotation_id)
        if target_type not in {"issue", "observation"}:
            msg = "target_type must be 'issue' or 'observation'"
            raise ValueError(msg)
        default_title = (title or annotation["note"].splitlines()[0]).strip()[:120] or "Promoted annotation"
        _begin_immediate(self.conn, "promote_annotation")
        try:
            if target_type == "issue":
                issue = self.create_issue(
                    default_title,
                    description=annotation["note"],
                    notes=reason,
                    labels=["from-annotation"],
                    actor=actor,
                    _skip_begin=True,
                )
                target_id = issue.id
            else:
                obs = self.create_observation(
                    default_title,
                    detail=annotation["note"],
                    file_path=annotation["file_path"],
                    line=annotation["line_start"],
                    actor=actor,
                    auto_commit=False,
                )
                target_id = obs["id"]
            link = self._insert_annotation_link(
                annotation_id,
                target_type=target_type,
                target_id=target_id,
                relationship="promoted_to",
                actor=actor,
            )
            if not keep_active:
                now = _now_iso()
                self.conn.execute(
                    "UPDATE annotations SET status = 'promoted', resolved_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, annotation_id),
                )
            self._record_annotation_event(
                annotation_id,
                "promoted",
                actor=actor,
                reason=reason,
                target_type=target_type,
                target_id=target_id,
            )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
        return {
            "annotation": self.get_annotation(annotation_id),
            "target_type": target_type,
            "target_id": target_id,
            "link": link,
        }

    def carry_forward_annotation(
        self,
        annotation_id: str,
        *,
        from_target_id: str,
        to_target_id: str,
        reason: str,
        actor: str = "",
    ) -> dict[str, Any]:
        if not reason or not reason.strip():
            msg = "reason is required"
            raise ValueError(msg)
        self._get_annotation_row(annotation_id)
        self._validate_annotation_link_target("issue", from_target_id)
        self._validate_annotation_link_target("issue", to_target_id)
        source_link = self.conn.execute(
            """
            SELECT 1
            FROM annotation_links l
            WHERE l.annotation_id = ?
              AND l.target_type = 'issue'
              AND l.target_id = ?
              AND l.relationship = 'must_consider'
            LIMIT 1
            """,
            (annotation_id, from_target_id),
        ).fetchone()
        if source_link is None:
            msg = f"Annotation {annotation_id} is not actively linked to issue {from_target_id} with relationship must_consider"
            raise ValueError(msg)
        now = _now_iso()
        try:
            link = self._insert_annotation_link(
                annotation_id,
                target_type="issue",
                target_id=to_target_id,
                relationship="must_consider",
                actor=actor,
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO annotation_closeout_acknowledgements "
                "(annotation_id, target_type, target_id, carried_to_target_id, actor, reason, acknowledged_at) "
                "VALUES (?, 'issue', ?, ?, ?, ?, ?)",
                (annotation_id, from_target_id, to_target_id, actor, reason, now),
            )
            self._record_annotation_event(
                annotation_id,
                "carried_forward",
                actor=actor,
                reason=reason,
                target_type="issue",
                target_id=to_target_id,
                old_value={"from_target_id": from_target_id},
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"annotation": self.get_annotation(annotation_id), "link": link, "acknowledged_target_id": from_target_id}

    def link_annotation(
        self,
        annotation_id: str,
        *,
        target_type: str,
        target_id: str,
        relationship: str,
        actor: str = "",
    ) -> dict[str, Any]:
        try:
            link = self._insert_annotation_link(
                annotation_id,
                target_type=target_type,
                target_id=target_id,
                relationship=relationship,
                actor=actor,
            )
            self._record_annotation_event(
                annotation_id,
                "linked",
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                new_value={"relationship": relationship},
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return link

    def unlink_annotation(
        self,
        annotation_id: str,
        *,
        target_type: str,
        target_id: str,
        relationship: str | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        self._get_annotation_row(annotation_id)
        self._validate_annotation_link_target(target_type, target_id)
        clauses = ["annotation_id = ?", "target_type = ?", "target_id = ?"]
        params: list[Any] = [annotation_id, target_type, target_id]
        if relationship is not None:
            if relationship not in VALID_ANNOTATION_RELATIONSHIPS:
                msg = f"relationship must be one of: {', '.join(sorted(VALID_ANNOTATION_RELATIONSHIPS))}"
                raise ValueError(msg)
            clauses.append("relationship = ?")
            params.append(relationship)
        try:
            cursor = self.conn.execute(f"DELETE FROM annotation_links WHERE {' AND '.join(clauses)}", params)
            self._record_annotation_event(
                annotation_id,
                "unlinked",
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                old_value={"relationship": relationship},
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"status": "unlinked", "deleted": cursor.rowcount}

    def get_annotation_closeout_warnings(self, issue_id: str) -> list[dict[str, Any]]:
        self._check_id_prefix(issue_id)
        rows = self.conn.execute(
            """
            SELECT a.*,
                   l.relationship AS link_relationship
            FROM annotations a
            JOIN annotation_links l ON l.annotation_id = a.id
            LEFT JOIN annotation_closeout_acknowledgements ack
              ON ack.annotation_id = a.id
             AND ack.target_type = 'issue'
             AND ack.target_id = l.target_id
            WHERE l.target_type = 'issue'
              AND l.target_id = ?
              AND l.relationship = 'must_consider'
              AND a.status = 'active'
              AND a.critical = 1
              AND ack.id IS NULL
            ORDER BY a.created_at ASC, a.id ASC
            """,
            (issue_id,),
        ).fetchall()
        warnings: list[dict[str, Any]] = []
        for row in rows:
            payload = self._build_annotation_payload(row, response_detail="summary")
            warnings.append(
                {
                    "annotation_id": payload["annotation_id"],
                    "file_path": payload["file_path"],
                    "line_start": payload["line_start"],
                    "line_end": payload["line_end"],
                    "note": payload["note"],
                    "intent": payload["intent"],
                    "relationship": row["link_relationship"],
                    "anchor_state": payload["anchor_state"],
                    "current_line_start": payload["current_line_start"],
                    "current_line_end": payload["current_line_end"],
                    "suggested_actions": [
                        "resolve_annotation",
                        "supersede_annotation",
                        "promote_annotation",
                        "carry_forward_annotation",
                    ],
                }
            )
        return warnings


def annotation_error_code(exc: Exception) -> ErrorCode:
    if isinstance(exc, KeyError):
        return ErrorCode.NOT_FOUND
    return ErrorCode.VALIDATION
