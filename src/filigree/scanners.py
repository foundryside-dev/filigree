"""Scanner TOML registry for filigree.

Reads scanner definitions from .filigree/scanners/*.toml.
Each TOML file defines one scanner with a command template.

Template variables substituted at invocation:
    {file}         — target file path
    {api_url}      — resolved dashboard callback URL
    {project_root} — filigree project root directory
    {scan_run_id}  — MCP-generated correlation ID for tracking results
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"^[\w-]+$")


@dataclass(frozen=True)
class ScannerConfig:
    """A scanner definition loaded from a TOML file."""

    name: str
    description: str
    command: str
    args: tuple[str, ...] = ()
    file_types: tuple[str, ...] = ()

    def build_command(
        self,
        *,
        file_path: str,
        api_url: str = "http://localhost:8377",
        project_root: str = ".",
        scan_run_id: str = "",
        prompt: str = "bug-hunt",
    ) -> list[str]:
        """Build the full command list with template variables substituted.

        The command string is first split with ``shlex.split()``, then template
        variables are substituted on the resulting tokens. Variables inside quoted
        segments expand literally within their token (they are not re-split).

        Raises ValueError if the command string is malformed (e.g. unmatched quotes).
        """
        subs = {
            "{file}": str(file_path),
            "{api_url}": str(api_url),
            "{project_root}": str(project_root),
            "{scan_run_id}": str(scan_run_id),
            "{prompt}": str(prompt),
        }
        # Single-pass replacement prevents double-substitution when a
        # substituted value (e.g. a file path) contains template variables.
        pattern = re.compile("|".join(re.escape(k) for k in subs))

        def _expand(token: str) -> str:
            return pattern.sub(lambda m: subs[m.group(0)], token)

        try:
            base = shlex.split(self.command)
        except (TypeError, ValueError) as e:
            msg = f"Malformed command string in scanner {self.name!r}: {e}"
            raise ValueError(msg) from e
        expanded_base = [_expand(token) for token in base]
        expanded_args = []
        for raw_arg in self.args:
            if not isinstance(raw_arg, str):
                msg = f"Malformed args in scanner {self.name!r}: expected string entries"
                raise ValueError(msg)
            expanded_args.append(_expand(raw_arg))
        return expanded_base + expanded_args

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "file_types": list(self.file_types),
            "accepts_prompt": self.accepts_prompt(),
            "prompt_pack_aware": self.prompt_pack_aware(),
            "applicable_prompts": self.applicable_prompts(),
            "prompt_packs_endpoint": "list_prompt_packs",
            "sandbox_summary": self.sandbox_summary(),
            "sandbox_class": self.sandbox_class(),
            "bundled_name": self.is_bundled_name(),
            "bundled_match": self.matches_bundled_definition(),
            "managed": self.matches_bundled_definition(),
            "language_focus": list(self.language_focus()),
            **self.risk_metadata(),
        }

    def accepts_prompt(self) -> bool:
        """Return whether this scanner command template accepts a prompt pack."""
        return "{prompt}" in self.command or any("{prompt}" in arg for arg in self.args)

    def prompt_pack_aware(self) -> bool:
        """Return whether this scanner can receive non-default prompt packs."""
        return self.accepts_prompt()

    def applicable_prompts(self) -> list[str]:
        """Return prompt packs that are a reasonable fit for this scanner."""
        if not self.prompt_pack_aware():
            return []
        from filigree.scanner_prompts import applicable_prompt_pack_names

        return applicable_prompt_pack_names(self.language_focus())

    def sandbox_summary(self) -> str:
        """Return a concise description of scanner-specific process constraints."""
        if self.command == "filigree-scanner-codex":
            return "codex exec --sandbox read-only with approval_policy=never; file access is governed by Codex CLI sandboxing."
        if self.command == "filigree-scanner-claude":
            return "claude --print; file access is governed by Claude Code permissions and sandboxing."
        return "Custom external process; file access is governed by the scanner command and its runtime sandbox."

    def sandbox_class(self) -> str:
        """Return a machine-readable scanner sandbox category."""
        if self.command == "filigree-scanner-codex":
            return "tool-sandboxed"
        if self.command == "filigree-scanner-claude":
            return "host-policy"
        return "custom"

    def is_bundled_name(self) -> bool:
        """Return whether this scanner name is reserved by a bundled definition."""
        from filigree.bundled_scanners import get_bundled_scanner

        return get_bundled_scanner(self.name) is not None

    def matches_bundled_definition(self) -> bool:
        """Return whether this config exactly matches the current bundled definition."""
        from filigree.bundled_scanners import get_bundled_scanner

        bundled = get_bundled_scanner(self.name)
        if bundled is None:
            return False
        return self.command == bundled.command and self.args == bundled.args and self.file_types == bundled.file_types

    def language_focus(self) -> tuple[str, ...]:
        """Return bundled language focus hints, if this scanner name has one."""
        from filigree.bundled_scanners import get_bundled_scanner

        bundled = get_bundled_scanner(self.name)
        if bundled is None:
            return ()
        return bundled.language_focus

    def risk_metadata(self) -> dict[str, object]:
        """Return conservative execution and egress metadata for agent callers.

        Registry scanners are arbitrary external processes. Filigree only
        passes file paths, but those processes can read repository files and
        report results through the configured callback URL, so expose that
        risk before callers trigger a scan.
        """
        return {
            "execution_mode": "external_process",
            "may_send_contents": True,
            "requires_dashboard": True,
            "estimated_cost": "unknown",
            "safe_preview_only": True,
            "preview_recommended": True,
            "requires_approval": True,
            "risk_summary": "External scanner process may read repository files; result callback is localhost-only by default.",
            "prompt_pack_scope": "advisory",
            "prompt_pack_scope_summary": (
                "The prompt pack nudges review focus but does not restrict what the scanner process can read or report. "
                "File-access scope is governed by the scanner CLI sandbox, not by the prompt pack."
            ),
        }


def _parse_toml(path: Path, *, errors: list[str] | None = None) -> ScannerConfig | None:
    """Parse a single scanner TOML file. Returns None on error.

    When *errors* is provided, human-readable error descriptions are appended
    so callers can surface them (CLI output, MCP responses, etc.).
    """
    import tomllib

    def _fail(msg: str) -> None:
        logger.warning("%s: %s", msg, path)
        if errors is not None:
            errors.append(f"{path.name}: {msg}")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Failed to read scanner TOML: %s", path, exc_info=True)
        if errors is not None:
            errors.append(f"{path.name}: failed to read file (permission denied or I/O error)")
        return None

    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        _fail("failed to parse TOML syntax")
        return None

    scanner = data.get("scanner")
    if not isinstance(scanner, dict):
        _fail("missing [scanner] table")
        return None

    name = scanner.get("name")
    command = scanner.get("command")
    description = scanner.get("description", "")
    args = scanner.get("args", [])
    file_types = scanner.get("file_types", [])

    if not isinstance(name, str) or not isinstance(command, str):
        _fail("[scanner] name and command must be strings")
        return None
    if not isinstance(description, str):
        _fail("[scanner] description must be a string")
        return None
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        _fail("[scanner] args must be a list of strings")
        return None
    if not isinstance(file_types, list) or not all(isinstance(ext, str) for ext in file_types):
        _fail("[scanner] file_types must be a list of strings")
        return None
    if name != path.stem:
        _fail(f"[scanner] name {name!r} must match filename stem {path.stem!r}")
        return None
    if not _SAFE_NAME_RE.match(name):
        _fail("[scanner] name contains unsafe characters")
        return None

    return ScannerConfig(
        name=name,
        description=description,
        command=command,
        args=tuple(args),
        file_types=tuple(file_types),
    )


def list_scanners(scanners_dir: Path, *, errors: list[str] | None = None) -> list[ScannerConfig]:
    """Read all *.toml files from the scanners directory.

    Skips .toml.example files, malformed files, and non-TOML files.
    Returns an empty list if the directory doesn't exist.

    When *errors* is provided, human-readable descriptions of skipped files
    are appended so callers can surface them to users.
    """
    if not scanners_dir.is_dir():
        return []
    results = []
    for p in sorted(scanners_dir.iterdir()):
        if p.suffix != ".toml" or p.name.endswith(".toml.example"):
            continue
        cfg = _parse_toml(p, errors=errors)
        if cfg is not None:
            results.append(cfg)
    return results


def load_scanner(scanners_dir: Path, name: str) -> ScannerConfig | None:
    """Load a single scanner by name. Returns None if not found or name is invalid."""
    if not _SAFE_NAME_RE.match(name):
        return None  # Reject path traversal attempts
    toml_path = scanners_dir / f"{name}.toml"
    if not toml_path.is_file():
        return None
    return _parse_toml(toml_path)


def validate_scanner_command(
    command: str | Sequence[str],
    *,
    project_root: str | Path | None = None,
) -> str | None:
    """Check that the first token of a command is available on PATH.

    Accepts either a raw shell command string or a pre-tokenized command list.
    Returns None if valid, or an error message string if not found.

    When *project_root* is provided, relative executable paths such as
    ``./scripts/run_scan`` are validated relative to that project root.
    """
    tokens: list[str]
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            return f"Malformed command string: {command!r}"
    else:
        try:
            tokens = [str(t) for t in command]
        except (TypeError, ValueError):
            return "Malformed command token list"
    if not tokens:
        return "Empty command"
    binary = tokens[0]

    # Path-like executable tokens (contains a separator or explicit relative
    # prefix) should be checked as files, optionally against project_root.
    if "/" in binary or "\\" in binary:
        candidate_paths: list[Path] = []
        binary_path = Path(binary)
        if binary_path.is_absolute():
            candidate_paths.append(binary_path)
        else:
            if project_root is not None:
                candidate_paths.append(Path(project_root) / binary_path)
            candidate_paths.append(binary_path)
        for candidate in candidate_paths:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return None

    if shutil.which(binary) is None:
        return f"Command {binary!r} not found on PATH"
    return None
