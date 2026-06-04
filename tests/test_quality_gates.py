"""Meta-tests for repository quality gates.

These guard against the test-suite gaps found in the QA review: dashboard JS
checks drifting outside CI, global-only coverage masking weak modules, resource
leak warnings staying non-fatal, and live Clarion integration silently skipping
even when a release lane requires it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_ci_runs_dashboard_javascript_quality_gate() -> None:
    workflow = _read(".github/workflows/ci.yml")
    package_json = json.loads(_read("package.json"))

    assert "frontend" in workflow
    assert "actions/setup-node" in workflow
    assert "npm ci" in workflow
    assert "npm run lint" in workflow
    assert "npm run format:check" in workflow
    assert "lint" in package_json["scripts"]
    assert "format:check" in package_json["scripts"]


def test_docs_deploy_waits_for_all_ci_quality_gates() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "needs: [lint, typecheck, frontend, test, clarion-contract]" in workflow


def test_ci_has_required_clarion_contract_lane() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "clarion-contract:" in workflow
    assert "tests/unit/test_registry.py" in workflow
    assert "tests/api/test_registry_backend_integration.py" in workflow
    assert "tests/core/test_registry_backend_matrix.py" in workflow
    assert "tests/api/test_loom_auth.py" in workflow
    assert "tests/federation/test_sei_conformance_oracle.py" in workflow


def test_ci_has_gated_live_clarion_lane() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "workflow_dispatch:" in workflow
    assert "require_live_clarion:" in workflow
    assert "live-clarion:" in workflow
    assert "FILIGREE_REQUIRE_LIVE_CLARION" in workflow
    assert "tests/integration/test_clarion_phase_d_e2e.py" in workflow
    assert "tests/federation/test_sei_oracle_live_clarion.py" in workflow


def test_make_ci_runs_javascript_and_coverage_floor_gates() -> None:
    makefile = _read("Makefile")

    assert "js-lint:" in makefile
    assert "coverage-floors:" in makefile
    assert "ci: lint typecheck js-lint test-cov coverage-floors" in makefile


def test_pytest_promotes_resource_warnings_to_errors() -> None:
    pyproject = _read("pyproject.toml")

    assert "filterwarnings" in pyproject
    assert "error::ResourceWarning" in pyproject
    assert "error::pytest.PytestUnraisableExceptionWarning" in pyproject


def test_coverage_floor_script_rejects_module_regression(tmp_path: Path) -> None:
    coverage_json = tmp_path / "coverage.json"
    coverage_json.write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 86.0},
                "files": {
                    "src/filigree/mcp_tools/annotations.py": {"summary": {"percent_covered": 40.0}},
                    "src/filigree/db_annotations.py": {"summary": {"percent_covered": 70.0}},
                },
            }
        )
    )

    result = subprocess.run(
        ["uv", "run", "python", "scripts/check_coverage_floors.py", str(coverage_json)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "src/filigree/mcp_tools/annotations.py" in result.stderr
    assert "below floor" in result.stderr


def test_coverage_floors_include_security_and_scanner_surfaces() -> None:
    from scripts.check_coverage_floors import FILE_FLOORS

    protected = {
        "src/filigree/dashboard_auth.py",
        "src/filigree/dashboard_routes/files.py",
        "src/filigree/mcp_server.py",
        "src/filigree/registry.py",
        "src/filigree/scanner_runtime.py",
        "src/filigree/scanner_scripts/scan_utils.py",
    }

    assert protected <= set(FILE_FLOORS)


def test_live_clarion_required_mode_turns_skips_into_failures() -> None:
    from tests.integration.test_clarion_phase_d_e2e import _clarion_unavailable_action

    assert _clarion_unavailable_action(require_live=False) == "skip"
    assert _clarion_unavailable_action(require_live=True) == "fail"
