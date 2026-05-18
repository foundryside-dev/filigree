"""Guards for agent-facing CLI/MCP surface documentation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read_doc(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_cli_workflow_docs_use_status_command_names() -> None:
    text = _read_doc("docs/cli.md")
    assert "explain-state" not in text
    assert "workflow-states" not in text
    assert "explain-status" in text
    assert "workflow-statuses" in text


def test_mcp_docs_use_issue_id_and_prefer_start_work() -> None:
    text = _read_doc("docs/mcp.md")
    assert "| `id` | string | yes | Issue ID |" not in text
    assert "| `issue_id` | string | yes | Issue ID |" in text
    assert "| `start_work` | Atomically claim and transition" in text
    assert "| `start_next_work` | Claim highest-priority ready issue and transition" in text
    assert "| `claim_issue` | Claim only" in text
    assert "| `claim_next` | Claim highest-priority ready issue only" in text


def test_registry_backend_contract_docs_reference_clarion_and_runbook() -> None:
    contracts = _read_doc("docs/federation/contracts.md")
    runbook = _read_doc("docs/federation/registry-backend-launch-runbook.md")

    assert "GET /api/v1/files?path=&language=" in contracts
    assert "FILIGREE_FILE_REGISTRY_DISPLACED" in contracts
    assert "registry-backend-launch-runbook.md" in contracts
    assert "migrate-registry --to clarion --dry-run" in runbook
    assert "--allow-local-fallback" in runbook
