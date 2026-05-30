"""Static guardrails for dashboard registry-backend test setup."""

from __future__ import annotations

import ast
from pathlib import Path


def test_dashboard_api_tests_do_not_assign_clarion_backend_directly() -> None:
    """Dashboard API tests must build Clarion DBs through FiligreeDB init validation."""
    api_tests = [
        Path("tests/api/test_files_api.py"),
        Path("tests/api/test_files_dashboard.py"),
    ]

    direct_assignments: list[str] = []
    for test_path in api_tests:
        tree = ast.parse(test_path.read_text(), filename=str(test_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            assigns_clarion = isinstance(node.value, ast.Constant) and node.value.value == "clarion"
            if not assigns_clarion:
                continue
            if any(isinstance(target, ast.Attribute) and target.attr == "registry_backend" for target in node.targets):
                direct_assignments.append(f"{test_path}:{node.lineno}")

    assert direct_assignments == []
