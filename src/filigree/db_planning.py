"""PlanningMixin — dependencies, plans, and DAG queries.

Extracted from core.py as part of the module architecture split.
All methods access ``self.conn``, ``self.get_issue()``, etc. via
Python's MRO when composed into ``FiligreeDB``.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from filigree.db_base import DBMixinProtocol, _now_iso
from filigree.models import _EMPTY_TS, Issue
from filigree.types.api import BatchFailure
from filigree.types.core import IssueDict
from filigree.types.planning import (
    ChildSummary,
    CriticalPathNode,
    DependencyRecord,
    IssueRef,
    PlanPhase,
    PlanTree,
    ProgressDict,
    ReleaseSummaryItem,
    ReleaseTree,
    TreeNode,
)

if TYPE_CHECKING:
    from filigree.types.inputs import MilestoneInput, PhaseInput

logger = logging.getLogger(__name__)


_MAX_TREE_DEPTH = 10


def _coerce_sequence(value: object) -> int | None:
    """Return the numeric sequence for *value*, or None if it has none.

    Accepts ints and int-valued strings (the CLI stores ``--field`` values as
    strings). Unlike :func:`_sequence_sort_key`, it substitutes no sentinel for
    missing / non-numeric values, so callers computing a max can ignore them.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _sequence_sort_key(value: object) -> tuple[int, int | str]:
    coerced = _coerce_sequence(value)
    if coerced is not None:
        return (0, coerced)
    if isinstance(value, str):
        return (1, value)
    return (0, 999)


def _validate_priority(value: Any, label: str) -> None:
    """Validate a plan-input priority up front.

    Mirrors the DB-layer CHECK constraint (``priority BETWEEN 0 AND 4``) so
    bad values surface as ValueError before the transaction begins — preventing
    a partial insert from tripping ``sqlite3.IntegrityError`` mid-plan and
    bubbling raw DB errors to CLI callers.

    Rejects booleans explicitly since ``bool`` is a subclass of ``int`` and
    would otherwise pass the range check silently.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 4):
        msg = f"{label} 'priority' must be an integer 0-4, got {value!r}"
        raise ValueError(msg)


def _validate_plan_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"{label} must be an object"
        raise ValueError(msg)
    return value


def _validate_plan_title(data: Mapping[str, Any], label: str) -> None:
    title = data.get("title", "")
    if not isinstance(title, str):
        msg = f"{label} 'title' must be a string"
        raise ValueError(msg)
    if not title.strip():
        msg = f"{label} 'title' is required and cannot be empty"
        raise ValueError(msg)


def _validate_plan_optional_string(data: Mapping[str, Any], key: str, label: str) -> None:
    if key in data and not isinstance(data[key], str):
        msg = f"{label} '{key}' must be a string"
        raise ValueError(msg)


def _normalize_plan_fields(fields: Any, label: str) -> dict[str, Any]:
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        msg = f"{label} fields must be a dict"
        raise TypeError(msg)
    for key in fields:
        if not isinstance(key, str):
            msg = f"{label} field keys must be strings"
            raise TypeError(msg)
        if not key.strip():
            msg = f"{label} field key cannot be empty"
            raise ValueError(msg)
    return fields


def _normalize_dep_ref(dep_ref: Any) -> str:
    """Validate a step ``dep_ref`` and return its canonical string form.

    Accepts:
    - ``int`` (excluding ``bool``): same-phase step index, must be non-negative.
    - ``str`` matching ``"N"`` or ``"P.S"`` where each part is a non-negative
      decimal integer literal.

    Rejects floats, booleans, malformed strings, negatives, and anything else
    with ``ValueError``. ``str()`` coercion at the parsing site silently
    accepted floats like ``0.1`` as ``phase 0 step 1``; this guard runs first
    so the DB API surface matches the validation already done by CLI/MCP.
    """
    if isinstance(dep_ref, bool):
        msg = f"dep_ref must be int or str, got bool: {dep_ref!r}"
        raise ValueError(msg)
    if isinstance(dep_ref, int):
        if dep_ref < 0:
            msg = f"Negative dep index not allowed: {dep_ref}"
            raise ValueError(msg)
        return str(dep_ref)
    if isinstance(dep_ref, str):
        parts = dep_ref.split(".")
        if len(parts) > 2 or any(not p.isdigit() for p in parts):
            msg = f"dep_ref must be 'N' or 'P.S' with non-negative integer parts, got {dep_ref!r}"
            raise ValueError(msg)
        return dep_ref
    msg = f"dep_ref must be int or str, got {type(dep_ref).__name__}: {dep_ref!r}"
    raise ValueError(msg)


def _merge_labels(*label_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in label_groups:
        for label in group:
            if label not in seen:
                merged.append(label)
                seen.add(label)
    return merged


class NotAReleaseError(ValueError):
    """Raised by ``get_release_tree`` when the issue exists but is not a release.

    Subclasses ``ValueError`` so existing ``except ValueError`` call sites keep
    working; new callers can disambiguate this case from unrelated ValueErrors
    (e.g. data-validation failures in ``Issue.__post_init__``) by catching this
    subclass specifically.
    """


class NotAMilestoneError(ValueError):
    """Raised by ``get_plan`` when the issue exists but is not a milestone."""


def _truncated_issue_sentinel(issue_id: str) -> IssueDict:
    """Minimal IssueDict placeholder for tree nodes truncated at the depth limit."""
    return IssueDict(
        id=issue_id,
        title="(truncated)",
        status="",
        status_category="open",
        priority=0,
        type="",
        parent_id=None,
        assignee="",
        claimed_at=None,
        last_heartbeat_at=None,
        claim_expires_at=None,
        created_at=_EMPTY_TS,
        updated_at=_EMPTY_TS,
        closed_at=None,
        description="",
        notes="",
        fields={},
        labels=[],
        blocks=[],
        blocked_by=[],
        is_ready=False,
        children=[],
        data_warnings=["Tree depth limit reached; children truncated"],
    )


class PlanningMixin(DBMixinProtocol):
    """Dependencies, plans, and DAG queries (ready/blocked/critical path).

    Inherits ``DBMixinProtocol`` for type-safe access to shared attributes.
    Actual implementations provided by ``FiligreeDB`` at composition time via MRO.
    """

    # -- Dependencies --------------------------------------------------------

    def add_dependency(self, issue_id: str, depends_on_id: str, *, dep_type: str = "blocks", actor: str = "") -> bool:
        # Reject cross-project IDs up front so the prefix mismatch is reported
        # for *either* argument, not whichever happens to be looked up first.
        self._check_id_prefix(issue_id)
        self._check_id_prefix(depends_on_id)
        # Validate both issues exist
        self.get_issue(issue_id)  # raises KeyError if not found
        self.get_issue(depends_on_id)  # raises KeyError if not found

        if issue_id == depends_on_id:
            msg = f"Cannot add self-dependency: {issue_id}"
            raise ValueError(msg)

        # Check for cycles: would depends_on_id transitively reach issue_id?
        if self._would_create_cycle(issue_id, depends_on_id):
            msg = f"Dependency {issue_id} -> {depends_on_id} would create a cycle"
            raise ValueError(msg)

        now = _now_iso()
        try:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, ?, ?)",
                (issue_id, depends_on_id, dep_type, now),
            )
            if cursor.rowcount == 0:
                # INSERT OR IGNORE still opens an implicit write transaction even
                # when no row changes; without an explicit rollback the lock
                # lingers and blocks other connections.
                self.conn.rollback()
                return False  # Already exists — no-op, no event
            self._record_event(issue_id, "dependency_added", actor=actor, new_value=f"{dep_type}:{depends_on_id}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def _would_create_cycle(self, issue_id: str, depends_on_id: str) -> bool:
        """Check if adding issue_id -> depends_on_id would create a cycle.

        Uses BFS from depends_on_id following existing dependency edges.
        If issue_id is reachable, adding the new edge would close a cycle.

        Loads all edges in a single query to avoid N+1 per-node queries.
        """
        # Build adjacency list from all edges in one query
        adj: dict[str, list[str]] = {}
        for r in self.conn.execute("SELECT issue_id, depends_on_id FROM dependencies").fetchall():
            adj.setdefault(r["issue_id"], []).append(r["depends_on_id"])

        visited: set[str] = set()
        queue = deque([depends_on_id])
        while queue:
            current = queue.popleft()
            if current == issue_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            for neighbor in adj.get(current, ()):
                queue.append(neighbor)
        return False

    def remove_dependency(self, issue_id: str, depends_on_id: str, *, actor: str = "") -> bool:
        self._check_id_prefix(issue_id)
        self._check_id_prefix(depends_on_id)
        self.get_issue(issue_id)  # raises KeyError if not found
        self.get_issue(depends_on_id)  # raises KeyError if not found
        try:
            # Read dep_type before deleting so undo can restore it
            row = self.conn.execute(
                "SELECT type FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
                (issue_id, depends_on_id),
            ).fetchone()
            if row is None:
                return False  # Nothing to remove
            dep_type = row["type"] or "blocks"
            self.conn.execute(
                "DELETE FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
                (issue_id, depends_on_id),
            )
            self._record_event(issue_id, "dependency_removed", actor=actor, old_value=f"{dep_type}:{depends_on_id}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def get_all_dependencies(self) -> list[DependencyRecord]:
        rows = self.conn.execute("SELECT issue_id, depends_on_id, type FROM dependencies").fetchall()
        return [{"from": r["issue_id"], "to": r["depends_on_id"], "type": r["type"]} for r in rows]

    # -- Ready / Blocked -----------------------------------------------------

    def _resolve_open_blocker_predicates(self) -> tuple[tuple[str, list[str]], tuple[str, list[str]]] | None:
        """Return ``(open_predicate, blocker_done_predicate)`` SQL fragments.

        Returns ``None`` when no open-category states are registered — callers
        treat that as "nothing can be ready".

        - ``open_predicate`` matches an issue row aliased ``i`` whose
          ``(type, status)`` is open-category.
        - ``blocker_done_predicate`` matches a blocker row aliased ``blocker``
          whose ``(type, status)`` is done-category, plus the synthetic
          ``'archived'`` terminal state (filigree-42045dd065).

        Type-aware (filigree-b55aa3191f): replaces the prior status-name list
        approach so colliding state names across types are classified per type.
        """
        open_pred = self._category_predicate_sql("open", type_col="i.type", status_col="i.status")
        if not open_pred[1]:
            return None
        blocker_done_pred = self._category_predicate_sql(
            "done",
            type_col="blocker.type",
            status_col="blocker.status",
            include_archived=True,
        )
        return open_pred, blocker_done_pred

    def get_ready(self) -> list[Issue]:
        """Unassigned issues in open-category states with no open blockers."""
        preds = self._resolve_open_blocker_predicates()
        if preds is None:
            return []
        (open_sql, open_params), (blocker_done_sql, blocker_done_params) = preds

        rows = self.conn.execute(
            f"SELECT i.id FROM issues i "
            f"WHERE {open_sql} "
            f"AND (i.assignee = '' OR i.assignee IS NULL) "
            f"AND NOT EXISTS ("
            f"  SELECT 1 FROM dependencies d "
            f"  JOIN issues blocker ON d.depends_on_id = blocker.id "
            f"  WHERE d.issue_id = i.id AND NOT ({blocker_done_sql})"
            f") ORDER BY i.priority ASC, i.created_at ASC, i.id ASC",
            [*open_params, *blocker_done_params],
        ).fetchall()

        return self._build_issues_batch([r["id"] for r in rows])

    def issue_startability(self, issue: Issue) -> tuple[bool, str | None]:
        """Return ``(startable, next_action)`` for a ready-queue projection.

        Resolves the issue's workflow template and delegates to
        ``TypeTemplate.startability`` (filigree-406e6b7ee0). A missing template
        is treated as non-startable with no actionable hop. This is the bridge
        wire surfaces use to attach ``startable`` / ``next_action`` to
        ``get_ready`` items without the projection layer importing templates.
        """
        tpl = self.templates.get_type(issue.type)
        if tpl is None:
            return False, None
        return tpl.startability(issue.status)

    def get_blocked(self) -> list[Issue]:
        """Issues in open- or wip-category states that have at least one non-done blocker.

        Includes wip-category issues so an agent doing "what's stuck right
        now" sees in_progress dead-ends, not just open-category waiters.
        Done-category issues are excluded — once an issue is closed, its
        dependencies are no longer interesting. (filigree-cb980eee0d, P2.8.)
        """
        # not-done predicate covers both open and wip; the blocker side stays
        # done-only so we surface anything that isn't yet finished but is
        # waiting on something also unfinished.
        not_done_pred = self._category_predicate_sql(
            "done",
            type_col="i.type",
            status_col="i.status",
            include_archived=True,
        )
        not_done_sql, not_done_params = not_done_pred
        blocker_done_pred = self._category_predicate_sql(
            "done",
            type_col="blocker.type",
            status_col="blocker.status",
            include_archived=True,
        )
        blocker_done_sql, blocker_done_params = blocker_done_pred
        if not not_done_params and not blocker_done_params:
            return []

        rows = self.conn.execute(
            f"SELECT DISTINCT i.id FROM issues i "
            f"JOIN dependencies d ON d.issue_id = i.id "
            f"JOIN issues blocker ON d.depends_on_id = blocker.id "
            f"WHERE NOT ({not_done_sql}) AND NOT ({blocker_done_sql}) "
            f"ORDER BY i.priority, i.created_at",
            [*not_done_params, *blocker_done_params],
        ).fetchall()

        return self._build_issues_batch([r["id"] for r in rows])

    # -- Critical path -------------------------------------------------------

    def get_critical_path(self) -> list[CriticalPathNode]:
        """Compute the longest dependency chain among non-done issues.

        Uses topological-order dynamic programming on the open-issue dependency DAG.
        Returns the chain as a list of {id, title, priority, type} dicts, ordered
        from the root blocker to the final blocked issue.
        """
        # Treat archived as done here: an archived issue has reached terminal
        # state and must not appear as an open node on the critical path.
        # (filigree-42045dd065). Match by (type, status) so colliding state
        # names across types are classified per type (filigree-b55aa3191f).
        not_done_sql, not_done_params = self._category_predicate_sql(
            "done",
            type_col="type",
            status_col="status",
            include_archived=True,
        )
        open_rows = self.conn.execute(
            f"SELECT id, title, priority, type FROM issues WHERE NOT ({not_done_sql})",
            not_done_params,
        ).fetchall()
        open_ids = {r["id"] for r in open_rows}
        info = {r["id"]: CriticalPathNode(id=r["id"], title=r["title"], priority=r["priority"], type=r["type"]) for r in open_rows}

        # edges: blocker -> list of issues it blocks (forward edges)
        forward: dict[str, list[str]] = {nid: [] for nid in open_ids}
        in_degree: dict[str, int] = dict.fromkeys(open_ids, 0)
        dep_rows = self.conn.execute("SELECT issue_id, depends_on_id FROM dependencies").fetchall()
        for dep in dep_rows:
            from_id, to_id = dep["issue_id"], dep["depends_on_id"]
            if from_id in open_ids and to_id in open_ids:
                forward[to_id].append(from_id)  # to_id blocks from_id
                in_degree[from_id] = in_degree.get(from_id, 0) + 1

        if not open_ids:
            return []

        # Topological sort (Kahn's algorithm) + longest path DP
        queue = deque(nid for nid in open_ids if in_degree[nid] == 0)
        dist: dict[str, int] = dict.fromkeys(open_ids, 0)
        pred: dict[str, str | None] = dict.fromkeys(open_ids, None)

        while queue:
            node = queue.popleft()
            for neighbor in forward[node]:
                if dist[node] + 1 > dist[neighbor]:
                    dist[neighbor] = dist[node] + 1
                    pred[neighbor] = node
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if not dist:
            return []

        # Find the node with the longest path
        end_node = max(dist, key=lambda n: dist[n])
        if dist[end_node] == 0:
            return []  # No chains at all

        # Reconstruct path
        path: list[str] = []
        current: str | None = end_node
        while current is not None:
            path.append(current)
            current = pred[current]
        path.reverse()

        return [info[nid] for nid in path]

    # -- Plan tree -----------------------------------------------------------

    def _list_all_children(self, parent_id: str) -> list[Issue]:
        """Return every direct child of ``parent_id`` — no pagination.

        Tree construction (``get_plan``, ``_build_tree``) needs the complete
        child set; using the paginated ``list_issues`` (default ``limit=100``)
        silently truncates large plans/releases.
        """
        rows = self.conn.execute(
            "SELECT id FROM issues WHERE parent_id = ? ORDER BY priority, created_at",
            (parent_id,),
        ).fetchall()
        return self._build_issues_batch([r["id"] for r in rows])

    def _normalize_label_inputs(self, labels: Any, source: str) -> list[str]:
        if labels is None:
            return []
        if not isinstance(labels, list):
            msg = f"{source} must be a list of strings"
            raise ValueError(msg)
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_label in labels:
            label = self._validate_label_name(raw_label)
            if label not in seen:
                normalized.append(label)
                seen.add(label)
        return normalized

    def _insert_label_no_commit(self, issue_id: str, label: str) -> bool:
        if label.startswith("review:"):
            existing_review = {
                row["label"]
                for row in self.conn.execute(
                    "SELECT label FROM labels WHERE issue_id = ? AND label LIKE 'review:%'",
                    (issue_id,),
                ).fetchall()
            }
            if existing_review == {label}:
                return False
            self.conn.execute("DELETE FROM labels WHERE issue_id = ? AND label LIKE 'review:%'", (issue_id,))
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO labels (issue_id, label) VALUES (?, ?)",
            (issue_id, label),
        )
        return cursor.rowcount > 0

    def _insert_labels_no_commit(self, issue_id: str, labels: list[str]) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for label in labels:
            added = self._insert_label_no_commit(issue_id, label)
            results.append({"id": issue_id, "label": label, "status": "added" if added else "already_exists"})
        return results

    def _validate_issue_id_list(self, issue_ids: Any, source: str) -> list[str]:
        if issue_ids is None:
            return []
        if not isinstance(issue_ids, list):
            msg = f"{source} must be a list of issue IDs"
            raise ValueError(msg)
        normalized: list[str] = []
        seen: set[str] = set()
        for idx, raw_issue_id in enumerate(issue_ids):
            if not isinstance(raw_issue_id, str) or not raw_issue_id.strip():
                msg = f"{source}[{idx}] must be a non-empty issue ID string"
                raise ValueError(msg)
            issue_id = raw_issue_id.strip()
            self._check_id_prefix(issue_id)
            self.get_issue(issue_id)
            if issue_id not in seen:
                normalized.append(issue_id)
                seen.add(issue_id)
        return normalized

    def _collect_subtree_issue_ids(self, parent_id: str) -> list[str]:
        self.get_issue(parent_id)
        issue_ids: list[str] = []
        queue = deque([parent_id])
        while queue:
            issue_id = queue.popleft()
            issue_ids.append(issue_id)
            rows = self.conn.execute(
                "SELECT id FROM issues WHERE parent_id = ? ORDER BY priority, created_at",
                (issue_id,),
            ).fetchall()
            queue.extend(row["id"] for row in rows)
        return issue_ids

    def label_subtree(self, parent_id: str, *, label: str) -> tuple[list[dict[str, str]], list[BatchFailure]]:
        """Add a label to a parent issue and every descendant."""
        normalized = self._validate_label_name(label)
        issue_ids = self._collect_subtree_issue_ids(parent_id)
        try:
            results: list[dict[str, str]] = []
            for issue_id in issue_ids:
                added = self._insert_label_no_commit(issue_id, normalized)
                results.append({"id": issue_id, "status": "added" if added else "already_exists"})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return results, []

    def get_plan(self, milestone_id: str) -> PlanTree:
        """Get milestone->phase->step tree with progress stats."""
        milestone = self.get_issue(milestone_id)
        if milestone.type != "milestone":
            raise NotAMilestoneError(f"Issue {milestone_id} is not a milestone")

        phases = self._list_all_children(milestone_id)
        phases.sort(key=lambda p: _sequence_sort_key(p.fields.get("sequence", 999)))

        phase_list: list[PlanPhase] = []
        total_steps = 0
        completed_steps = 0

        for phase in phases:
            steps = self._list_all_children(phase.id)
            steps.sort(key=lambda s: _sequence_sort_key(s.fields.get("sequence", 999)))

            completed = sum(1 for s in steps if s.status_category == "done")
            ready = sum(1 for s in steps if s.is_ready)

            phase_list.append(
                PlanPhase(
                    phase=phase.to_dict(),
                    steps=[s.to_dict() for s in steps],
                    total=len(steps),
                    completed=completed,
                    ready=ready,
                )
            )
            total_steps += len(steps)
            completed_steps += completed

        return PlanTree(
            milestone=milestone.to_dict(),
            phases=phase_list,
            total_steps=total_steps,
            completed_steps=completed_steps,
        )

    def _next_child_sequence(self, parent_id: str, child_type: str) -> int:
        rows = self.conn.execute(
            "SELECT fields FROM issues WHERE parent_id = ? AND type = ?",
            (parent_id, child_type),
        ).fetchall()
        max_sequence = 0
        for row in rows:
            raw_fields = row["fields"] or "{}"
            try:
                fields = json.loads(raw_fields)
            except json.JSONDecodeError:
                continue
            sequence = fields.get("sequence") if isinstance(fields, dict) else None
            normalized = _coerce_sequence(sequence)
            if normalized is not None and normalized > max_sequence:
                max_sequence = normalized
        return max_sequence + 1

    def add_plan_step(
        self,
        phase_id: str,
        title: str,
        *,
        priority: int = 2,
        description: str = "",
        notes: str = "",
        labels: list[str] | None = None,
        deps: list[str] | None = None,
        actor: str = "",
    ) -> Issue:
        """Create a step under an existing phase with plan-context defaults."""
        self._check_id_prefix(phase_id)
        phase = self.get_issue(phase_id)
        if phase.type != "phase":
            msg = f"phase_id must reference a phase issue, got {phase.type!r}: {phase_id}"
            raise ValueError(msg)
        if not title or not title.strip():
            msg = "Step 'title' is required and cannot be empty"
            raise ValueError(msg)
        _validate_priority(priority, "Step")
        extra_labels = self._normalize_label_inputs(labels, "labels")
        dep_ids = self._validate_issue_id_list(deps, "deps")

        step_id = self._generate_unique_id("issues")
        now = _now_iso()
        step_initial = self.templates.get_initial_state("step")
        step_fields = {"sequence": self._next_child_sequence(phase_id, "step")}
        step_labels = _merge_labels(phase.labels, extra_labels)

        try:
            self.conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, parent_id, assignee, "
                "created_at, updated_at, description, notes, fields) "
                "VALUES (?, ?, ?, ?, 'step', ?, '', ?, ?, ?, ?, ?)",
                (
                    step_id,
                    title,
                    step_initial,
                    priority,
                    phase_id,
                    now,
                    now,
                    description,
                    notes,
                    json.dumps(step_fields),
                ),
            )
            self._record_event(step_id, "created", actor=actor, new_value=title)
            self._insert_labels_no_commit(step_id, step_labels)

            for dep_id in dep_ids:
                if step_id == dep_id:
                    msg = f"Cannot add self-dependency: {step_id}"
                    raise ValueError(msg)
                if self._would_create_cycle(step_id, dep_id):
                    msg = f"Dependency {step_id} -> {dep_id} would create a cycle"
                    raise ValueError(msg)
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, 'blocks', ?)",
                    (step_id, dep_id, now),
                )
                if cursor.rowcount > 0:
                    self._record_event(step_id, "dependency_added", actor=actor, new_value=f"blocks:{dep_id}")

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return self.get_issue(step_id)

    def retarget_plan_dependency(
        self,
        step_id: str,
        old_depends_on_id: str,
        new_depends_on_id: str,
        *,
        actor: str = "",
    ) -> Issue:
        """Replace one dependency edge on a plan step in one operation."""
        for issue_id in (step_id, old_depends_on_id, new_depends_on_id):
            self._check_id_prefix(issue_id)

        step = self.get_issue(step_id)
        old_dep = self.get_issue(old_depends_on_id)
        new_dep = self.get_issue(new_depends_on_id)
        if step.type != "step":
            msg = f"step_id must reference a step issue, got {step.type!r}: {step_id}"
            raise ValueError(msg)
        if old_dep.type != "step" or new_dep.type != "step":
            msg = "old_depends_on_id and new_depends_on_id must both reference step issues"
            raise ValueError(msg)
        if step_id == new_depends_on_id:
            msg = f"Cannot add self-dependency: {step_id}"
            raise ValueError(msg)

        row = self.conn.execute(
            "SELECT type FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
            (step_id, old_depends_on_id),
        ).fetchone()
        if row is None:
            msg = f"Dependency not found: {step_id} -> {old_depends_on_id}"
            raise ValueError(msg)
        dep_type = row["type"] or "blocks"

        if old_depends_on_id == new_depends_on_id:
            return self.get_issue(step_id)
        if self._would_create_cycle(step_id, new_depends_on_id):
            msg = f"Dependency {step_id} -> {new_depends_on_id} would create a cycle"
            raise ValueError(msg)

        now = _now_iso()
        try:
            self.conn.execute(
                "DELETE FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
                (step_id, old_depends_on_id),
            )
            self._record_event(step_id, "dependency_removed", actor=actor, old_value=f"{dep_type}:{old_depends_on_id}")
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, ?, ?)",
                (step_id, new_depends_on_id, dep_type, now),
            )
            if cursor.rowcount > 0:
                self._record_event(step_id, "dependency_added", actor=actor, new_value=f"{dep_type}:{new_depends_on_id}")
            self.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, step_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return self.get_issue(step_id)

    def move_plan_step(self, step_id: str, phase_id: str, *, actor: str = "") -> Issue:
        """Move an existing step under a different phase."""
        self._check_id_prefix(step_id)
        self._check_id_prefix(phase_id)
        step = self.get_issue(step_id)
        phase = self.get_issue(phase_id)
        if step.type != "step":
            msg = f"step_id must reference a step issue, got {step.type!r}: {step_id}"
            raise ValueError(msg)
        if phase.type != "phase":
            msg = f"phase_id must reference a phase issue, got {phase.type!r}: {phase_id}"
            raise ValueError(msg)
        sequence = self._next_child_sequence(phase_id, "step")
        return self.update_issue(step_id, parent_id=phase_id, fields={"sequence": sequence}, actor=actor)

    def create_plan(
        self,
        milestone: MilestoneInput,
        phases: list[PhaseInput],
        *,
        actor: str = "",
    ) -> PlanTree:
        """Create a full milestone -> phase -> step hierarchy in one transaction.

        Args:
            milestone: {title, priority?, description?, fields?}
            phases: [{title, priority?, description?, steps: [{title, priority?, description?, deps?: [step_index]}]}]
            actor: Who created the plan

        Step deps use integer indices (0-based within the phase's steps list)
        or cross-phase references as "phase_idx.step_idx" strings.

        Returns the full plan tree (same format as get_plan).
        """
        # Validate inputs — specific error messages for each level
        milestone = _validate_plan_object(milestone, "Milestone")  # type: ignore[assignment]
        if not isinstance(phases, list):
            msg = "'phases' must be a list of phase objects"
            raise ValueError(msg)
        _validate_plan_title(milestone, "Milestone")
        _validate_plan_optional_string(milestone, "description", "Milestone")
        _normalize_plan_fields(milestone.get("fields"), "Milestone")
        _validate_priority(milestone.get("priority", 2), "Milestone")
        for phase_idx, phase_data in enumerate(phases):
            phase_label = f"Phase {phase_idx + 1}"
            phase_data = _validate_plan_object(phase_data, phase_label)  # type: ignore[assignment]
            _validate_plan_title(phase_data, phase_label)
            _validate_plan_optional_string(phase_data, "description", phase_label)
            _normalize_plan_fields(phase_data.get("fields"), phase_label)
            _validate_priority(phase_data.get("priority", 2), phase_label)
            steps = phase_data.get("steps", [])
            if not isinstance(steps, list):
                msg = f"{phase_label} 'steps' must be a list"
                raise ValueError(msg)
            for step_idx, step_data in enumerate(steps):
                step_label = f"{phase_label}, Step {step_idx + 1}"
                step_data = _validate_plan_object(step_data, step_label)  # type: ignore[assignment]
                _validate_plan_title(step_data, step_label)
                _validate_plan_optional_string(step_data, "description", step_label)
                _normalize_plan_fields(step_data.get("fields"), step_label)
                _validate_priority(
                    step_data.get("priority", 2),
                    step_label,
                )
        milestone_labels = self._normalize_label_inputs(milestone.get("labels"), "milestone.labels")
        phase_labels: list[list[str]] = []
        step_labels: list[list[list[str]]] = []
        for phase_idx, phase_data in enumerate(phases):
            labels_for_phase = _merge_labels(
                milestone_labels,
                self._normalize_label_inputs(phase_data.get("labels"), f"phases[{phase_idx}].labels"),
            )
            phase_labels.append(labels_for_phase)
            labels_for_steps: list[list[str]] = []
            for step_idx, step_data in enumerate(phase_data.get("steps", [])):
                labels_for_steps.append(
                    _merge_labels(
                        labels_for_phase,
                        self._normalize_label_inputs(step_data.get("labels"), f"phases[{phase_idx}].steps[{step_idx}].labels"),
                    )
                )
            step_labels.append(labels_for_steps)

        now = _now_iso()
        milestone_initial = self.templates.get_initial_state("milestone")
        phase_initial = self.templates.get_initial_state("phase")
        step_initial = self.templates.get_initial_state("step")

        try:
            # Create milestone
            ms_id = self._generate_unique_id("issues")
            ms_fields = _normalize_plan_fields(milestone.get("fields"), "Milestone")
            self.conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, parent_id, assignee, "
                "created_at, updated_at, description, notes, fields) "
                "VALUES (?, ?, ?, ?, 'milestone', NULL, '', ?, ?, ?, '', ?)",
                (
                    ms_id,
                    milestone["title"],
                    milestone_initial,
                    milestone.get("priority", 2),
                    now,
                    now,
                    milestone.get("description", ""),
                    json.dumps(ms_fields),
                ),
            )
            self._record_event(ms_id, "created", actor=actor, new_value=milestone["title"])
            self._insert_labels_no_commit(ms_id, milestone_labels)

            # Track all created step IDs for cross-phase dependency resolution
            # step_ids[phase_idx][step_idx] = issue_id
            step_ids: list[list[str]] = []

            for phase_idx, phase_data in enumerate(phases):
                # Create phase
                phase_id = self._generate_unique_id("issues")
                phase_fields = dict(_normalize_plan_fields(phase_data.get("fields"), f"Phase {phase_idx + 1}"))
                phase_fields["sequence"] = phase_idx + 1
                self.conn.execute(
                    "INSERT INTO issues (id, title, status, priority, type, parent_id, assignee, "
                    "created_at, updated_at, description, notes, fields) "
                    "VALUES (?, ?, ?, ?, 'phase', ?, '', ?, ?, ?, '', ?)",
                    (
                        phase_id,
                        phase_data["title"],
                        phase_initial,
                        phase_data.get("priority", 2),
                        ms_id,
                        now,
                        now,
                        phase_data.get("description", ""),
                        json.dumps(phase_fields),
                    ),
                )
                self._record_event(phase_id, "created", actor=actor, new_value=phase_data["title"])
                self._insert_labels_no_commit(phase_id, phase_labels[phase_idx])

                # Create steps
                phase_step_ids: list[str] = []
                steps = phase_data.get("steps") or []
                for step_idx, step_data in enumerate(steps):
                    step_id = self._generate_unique_id("issues")
                    step_fields = dict(_normalize_plan_fields(step_data.get("fields"), f"Phase {phase_idx + 1}, Step {step_idx + 1}"))
                    step_fields["sequence"] = step_idx + 1
                    self.conn.execute(
                        "INSERT INTO issues (id, title, status, priority, type, parent_id, assignee, "
                        "created_at, updated_at, description, notes, fields) "
                        "VALUES (?, ?, ?, ?, 'step', ?, '', ?, ?, ?, '', ?)",
                        (
                            step_id,
                            step_data["title"],
                            step_initial,
                            step_data.get("priority", 2),
                            phase_id,
                            now,
                            now,
                            step_data.get("description", ""),
                            json.dumps(step_fields),
                        ),
                    )
                    self._record_event(step_id, "created", actor=actor, new_value=step_data["title"])
                    self._insert_labels_no_commit(step_id, step_labels[phase_idx][step_idx])
                    phase_step_ids.append(step_id)
                step_ids.append(phase_step_ids)

            # Wire up dependencies after all steps exist
            for phase_idx, phase_data in enumerate(phases):
                steps = phase_data.get("steps") or []
                for step_idx, step_data in enumerate(steps):
                    for dep_ref in step_data.get("deps", []):
                        dep_ref_str = _normalize_dep_ref(dep_ref)
                        if "." in dep_ref_str:
                            # Cross-phase: "phase_idx.step_idx"
                            p_idx_str, s_idx_str = dep_ref_str.split(".", 1)
                            p_idx_int, s_idx_int = int(p_idx_str), int(s_idx_str)
                            if p_idx_int < 0 or s_idx_int < 0:
                                msg = f"Negative dep index not allowed: {dep_ref_str}"
                                raise ValueError(msg)
                            if p_idx_int >= len(step_ids) or s_idx_int >= len(step_ids[p_idx_int]):
                                n_steps = len(step_ids[p_idx_int]) if p_idx_int < len(step_ids) else "?"
                                msg = f"Dep index out of range: {dep_ref_str} (phases={len(step_ids)}, steps={n_steps})"
                                raise ValueError(msg)
                            dep_issue_id = step_ids[p_idx_int][s_idx_int]
                        else:
                            # Same phase: step index
                            same_idx = int(dep_ref_str)
                            if same_idx < 0:
                                msg = f"Negative dep index not allowed: {dep_ref_str}"
                                raise ValueError(msg)
                            if same_idx >= len(step_ids[phase_idx]):
                                msg = f"Dep index out of range: step {same_idx} in phase {phase_idx} (max={len(step_ids[phase_idx]) - 1})"
                                raise ValueError(msg)
                            dep_issue_id = step_ids[phase_idx][same_idx]

                        issue_id = step_ids[phase_idx][step_idx]
                        if issue_id == dep_issue_id:
                            msg = f"Cannot add self-dependency: {issue_id}"
                            raise ValueError(msg)
                        if self._would_create_cycle(issue_id, dep_issue_id):
                            msg = f"Dependency {issue_id} -> {dep_issue_id} would create a cycle"
                            raise ValueError(msg)

                        cursor = self.conn.execute(
                            "INSERT OR IGNORE INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, 'blocks', ?)",
                            (issue_id, dep_issue_id, now),
                        )
                        # Emit event only when a new row was inserted: duplicate deps
                        # (``deps: [0, 0]``) collapse to one dep row and must not produce
                        # multiple events, which otherwise wedge undo_last().
                        if cursor.rowcount > 0:
                            self._record_event(
                                issue_id,
                                "dependency_added",
                                actor=actor,
                                new_value=f"blocks:{dep_issue_id}",
                            )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return self.get_plan(ms_id)

    # -- Release tree --------------------------------------------------------

    def get_releases_summary(self, *, include_released: bool = False) -> list[ReleaseSummaryItem]:
        rows = self.conn.execute("SELECT id FROM issues WHERE type = 'release' ORDER BY priority, created_at").fetchall()
        releases = self._build_issues_batch([row["id"] for row in rows])
        if not include_released:
            # Note: rolled_back is category "wip", so it IS included (intentional —
            # a rolled-back release needs attention, it is not "finished")
            releases = [r for r in releases if r.status_category != "done"]

        result: list[ReleaseSummaryItem] = []
        for release in releases:
            # Build the full tree once; extract progress from it
            subtree = self._build_tree(release.id)
            progress = self._progress_from_subtree(subtree)

            child_summary = self._summarize_children_by_type(subtree)

            blocks_resolved = self._resolve_issue_refs(release.blocks)
            blocked_by_resolved = self._resolve_issue_refs(release.blocked_by)

            # Build response dict explicitly — do not spread to_dict() and override keys
            data: dict[str, Any] = dict(release.to_dict())
            data["version"] = release.fields.get("version")
            data["target_date"] = release.fields.get("target_date")
            data["blocks"] = blocks_resolved
            data["blocked_by"] = blocked_by_resolved
            data["progress"] = progress
            data["child_summary"] = child_summary

            # Surface truncation warnings from the subtree
            tree_warnings = self._collect_tree_warnings(subtree)
            if tree_warnings:
                existing: list[str] = data.get("data_warnings") or []
                data["data_warnings"] = existing + tree_warnings

            result.append(data)  # type: ignore[arg-type]  # dict built incrementally

        return result

    def get_release_tree(self, release_id: str) -> ReleaseTree:
        release = self.get_issue(release_id)  # raises KeyError if not found
        if release.type != "release":
            raise NotAReleaseError(f"Issue {release_id} is not a release")
        children = self._build_tree(release.id)
        tree_warnings = self._collect_tree_warnings(children)
        return {
            "release": release.to_dict(),
            "children": children,
            "data_warnings": tree_warnings,
        }

    def _build_tree(self, parent_id: str, *, _depth: int = 0) -> list[TreeNode]:
        if _depth > _MAX_TREE_DEPTH:
            logger.warning("_build_tree: depth limit reached at parent_id=%s", parent_id)
            return [TreeNode(issue=_truncated_issue_sentinel(parent_id), progress=None, children=[], truncated=True)]

        children = self._list_all_children(parent_id)
        nodes: list[TreeNode] = []
        for child in children:
            subtree = self._build_tree(child.id, _depth=_depth + 1)
            progress = self._progress_from_subtree(subtree) if subtree else None
            nodes.append(
                {
                    "issue": child.to_dict(),
                    "progress": progress,
                    "children": subtree,
                }
            )
        # Group nodes: epics/milestones first, then loose items (tasks, bugs, etc.)
        nodes.sort(key=lambda n: 0 if n["issue"].get("type") in ("epic", "milestone") else 1)
        return nodes

    def _progress_from_subtree(self, nodes: list[TreeNode]) -> ProgressDict:
        total = completed = in_progress = open_count = 0
        for node in nodes:
            # Depth-limit sentinels stand for "unknown more items below" — skip
            # them from the count (surfaced separately via _collect_tree_warnings)
            # so a truncated subtree does not masquerade as a single open leaf.
            if node.get("truncated"):
                continue
            if not node["children"]:  # leaf node
                cat = node["issue"].get("status_category", "open")
                total += 1
                if cat == "done":
                    completed += 1
                elif cat == "wip":
                    in_progress += 1
                else:
                    open_count += 1
            else:  # recurse into non-leaf's children
                sub = self._progress_from_subtree(node["children"])
                total += sub["total"]
                completed += sub["completed"]
                in_progress += sub["in_progress"]
                open_count += sub["open"]
        pct = round(completed / total * 100) if total > 0 else 0
        return {"total": total, "completed": completed, "in_progress": in_progress, "open": open_count, "pct": pct}

    def _collect_tree_warnings(self, nodes: list[TreeNode]) -> list[str]:
        """Recursively collect ``data_warnings`` from truncated tree nodes."""
        warnings: list[str] = []
        for node in nodes:
            if node.get("truncated"):
                node_warnings = node["issue"].get("data_warnings", [])
                warnings.extend(node_warnings)
            if node["children"]:
                warnings.extend(self._collect_tree_warnings(node["children"]))
        return warnings

    def _summarize_children_by_type(self, nodes: list[TreeNode]) -> ChildSummary:
        counts: ChildSummary = {"epics": 0, "milestones": 0, "tasks": 0, "bugs": 0, "other": 0, "total": len(nodes)}
        type_map = {"epic": "epics", "milestone": "milestones", "task": "tasks", "bug": "bugs"}
        for node in nodes:
            key = type_map.get(node["issue"]["type"], "other")
            counts[key] += 1  # type: ignore[literal-required]
        return counts

    def _resolve_issue_refs(self, ids: list[str]) -> list[IssueRef]:
        refs: list[IssueRef] = []
        for issue_id in ids:
            try:
                issue = self.get_issue(issue_id)
                refs.append({"id": issue.id, "title": issue.title, "type": issue.type})
            except KeyError:
                logger.warning("_resolve_issue_refs: dangling reference %s", issue_id)
                refs.append({"id": issue_id, "title": "(deleted)", "type": "unknown", "dangling": True})
        return refs
