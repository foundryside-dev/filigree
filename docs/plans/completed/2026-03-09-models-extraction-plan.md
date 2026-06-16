# Models Extraction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Break the circular dependency between core.py and types/core.py by extracting dataclasses into models.py.

**Architecture:** Create `src/filigree/models.py` with Issue, FileRecord, ScanFinding dataclasses and `_EMPTY_TS` sentinel. The dependency chain becomes `types/core.py <- models.py <- core.py`. All TYPE_CHECKING guards and local imports in mixin files become real module-level imports.

**Tech Stack:** Python 3.12+, dataclasses, typing (get_args), ruff, mypy, pytest

**Design doc:** `docs/plans/2026-03-09-models-extraction-design.md`

---

### Task 1: Create models.py with dataclasses

**Files:**
- Create: `src/filigree/models.py`
- Reference: `src/filigree/core.py:213-332` (current dataclass definitions)

**Step 1: Create models.py**

Create `src/filigree/models.py` with the following content. Note the key change:
`ScanFinding.__post_init__` uses `get_args()` on the Literal types from
`types/core.py` instead of importing from `db_files.py`.

```python
"""Pure data models — Issue, FileRecord, ScanFinding.

These dataclasses represent database rows as typed Python objects. They depend
only on ``filigree.types.core`` (TypedDicts and Literal types), so any module
in the package can import them without circular-dependency risk.

Extracted from ``core.py`` to break the cycle:
    types/core.py  <--  models.py  <--  core.py / db_*.py mixins
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, get_args

from filigree.types.core import (
    FileRecordDict,
    FindingStatus,
    ISOTimestamp,
    IssueDict,
    ScanFindingDict,
    Severity,
    StatusCategory,
)

_EMPTY_TS: ISOTimestamp = ISOTimestamp("")

# Derive valid sets from Literal types (avoids importing from db_files)
_VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))
_VALID_FINDING_STATUSES: frozenset[str] = frozenset(get_args(FindingStatus))


@dataclass
class Issue:
    id: str
    title: str
    status: str = "open"
    priority: int = 2
    type: str = "task"
    parent_id: str | None = None
    assignee: str = ""
    created_at: ISOTimestamp = _EMPTY_TS
    updated_at: ISOTimestamp = _EMPTY_TS
    closed_at: ISOTimestamp | None = None
    description: str = ""
    notes: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    # Computed (not stored directly)
    labels: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    is_ready: bool = False
    children: list[str] = field(default_factory=list)
    status_category: StatusCategory = "open"

    def to_dict(self) -> IssueDict:
        return IssueDict(
            id=self.id,
            title=self.title,
            status=self.status,
            status_category=self.status_category,
            priority=self.priority,
            type=self.type,
            parent_id=self.parent_id,
            assignee=self.assignee,
            created_at=self.created_at,
            updated_at=self.updated_at,
            closed_at=self.closed_at,
            description=self.description,
            notes=self.notes,
            fields=self.fields,
            labels=self.labels,
            blocks=self.blocks,
            blocked_by=self.blocked_by,
            is_ready=self.is_ready,
            children=self.children,
        )


@dataclass
class FileRecord:
    id: str
    path: str
    language: str = ""
    file_type: str = ""
    first_seen: ISOTimestamp = _EMPTY_TS
    updated_at: ISOTimestamp = _EMPTY_TS
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> FileRecordDict:
        return FileRecordDict(
            id=self.id,
            path=self.path,
            language=self.language,
            file_type=self.file_type,
            first_seen=self.first_seen,
            updated_at=self.updated_at,
            metadata=self.metadata,
        )


@dataclass
class ScanFinding:
    id: str
    file_id: str
    severity: Severity = "info"
    status: FindingStatus = "open"
    scan_source: str = ""
    rule_id: str = ""
    message: str = ""
    suggestion: str = ""
    scan_run_id: str = ""
    line_start: int | None = None
    line_end: int | None = None
    issue_id: str | None = None
    seen_count: int = 1
    first_seen: ISOTimestamp = _EMPTY_TS
    updated_at: ISOTimestamp = _EMPTY_TS
    last_seen_at: ISOTimestamp | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(f"Invalid severity {self.severity!r}, expected one of {sorted(_VALID_SEVERITIES)}")
        if self.status not in _VALID_FINDING_STATUSES:
            raise ValueError(f"Invalid finding status {self.status!r}, expected one of {sorted(_VALID_FINDING_STATUSES)}")

    def to_dict(self) -> ScanFindingDict:
        return ScanFindingDict(
            id=self.id,
            file_id=self.file_id,
            severity=self.severity,
            status=self.status,
            scan_source=self.scan_source,
            rule_id=self.rule_id,
            message=self.message,
            suggestion=self.suggestion,
            scan_run_id=self.scan_run_id,
            line_start=self.line_start,
            line_end=self.line_end,
            issue_id=self.issue_id,
            seen_count=self.seen_count,
            first_seen=self.first_seen,
            updated_at=self.updated_at,
            last_seen_at=self.last_seen_at,
            metadata=self.metadata,
        )
```

**Step 2: Run import smoke tests**

Run: `cd /home/user/filigree && uv run python -c "from filigree.models import Issue, FileRecord, ScanFinding; print('OK')"`
Expected: `OK`

Run: `cd /home/user/filigree && uv run python -c "from filigree.models import ScanFinding; ScanFinding(id='x', file_id='y', severity='bad')"`
Expected: `ValueError` (confirms `get_args()` produced valid validation sets)

**Step 3: Commit**

```bash
git add src/filigree/models.py
git commit -m "refactor: extract Issue, FileRecord, ScanFinding to models.py

Break circular dependency between core.py and types/core.py by moving
pure dataclasses to a new models.py module with no upward dependencies.
ScanFinding.__post_init__ now uses get_args() on Literal types instead
of importing VALID_* frozensets from db_files.py."
```

---

### Task 2: Update core.py — remove dataclasses, import and re-export

**Files:**
- Modify: `src/filigree/core.py:13-75` (imports), `src/filigree/core.py:210-332` (dataclass defs)

**Step 1: Update core.py**

Remove the dataclass definitions (lines 210-332) and the `_EMPTY_TS` sentinel (line 213). Replace with imports from models.py. Update `__all__` to include the re-exports. Remove `dataclass` and `field` from imports since core.py no longer defines dataclasses.

In the imports section, add:
```python
from filigree.models import FileRecord, Issue, ScanFinding, _EMPTY_TS
```

In `__all__`, add:
```python
    "FileRecord",
    "Issue",
    "ScanFinding",
    "_EMPTY_TS",
```

Remove from line 23:
```python
from dataclasses import dataclass, field
```

Remove the entire block from `_EMPTY_TS` definition through end of `ScanFinding.to_dict()` (lines 210-332), replacing with a comment:
```python
# Issue, FileRecord, ScanFinding moved to filigree.models (re-exported above)
```

**Step 2: Run smoke test and full test suite**

Run: `cd /home/user/filigree && uv run python -c "from filigree.core import Issue, FileRecord, ScanFinding, _EMPTY_TS; print('OK')"`
Expected: `OK`

Run: `cd /home/user/filigree && uv run pytest --tb=short -q`
Expected: All tests pass (validates re-export doesn't break any existing consumers)

**Step 3: Commit**

```bash
git add src/filigree/core.py
git commit -m "refactor(core): remove dataclass defs, re-export from models"
```

---

### Task 3: Fix PromoteObservationResult typing

**Files:**
- Modify: `src/filigree/types/core.py:1-5` (imports), `src/filigree/types/core.py:135-139`

**Step 1: Update types/core.py**

Add `Issue` to the existing `TYPE_CHECKING` block (do **not** add a top-level import — that would create a circular import with `models.py`):
```python
if TYPE_CHECKING:
    from filigree.models import Issue
```

Change line 138 from:
```python
    issue: Any  # Issue dataclass (circular import prevents direct typing)
```
to:
```python
    issue: Issue
```

This works because `types/core.py` already has `from __future__ import annotations`, so `Issue` in the annotation is a string at runtime and only resolved by mypy via the `TYPE_CHECKING` import. No runtime cycle.

If `Any` is no longer used elsewhere in this file, remove it from the `typing` import. Check: `Any` is used in `IssueDict.fields`, `FileRecordDict.metadata`, `ScanFindingDict.metadata` — so keep it.

**Step 2: Run import smoke test**

Run: `cd /home/user/filigree && uv run python -c "import filigree.models; import filigree.types.core; from filigree.types.core import PromoteObservationResult; print('OK')"`
Expected: `OK` (tests both import directions to confirm no cycle)

**Step 3: Commit**

```bash
git add src/filigree/types/core.py
git commit -m "fix(types): type PromoteObservationResult.issue as Issue (was Any)"
```

---

### Task 4: Update mixin imports — replace TYPE_CHECKING guards and local imports

**Files:**
- Modify: `src/filigree/db_base.py:8,13-14`
- Modify: `src/filigree/db_files.py:16,22-23,81-83,97-99`
- Modify: `src/filigree/db_issues.py:15,21-24,230-232`
- Modify: `src/filigree/db_planning.py:13,29-30`

**Step 1: Update db_base.py**

Add module-level import:
```python
from filigree.models import FileRecord, Issue
```

Remove `Issue` and `FileRecord` from the `TYPE_CHECKING` block (line 14):
```python
if TYPE_CHECKING:
    from filigree.core import FileRecord, Issue  # DELETE THIS LINE
    from filigree.templates import TemplateRegistry, TransitionOption
```
becomes:
```python
if TYPE_CHECKING:
    from filigree.templates import TemplateRegistry, TransitionOption
```

Remove `TYPE_CHECKING` from typing import if no other TYPE_CHECKING uses remain. Check: `TemplateRegistry` and `TransitionOption` are still under TYPE_CHECKING — keep it.

**Step 2: Update db_files.py**

Add module-level import:
```python
from filigree.models import FileRecord, ScanFinding
```

Remove from `TYPE_CHECKING` block (line 23):
```python
    from filigree.core import FileRecord, ScanFinding  # DELETE THIS LINE
```

Remove local import at line 83 inside `_build_file_record`:
```python
        from filigree.core import FileRecord  # DELETE THIS LINE
```

Remove local import at line 99 inside `_build_scan_finding`:
```python
        from filigree.core import ScanFinding  # DELETE THIS LINE
```

**Step 3: Update db_issues.py**

Add module-level import:
```python
from filigree.models import Issue
```

Remove from `TYPE_CHECKING` block (line 24):
```python
    from filigree.core import Issue  # DELETE THIS LINE
```

If `TYPE_CHECKING` block now only contains `Callable` import, keep it as-is (Callable is still needed under TYPE_CHECKING).

Remove local import at line 232 inside `_build_issues_batch`:
```python
        from filigree.core import Issue  # DELETE THIS LINE
```

**Step 4: Update db_planning.py**

Add module-level import:
```python
from filigree.models import Issue
```

Remove from `TYPE_CHECKING` block (line 30):
```python
    from filigree.core import Issue  # DELETE THIS LINE
```

If TYPE_CHECKING block now only contains `MilestoneInput`/`PhaseInput` imports, keep it.

**Step 5: Run full test suite**

Run: `cd /home/user/filigree && uv run pytest --tb=short -q`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/filigree/db_base.py src/filigree/db_files.py src/filigree/db_issues.py src/filigree/db_planning.py
git commit -m "refactor(mixins): replace TYPE_CHECKING guards with real imports from models"
```

---

### Task 5: Update non-mixin source files

**Files:**
- Modify: `src/filigree/__init__.py:10`
- Modify: `src/filigree/mcp_tools/common.py:15`
- Modify: `src/filigree/analytics.py:13`
- Modify: `src/filigree/summary.py:18`
- Modify: `src/filigree/dashboard_routes/analytics.py:18`

**Step 1: Update __init__.py**

Change line 10 from:
```python
from filigree.core import FiligreeDB, Issue
```
to:
```python
from filigree.core import FiligreeDB
from filigree.models import Issue
```

**Step 2: Update mcp_tools/common.py**

Change line 15 from:
```python
from filigree.core import Issue
```
to:
```python
from filigree.models import Issue
```

**Step 3: Update analytics.py**

Change line 13 from:
```python
from filigree.core import FiligreeDB, Issue
```
to:
```python
from filigree.core import FiligreeDB
from filigree.models import Issue
```

**Step 4: Update summary.py**

Change line 18 from:
```python
from filigree.core import FiligreeDB, Issue
```
to:
```python
from filigree.core import FiligreeDB
from filigree.models import Issue
```

**Step 5: Update dashboard_routes/analytics.py**

Change line 18 from:
```python
from filigree.core import FiligreeDB, Issue
```
to:
```python
from filigree.core import FiligreeDB
from filigree.models import Issue
```

**Step 6: Run full test suite**

Run: `cd /home/user/filigree && uv run pytest --tb=short -q`
Expected: All tests pass

**Step 7: Commit**

```bash
git add src/filigree/__init__.py src/filigree/mcp_tools/common.py src/filigree/analytics.py src/filigree/summary.py src/filigree/dashboard_routes/analytics.py
git commit -m "refactor: update non-mixin imports to use models.py directly"
```

---

### Task 6: Full CI validation

**Step 1: Run linters**

Run: `cd /home/user/filigree && uv run ruff check src/ tests/`
Expected: No errors (or only pre-existing ones)

**Step 2: Run formatter check**

Run: `cd /home/user/filigree && uv run ruff format --check src/ tests/`
Expected: All files formatted correctly

**Step 3: Run type checker**

Run: `cd /home/user/filigree && uv run mypy src/filigree/`
Expected: No new errors. `PromoteObservationResult.issue` should now type-check correctly.

**Step 4: Run full test suite**

Run: `cd /home/user/filigree && uv run pytest --tb=short`
Expected: All tests pass

**Step 5: Fix any issues found, commit fixes**
