# File Traceability Playbook

This guide shows how to keep issue work, files, and scan findings connected end-to-end across MCP tools and the dashboard UI.

## What "Traceability" Means in Filigree

Filigree connects three records:

1. `file_records` (the file itself, keyed by project-relative path)
2. `file_associations` (links between files and issues)
3. `scan_findings` (scanner output tied to file IDs)

If these stay aligned, you can move from issue -> file -> findings -> timeline without manual lookup.

## MCP Workflow (Operator-Friendly)

Use this when an agent or automation is driving the work.

### 1) Discover valid enums and endpoints (optional but recommended)

```bash
curl -s http://localhost:8377/api/files/_schema
```

For server-mode multi-project dashboards, prefix with project key:

```bash
curl -s http://localhost:8377/api/p/<project-key>/files/_schema
```

### 2) Ensure the file record exists

MCP tool:

```json
{
  "tool": "register_file",
  "arguments": {
    "path": "src/filigree/mcp_server.py",
    "language": "python"
  }
}
```

Important: always use a project-relative path (not absolute).

### 3) Link file <-> issue

MCP tool:

```json
{
  "tool": "add_file_association",
  "arguments": {
    "file_id": "filigree-file-abc123",
    "issue_id": "filigree-f2492d",
    "assoc_type": "task_for"
  }
}
```

Valid `assoc_type` values:
- `bug_in`
- `task_for`
- `scan_finding`
- `mentioned_in`

### 4) Trigger scanner (optional)

```json
{
  "tool": "trigger_scan",
  "arguments": {
    "scanner": "example_scanner",
    "file_path": "src/filigree/mcp_server.py",
    "api_url": "http://localhost:8377"
  }
}
```

`trigger_scan` registers the file and returns `file_id` + `scan_run_id` for correlation.

External producers that post directly to `POST /api/scan-results` should send
a globally unique, non-empty `scan_run_id` when they want the run to appear in
`GET /api/scan-runs`. Omitting it or sending `""` is still accepted for
fire-and-forget findings, but those findings are intentionally excluded from
scan-run history.

Filigree's scan-results endpoint ingests Filigree finding JSON, not raw SARIF.
If a Wardline or SARIF producer wants stable dedup and lifecycle behavior, its
adapter must map SARIF `result.partialFingerprints` or `result.fingerprints`
into each posted finding's `fingerprint` field before calling
`POST /api/scan-results`. Filigree preserves that `finding.fingerprint` through
readback, promotion, dedup, stale/fixed cleanup, and reopen-on-regress handling.

### 5) Verify from issue and file sides

Issue -> files:

```json
{
  "tool": "get_issue_files",
  "arguments": {
    "issue_id": "filigree-f2492d"
  }
}
```

File detail and timeline:

```json
{
  "tool": "get_file",
  "arguments": {
    "file_id": "filigree-file-abc123"
  }
}
```

```json
{
  "tool": "get_file_timeline",
  "arguments": {
    "file_id": "filigree-file-abc123",
    "limit": 50
  }
}
```

### Finding Lifecycle and Reasons

Finding statuses are:

- `open`: newly reported and actionable.
- `acknowledged`: reviewed but still active.
- `unseen_in_latest`: absent from the latest scan, but not yet aged out.
- `fixed`: terminal; no longer counted as active.
- `false_positive`: terminal; no longer counted as active.

`dismiss_finding` defaults to `false_positive`. Use its `status` argument when
the same dismissal action should instead record `fixed`, `unseen_in_latest`, or
`acknowledged`. Dismissal reasons are stored in finding metadata as
`dismiss_reason`; actor attribution is stored in `updated_by`. File deletion
without `force` treats only `fixed` and `false_positive` as terminal. Stale
`unseen_in_latest` findings are moved to `fixed` by `clean_stale_findings`.

## Dashboard UI Workflow

Use this when working manually in the web UI.

1. Open an issue detail panel.
2. In the issue panel, use the **Associated Files** section to jump directly into file detail.
3. In the **Files** view, open a file row to see findings, timeline, and current linked issues.
4. Use **Link to Issue** in file detail to add missing associations.
5. Return to the issue detail panel and confirm the file now appears under **Associated Files**.

## Troubleshooting Missing File Records or Missing Links

### Symptom: scan findings appear, but not under the expected file

Checklist:

1. Confirm scanner submitted project-relative paths in finding `path`.
2. Check for duplicate logical files with different path forms:
   - `src/filigree/mcp_server.py`
   - `/home/user/repo/src/filigree/mcp_server.py`
3. Use `list_files` (or `GET /api/files`) with `path_prefix` to inspect both variants.

Fix:

1. Standardize scanner output paths to project-relative form.
2. Re-run scan with normalized paths.
3. Re-link issue/file associations to the canonical file record if needed.

### Symptom: issue detail has no **Associated Files** section

Checklist:

1. Confirm associations exist via `get_issue_files` or `GET /api/issue/{issue_id}/files`.
2. Ensure the issue ID is correct for the current project.
3. If using server mode, verify you are in the correct project selector context.

### Symptom: scan run accepted but no findings visible

Checklist:

1. Confirm dashboard API was reachable from scanner process at trigger time.
2. Check scan run history via `GET /api/scan-runs`. If the producer sent an
   empty `scan_run_id`, this history is intentionally empty; query findings
   directly instead.
3. Query `GET /api/files/{file_id}/findings` directly to confirm ingestion.
4. If scanner sends no findings, a `202` response can be expected.

## Quick API Reference Used in This Guide

- `GET /api/files/_schema`
- `GET /api/files`
- `GET /api/files/{file_id}`
- `GET /api/files/{file_id}/findings`
- `GET /api/files/{file_id}/timeline`
- `POST /api/files/{file_id}/associations`
- `GET /api/issue/{issue_id}/files`
- `POST /api/scan-results` (living Loom alias; `/api/v1/scan-results` remains supported for classic integrations)
- `GET /api/scan-runs`
