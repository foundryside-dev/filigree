#!/usr/bin/env python3
"""Enforce coverage floors for high-risk Filigree surfaces.

The repository-level 85% gate is useful, but it can hide regression in thinner
surfaces. This checker consumes ``coverage json`` output and fails when any
listed module falls below its explicit floor.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOTAL_FLOOR = 85.0

FILE_FLOORS = {
    "src/filigree/db_annotations.py": 60.0,
    "src/filigree/mcp_tools/annotations.py": 45.0,
    "src/filigree/mcp_tools/issues.py": 75.0,
    "src/filigree/mcp_tools/observations.py": 70.0,
    "src/filigree/scanner_scripts/claude_bug_hunt.py": 45.0,
    "src/filigree/scanner_scripts/codex_bug_hunt.py": 45.0,
}


def _percent(summary: dict[str, object]) -> float:
    value = summary.get("percent_covered")
    if not isinstance(value, int | float):
        msg = f"coverage summary missing numeric percent_covered: {summary!r}"
        raise ValueError(msg)
    return float(value)


def check_coverage_floors(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    totals = data.get("totals")
    files = data.get("files")
    if not isinstance(totals, dict) or not isinstance(files, dict):
        msg = "coverage JSON must contain object 'totals' and 'files' entries"
        raise ValueError(msg)

    failures: list[str] = []
    total_percent = _percent(totals)
    if total_percent < TOTAL_FLOOR:
        failures.append(f"TOTAL coverage {total_percent:.2f}% below floor {TOTAL_FLOOR:.2f}%")

    for file_path, floor in FILE_FLOORS.items():
        file_data = files.get(file_path)
        if not isinstance(file_data, dict):
            failures.append(f"{file_path} missing from coverage report")
            continue
        summary = file_data.get("summary")
        if not isinstance(summary, dict):
            failures.append(f"{file_path} missing coverage summary")
            continue
        percent = _percent(summary)
        if percent < floor:
            failures.append(f"{file_path} coverage {percent:.2f}% below floor {floor:.2f}%")

    return failures


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: check_coverage_floors.py COVERAGE_JSON", file=sys.stderr)
        return 2

    try:
        failures = check_coverage_floors(Path(args[0]))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"coverage floor check failed: {exc}", file=sys.stderr)
        return 2

    if failures:
        print("coverage floor check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("coverage floors satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
