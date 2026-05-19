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
    assert "FILE_REGISTRY_DISPLACED" in contracts
    assert "registry-backend-launch-runbook.md" in contracts
    assert "migrate-registry --to clarion --dry-run" in runbook
    assert "--allow-local-fallback" in runbook
    assert "Lost Rollback Manifest" in runbook
    assert "no supported `migrate-registry --to local` reconstruction path" in runbook


def test_adr014_documents_current_displaced_auto_create_contract() -> None:
    adr = _read_doc("docs/architecture/decisions/ADR-014-registry-backend-and-file-identity-displacement.md")

    assert "three auto-create paths" not in adr
    assert "Implicit auto-create paths route through `RegistryProtocol`" in adr
    for expected in (
        "`FiligreeDB.register_file`",
        "`FiligreeDB.process_scan_results`",
        "`ObservationsMixin.create_observation`",
        "`AnnotationsMixin.annotate_file`",
        "`report_finding`",
        "`preview-scan`",
        "`trigger-scan`",
        "`trigger-scan-batch`",
    ):
        assert expected in adr
    assert "`delete_file_record` is intentionally not displaced" in adr
    assert "does not delete or mutate the Clarion entity" in adr


def test_adr014_documents_current_registry_migration_consumers() -> None:
    adr = _read_doc("docs/architecture/decisions/ADR-014-registry-backend-and-file-identity-displacement.md")

    assert "four NOT-NULL FK consumers" not in adr
    assert "all four FK consumers" not in adr
    for expected in (
        "`scan_findings.file_id`",
        "`file_associations.file_id`",
        "`file_events.file_id`",
        "`observations.file_id`",
        "`observation_links.file_id`",
        "`annotations.file_id`",
        "`scan_runs.file_ids`",
    ):
        assert expected in adr


def test_adr014_documents_clarion_resolution_batch_retry_boundary() -> None:
    adr = _read_doc("docs/architecture/decisions/ADR-014-registry-backend-and-file-identity-displacement.md")

    assert "`ClarionRegistry` does not retry failed HTTP calls in this release" in adr
    assert "Batched resolution and retry policy are deferred together" in adr


def test_event_seq_source_docs_describe_ordering_not_dedup_semantics() -> None:
    migrations = _read_doc("src/filigree/migrations.py")
    schema = _read_doc("src/filigree/db_schema.py")
    events = _read_doc("src/filigree/db_events.py")
    combined = "\n".join((migrations, schema, events))

    assert "per-issue event ordering key" in migrations
    assert "same-second emissions get distinct event_seq values" in schema
    assert "same-second emissions get distinct sequence numbers" in events
    assert "rebuild dedup UNIQUE index" not in combined
    assert "extends the dedup tuple" not in combined
    assert "colliding on the dedup index" not in combined
