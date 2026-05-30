"""TypedDicts for db_events.py return types."""

from __future__ import annotations

from typing import Literal, NotRequired, TypeAlias, TypedDict, assert_never, get_args

from filigree.types.core import ISOTimestamp, IssueDict

# Canonical enumeration of all event types recorded by _record_event().
# Keep in sync with call sites in db_issues.py, db_events.py, db_planning.py.
EventType = Literal[
    "created",
    "status_changed",
    "title_changed",
    "priority_changed",
    "assignee_changed",
    "description_changed",
    "notes_changed",
    "fields_changed",
    "corrupt_fields_overwritten",
    "parent_changed",
    "claimed",
    "released",
    "heartbeat",
    "reclaimed",
    "reopened",
    "dependency_added",
    "dependency_removed",
    "transition_warning",
    "transition_forced",
    "entity_association_added",
    "entity_association_refreshed",
    "entity_association_removed",
    "undone",
    "archived",
    "issue_deleted",
]

ReversibleEventType: TypeAlias = Literal[
    "status_changed",
    "title_changed",
    "priority_changed",
    "assignee_changed",
    "claimed",
    "dependency_added",
    "dependency_removed",
    "description_changed",
    "notes_changed",
    "fields_changed",
    "parent_changed",
]

# Single source of truth: derived from the ``ReversibleEventType`` alias rather
# than re-listed, so the alias and the tuple cannot drift. The exhaustive
# ``match`` in ``is_reversible_event_type`` is the *third* listing of these
# names — it is kept deliberately (it forces a yes/no undo decision on every
# new ``EventType`` via ``assert_never``), and a contract test pins its
# True-set against ``get_args(ReversibleEventType)`` so all three stay in sync.
REVERSIBLE_EVENT_TYPES: tuple[ReversibleEventType, ...] = get_args(ReversibleEventType)


def is_reversible_event_type(event_type: EventType) -> bool:
    """Return whether an event type participates in undo.

    The exhaustive match is deliberate: adding a new EventType forces a
    yes/no undo decision in mypy, mirroring the ErrorCode assert_never guard.
    """
    match event_type:
        case (
            "status_changed"
            | "title_changed"
            | "priority_changed"
            | "assignee_changed"
            | "claimed"
            | "dependency_added"
            | "dependency_removed"
            | "description_changed"
            | "notes_changed"
            | "fields_changed"
            | "parent_changed"
        ):
            return True
        case (
            "created"
            | "corrupt_fields_overwritten"
            | "released"
            | "heartbeat"
            | "reclaimed"
            | "reopened"
            | "transition_warning"
            | "transition_forced"
            | "entity_association_added"
            | "entity_association_refreshed"
            | "entity_association_removed"
            | "undone"
            | "archived"
            | "issue_deleted"
        ):
            return False
        case _:
            assert_never(event_type)


class EventRecord(TypedDict):
    """Row from the events table (SELECT * FROM events).

    Returned by ``get_issue_events()``.  The ``get_recent_events()`` and
    ``get_events_since()`` queries join on ``issues`` and add ``issue_title``,
    so they return ``EventRecordWithTitle`` instead.
    """

    id: int
    issue_id: str
    event_type: EventType
    actor: str
    old_value: str | None
    new_value: str | None
    comment: str
    created_at: ISOTimestamp


class EventRecordWithTitle(EventRecord):
    """EventRecord with the joined issue_title column.

    Returned by ``get_recent_events()`` and ``get_events_since()``.
    """

    issue_title: str
    # Only populated on synthetic ``issue_deleted`` tombstone records: the sorted
    # ``clarion_entity_id``s whose entity_associations the delete cascade removed
    # (schema v21, F5 amplifier). Absent on real event rows; consumers of the wire
    # shape see it normalized to ``[]`` via ``change_record_to_loom``.
    affected_entities: NotRequired[list[str]]


class UndoSuccess(TypedDict):
    """Successful undo result from ``undo_last()``."""

    undone: Literal[True]
    event_type: EventType
    event_id: int
    issue: IssueDict


class UndoFailure(TypedDict):
    """Failed undo result from ``undo_last()``."""

    undone: Literal[False]
    reason: str


UndoResult: TypeAlias = UndoSuccess | UndoFailure
