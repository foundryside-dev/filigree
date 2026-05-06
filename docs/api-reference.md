# Python API Reference

Programmatic interface for the filigree issue tracker. All public classes and functions are importable from the `filigree` package or its submodules.

**See also:** [CLI Reference](cli.md) | [MCP Server](mcp.md) | [Architecture](architecture.md) | [Workflows](workflows.md)

> **Surface scope.** This page documents the in-process Python API (`FiligreeDB` and helpers). It returns native Python objects (`Issue`, tuples, `None`) and raises native exceptions. The unified 2.0 response envelopes (`BatchResponse`, `ListResponse`, `ErrorResponse` with closed `ErrorCode`) live one layer up, on the MCP and CLI surfaces — see [MCP Server](mcp.md), [CLI Reference](cli.md), and [Agent Integration](agent-integration.md). The mapping from `ValueError` / `KeyError` here to `ErrorCode` on the wire is in `filigree.types.api.classify_value_error`.

---

## Quick Start

```python
from filigree import FiligreeDB

with FiligreeDB.from_project() as db:
    issue = db.create_issue("Fix login bug", type="bug", priority=1)
    print(issue.id, issue.status)
```

---

## FiligreeDB

```python
from filigree import FiligreeDB
```

The central class for all issue tracker operations. Wraps a SQLite database with WAL mode, providing direct read/write access with no daemon or sync layer.

### Constructor

```python
FiligreeDB(
    db_path: str | Path,
    *,
    prefix: str = "filigree",
    enabled_packs: list[str] | None = None,
    template_registry: TemplateRegistry | None = None,
) -> None
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str \| Path` | *(required)* | Path to the SQLite database file |
| `prefix` | `str` | `"filigree"` | Prefix for generated issue IDs (e.g. `"myproject"` yields `myproject-a3f`) |
| `enabled_packs` | `list[str] \| None` | `None` | Workflow packs to enable. `None` reads from config; defaults to `["core", "planning"]` |
| `template_registry` | `TemplateRegistry \| None` | `None` | Inject a pre-configured registry (useful for testing). `None` creates one lazily |

### Class Method: `from_project`

```python
@classmethod
FiligreeDB.from_project(project_path: Path | None = None) -> FiligreeDB
```

Discovers the `.filigree/` directory by walking up from `project_path` (or the current working directory), reads `config.json`, creates the database connection, and calls `initialize()`. Returns a ready-to-use instance.

Raises `FileNotFoundError` if no `.filigree/` directory is found.

### Context Manager

`FiligreeDB` supports the context manager protocol. The connection is closed on exit:

```python
with FiligreeDB.from_project() as db:
    db.create_issue("My task")
# db.close() called automatically
```

### Properties

| Property | Type | Description |
|---|---|---|
| `conn` | `sqlite3.Connection` | Lazy-opened SQLite connection with WAL mode, foreign keys, and 5s busy timeout |
| `templates` | `TemplateRegistry` | Lazy-loaded template registry. Created on first access from `.filigree/` config |

---

### Setup Methods

#### `initialize`

```python
def initialize(self) -> None
```

Creates tables, runs pending schema migrations, and seeds built-in templates. Called automatically by `from_project()`. Safe to call multiple times (idempotent).

#### `close`

```python
def close(self) -> None
```

Closes the underlying SQLite connection. Safe to call multiple times.

#### `reload_templates`

```python
def reload_templates(self) -> None
```

Clears the cached `TemplateRegistry` so it reloads from disk on next access. Use after editing `.filigree/templates/` or `.filigree/packs/` files at runtime.

#### `get_schema_version`

```python
def get_schema_version(self) -> int
```

Returns the current database schema version (from SQLite `PRAGMA user_version`).

---

### CRUD Methods

#### `create_issue`

```python
def create_issue(
    self,
    title: str,
    *,
    type: str = "task",
    priority: int = 2,
    parent_id: str | None = None,
    assignee: str = "",
    description: str = "",
    notes: str = "",
    fields: dict[str, Any] | None = None,
    labels: list[str] | None = None,
    deps: list[str] | None = None,
    actor: str = "",
) -> Issue
```

Creates a new issue. The initial status is determined by the type's template (typically `"open"`). Generates a unique ID using the configured prefix.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `title` | `str` | *(required)* | Issue title. Cannot be empty |
| `type` | `str` | `"task"` | Issue type. Must be a registered type |
| `priority` | `int` | `2` | Priority 0-4 (0=critical, 4=backlog) |
| `parent_id` | `str \| None` | `None` | Parent issue ID for hierarchy |
| `assignee` | `str` | `""` | Assignee name |
| `description` | `str` | `""` | Detailed description |
| `notes` | `str` | `""` | Additional notes |
| `fields` | `dict[str, Any] \| None` | `None` | Custom fields defined by the type's template |
| `labels` | `list[str] \| None` | `None` | Labels to attach |
| `deps` | `list[str] \| None` | `None` | Issue IDs this issue depends on |
| `actor` | `str` | `""` | Identity for the audit trail |

**Returns:** The created `Issue`.

**Raises:** `ValueError` if the title is empty, priority is out of range, type is unknown, or parent_id is invalid.

#### `get_issue`

```python
def get_issue(self, issue_id: str) -> Issue
```

Retrieves a single issue with all computed fields (labels, dependencies, children, readiness).

**Raises:** `KeyError` if the issue does not exist.

#### `update_issue`

```python
def update_issue(
    self,
    issue_id: str,
    *,
    title: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    description: str | None = None,
    notes: str | None = None,
    parent_id: str | None = None,
    fields: dict[str, Any] | None = None,
    actor: str = "",
) -> Issue
```

Updates one or more fields on an existing issue. Only provided (non-`None`) fields are changed. Status transitions are validated against the type's workflow template. Fields are merged into the existing fields dict (not replaced).

Pass `parent_id=""` to clear the parent. Self-parenting and circular parent chains are rejected.

**Returns:** The updated `Issue`.

**Raises:**
- `KeyError` if the issue does not exist.
- `ValueError` if the status transition is not allowed, required fields are missing (hard enforcement), priority is out of range, or parent_id would create a cycle.

#### `close_issue`

```python
def close_issue(
    self,
    issue_id: str,
    *,
    reason: str = "",
    actor: str = "",
    status: str | None = None,
    fields: dict[str, Any] | None = None,
) -> Issue
```

Closes an issue by moving it to a done-category state. Direct close bypasses transition graph traversal, but hard-enforcement field gates still apply. Sets `closed_at` automatically.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `issue_id` | `str` | *(required)* | Issue to close |
| `reason` | `str` | `""` | Stored in `fields.close_reason` |
| `actor` | `str` | `""` | Identity for the audit trail |
| `status` | `str \| None` | `None` | Specific done-category state. `None` uses the first done state from the template |
| `fields` | `dict[str, Any] \| None` | `None` | Additional fields to merge while closing |

**Raises:** `ValueError` if the issue is already closed or the specified status is not a done-category state.

#### `reopen_issue`

```python
def reopen_issue(self, issue_id: str, *, actor: str = "") -> Issue
```

Reopens a closed issue, returning it to its type's initial state. Clears `closed_at`.

**Raises:** `ValueError` if the issue is not in a done-category state.

#### `list_issues`

```python
def list_issues(
    self,
    *,
    status: str | None = None,
    type: str | None = None,
    priority: int | None = None,
    parent_id: str | None = None,
    assignee: str | None = None,
    label: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Issue]
```

Lists issues with optional filters. Results are sorted by priority then creation time. All filters are ANDed together.

The `status` parameter supports both literal state names (e.g. `"triaged"`) and category aliases: passing `"open"`, `"in_progress"`/`"wip"`, or `"closed"`/`"done"` expands to all states in that category across all registered types.

#### `search_issues`

```python
def search_issues(
    self,
    query: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Issue]
```

Full-text search over issue titles and descriptions. Uses SQLite FTS5 with prefix matching. Falls back to `LIKE` if FTS is unavailable.

---

### Claiming Methods

#### `claim_issue`

```python
def claim_issue(
    self,
    issue_id: str,
    *,
    assignee: str,
    actor: str = "",
) -> Issue
```

Atomically claims an issue by setting its assignee. Uses optimistic locking -- the issue must be in an open-category state and either unassigned or already assigned to the same assignee. Does **not** change status.

**Raises:**
- `KeyError` if the issue does not exist.
- `ValueError` if the issue is already assigned to someone else or not in an open-category state.

#### `claim_next`

```python
def claim_next(
    self,
    assignee: str,
    *,
    type_filter: str | None = None,
    priority_min: int | None = None,
    priority_max: int | None = None,
    actor: str = "",
) -> Issue | None
```

Claims the highest-priority ready issue matching the filters. Iterates ready issues and attempts `claim_issue()` on each, handling race conditions with retry.

**Returns:** The claimed `Issue`, or `None` if no matching ready issues exist.

#### `release_claim`

```python
def release_claim(self, issue_id: str, *, actor: str = "") -> Issue
```

Releases a claimed issue by clearing its assignee. Does **not** change status.

**Raises:** `ValueError` if the issue has no assignee set.

#### `start_work`

```python
def start_work(
    self,
    issue_id: str,
    *,
    assignee: str,
    target_status: str | None = None,
    actor: str = "",
) -> Issue
```

Composed 2.0 operation: atomically claim an issue **and** transition it to a working status in one call. Performs `claim_issue` followed by `update_issue(status=target_status)` with a compensating-action rollback — if the transition fails, the claim is released. Rollback only fires when *this* call acquired the claim, so a pre-existing same-assignee claim is preserved.

`target_status` defaults to the type's `canonical_working_status()`. If the type defines multiple wip-category targets reachable from the current status, raises `AmbiguousTransitionError` (caller must specify `target_status` explicitly); if zero, raises `InvalidTransitionError`.

This is the recommended path for picking up work in 2.0 — `claim_issue` remains for the niche "reserve without transitioning" case.

**Raises:**
- `KeyError` if the issue does not exist.
- `ValueError` (`CONFLICT`) if the issue is already assigned to someone else.
- `AmbiguousTransitionError` / `InvalidTransitionError` from working-status resolution.

#### `start_next_work`

```python
def start_next_work(
    self,
    *,
    assignee: str,
    type_filter: str | None = None,
    priority_min: int | None = None,
    priority_max: int | None = None,
    target_status: str | None = None,
    actor: str = "",
) -> Issue | None
```

Composed 2.0 operation: claim the highest-priority ready issue matching the filters and atomically transition it to a working status. Same rollback contract as `start_work`. Tie-break ordering inherits from `claim_next` (priority asc, `created_at` asc, `issue_id` asc).

**Returns:** The claimed and transitioned `Issue`, or `None` if no matching ready issue exists.

---

### Batch Methods

#### `batch_close`

```python
def batch_close(
    self,
    issue_ids: list[str],
    *,
    reason: str = "",
    actor: str = "",
) -> tuple[list[Issue], list[dict[str, str]]]
```

Closes multiple issues with per-item error handling. Each issue is closed via `close_issue()`.

**Returns:** A 2-tuple of `(closed_issues, errors)` where each error is `{"id": str, "error": str}`.

#### `batch_update`

```python
def batch_update(
    self,
    issue_ids: list[str],
    *,
    status: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    fields: dict[str, Any] | None = None,
    actor: str = "",
) -> tuple[list[Issue], list[dict[str, str]]]
```

Applies the same changes to multiple issues. Errors on individual issues do not abort the batch.

**Returns:** A 2-tuple of `(updated_issues, errors)` where each error is `{"id": str, "error": str}`.

---

### File Traceability Methods

#### `register_file`

```python
def register_file(
    self,
    path: str,
    *,
    language: str = "",
    file_type: str = "",
    metadata: dict[str, Any] | None = None,
) -> FileRecord
```

Upserts a file record by project-relative path and returns the resulting `FileRecord`.

#### `list_files`

```python
def list_files(
    self,
    *,
    limit: int = 100,
    offset: int = 0,
    language: str | None = None,
    path_prefix: str | None = None,
    sort: str = "updated_at",
) -> list[FileRecord]
```

Lists tracked files with optional filtering and sorting.

#### `get_file`

```python
def get_file(self, file_id: str) -> FileRecord
```

Returns one file record by ID.

#### `delete_file_record`

```python
def delete_file_record(self, file_id: str, *, force: bool = False) -> dict[str, Any]
```

Deletes a file record plus file-domain cleanup rows. Without `force`, refuses records that still have issue associations or non-terminal findings; with `force`, cascades associations and findings and unlinks observations from the deleted file.

#### `get_file_timeline`

```python
def get_file_timeline(
    self,
    file_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    include_issue_events: bool = False,
) -> dict[str, Any]
```

Returns merged finding/association/metadata events for a file with pagination metadata.
Pass `include_issue_events=True` to merge events from associated issues; `event_type="issue_event"` filters to those issue-side events.

#### `get_issue_files`

```python
def get_issue_files(self, issue_id: str) -> list[dict[str, Any]]
```

Lists files associated with an issue.

#### `add_file_association`

```python
def add_file_association(
    self,
    file_id: str,
    issue_id: str,
    assoc_type: str,
) -> None
```

Creates an idempotent file<->issue association.

---

### Dependency Methods

#### `add_dependency`

```python
def add_dependency(
    self,
    issue_id: str,
    depends_on_id: str,
    *,
    dep_type: str = "blocks",
    actor: str = "",
) -> bool
```

Adds a dependency: `issue_id` depends on (is blocked by) `depends_on_id`. Validates both issues exist and rejects self-dependencies and cycles.

**Returns:** `True` if the dependency was created, `False` if it already existed.

**Raises:**
- `KeyError` if either issue does not exist.
- `ValueError` for self-dependencies or if the dependency would create a cycle.

#### `remove_dependency`

```python
def remove_dependency(
    self,
    issue_id: str,
    depends_on_id: str,
    *,
    actor: str = "",
) -> bool
```

Removes a dependency between two issues.

**Returns:** `True` if removed, `False` if the dependency did not exist.

#### `get_all_dependencies`

```python
def get_all_dependencies(self) -> list[dict[str, str]]
```

Returns all dependencies as a list of `{"from": str, "to": str, "type": str}` dicts, where `"from"` is the blocked issue and `"to"` is the blocker.

---

### Query Methods

#### `get_ready`

```python
def get_ready(self) -> list[Issue]
```

Returns issues in open-category states with no unresolved blockers, sorted by priority then creation time.

#### `get_blocked`

```python
def get_blocked(self) -> list[Issue]
```

Returns issues in open-category states that have at least one non-done blocker.

#### `get_critical_path`

```python
def get_critical_path(self) -> list[dict[str, Any]]
```

Computes the longest dependency chain among non-done issues using topological-order dynamic programming.

**Returns:** The chain as a list of `{"id": str, "title": str, "priority": int, "type": str}` dicts, ordered from root blocker to final blocked issue. Empty list if no chains exist.

---

### Planning Methods

#### `get_plan`

```python
def get_plan(self, milestone_id: str) -> dict[str, Any]
```

Retrieves the milestone/phase/step hierarchy with progress statistics.

**Returns:**
```python
{
    "milestone": dict,           # Issue.to_dict()
    "phases": [
        {
            "phase": dict,       # Issue.to_dict()
            "steps": [dict, ...],
            "total": int,
            "completed": int,
            "ready": int,
        },
    ],
    "total_steps": int,
    "completed_steps": int,
}
```

#### `create_plan`

```python
def create_plan(
    self,
    milestone: dict[str, Any],
    phases: list[dict[str, Any]],
    *,
    actor: str = "",
) -> dict[str, Any]
```

Creates a full milestone, phase, and step hierarchy in one transaction.

| Parameter | Type | Description |
|---|---|---|
| `milestone` | `dict` | `{"title": str, "priority?": int, "description?": str, "fields?": dict}` |
| `phases` | `list[dict]` | `[{"title": str, "priority?": int, "description?": str, "steps": [{"title": str, "deps?": [int \| str]}]}]` |
| `actor` | `str` | Identity for the audit trail |

Step dependencies use integer indices (0-based within the same phase) or cross-phase references as `"phase_idx.step_idx"` strings.

**Returns:** The full plan tree (same format as `get_plan()`).

**Raises:** `ValueError` if any title is empty.

---

### Comment Methods

#### `add_comment`

```python
def add_comment(
    self,
    issue_id: str,
    text: str,
    *,
    author: str = "",
) -> int
```

Adds a comment to an issue.

**Returns:** The comment's integer ID.

**Raises:** `ValueError` if text is empty.

#### `get_comments`

```python
def get_comments(self, issue_id: str) -> list[dict[str, Any]]
```

Returns all comments on an issue, ordered chronologically. Each dict contains `id`, `author`, `text`, and `created_at`.

---

### Label Methods

#### `add_label`

```python
def add_label(self, issue_id: str, label: str) -> bool
```

Adds a label to an issue. **Returns:** `True` if added, `False` if already present.

#### `remove_label`

```python
def remove_label(self, issue_id: str, label: str) -> bool
```

Removes a label from an issue. **Returns:** `True` if removed, `False` if not found.

---

### Stats and Events

#### `get_stats`

```python
def get_stats(self) -> dict[str, Any]
```

Returns project statistics:

```python
{
    "by_status": {"open": 5, "in_progress": 2, ...},
    "by_category": {"open": 5, "wip": 2, "done": 10},
    "by_type": {"task": 8, "bug": 4, ...},
    "ready_count": int,
    "blocked_count": int,
    "total_dependencies": int,
}
```

#### `get_recent_events`

```python
def get_recent_events(self, limit: int = 20) -> list[dict[str, Any]]
```

Returns the most recent events across all issues, newest first. Each dict includes all event fields plus `issue_title`.

#### `get_events_since`

```python
def get_events_since(self, since: str, *, limit: int = 100) -> list[dict[str, Any]]
```

Returns events after the given ISO timestamp, ordered chronologically (oldest first). Useful for session resumption and polling.

#### `get_issue_events`

```python
def get_issue_events(self, issue_id: str, *, limit: int = 50) -> list[dict[str, Any]]
```

Returns events for a specific issue, newest first.

**Raises:** `KeyError` if the issue does not exist.

---

### Undo

#### `undo_last`

```python
def undo_last(self, issue_id: str, *, actor: str = "") -> dict[str, Any]
```

Undoes the most recent reversible event for an issue. Reversible events: `status_changed`, `title_changed`, `priority_changed`, `assignee_changed`, `claimed`, `dependency_added`, `dependency_removed`, `description_changed`, `notes_changed`.

**Returns:**
```python
# Success:
{"undone": True, "event_type": str, "event_id": int, "issue": dict}

# Nothing to undo:
{"undone": False, "reason": str}
```

---

### Template Methods

#### `get_template`

```python
def get_template(self, issue_type: str) -> dict[str, Any] | None
```

Returns the workflow template for a type as a dict with `type`, `display_name`, `description`, `states`, `initial_state`, `transitions`, and `fields_schema`. Returns `None` if the type is not registered.

#### `list_templates`

```python
def list_templates(self) -> list[dict[str, Any]]
```

Lists all registered templates (respects `enabled_packs`), sorted by type name. Each dict contains `type`, `display_name`, `description`, and `fields_schema`.

---

### Transition Methods

#### `get_valid_transitions`

```python
def get_valid_transitions(self, issue_id: str) -> list[TransitionOption]
```

Returns valid next states for an issue with readiness indicators. Each `TransitionOption` shows which fields are needed before the transition can proceed. See [TransitionOption](#transitionoption) below.

#### `validate_issue`

```python
def validate_issue(self, issue_id: str) -> ValidationResult
```

Validates an issue against its type template. Checks fields required at the current state and fields needed for upcoming transitions. See [ValidationResult](#validationresult) below.

---

### Data Import/Export

#### `export_jsonl`

```python
def export_jsonl(self, output_path: str | Path) -> int
```

Exports all issues, dependencies, labels, comments, and events to a JSONL file. Each line is a JSON object with a `_type` field (`"issue"`, `"dependency"`, `"label"`, `"comment"`, `"event"`).

**Returns:** Total number of records written.

#### `import_jsonl`

```python
def import_jsonl(self, input_path: str | Path, *, merge: bool = False) -> int
```

Imports records from a JSONL file.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `input_path` | `str \| Path` | *(required)* | Path to the JSONL file |
| `merge` | `bool` | `False` | If `True`, skips existing records. If `False`, raises on conflict |

**Returns:** Number of records imported.

---

### Archival Methods

#### `archive_closed`

```python
def archive_closed(self, *, days_old: int = 30, actor: str = "", label: str | None = None) -> list[str]
```

Archives issues that have been closed for more than `days_old` days by setting their status to `"archived"`.
When `label` is provided, only closed issues currently carrying that label are archived.

**Returns:** List of archived issue IDs.

#### `compact_events`

```python
def compact_events(self, *, keep_recent: int = 50, actor: str = "") -> int
```

Removes old events for archived issues, keeping only the `keep_recent` most recent events per issue.

**Returns:** Number of events deleted.

---

## Issue

```python
from filigree import Issue
```

A mutable dataclass representing an issue with both stored and computed fields.

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | `str` | *(required)* | Unique identifier (e.g. `"myproject-a3f"`) |
| `title` | `str` | *(required)* | Issue title |
| `status` | `str` | `"open"` | Current workflow state |
| `priority` | `int` | `2` | Priority level 0-4 (0=critical) |
| `type` | `str` | `"task"` | Issue type (e.g. `"task"`, `"bug"`, `"feature"`) |
| `parent_id` | `str \| None` | `None` | Parent issue ID for hierarchy |
| `assignee` | `str` | `""` | Assigned agent or user |
| `created_at` | `str` | `""` | ISO 8601 creation timestamp |
| `updated_at` | `str` | `""` | ISO 8601 last-update timestamp |
| `closed_at` | `str \| None` | `None` | ISO 8601 close timestamp, or `None` if open |
| `description` | `str` | `""` | Detailed description |
| `notes` | `str` | `""` | Additional notes |
| `fields` | `dict[str, Any]` | `{}` | Custom fields defined by the type's template |

**Computed fields** (populated by `FiligreeDB` when retrieving issues):

| Field | Type | Default | Description |
|---|---|---|---|
| `labels` | `list[str]` | `[]` | Attached labels |
| `blocks` | `list[str]` | `[]` | Issue IDs that this issue blocks |
| `blocked_by` | `list[str]` | `[]` | Issue IDs blocking this issue (only non-done blockers) |
| `is_ready` | `bool` | `False` | `True` if in open-category state with no unresolved blockers |
| `children` | `list[str]` | `[]` | Child issue IDs |
| `status_category` | `str` | `"open"` | Resolved category: `"open"`, `"wip"`, or `"done"` |

### Methods

#### `to_dict`

```python
def to_dict(self) -> dict[str, Any]
```

Serializes the issue to a plain dict suitable for JSON output.

---

## TemplateRegistry

```python
from filigree.templates import TemplateRegistry
```

Loads, caches, and queries workflow templates and packs. Templates are loaded once per instance and cached for the entire lifetime. Typically accessed via `FiligreeDB.templates` rather than instantiated directly.

### Constructor

```python
TemplateRegistry() -> None
```

Creates an empty registry. Call `load()` to populate it.

### Loading

#### `load`

```python
def load(
    self,
    filigree_dir: Path,
    *,
    enabled_packs: list[str] | None = None,
) -> None
```

Loads templates from three layers (later layers override earlier ones):

1. **Built-in packs** from `filigree.templates_data.BUILT_IN_PACKS`
2. **Installed packs** from `.filigree/packs/*.json`
3. **Project-local overrides** from `.filigree/templates/*.json`

Idempotent: a second call is a no-op.

### Query Methods

| Method | Signature | Description |
|---|---|---|
| `get_type` | `(type_name: str) -> TypeTemplate \| None` | Get a type template by name |
| `get_pack` | `(pack_name: str) -> WorkflowPack \| None` | Get a workflow pack by name |
| `list_types` | `() -> list[TypeTemplate]` | All types from enabled packs |
| `list_packs` | `() -> list[WorkflowPack]` | All enabled packs |
| `get_initial_state` | `(type_name: str) -> str` | Initial state for a type. Falls back to `"open"` |
| `get_category` | `(type_name: str, state: str) -> StateCategory \| None` | Map `(type, state)` to category via O(1) cache |
| `get_valid_states` | `(type_name: str) -> list[str] \| None` | Valid state names for a type. `None` if unknown |
| `get_first_state_of_category` | `(type_name: str, category: StateCategory) -> str \| None` | First state of a given category |

### Validation Methods

#### `validate_transition`

```python
def validate_transition(
    self,
    type_name: str,
    from_state: str,
    to_state: str,
    fields: dict[str, Any],
) -> TransitionResult
```

Validates a state transition. Unknown types allow all transitions (permissive fallback).

**Returns:** A `TransitionResult` indicating whether the transition is allowed.

#### `get_valid_transitions`

```python
def get_valid_transitions(
    self,
    type_name: str,
    current_state: str,
    fields: dict[str, Any],
) -> list[TransitionOption]
```

Returns all valid transitions from the current state with readiness info.

#### `validate_fields_for_state`

```python
def validate_fields_for_state(
    self,
    type_name: str,
    state: str,
    fields: dict[str, Any],
) -> list[str]
```

Returns field names that are required at the given state but not yet populated.

### Static Methods

#### `parse_type_template`

```python
@staticmethod
TemplateRegistry.parse_type_template(raw: dict[str, Any]) -> TypeTemplate
```

Parses a type template from a JSON-compatible dict. Enforces size limits (max 50 states, 200 transitions, 50 fields).

**Raises:** `ValueError` for invalid data, `KeyError` for missing required keys.

#### `validate_type_template`

```python
@staticmethod
TemplateRegistry.validate_type_template(tpl: TypeTemplate) -> list[str]
```

Validates a `TypeTemplate` for internal consistency (state references, field references).

**Returns:** List of error messages. Empty list means valid.

---

## Template Data Types

All template data types are frozen (immutable) dataclasses defined in `filigree.templates`.

```python
from filigree.templates import (
    StateDefinition,
    TransitionDefinition,
    FieldSchema,
    TypeTemplate,
    WorkflowPack,
    TransitionResult,
    TransitionOption,
    ValidationResult,
)
```

### StateDefinition

A named state within a type's workflow.

| Field | Type | Description |
|---|---|---|
| `name` | `str` | State name (lowercase, alphanumeric + underscore, max 64 chars) |
| `category` | `StateCategory` | One of `"open"`, `"wip"`, `"done"` |

### TransitionDefinition

A valid state transition with enforcement level and field requirements.

| Field | Type | Default | Description |
|---|---|---|---|
| `from_state` | `str` | *(required)* | Source state |
| `to_state` | `str` | *(required)* | Target state |
| `enforcement` | `EnforcementLevel` | *(required)* | `"hard"` (blocks transition) or `"soft"` (warns only) |
| `requires_fields` | `tuple[str, ...]` | `()` | Fields that must be populated for this transition |

### FieldSchema

Schema for a custom field on an issue type.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | *(required)* | Field name |
| `type` | `FieldType` | *(required)* | One of `"text"`, `"enum"`, `"number"`, `"date"`, `"list"`, `"boolean"` |
| `description` | `str` | `""` | Human-readable description |
| `options` | `tuple[str, ...]` | `()` | Valid values for `"enum"` fields |
| `default` | `Any` | `None` | Default value |
| `required_at` | `tuple[str, ...]` | `()` | States at which this field must be populated |

### TypeTemplate

Complete workflow definition for an issue type.

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | `str` | *(required)* | Type identifier |
| `display_name` | `str` | *(required)* | Human-readable name |
| `description` | `str` | *(required)* | Type description |
| `pack` | `str` | *(required)* | Workflow pack this type belongs to |
| `states` | `tuple[StateDefinition, ...]` | *(required)* | All states in the workflow |
| `initial_state` | `str` | *(required)* | State for newly created issues |
| `transitions` | `tuple[TransitionDefinition, ...]` | *(required)* | Valid state transitions |
| `fields_schema` | `tuple[FieldSchema, ...]` | *(required)* | Custom fields for this type |
| `suggested_children` | `tuple[str, ...]` | `()` | Suggested child issue types |
| `suggested_labels` | `tuple[str, ...]` | `()` | Suggested labels |

### WorkflowPack

A bundle of related type templates.

| Field | Type | Default | Description |
|---|---|---|---|
| `pack` | `str` | *(required)* | Pack identifier |
| `version` | `str` | *(required)* | Semantic version |
| `display_name` | `str` | *(required)* | Human-readable name |
| `description` | `str` | *(required)* | Pack description |
| `types` | `dict[str, TypeTemplate]` | *(required)* | Type templates in this pack |
| `requires_packs` | `tuple[str, ...]` | *(required)* | Pack dependencies |
| `relationships` | `tuple[dict[str, Any], ...]` | *(required)* | Intra-pack type relationships |
| `cross_pack_relationships` | `tuple[dict[str, Any], ...]` | *(required)* | Cross-pack type relationships |
| `guide` | `dict[str, Any] \| None` | *(required)* | Workflow guide content (state diagram, tips) |

### TransitionResult

Result of validating a specific state transition.

| Field | Type | Description |
|---|---|---|
| `allowed` | `bool` | Whether the transition is permitted |
| `enforcement` | `EnforcementLevel \| None` | `"hard"`, `"soft"`, or `None` (unknown transition) |
| `missing_fields` | `tuple[str, ...]` | Fields required but not populated |
| `warnings` | `tuple[str, ...]` | Warning messages (e.g. soft enforcement advisories) |

### TransitionOption

A possible next state from the current state.

| Field | Type | Description |
|---|---|---|
| `to` | `str` | Target state name |
| `category` | `StateCategory` | Target state category |
| `enforcement` | `EnforcementLevel \| None` | Enforcement level for this transition |
| `requires_fields` | `tuple[str, ...]` | All fields required for this transition |
| `missing_fields` | `tuple[str, ...]` | Required fields not yet populated |
| `ready` | `bool` | `True` if all required fields are populated |

### ValidationResult

Result of validating an issue against its template.

| Field | Type | Description |
|---|---|---|
| `valid` | `bool` | Whether the issue passes validation |
| `warnings` | `tuple[str, ...]` | Advisory messages for missing recommended fields |
| `errors` | `tuple[str, ...]` | Hard validation errors |

### Type Aliases

```python
StateCategory = Literal["open", "wip", "done"]
EnforcementLevel = Literal["hard", "soft"]
FieldType = Literal["text", "enum", "number", "date", "list", "boolean"]
```

---

## Exceptions

```python
from filigree.templates import TransitionNotAllowedError, HardEnforcementError
```

### TransitionNotAllowedError

Subclass of `ValueError`. Raised when a transition is not defined in the type's transition table.

```python
class TransitionNotAllowedError(ValueError):
    from_state: str
    to_state: str
    type_name: str
```

### HardEnforcementError

Subclass of `ValueError`. Raised when a hard-enforced transition fails field validation.

```python
class HardEnforcementError(ValueError):
    from_state: str
    to_state: str
    type_name: str
    missing_fields: list[str]
```

---

## Module Functions

```python
from filigree.core import find_filigree_root, read_config, write_config
```

### `find_filigree_root`

```python
def find_filigree_root(start: Path | None = None) -> Path
```

Walks up from `start` (default: current working directory) looking for a `.filigree/` directory.

**Returns:** The `.filigree/` directory path (not the project root).

**Raises:** `FileNotFoundError` if no `.filigree/` directory is found.

### `read_config`

```python
def read_config(filigree_dir: Path) -> dict[str, Any]
```

Reads `.filigree/config.json`. Returns defaults (`{"prefix": "filigree", "version": 1, "enabled_packs": ["core", "planning"]}`) if the file is missing.

### `write_config`

```python
def write_config(filigree_dir: Path, config: dict[str, Any]) -> None
```

Writes a config dict to `.filigree/config.json`.

---

## Analytics

```python
from filigree.analytics import cycle_time, lead_time, get_flow_metrics
```

Flow metrics derived from event history. All functions operate read-only on a `FiligreeDB` instance.

### `cycle_time`

```python
def cycle_time(db: FiligreeDB, issue_id: str) -> float | None
```

Computes cycle time in hours: time from the first `in_progress` status to `closed`.

**Returns:** Hours as a float, or `None` if the issue has not completed the in_progress-to-closed cycle.

### `lead_time`

```python
def lead_time(db: FiligreeDB, issue_id: str) -> float | None
```

Computes lead time in hours: time from issue creation to close.

**Returns:** Hours as a float, or `None` if the issue is not closed.

### `get_flow_metrics`

```python
def get_flow_metrics(db: FiligreeDB, *, days: int = 30) -> dict[str, Any]
```

Computes aggregate flow metrics for issues closed within the last `days` days.

**Returns:**

```python
{
    "period_days": int,
    "throughput": int,                    # Number of issues closed in the period
    "avg_cycle_time_hours": float | None, # Average cycle time, or None if no data
    "avg_lead_time_hours": float | None,  # Average lead time, or None if no data
    "by_type": {
        "task": {"avg_cycle_time_hours": float, "count": int},
        ...
    },
}
```
