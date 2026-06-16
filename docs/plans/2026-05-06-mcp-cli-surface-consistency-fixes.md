# MCP CLI Surface Consistency Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the two confirmed surface defects: full CLI/MCP issue payloads must expose `issue_id`, and agent-facing MCP/CLI documentation must teach the current 2.0 workflow and command names.

**Architecture:** Keep `Issue.to_dict()` / `IssueDict` as the internal and classic-dashboard shape so existing DB, dashboard, and classic API code does not churn. Add a neutral public issue projection for agent-facing surfaces, then route CLI JSON and MCP issue responses through that projection. Documentation and the MCP workflow prompt should describe `start_work` / `start_next_work` as the normal path and reserve `claim_issue` / `claim_next` for claim-only use.

**Tech Stack:** Python 3.13, Click CLI, MCP `Tool` schemas, `TypedDict` response contracts, pytest, ruff, mypy.

**Prerequisites:**
- Work in `/home/user/filigree`.
- Keep the existing dirty worktree intact; do not revert unrelated edits.
- Use `uv run ...` for all commands.
- Confirm tracker issues `filigree-e79c19ff6b` and `filigree-0e025002a6` are still open before starting.

---

### Task 1: Add a Public Issue Projection Contract

**Files:**
- Modify: `src/filigree/types/api.py`
- Create: `src/filigree/issue_payloads.py`
- Test: `tests/util/test_type_contracts.py`

**Step 1: Write failing contract tests**

Add tests near `TestIssueDictShape` / MCP response shape tests:

```python
from filigree.issue_payloads import issue_to_public
from filigree.types.api import PublicIssue


class TestPublicIssueShape:
    def test_keys_match(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Public issue", type="task")
        result = issue_to_public(issue)
        hints = get_type_hints(PublicIssue)
        assert set(result.keys()) == set(hints.keys())
        assert "issue_id" in result
        assert "id" not in result

    def test_value_types(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Public issue", type="task", priority=1, labels=["a"])
        result = issue_to_public(issue)
        assert isinstance(result["issue_id"], str)
        assert isinstance(result["title"], str)
        assert isinstance(result["priority"], int)
        assert isinstance(result["is_ready"], bool)
        assert isinstance(result["labels"], list)
```

Update the existing `IssueWithTransitions`, `IssueWithUnblocked`, and `ClaimNextResponse` tests to construct from `issue_to_public(issue)` instead of `issue.to_dict()`, and update the stale `SlimIssue(id="x", ...)` fixture to `SlimIssue(issue_id="x", ...)`.

**Why this test:** It pins the new public response shape without breaking the existing internal `IssueDict` tests, which should continue to assert `Issue.to_dict()` has `id`.

**Step 2: Run the tests to verify failure**

Run:

```bash
uv run pytest -q tests/util/test_type_contracts.py::TestPublicIssueShape tests/util/test_type_contracts.py::TestIssueWithTransitionsShape tests/util/test_type_contracts.py::TestIssueWithUnblockedShape tests/util/test_type_contracts.py::TestClaimNextResponseShape
```

Expected failure:

```text
ModuleNotFoundError: No module named 'filigree.issue_payloads'
```

or, after adding imports before implementation:

```text
ImportError: cannot import name 'PublicIssue'
```

**Step 3: Add the minimal public projection**

In `src/filigree/types/api.py`, add a full public issue shape next to `SlimIssue`:

```python
class PublicIssue(TypedDict):
    """Full issue shape for MCP and CLI JSON responses.

    Internal/classic code may still use IssueDict with `id`; agent-facing
    2.0 surfaces expose the entity primary key as `issue_id`.
    """

    issue_id: str
    title: str
    status: str
    status_category: StatusCategory
    priority: int
    type: str
    parent_id: str | None
    assignee: str
    created_at: ISOTimestamp
    updated_at: ISOTimestamp
    closed_at: ISOTimestamp | None
    description: str
    notes: str
    fields: dict[str, Any]
    labels: list[str]
    blocks: list[str]
    blocked_by: list[str]
    is_ready: bool
    children: list[str]
    data_warnings: list[str]
```

Then change:

```python
class IssueWithTransitions(PublicIssue):
    valid_transitions: NotRequired[list[TransitionDetail]]


class IssueWithChangedFields(PublicIssue):
    changed_fields: list[str]


class IssueWithUnblocked(PublicIssue):
    newly_unblocked: NotRequired[list[SlimIssue]]


class ClaimNextResponse(PublicIssue):
    selection_reason: str
```

Create `src/filigree/issue_payloads.py`:

```python
"""Public issue projections for agent-facing wire surfaces."""

from __future__ import annotations

from typing import Any

from filigree.models import Issue
from filigree.types.api import PublicIssue


def issue_to_public(issue: Issue) -> PublicIssue:
    """Return the full 2.0 public issue shape with `issue_id`.

    Keep `Issue.to_dict()` internal/classic-shaped; this helper is for MCP,
    CLI JSON, and other agent-facing surfaces that promise `issue_id`.
    """
    classic = issue.to_dict()
    return PublicIssue(
        issue_id=classic["id"],
        title=classic["title"],
        status=classic["status"],
        status_category=classic["status_category"],
        priority=classic["priority"],
        type=classic["type"],
        parent_id=classic["parent_id"],
        assignee=classic["assignee"],
        created_at=classic["created_at"],
        updated_at=classic["updated_at"],
        closed_at=classic["closed_at"],
        description=classic["description"],
        notes=classic["notes"],
        fields=classic["fields"],
        labels=classic["labels"],
        blocks=classic["blocks"],
        blocked_by=classic["blocked_by"],
        is_ready=classic["is_ready"],
        children=classic["children"],
        data_warnings=classic["data_warnings"],
    )


def public_issue_with(issue: Issue, **extra: Any) -> dict[str, Any]:
    """Return public issue payload plus response-specific extension keys."""
    payload: dict[str, Any] = dict(issue_to_public(issue))
    payload.update(extra)
    return payload
```

**Why minimal:** This avoids a repo-wide internal rename while giving CLI/MCP a single shared projection.

**Step 4: Run the contract tests**

Run:

```bash
uv run pytest -q tests/util/test_type_contracts.py::TestPublicIssueShape tests/util/test_type_contracts.py::TestIssueWithTransitionsShape tests/util/test_type_contracts.py::TestIssueWithUnblockedShape tests/util/test_type_contracts.py::TestClaimNextResponseShape
```

Expected output:

```text
... passed
```

**Definition of Done:**
- [ ] `PublicIssue` exists and uses `issue_id`, not `id`.
- [ ] `Issue.to_dict()` / `IssueDict` tests still pass unchanged.
- [ ] MCP extension response types inherit `PublicIssue`.
- [ ] Contract tests pass.

---

### Task 2: Route MCP Full Issue Responses Through the Public Projection

**Files:**
- Modify: `src/filigree/mcp_tools/issues.py`
- Test: `tests/mcp/test_tools.py`

**Step 1: Write failing MCP tests**

Add focused assertions in `tests/mcp/test_tools.py`:

```python
class TestPublicIssueVocabulary:
    async def test_create_issue_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "MCP public"})
        data = _parse(result)
        assert data["issue_id"].startswith("mcp-")
        assert "id" not in data

    async def test_get_issue_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP get public")
        result = await call_tool("get_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert "id" not in data

    async def test_list_issues_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP list public")
        result = await call_tool("list_issues", {"type": "task"})
        data = _parse(result)
        item = next(i for i in data["items"] if i["title"] == issue.title)
        assert item["issue_id"] == issue.id
        assert "id" not in item

    async def test_start_next_work_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP start next public", priority=0)
        result = await call_tool("start_next_work", {"assignee": "bot", "type": "task"})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert "id" not in data
```

Update existing MCP assertions that read `data["id"]` from full issue responses to `data["issue_id"]`.

**Why this test:** It proves all major full issue-returning MCP paths use one vocabulary, while slim responses already do.

**Step 2: Run the tests to verify failure**

Run:

```bash
uv run pytest -q tests/mcp/test_tools.py::TestPublicIssueVocabulary tests/mcp/test_tools.py::TestCreateAndGet tests/mcp/test_tools.py::TestListAndSearch
```

Expected failure:

```text
KeyError: 'issue_id'
```

or:

```text
AssertionError: assert 'id' not in data
```

**Step 3: Update MCP serialization**

Import the projection helpers:

```python
from filigree.issue_payloads import issue_to_public, public_issue_with
```

Replace full issue `to_dict()` returns in `src/filigree/mcp_tools/issues.py`:

```python
return _text(issue_to_public(issue))
```

For extended payloads:

```python
result = IssueWithChangedFields(
    **issue_to_public(issue),
    changed_fields=changed,
)
```

```python
result = public_issue_with(
    issue,
    newly_unblocked=[_slim_issue(i) for i in newly_unblocked],
)
```

For `get_issue` with optional fields:

```python
issue_payload = issue_to_public(issue)
out: dict[str, Any] = dict(issue_payload)
if include_files:
    out["files"] = file_assocs
...
```

For list:

```python
items = [issue_to_public(i) for i in issues]
```

Use `rg "to_dict\\(\\)" src/filigree/mcp_tools/issues.py` and replace every full issue response. Do not change `_slim_issue()` paths, because those already emit `issue_id`.

**Step 4: Run MCP tests**

Run:

```bash
uv run pytest -q tests/mcp/test_tools.py tests/mcp/test_boundary_validation.py tests/mcp/test_error_handling.py
```

Expected output:

```text
... passed
```

**Definition of Done:**
- [ ] MCP `create_issue`, `get_issue`, `list_issues`, `update_issue`, `close_issue`, `claim_issue`, `claim_next`, `release_claim`, `reopen_issue`, `start_work`, and `start_next_work` full issue payloads use `issue_id`.
- [ ] MCP slim and batch payloads still use existing `issue_id` shapes.
- [ ] No MCP response returns both `id` and `issue_id` for an issue entity.

---

### Task 3: Route CLI JSON Full Issue Responses Through the Same Projection

**Files:**
- Modify: `src/filigree/cli_commands/issues.py`
- Test: `tests/cli/test_issue_commands.py`

**Step 1: Write failing CLI tests**

Add a CLI vocabulary test class:

```python
class TestCliPublicIssueVocabulary:
    def test_create_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "CLI public", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"].startswith("test-")
        assert "id" not in data

    def test_show_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "CLI show public", "--json"])
        issue_id = json.loads(created.output)["issue_id"]
        result = runner.invoke(cli, ["show", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert "id" not in data

    def test_list_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "CLI list public"])
        result = runner.invoke(cli, ["list", "--type", "task", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        item = next(i for i in data["items"] if i["title"] == "CLI list public")
        assert "issue_id" in item
        assert "id" not in item

    def test_start_next_work_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "CLI start public", "--type", "task", "--priority", "0", "--json"])
        issue_id = json.loads(created.output)["issue_id"]
        result = runner.invoke(cli, ["start-next-work", "--assignee", "bot", "--type", "task", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert "id" not in data
```

Update existing CLI tests that read `data["id"]` from full issue JSON to `data["issue_id"]`.

**Why this test:** It catches exactly the live smoke failure from `filigree-e79c19ff6b`.

**Step 2: Run the tests to verify failure**

Run:

```bash
uv run pytest -q tests/cli/test_issue_commands.py::TestCliPublicIssueVocabulary
```

Expected failure:

```text
KeyError: 'issue_id'
```

**Step 3: Update CLI JSON serialization**

Import helpers in `src/filigree/cli_commands/issues.py`:

```python
from filigree.issue_payloads import issue_to_public, public_issue_with
```

Replace full issue JSON outputs:

```python
click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
```

For `show --with-files`:

```python
out: dict[str, Any] = dict(issue_to_public(issue))
out["files"] = files
```

For list:

```python
list_payload: dict[str, Any] = {
    "items": [issue_to_public(i) for i in issues],
    "has_more": has_more,
}
```

For `claim-next --json`:

```python
payload = public_issue_with(issue, selection_reason=issue.format_claim_next_reason())
```

Use `rg "to_dict\\(\\)" src/filigree/cli_commands/issues.py` and replace every full issue JSON response. Do not change plain text output.

**Step 4: Run CLI tests**

Run:

```bash
uv run pytest -q tests/cli/test_issue_commands.py tests/cli/test_query_commands.py tests/cli/test_boundary_validation.py
```

Expected output:

```text
... passed
```

**Definition of Done:**
- [ ] CLI full issue JSON payloads use `issue_id`.
- [ ] CLI slim responses (`ready`, `blocked`, search/batch slim projections) remain consistent.
- [ ] Plain-text CLI output remains unchanged.

---

### Task 4: Repair Agent-Facing MCP Prompt and Documentation

**Files:**
- Modify: `src/filigree/mcp_server.py`
- Modify: `docs/mcp.md`
- Modify: `docs/cli.md`
- Test: `tests/mcp/test_tools.py` or `tests/mcp/test_prompts.py`
- Optional Test: `tests/docs/test_surface_docs.py`

**Step 1: Write failing prompt/docs tests**

Add a prompt test:

```python
async def test_workflow_prompt_prefers_start_work(mcp_db: FiligreeDB) -> None:
    result = await get_workflow_prompt("filigree-workflow", {"include_context": "false"})
    text = "\n".join(msg.content.text for msg in result.messages)
    assert "Use `start_work` or `start_next_work`" in text
    assert "claim_issue` or `claim_next` to atomically claim" not in text
    assert "claim_issue / claim_next" in text
    assert "claim-only" in text
```

Add a lightweight docs guard:

```python
from pathlib import Path


def test_cli_docs_use_current_workflow_command_names() -> None:
    text = Path("docs/cli.md").read_text()
    assert "explain-state" not in text
    assert "workflow-states" not in text
    assert "explain-status" in text
    assert "workflow-statuses" in text


def test_mcp_docs_use_issue_id_for_issue_inputs() -> None:
    text = Path("docs/mcp.md").read_text()
    assert "| `id` | string | yes | Issue ID |" not in text
    assert "start_work" in text
    assert "start_next_work" in text
```

**Why this test:** The stale prompt is the user-visible MCP experience regression; the docs guard prevents the exact stale command/parameter drift from returning.

**Step 2: Run the tests to verify failure**

Run:

```bash
uv run pytest -q tests/mcp/test_tools.py::test_workflow_prompt_prefers_start_work tests/docs/test_surface_docs.py
```

If the docs test file is new, run the specific path after creating it.

Expected failure:

```text
AssertionError: assert 'Use `start_work` or `start_next_work`' in text
```

and/or:

```text
AssertionError: assert 'explain-state' not in text
```

**Step 3: Update the MCP workflow prompt**

In `src/filigree/mcp_server.py`, change the quick start and key tools:

```markdown
3. Use `start_work` or `start_next_work` to atomically claim and transition to a working status
4. Use `get_valid_transitions` to inspect allowed status changes before manual updates
...
- **start_work / start_next_work** — usual 2.0 path: claim + transition atomically
- **claim_issue / claim_next** — niche claim-only reservation; prefer start_work for normal execution
```

Also replace visible "state" wording in the prompt with "status" where it refers to user-facing workflow vocabulary.

**Step 4: Update docs**

In `docs/mcp.md`:

- Add `start_work` and `start_next_work` to the claiming section before claim-only tools.
- Change issue input parameter names from `id` to `issue_id` for `close_issue`, `claim_issue`, `release_claim`, and any other MCP issue tool rows.
- Describe `claim_issue` / `claim_next` as claim-only, not the primary start path.

In `docs/cli.md`:

- Replace `explain-state` with `explain-status`.
- Replace `workflow-states` with `workflow-statuses`.
- Replace section headers and parameter labels `state` with `status` for those commands.

**Step 5: Run docs/prompt tests**

Run:

```bash
uv run pytest -q tests/mcp/test_tools.py::test_workflow_prompt_prefers_start_work tests/docs/test_surface_docs.py
```

Expected output:

```text
... passed
```

**Definition of Done:**
- [ ] MCP prompt points agents to `start_work` / `start_next_work`.
- [ ] Claim-only tools are still documented, but as niche/reservation tools.
- [ ] MCP docs use `issue_id` for issue inputs.
- [ ] CLI docs list implemented command names only.

---

### Task 5: Full Surface Verification and Tracker Closeout

**Files:**
- No new source files unless earlier tasks require follow-up.
- Tracker: `filigree-e79c19ff6b`, `filigree-0e025002a6`

**Step 1: Run focused surface verification**

Run:

```bash
uv run pytest -q tests/cli tests/mcp tests/api/test_envelope_types.py tests/util/test_generation_parity.py tests/util/test_cross_surface_parity.py tests/test_error_envelope_contract.py
```

Expected output:

```text
... passed
```

**Step 2: Run full verification**

Run:

```bash
uv run pytest -q
uv run ruff check
uv run mypy src/filigree
```

Expected output:

```text
pytest: 100% passed
ruff: All checks passed!
mypy: Success: no issues found in 79 source files
```

Do not use `uv run mypy src/filigree tests` as the closeout gate unless the existing test typing debt is separately addressed; the review found it currently reports many test-only typing errors.

**Step 3: Live smoke the CLI vocabulary**

Use a temp project:

```bash
tmpdir="$(mktemp -d /tmp/filigree-surface.XXXXXX)"
cd "$tmpdir"
uv run --project /home/user/filigree filigree init --name Smoke --prefix smoke
uv run --project /home/user/filigree filigree create "Smoke task" --json
uv run --project /home/user/filigree filigree list --json
uv run --project /home/user/filigree filigree ready --json
uv run --project /home/user/filigree filigree start-next-work --assignee smoke-bot --type task --json
uv run --project /home/user/filigree filigree explain-status task open --help
uv run --project /home/user/filigree filigree workflow-statuses --help
```

Expected:

- Full issue JSON contains `issue_id`.
- Full issue JSON does not contain top-level `id`.
- `ready` still contains `issue_id`.
- Current command names exist.

**Step 4: Update and close tracker issues**

For `filigree-e79c19ff6b`, set:

```text
root_cause: Full issue CLI/MCP paths reused internal Issue.to_dict(), whose classic/internal shape uses id, while slim and loom surfaces had already migrated to issue_id.
fix_verification: [paste exact command list and passing outputs]
```

For `filigree-0e025002a6`, set:

```text
root_cause: MCP prompt and docs were not updated with the final 2.0 start_work/start_next_work and issue_id vocabulary after the command migration.
fix_verification: [paste exact command list and passing outputs]
```

Then transition each issue through the valid bug workflow:

```bash
uv run filigree update <issue-id> --status=fixing -f root_cause="..."
uv run filigree update <issue-id> --status=verifying -f fix_verification="..."
uv run filigree close <issue-id> --reason="..."
```

**Definition of Done:**
- [ ] Focused CLI/MCP/API surface suite passes.
- [ ] Full pytest passes.
- [ ] Ruff passes.
- [ ] Source mypy passes.
- [ ] Live CLI smoke proves `issue_id` vocabulary.
- [ ] Both Filigree issues are closed with root cause and verification evidence.

---

## Notes and Risks

- Do not globally rename `IssueDict.id` unless you intentionally migrate classic/dashboard/internal callers too. The low-risk fix is a public projection boundary.
- Do not import loom adapters into MCP/CLI. Keep MCP/CLI independent of generation-specific modules; the new helper is the shared neutral projection.
- Watch for tests that assert `data["id"]` from full MCP/CLI issue payloads; those assertions should become `data["issue_id"]`.
- Keep cross-entity references (`parent_id`, `blocks`, `blocked_by`, `children`) unchanged. Only the issue entity's own primary key becomes `issue_id`.
