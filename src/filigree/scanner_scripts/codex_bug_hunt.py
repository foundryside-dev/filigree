#!/usr/bin/env python3
"""Per-file bug hunt using Codex — example scanner for filigree.

Scans source files, runs `codex exec` on each with a static-analysis prompt,
writes markdown reports, and optionally POSTs findings to filigree's scan API.

No external dependencies beyond Python 3.11+ and `codex` on PATH.

Usage:
    filigree-scanner-codex                      # scan src/filigree/
    filigree-scanner-codex --root src/           # scan all of src/
    filigree-scanner-codex --dry-run             # list files + token estimate
    filigree-scanner-codex --no-ingest           # markdown only, skip API
    filigree-scanner-codex --max-files 20        # limit file count
    filigree-scanner-codex --api-url http://..   # custom dashboard URL
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


# ── Codex execution with retry ──────────────────────────────────────────


async def run_codex(
    *,
    prompt: str,
    output_path: Path,
    model: str | None,
    repo_root: Path,
    timeout: int,
) -> None:
    """Run `codex exec` once. Raises RuntimeError on failure."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "-c",
        'approval_policy="never"',
        "--output-last-message",
        str(output_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=repo_root,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(input=prompt.encode("utf-8")), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"codex exec timed out after {timeout}s") from None
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[:STDERR_TRUNCATE]
        raise RuntimeError(f"codex exec failed (rc={proc.returncode}): {err}")


async def run_codex_with_retry(
    *,
    prompt: str,
    output_path: Path,
    model: str | None,
    repo_root: Path,
    timeout: int,
) -> None:
    """Run codex exec with exponential backoff retries."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await run_codex(
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
            executor=run_codex_with_retry,
            scan_source="codex",
            description="Per-file bug hunt via Codex.",
            cli_tool="codex",
            default_batch_size=10,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
