"""Meta-tests for repository quality gates.

These guard against the test-suite gaps found in the QA review: dashboard JS
checks drifting outside CI, global-only coverage masking weak modules, resource
leak warnings staying non-fatal, and live Loomweave integration silently skipping
even when a release lane requires it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# The live-Loomweave lane modules: run by the `live-loomweave` CI job and watched
# by release.yml's LAST_CONTRACT_SHA list. Pinned here so a rename cannot land in
# one place without the other.
_LIVE_LANE_TEST_MODULES = (
    "tests/integration/test_loomweave_staging_smoke.py",
    "tests/integration/test_loomweave_phase_d_e2e.py",
    "tests/federation/test_sei_oracle_live_loomweave.py",
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def _workflow_job(workflow: str, job_name: str) -> str:
    match = re.search(rf"^  {re.escape(job_name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", workflow, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, f"missing workflow job {job_name!r}"
    return match.group("body")


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


def test_python_pytest_job_provisions_node_for_static_dashboard_tests() -> None:
    workflow = _read(".github/workflows/ci.yml")
    test_job = _workflow_job(workflow, "test")

    setup_node_at = test_job.find("actions/setup-node")
    pytest_at = test_job.find("uv run pytest")

    assert setup_node_at != -1
    assert pytest_at != -1
    assert setup_node_at < pytest_at
    assert 'node-version: "24"' in test_job


def test_static_dashboard_pytest_has_clear_node_prerequisite_guard() -> None:
    guard_path = ROOT / "tests/static/conftest.py"

    assert guard_path.exists()
    guard = guard_path.read_text()
    assert 'shutil.which("node")' in guard
    assert "Node.js 24" in guard
    assert "pytest.fail" in guard


def test_development_docs_list_node_as_pytest_prerequisite() -> None:
    readme = _read("README.md")

    assert "Node.js 24" in readme
    assert "Node-backed static dashboard pytest tests" in readme


def test_ci_has_required_loomweave_contract_lane() -> None:
    workflow = _read(".github/workflows/ci.yml")

    contract_job = _workflow_job(workflow, "loomweave-contract")

    assert "loomweave-contract:" in workflow
    # Marker-selected since 3.3.0 (filigree-4a4a10f93a): the job must not
    # regress to a hand-enumerated file list, and it must not arm the Layer-2
    # sibling drift checks (no sibling repo is checked out in CI).
    code_lines = [line for line in contract_job.splitlines() if not line.lstrip().startswith("#")]
    assert any("uv run pytest -m federation_contract" in line for line in code_lines)
    assert not any("tests/federation/" in line for line in code_lines)
    assert not any("FILIGREE_REQUIRE_" in line for line in code_lines)


def test_ci_has_gated_live_loomweave_lane() -> None:
    workflow = _read(".github/workflows/ci.yml")
    live_job = _workflow_job(workflow, "live-loomweave")

    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "require_live_loomweave:" in workflow
    assert "live-loomweave:" in workflow
    assert "github.event_name == 'schedule'" in live_job
    assert "github.event_name == 'workflow_dispatch' && inputs.require_live_loomweave" in live_job
    assert "LOOMWEAVE_STAGING_BASE_URL: ${{ secrets.LOOMWEAVE_STAGING_BASE_URL }}" in live_job
    assert 'FILIGREE_REQUIRE_LIVE_LOOMWEAVE: "1"' in live_job
    for path in _LIVE_LANE_TEST_MODULES:
        assert path in live_job, f"live lane no longer runs {path}"
    # Clean rename (no compatibility alias): nothing was ever provisioned under
    # the Clarion-era secret name, so no `|| secrets.CLARION_...` fallback and
    # no old env flag may survive anywhere in the workflow.
    assert "CLARION" not in workflow


def test_release_workflow_emits_live_loomweave_release_checklist_warning() -> None:
    workflow = _read(".github/workflows/release.yml")

    assert "live-loomweave-release-check:" in workflow
    assert "actions/workflows/ci.yml/runs" in workflow
    assert "::warning title=Live Loomweave release checklist::" in workflow
    assert "scheduled Live Loomweave Integration lane" in workflow
    # The LAST_CONTRACT_SHA watch list must track the renamed live-lane modules,
    # otherwise a contract-bearing test change stops moving the release checklist.
    for path in _LIVE_LANE_TEST_MODULES:
        assert path in workflow, f"release checklist no longer watches {path}"
    assert "CLARION" not in workflow


def test_live_lane_modules_exist_and_carry_no_clarion_names() -> None:
    """The three live-lane modules are Loomweave-named end to end.

    Filenames, the required-mode env flag, the staging-URL secret and the
    failure messages all moved to ``LOOMWEAVE`` names; a stray ``CLARION`` token
    means the deploy surface (secret name / env flag) silently drifted back.
    """
    for path in _LIVE_LANE_TEST_MODULES:
        assert (ROOT / path).is_file(), f"missing live-lane module {path}"
        source = _read(path)
        assert "CLARION" not in source, f"{path} still carries a Clarion-era env/secret name"
        assert "FILIGREE_REQUIRE_LIVE_LOOMWEAVE" in source
    assert "LOOMWEAVE_STAGING_BASE_URL" in _read("tests/integration/test_loomweave_staging_smoke.py")


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


def test_sensitive_surface_coverage_floors_are_hardened() -> None:
    from scripts.check_coverage_floors import FILE_FLOORS

    assert FILE_FLOORS["src/filigree/dashboard_auth.py"] >= 90.0
    assert FILE_FLOORS["src/filigree/mcp_server.py"] >= 75.0
    assert FILE_FLOORS["src/filigree/registry.py"] >= 80.0
    assert FILE_FLOORS["src/filigree/scanner_scripts/scan_utils.py"] >= 85.0


def test_xss_guardrails_execute_rendered_output_instead_of_source_string_checks() -> None:
    xss_tests = _read("tests/static/test_xss_guards.py")

    assert "def _render_js(" in xss_tests
    assert "HTMLParser" in xss_tests
    assert "issueIdChip(" in xss_tests
    assert "renderCard(" in xss_tests
    assert "def _read(" not in xss_tests


def test_readme_documents_auth_route_classes() -> None:
    readme = _read("README.md")

    assert "WEFT_FEDERATION_TOKEN" in readme
    assert "| Route class | Authentication |" in readme
    assert "Classic dashboard API" in readme
    assert "Federation and scanner ingest" in readme
    assert "MCP HTTP endpoint" in readme


def test_live_loomweave_required_mode_turns_skips_into_failures() -> None:
    from tests.integration.test_loomweave_phase_d_e2e import _loomweave_unavailable_action

    assert _loomweave_unavailable_action(require_live=False) == "skip"
    assert _loomweave_unavailable_action(require_live=True) == "fail"
