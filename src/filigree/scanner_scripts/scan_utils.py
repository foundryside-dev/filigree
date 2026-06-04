"""Shared utilities for filigree example scanner scripts.

These are helper functions for external scanner integrations — NOT part of
the filigree core package. The API (POST /api/scan-results) is the
first-class product; these utilities are documentation-by-code.

Key functions:
    find_files              — Walk directory tree collecting source files
    load_context            — Load repo context files for inclusion in scanner prompts
    parse_findings          — Parse structured markdown output into finding dicts
    severity_map            — Map scanner-native severities to filigree severities
    post_to_api             — POST findings to filigree scan API (returns (ok, error_detail))
    estimate_tokens         — Estimate token cost for a set of files
    run_scanner_pipeline    — End-to-end CLI pipeline (discovery → execute → parse → ingest)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from filigree.core import ForeignDatabaseError, ProjectNotInitialisedError, find_filigree_root
from filigree.scanner_callback import resolve_scanner_api_url_with_source
from filigree.scanner_prompts import PROMPT_PACKS, expand_prompt_pack_names

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_SCANNER_API_URL = "http://localhost:8377"
DEFAULT_SCANNER_API_TOKEN_ENV = "FILIGREE_FEDERATION_API_TOKEN"  # noqa: S105 - env var name, not a token value
LEGACY_SCANNER_API_TOKEN_ENV = "FILIGREE_API_TOKEN"  # noqa: S105 - env var name, not a token value

EXCLUDE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    ".eggs",
    "htmlcov",
    "node_modules",
    ".filigree",
}

BINARY_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".bmp",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".whl",
    ".egg",
    ".tar",
    ".gz",
    ".zip",
    ".bz2",
    ".xz",
    ".pdf",
    ".doc",
    ".docx",
    ".bin",
    ".dat",
    ".pickle",
    ".pkl",
}

# Closed vocabulary for rule_id — maps free-text categories to canonical IDs
VALID_RULE_IDS = frozenset(
    {
        "logic-error",
        "resource-leak",
        "race-condition",
        "type-error",
        "error-handling",
        "injection",
        "performance",
        "api-misuse",
        "other",
    }
)

# Severity mapping from scanner-native terms to filigree severities
_SEVERITY_MAP = {
    "critical": "critical",
    "major": "high",
    "minor": "medium",
    "trivial": "low",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "info",
}

# Category keywords → rule_id mapping
_CATEGORY_KEYWORDS = {
    "logic": "logic-error",
    "off-by-one": "logic-error",
    "unreachable": "logic-error",
    "resource": "resource-leak",
    "leak": "resource-leak",
    "unclosed": "resource-leak",
    "race": "race-condition",
    "concurren": "race-condition",
    "lock": "race-condition",
    "type": "type-error",
    "contract": "type-error",
    "schema": "type-error",
    "error handling": "error-handling",
    "exception": "error-handling",
    "except": "error-handling",
    "inject": "injection",
    "sql": "injection",
    "saniti": "injection",
    "xss": "injection",
    "performance": "performance",
    "o(n": "performance",
    "blocking": "performance",
    "api": "api-misuse",
    "deprecated": "api-misuse",
    "misuse": "api-misuse",
}


def _is_no_finding_section(section: str) -> bool:
    normalized = section.strip().strip("\"'").strip()
    return bool(re.fullmatch(r"No concrete bug found(?:\b.*)?", normalized, re.IGNORECASE))


def _extract_evidence_line(evidence: str, *, file_path: str = "") -> int | None:
    if not evidence:
        return None
    if file_path:
        path_m = re.search(rf"(?<![\w./-]){re.escape(file_path)}:(\d+)\b", evidence)
        if path_m:
            return int(path_m.group(1))
    citation_m = re.search(r"(?:^|[\s`(])[\w./-]+\.[A-Za-z0-9_]+:(\d+)\b", evidence)
    if citation_m:
        return int(citation_m.group(1))
    line_m = re.search(r"\bline\s+(\d+)\b", evidence, re.IGNORECASE)
    if line_m:
        return int(line_m.group(1))
    return None


# ── File discovery ──────────────────────────────────────────────────────


def _is_python(path: Path) -> bool:
    return path.suffix == ".py" and not path.name.startswith("test_")


def find_files(
    root: Path,
    *,
    file_type: str = "python",
    exclude_dirs: set[Path] | None = None,
    max_files: int = 0,
) -> list[Path]:
    """Walk *root* collecting files, pruning EXCLUDE_DIRS.

    Args:
        root: Directory to scan.
        file_type: "python" to filter .py files (excludes test_ prefixed files),
            "all" for everything non-binary.
        exclude_dirs: Additional directories to skip.
        max_files: If > 0, truncate the result list to this many files.

    Returns:
        Sorted list of file paths.
    """
    extra_exclude = {p.resolve() for p in exclude_dirs} if exclude_dirs else set()
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and (Path(dirpath) / d).resolve() not in extra_exclude]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fpath.suffix in {".pyc", ".pyo"}:
                continue
            candidates.append(fpath)

    candidates.sort()
    if file_type == "python":
        candidates = [p for p in candidates if _is_python(p)]
    else:
        candidates = [p for p in candidates if p.suffix not in BINARY_EXTENSIONS]

    if max_files > 0:
        candidates = candidates[:max_files]
    return candidates


# ── Context loader ──────────────────────────────────────────────────────


def load_context(repo_root: Path) -> str:
    """Load repo context files (CLAUDE.md, ARCHITECTURE.md) for inclusion in scanner prompts."""
    parts: list[str] = []
    for name in ("CLAUDE.md", "ARCHITECTURE.md"):
        path = repo_root / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Warning: Skipping unreadable context file {name}: {exc}", file=sys.stderr)
            continue
        parts.append(f"--- {name} ---\n{text}")
    return "\n\n".join(parts)


# ── Severity mapping ────────────────────────────────────────────────────


def severity_map(severity: str) -> str:
    """Map scanner-native severity to filigree severity.

    Returns one of: critical, high, medium, low, info.
    Unknown values map to "info" with a warning log.
    """
    key = severity.strip().lower()
    mapped = _SEVERITY_MAP.get(key)
    if mapped is not None:
        return mapped
    logger.warning("Unknown severity %r, mapping to 'info'", severity)
    return "info"


# ── Rule ID mapping ─────────────────────────────────────────────────────


def _infer_rule_id(summary: str) -> str:
    """Infer a canonical rule_id from a finding's summary text."""
    lower = summary.lower()
    for keyword, rule_id in _CATEGORY_KEYWORDS.items():
        if keyword in lower:
            return rule_id
    return "other"


# ── Finding parser ──────────────────────────────────────────────────────


def parse_findings(text: str, *, file_path: str = "") -> list[dict[str, Any]]:
    """Parse structured markdown output into finding dicts.

    Expects sections separated by '---' with ## Summary, ## Severity,
    ## Evidence, ## Root Cause Hypothesis, ## Suggested Fix subsections.

    Returns:
        List of finding dicts ready for POST to /api/scan-results.
    """
    if not text or not text.strip():
        return []

    # Split on --- separator (must be on its own line)
    sections = re.split(r"\n---+\n", text)
    findings: list[dict[str, Any]] = []

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Skip only the standalone "No concrete bug found" sentinel. A real
        # finding may cite that phrase inside evidence or root-cause text.
        if _is_no_finding_section(section):
            continue

        # Extract ## Summary
        summary_m = re.search(r"##\s*Summary\s*\n(.+?)(?=\n##|\Z)", section, re.DOTALL)
        if not summary_m:
            continue
        summary = summary_m.group(1).strip()

        # Extract ## Severity
        sev_m = re.search(r"Severity:\s*(\S+)", section, re.IGNORECASE)
        raw_severity = sev_m.group(1) if sev_m else "info"

        # Extract ## Evidence
        evidence_m = re.search(r"##\s*Evidence\s*\n(.+?)(?=\n##|\Z)", section, re.DOTALL)
        evidence = evidence_m.group(1).strip() if evidence_m else ""

        # Extract ## Root Cause Hypothesis
        root_m = re.search(r"##\s*Root Cause Hypothesis\s*\n(.+?)(?=\n##|\Z)", section, re.DOTALL)
        root_cause = root_m.group(1).strip() if root_m else ""

        # Extract ## Suggested Fix
        fix_m = re.search(r"##\s*Suggested Fix\s*\n(.+?)(?=\n##|\Z)", section, re.DOTALL)
        suggestion = fix_m.group(1).strip() if fix_m else ""

        # Infer rule_id from summary text — strip any newlines
        rule_id = _infer_rule_id(summary).strip().replace("\n", "")

        # Extract line number from file citations, avoiding unrelated colons
        # such as URL schemes and localhost ports in evidence text.
        line_start = _extract_evidence_line(evidence, file_path=file_path)

        message = summary
        if root_cause:
            message += f"\n\nRoot cause: {root_cause}"

        finding: dict[str, Any] = {
            "path": file_path,
            "rule_id": rule_id,
            "severity": severity_map(raw_severity),
            "message": message,
            "suggestion": suggestion,
        }
        if line_start is not None:
            finding["line_start"] = line_start

        findings.append(finding)

    return findings


# ── API posting ─────────────────────────────────────────────────────────


def _resolve_api_token_from_env(api_token_env: str) -> str | None:
    api_token = os.environ.get(api_token_env, "").strip()
    if api_token or api_token_env != DEFAULT_SCANNER_API_TOKEN_ENV:
        return api_token or None
    return os.environ.get(LEGACY_SCANNER_API_TOKEN_ENV, "").strip() or None


def post_to_api(
    *,
    api_url: str,
    scan_source: str,
    scan_run_id: str,
    findings: list[dict[str, Any]],
    create_observations: bool = False,
    complete_scan_run: bool = True,
    api_token: str | None = None,
) -> tuple[bool, str]:
    """POST findings to filigree's scan API.

    Args:
        api_url: Base URL (e.g., "http://localhost:8377").
        scan_source: Scanner identifier (e.g., "codex", "claude").
        scan_run_id: Globally unique run identifier. Use a non-empty value for
            scan-run history; an empty string is accepted by Filigree for
            fire-and-forget findings but is excluded from ``GET /api/scan-runs``.
        findings: List of finding dicts.
            Filigree expects native scan-results findings. SARIF adapters must
            map SARIF ``partialFingerprints``/``fingerprints`` into each
            finding's ``fingerprint`` before calling this helper.
        create_observations: If True, auto-promote findings to observations for triage.
        complete_scan_run: If False, don't mark the scan run as completed.
            Use for batch scans where multiple POSTs share a scan_run_id.
        api_token: Optional bearer token for token-gated scanner callback endpoints.

    Returns:
        ``(True, "")`` on success, ``(False, error_detail)`` on failure.
    """
    import urllib.error
    import urllib.request

    endpoint = f"{api_url}/api/scan-results"
    payload: dict[str, Any] = {
        "scan_source": scan_source,
        "scan_run_id": scan_run_id,
        "findings": findings,
        "create_observations": create_observations,
    }
    if not complete_scan_run:
        payload["complete_scan_run"] = False
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    req = urllib.request.Request(  # noqa: S310
        endpoint,
        data=data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                detail = f"API success response was not a JSON object: {e}"
                logger.warning("%s (endpoint: %s)", detail, endpoint)
                return False, detail
            if not isinstance(body, dict):
                detail = f"API success response was not a JSON object: {type(body).__name__}"
                logger.warning("%s (endpoint: %s)", detail, endpoint)
                return False, detail
            # Log any severity coercion warnings from the API [B2]
            for w in body.get("warnings", []):
                logger.warning("API warning: %s", w)
            return True, ""
    except urllib.error.HTTPError as e:
        body_text = ""
        with contextlib.suppress(OSError, urllib.error.URLError):
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        detail = f"HTTP {e.code}: {body_text}" if body_text else f"HTTP {e.code}"
        logger.warning(
            "API POST failed: %s for %s (endpoint: %s)",
            detail,
            scan_source,
            endpoint,
        )
        return False, detail
    except (urllib.error.URLError, OSError) as e:
        detail = f"Connection error: {e}"
        logger.warning(
            "API unreachable for %s: %s (endpoint: %s)",
            scan_source,
            e,
            endpoint,
        )
        return False, detail


# ── Token estimation ────────────────────────────────────────────────────


def estimate_tokens(files: list[Path], context_overhead: int = 2000) -> int:
    """Estimate total tokens for scanning a list of files.

    Uses chars/4 as a rough token estimate per file, plus context overhead per file.
    """
    total = 0
    for f in files:
        try:
            size = f.stat().st_size
            total += (size // 4) + context_overhead
        except OSError as exc:
            logger.warning("Cannot stat %s for token estimation: %s", f, exc)
            total += context_overhead
    return total


# ── Shared pipeline ────────────────────────────────────────────────────


PROMPT_TEMPLATE = """\
You are a static analysis agent doing a deep bug audit.

Instructions:
- Analyse the target file for real, concrete bugs.
- You may read any repo file to verify integration behaviour.
- Report bugs only if the primary fix belongs in the target file.
- If you find multiple distinct bugs, separate them with a line containing only '---'.
- If you find no credible bug, output exactly:
  "No concrete bug found in target file"
- Cite file paths and line numbers in evidence.

Bug categories to check (use these exact rule_id values):
1. **logic-error** — off-by-one, wrong comparison, unreachable branches
2. **error-handling** — bare except, swallowed exceptions, missing cleanup
3. **resource-leak** — unclosed files/connections, missing context managers
4. **race-condition** — shared mutable state in async code, missing locks
5. **type-error** — wrong return types, schema mismatches
6. **injection** — unsanitised inputs, missing parameterisation, XSS
7. **performance** — O(n²) where avoidable, blocking I/O in async
8. **api-misuse** — wrong argument order, deprecated calls, silent failures
9. **other** — anything that doesn't fit the above

For each bug found, use this format:

## Summary
<one-line description>

## Severity
- Severity: <critical|major|minor|trivial>
- Priority: <P0|P1|P2|P3>

## Evidence
<file:line citations, code snippets>

## Root Cause Hypothesis
<why the bug exists>

## Suggested Fix
<concrete fix>

Repository context:
{context}

Target file:
{file_path}
"""


def build_prompt_template(prompt_pack: str) -> str:
    """Return the base prompt template with a bundled review focus inserted."""
    if prompt_pack == "bug-hunt":
        return PROMPT_TEMPLATE

    pack_names = expand_prompt_pack_names(prompt_pack)
    focus_sections = []
    for pack_name in pack_names:
        pack = PROMPT_PACKS[pack_name]
        focus_sections.append(f"Review focus: {pack.name}\n{pack.instructions}")

    focus_text = "\n\n".join(focus_sections)
    marker = "\nRepository context:\n"
    return PROMPT_TEMPLATE.replace(marker, f"\nAdditional review focus:\n{focus_text}\n\nRepository context:\n", 1)


def _display_path(path: Path, base: Path) -> Path:
    """Best-effort relative path for display; falls back to absolute."""
    try:
        return path.relative_to(base)
    except ValueError:
        return path


def _default_scan_root(repo_root: Path) -> str:
    """Infer the default Python package root for the repo being scanned."""
    src_dir = repo_root / "src"
    if not src_dir.is_dir():
        return "."

    repo_package_name = repo_root.name.replace("-", "_")
    repo_named_package = src_dir / repo_package_name
    if repo_named_package.is_dir():
        return str(repo_named_package.relative_to(repo_root))

    package_dirs = sorted(path for path in src_dir.iterdir() if path.is_dir() and (path / "__init__.py").is_file())
    if len(package_dirs) == 1:
        return str(package_dirs[0].relative_to(repo_root))

    return "src"


def _resolve_target_file(*, repo_root: Path, root_dir: Path, file_arg: str) -> Path:
    """Resolve a target file for single-file scan mode."""
    raw = Path(file_arg)
    target = raw.resolve() if raw.is_absolute() else (repo_root / raw).resolve()
    if not target.is_file():
        msg = f"target file does not exist: {target}"
        raise ValueError(msg)
    try:
        target.relative_to(root_dir)
    except ValueError:
        msg = f"target file is outside scan root: {target} (root: {root_dir})"
        raise ValueError(msg) from None
    return target


def _default_api_url_for_repo(repo_root: Path) -> str:
    """Resolve the active local dashboard URL for direct scanner entrypoints."""
    try:
        filigree_dir = find_filigree_root(repo_root)
    except (ProjectNotInitialisedError, ForeignDatabaseError):
        return DEFAULT_SCANNER_API_URL
    return resolve_scanner_api_url_with_source(filigree_dir).url


async def _analyse_files(
    *,
    files: list[Path],
    output_dir: Path,
    root_dir: Path,
    repo_root: Path,
    model: str | None,
    batch_size: int,
    context: str,
    skip_existing: bool,
    timeout: int,
    api_url: str,
    no_ingest: bool,
    scan_run_id: str,
    scan_source: str,
    executor: Any,
    prompt_template: str,
    api_token: str | None = None,
    cache_warmup: bool = True,
) -> dict[str, int]:
    """Run analysis on all files in batches. Returns summary stats."""
    import asyncio
    import re
    from collections import Counter

    failed: list[tuple[Path, Exception]] = []
    report_paths: list[tuple[Path, Path]] = []
    bad_report_paths: set[Path] = set()
    done = 0
    total = len(files)
    api_successes = 0
    api_failures = 0

    def record_report_failure(fpath: Path, out: Path, detail: str) -> None:
        failed.append((fpath, RuntimeError(detail)))
        bad_report_paths.add(out)
        print(f"  FAIL {_display_path(fpath, repo_root)}: {detail}", file=sys.stderr)

    def read_report_text(fpath: Path, out: Path) -> str | None:
        try:
            return out.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as read_exc:
            if out not in bad_report_paths:
                failed.append((fpath, read_exc))
                bad_report_paths.add(out)
            print(f"  FAIL reading report for {_display_path(fpath, repo_root)}: {read_exc}", file=sys.stderr)
            return None

    def ingest_report(fpath: Path, out: Path) -> None:
        nonlocal api_successes, api_failures
        if no_ingest or not out.is_file():
            return
        text = read_report_text(fpath, out)
        if text is None:
            return
        rel_path = str(_display_path(fpath, repo_root))
        findings = parse_findings(text, file_path=rel_path)
        if not findings:
            return

        # Ingest findings but defer scan run completion to the final POST after
        # all files are processed.
        ok, err_detail = post_to_api(
            api_url=api_url,
            scan_source=scan_source,
            scan_run_id=scan_run_id,
            findings=findings,
            create_observations=True,
            complete_scan_run=False,
            api_token=api_token,
        )
        if ok:
            api_successes += 1
        else:
            api_failures += 1
            print(f"  API error for {rel_path}: {err_detail}", file=sys.stderr)

    async def run_batch(batch: list[Path]) -> None:
        nonlocal done
        tasks: list[tuple[Path, Path, asyncio.Task[None]]] = []

        for fpath in batch:
            rel = fpath.relative_to(root_dir)
            out = (output_dir / rel).with_suffix(rel.suffix + ".md")

            if skip_existing and out.exists():
                done += 1
                if not out.is_file():
                    record_report_failure(fpath, out, "existing report path is not a file")
                    continue
                report_paths.append((fpath, out))
                print(f"  [skip] {_display_path(fpath, repo_root)}", file=sys.stderr)
                ingest_report(fpath, out)
                continue

            prompt = prompt_template.format(file_path=fpath, context=context)
            task = asyncio.create_task(
                executor(
                    prompt=prompt,
                    output_path=out,
                    model=model,
                    repo_root=repo_root,
                    timeout=timeout,
                )
            )
            tasks.append((fpath, out, task))

        results = await asyncio.gather(*(t for _, _, t in tasks), return_exceptions=True)
        for (fpath, out, _), result in zip(tasks, results, strict=True):
            done += 1
            if isinstance(result, Exception):
                failed.append((fpath, result))
                print(f"  FAIL {_display_path(fpath, repo_root)}: {result}", file=sys.stderr)
            else:
                if not out.exists():
                    record_report_failure(fpath, out, "scanner did not generate report")
                    continue
                if not out.is_file():
                    record_report_failure(fpath, out, "generated report path is not a file")
                    continue
                report_paths.append((fpath, out))
                print(f"  [{done}/{total}] {_display_path(fpath, repo_root)}", file=sys.stderr)
                ingest_report(fpath, out)

    remaining_files = files
    if cache_warmup and batch_size > 1 and total > 1:
        warmup_index: int | None = None
        for index, fpath in enumerate(files):
            rel = fpath.relative_to(root_dir)
            out = (output_dir / rel).with_suffix(rel.suffix + ".md")
            if skip_existing and out.exists():
                continue
            warmup_index = index
            break
        if warmup_index is not None:
            await run_batch(files[: warmup_index + 1])
            remaining_files = files[warmup_index + 1 :]

    for batch_start in range(0, len(remaining_files), batch_size):
        await run_batch(remaining_files[batch_start : batch_start + batch_size])

    # Send a final completion POST only when every local execution/report read
    # and finding-ingest POST succeeded. Otherwise the parent scanner process
    # exits non-zero; the scan run must not be marked completed falsely.
    if not no_ingest and scan_run_id and not failed and api_failures == 0:
        ok, err_detail = post_to_api(
            api_url=api_url,
            scan_source=scan_source,
            scan_run_id=scan_run_id,
            findings=[],
            complete_scan_run=True,
            api_token=api_token,
        )
        if not ok:
            print(f"  API error completing scan run: {err_detail}", file=sys.stderr)
            api_failures += 1

    stats: Counter[str] = Counter()
    for fpath, md in report_paths:
        if md in bad_report_paths:
            continue
        if not md.exists():
            record_report_failure(fpath, md, "scanner did not generate report")
            continue
        text = read_report_text(fpath, md)
        if text is None:
            continue
        # Classify per finding-section, mirroring parse_findings (the ingestion
        # source of truth): split on the '---' separator, drop empty/sentinel
        # sections, and require a '## Summary'. A whole-text substring check for
        # the sentinel mis-counted a report that pairs a real finding section
        # with a separate "No concrete bug found" section as "clean" — under-
        # reporting (and masking) findings the API path actually ingested.
        finding_sections = [
            section
            for raw in re.split(r"\n---+\n", text)
            if (section := raw.strip()) and not _is_no_finding_section(section) and re.search(r"##\s*Summary", section)
        ]
        if not finding_sections:
            stats["clean"] += 1
            continue
        for section in finding_sections:
            pri_m = re.search(r"Priority:\s*(P\d)", section, re.IGNORECASE)
            if pri_m:
                stats[pri_m.group(1).upper()] += 1
            else:
                stats["unknown"] += 1

    stats["failed"] = len(failed)
    stats["api_files_posted"] = api_successes
    stats["api_files_failed"] = api_failures
    return dict(stats)


async def run_scanner_pipeline(
    *,
    executor: Any,
    scan_source: str,
    description: str = "",
    cli_tool: str = "",
    default_model: str | None = None,
    default_batch_size: int = 10,
    prompt_template: str = "",
) -> int:
    """End-to-end CLI pipeline: parse args → discover files → execute → ingest.

    Args:
        executor: Async callable with signature
            ``(prompt, output_path, model, repo_root, timeout) -> None``
        scan_source: Scanner identifier for API ingestion (e.g. "codex").
        description: CLI description text.
        cli_tool: CLI tool name to check on PATH (e.g. "codex", "claude").
        default_model: Default model arg (None = no --model flag).
        default_batch_size: Default concurrency.
        prompt_template: Prompt template string (uses PROMPT_TEMPLATE if empty).

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    import argparse
    import shutil
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(description=description or f"Per-file bug hunt ({scan_source}).")
    parser.add_argument("--root", default=None, help="Directory to scan")
    parser.add_argument("--file", default=None, help="Scan exactly one file")
    parser.add_argument("--output-dir", default="docs/bugs/generated", help="Report output dir")
    parser.add_argument("--batch-size", type=int, default=default_batch_size, help=f"Concurrent runs (default: {default_batch_size})")
    if default_model is not None:
        parser.add_argument("--model", default=default_model, help=f"Model override (default: {default_model})")
    else:
        parser.add_argument("--model", default=None, help="Model override")
    parser.add_argument("--file-type", choices=["python", "all"], default="python", help="File filter")
    parser.add_argument("--skip-existing", action="store_true", help="Skip files with existing reports")
    parser.add_argument("--timeout", type=int, default=300, help="Per-file timeout in seconds (default: 300)")
    parser.add_argument("--dry-run", action="store_true", help="List files with count and token estimate")
    parser.add_argument("--max-files", type=int, default=50, help="Maximum files to scan (default: 50)")
    parser.add_argument("--api-url", default=None, help="Filigree dashboard URL")
    parser.add_argument(
        "--api-token-env",
        default=DEFAULT_SCANNER_API_TOKEN_ENV,
        help=f"Environment variable carrying the Filigree API bearer token (default: {DEFAULT_SCANNER_API_TOKEN_ENV})",
    )
    parser.add_argument("--no-ingest", action="store_true", help="Skip API POST (markdown-only mode)")
    parser.add_argument("--scan-run-id", default=None, help="External scan run ID")
    parser.add_argument("--prompt", default="bug-hunt", help="Bundled prompt pack to use")
    parser.add_argument(
        "--no-cache-warmup",
        action="store_true",
        help="Skip the serial first-file warm-up before parallel scanner runs",
    )

    args = parser.parse_args()

    if prompt_template:
        template = prompt_template
    else:
        try:
            template = build_prompt_template(args.prompt)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.batch_size < 1:
        print("Error: --batch-size must be at least 1", file=sys.stderr)
        return 1

    repo_root = Path.cwd().resolve()
    scan_root = args.root or _default_scan_root(repo_root)
    root_dir = (repo_root / scan_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()

    if not root_dir.is_dir():
        print(f"Error: scan root is not a directory: {root_dir}", file=sys.stderr)
        return 1

    if args.file:
        try:
            files = [_resolve_target_file(repo_root=repo_root, root_dir=root_dir, file_arg=args.file)]
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    else:
        files = find_files(
            root_dir,
            file_type=args.file_type,
            exclude_dirs={output_dir},
            max_files=args.max_files,
        )
    if not files:
        print(f"No files found under {root_dir}", file=sys.stderr)
        return 1

    if args.dry_run:
        tokens = estimate_tokens(files)
        print(f"Would analyse {len(files)} files (~{tokens:,} estimated tokens):")
        for f in files:
            print(f"  {_display_path(f, repo_root)}")
        return 0

    if cli_tool and shutil.which(cli_tool) is None:
        print(f"Error: `{cli_tool}` not found on PATH", file=sys.stderr)
        return 1

    context = load_context(repo_root)
    scan_run_id = args.scan_run_id or f"{scan_source}-{datetime.now(UTC).isoformat()}"
    try:
        if args.api_url is not None:
            api_url = args.api_url.strip().rstrip("/")
            if not api_url:
                print("Error: --api-url must be a non-empty URL", file=sys.stderr)
                return 1
        else:
            api_url = _default_api_url_for_repo(repo_root)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    api_token_env = args.api_token_env.strip()
    if not api_token_env:
        print("Error: --api-token-env must be a non-empty environment variable name", file=sys.stderr)
        return 1
    api_token = _resolve_api_token_from_env(api_token_env)

    model_display = f", model={args.model}" if args.model else ""
    print(f"Analysing {len(files)} files (batch={args.batch_size}{model_display}) ...", file=sys.stderr)
    if not args.no_cache_warmup and args.batch_size > 1 and len(files) > 1:
        print("  Cache warm-up: first file runs before parallel batches.", file=sys.stderr)
    if not args.no_ingest:
        print(f"  API: {api_url}  run_id: {scan_run_id}", file=sys.stderr)

    stats = await _analyse_files(
        files=files,
        output_dir=output_dir,
        root_dir=root_dir,
        repo_root=repo_root,
        model=args.model,
        batch_size=args.batch_size,
        context=context,
        skip_existing=args.skip_existing,
        timeout=args.timeout,
        api_url=api_url,
        no_ingest=args.no_ingest,
        scan_run_id=scan_run_id,
        scan_source=scan_source,
        executor=executor,
        prompt_template=template,
        api_token=api_token,
        cache_warmup=not args.no_cache_warmup,
    )

    print("\n" + "=" * 50)
    print(f"Bug Hunt Summary ({scan_source})")
    print("=" * 50)
    defects = sum(v for k, v in stats.items() if k not in ("clean", "failed", "unknown", "api_files_posted", "api_files_failed"))
    print(f"  Defects found:  {defects}")
    for pri in ("P0", "P1", "P2", "P3"):
        c = stats.get(pri, 0)
        if c:
            print(f"    {pri}: {c}")
    print(f"  Clean files:    {stats.get('clean', 0)}")
    if stats.get("failed", 0):
        print(f"  Failed:         {stats['failed']}")
    if not args.no_ingest:
        print(f"  API files posted:  {stats.get('api_files_posted', 0)}")
        if stats.get("api_files_failed", 0):
            print(f"  API files failed:  {stats['api_files_failed']}")
    print("=" * 50)

    # Any ingest failure is fatal — symmetric with the analysis-failure guard
    # below. api_files_failed counts both dropped per-file findings POSTs and a
    # failed completion POST (an un-completed scan run), so a partial success
    # must not be reported to the orchestrator as success.
    if not args.no_ingest and stats.get("api_files_failed", 0) > 0:
        return 1

    if stats.get("failed", 0) > 0:
        return 1

    return 0
