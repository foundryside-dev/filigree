"""TypedDicts for db_workflow.py return types."""

from __future__ import annotations

from typing import Any, TypedDict


class StateInfo(TypedDict):
    """State entry in a template's states list."""

    name: str
    category: str


# TransitionInfo uses "from" as a key at runtime (a Python keyword).
# TypedDict cannot express this with class syntax; we use functional form.
TransitionInfo = TypedDict("TransitionInfo", {"from": str, "to": str, "enforcement": str, "requires_fields": list[str]})


class _FieldSchemaRequired(TypedDict):
    """Required keys for FieldSchemaInfo (always present)."""

    name: str
    type: str
    description: str


class FieldSchemaInfo(_FieldSchemaRequired, total=False):
    """Single field in a template's fields_schema.

    The ``name``, ``type``, and ``description`` keys are always present.
    Optional keys (``options``, ``default``, ``required_at``, ``pattern``,
    ``unique``) are only included when the underlying template field defines them.
    """

    options: list[str]
    default: Any
    required_at: list[str]
    pattern: str
    unique: bool


class TemplateInfo(TypedDict):
    """Full template details returned by ``get_template()``."""

    type: str
    display_name: str
    description: str
    pack: str
    states: list[StateInfo]
    initial_state: str
    transitions: list[TransitionInfo]
    reverse_transitions: list[TransitionInfo]
    fields_schema: list[FieldSchemaInfo]


class TemplateListItem(TypedDict):
    """Summary template info returned by ``list_templates()``."""

    type: str
    display_name: str
    description: str
    fields_schema: list[FieldSchemaInfo]


class TypeListItem(TypedDict):
    """Type summary returned by ``list_types`` MCP handler.

    Shares several keys with ``TemplateListItem`` but omits
    ``fields_schema`` and adds ``pack``, ``states``, and ``initial_state``.
    """

    type: str
    display_name: str
    description: str
    pack: str
    states: list[StateInfo]
    initial_state: str


class TypeInfoResponse(TemplateInfo):
    """Backward-compatible alias shape for ``get_type_info``."""
