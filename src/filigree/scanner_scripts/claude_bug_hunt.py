#!/usr/bin/env python3
"""Per-file bug hunt using Claude Code CLI — scanner for filigree.

Uses `claude --print` in read-only mode. Same prompt and parsing as codex scanner.
Requires: `claude` CLI on PATH (Claude Code).

Usage:
    filigree-scanner-claude                      # scan src/filigree/
    filigree-scanner-claude --root src/           # scan all of src/
    filigree-scanner-claude --dry-run             # list files + token estimate
    filigree-scanner-claude --no-ingest           # markdown only, skip API
    filigree-scanner-claude --max-files 20        # limit file count
    filigree-scanner-claude --model opus          # override model
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from filigree.scanner_scripts.scan_utils import run_scanner_pipeline

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ── Config ──────────────────────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_BASE_S = 2
STDERR_TRUNCATE = 500


# ── Claude Code execution with retry ───────────────────────────────────


async def run_claude_code(
    *,
    prompt: str,
    output_path: Path,
    model: str | None,
    repo_root: Path,
    timeout: int,
) -> None:
    """Run `claude --print` once. Raises RuntimeError on failure."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [
        "claude",
        "--print",
        "--exclude-dynamic-system-prompt-sections",
        "-p",
        prompt,
    ]
    if model:
        cmd.extend(["--model", model])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"claude --print timed out after {timeout}s") from None
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[:STDERR_TRUNCATE]
        raise RuntimeError(f"claude --print failed (rc={proc.returncode}): {err}")

    output_path.write_bytes(stdout)


async def run_claude_code_with_retry(
    *,
    prompt: str,
    output_path: Path,
    model: str | None,
    repo_root: Path,
    timeout: int,
) -> None:
    """Run claude --print with exponential backoff retries."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await run_claude_code(
                prompt=prompt,
                output_path=output_path,
                model=model,
                repo_root=repo_root,
                timeout=timeout,
            )
            return
        except (RuntimeError, TimeoutError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BASE_S * (2 ** (attempt - 1))
                sys.stderr.write(f"  retry {attempt}/{MAX_RETRIES} in {wait}s ...\n")
                await asyncio.sleep(wait)
    raise RuntimeError(f"all {MAX_RETRIES} attempts failed") from last_exc


# ── Entry point ────────────────────────────────────────────────────────


def main() -> int:
    return asyncio.run(
        run_scanner_pipeline(
            executor=run_claude_code_with_retry,
            scan_source="claude",
            description="Per-file bug hunt via Claude Code CLI.",
            cli_tool="claude",
            default_model="sonnet",
            default_batch_size=5,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
