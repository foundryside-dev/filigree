# src/filigree/templates.py
"""Workflow template system -- loading, caching, and validation.

Provides TemplateRegistry for managing per-type state machines, transition
enforcement, and field validation. Type templates define states, transitions,
and field schemas. Workflow packs bundle related types.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, get_args

from filigree.types.core import StatusCategory as StateCategory

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# State/type names must match this pattern to be safe for use in SQL queries
# and filesystem paths. Validated at parse time.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VALID_CATEGORIES: frozenset[str] = frozenset(get_args(StateCategory))

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

EnforcementLevel = Literal["hard", "soft"]
FieldType = Literal["text", "enum", "number", "date", "list", "boolean"]

# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------
# Templates use frozen=True for immutability (configuration data).
# This differs intentionally from the mutable Issue dataclass (domain entity
# in models.py). Frozen dataclasses prevent accidental mutation after loading
# and enable safe caching in dict lookups.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateDefinition:
    """A named state within a type's workflow, mapped to a universal category."""

    name: str
    category: StateCategory

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.match(self.name):
            msg = f"Invalid state name '{self.name}': must match ^[a-z][a-z0-9_]{{0,63}}$"
            raise ValueError(msg)
        if self.category not in _VALID_CATEGORIES:
            allowed = sorted(_VALID_CATEGORIES)
            msg = f"Invalid category '{self.category}' for state '{self.name}': must be one of {allowed}"
            raise ValueError(msg)


@dataclass(frozen=True)
class TransitionDefinition:
    """A valid state transition with enforcement level and field requirements."""

    from_state: str
    to_state: str
    enforcement: EnforcementLevel
    requires_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.match(self.from_state):
            msg = f"Invalid from_state '{self.from_state}': must match ^[a-z][a-z0-9_]{{0,63}}$"
            raise ValueError(msg)
        if not _NAME_PATTERN.match(self.to_state):
            msg = f"Invalid to_state '{self.to_state}': must match ^[a-z][a-z0-9_]{{0,63}}$"
            raise ValueError(msg)
        if self.enforcement not in ("hard", "soft"):
            msg = f"Invalid enforcement '{self.enforcement}': must be 'hard' or 'soft'"
            raise ValueError(msg)
        for field_name in self.requires_fields:
            if not _NAME_PATTERN.match(field_name):
                msg = f"Invalid requires_fields entry '{field_name}': must match ^[a-z][a-z0-9_]{{0,63}}$"
                raise ValueError(msg)


_VALID_FIELD_TYPES: frozenset[str] = frozenset(get_args(FieldType))


@dataclass(frozen=True)
class FieldSchema:
    """Schema for a custom field on an issue type."""

    name: str
    type: FieldType
    description: str = ""
    options: tuple[str, ...] = ()
    default: Any = None
    required_at: tuple[str, ...] = ()
    pattern: str | None = None
    unique: bool = False

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.match(self.name):
            msg = f"Invalid field name '{self.name}': must match ^[a-z][a-z0-9_]{{0,63}}$"
            raise ValueError(msg)
        if self.type not in _VALID_FIELD_TYPES:
            allowed = sorted(_VALID_FIELD_TYPES)
            msg = f"Invalid field type '{self.type}' for field '{self.name}': must be one of {allowed}"
            raise ValueError(msg)
        if self.type == "enum" and not self.options:
            msg = f"Field '{self.name}' has type 'enum' but no options defined"
            raise ValueError(msg)
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                msg = f"Invalid regex pattern for field '{self.name}': {exc}"
                raise ValueError(msg) from exc
        for state_name in self.required_at:
            if not _NAME_PATTERN.match(state_name):
                msg = f"Invalid required_at entry '{state_name}' in field '{self.name}': must match ^[a-z][a-z0-9_]{{0,63}}$"
                raise ValueError(msg)


_MAX_PATTERN_VALUE_LENGTH = 10_000


def validate_field_pattern(field: FieldSchema, value: Any) -> str | None:
    """Return an error message if *value* doesn't match *field.pattern*, else None.

    Skips None and blank strings (empty or whitespace-only) — let ``required_at`` handle presence.
    Uses ``re.fullmatch`` so the pattern must match the entire value.
    Values exceeding _MAX_PATTERN_VALUE_LENGTH are rejected to prevent
    catastrophic backtracking on complex patterns.
    """
    if field.pattern is None:
        return None
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if not isinstance(value, str):
        return f"Field '{field.name}' pattern requires a string value, got {type(value).__name__}"
    if len(value) > _MAX_PATTERN_VALUE_LENGTH:
        return f"Field '{field.name}' value length {len(value)} exceeds maximum {_MAX_PATTERN_VALUE_LENGTH} for pattern matching"
    if not re.fullmatch(field.pattern, value):
        return f"Field '{field.name}' value '{value}' does not match required pattern: {field.pattern}"
    return None


@dataclass(frozen=True)
class TypeTemplate:
    """Complete workflow definition for an issue type."""

    type: str
    display_name: str
    description: str
    pack: str
    states: tuple[StateDefinition, ...]
    initial_state: str
    transitions: tuple[TransitionDefinition, ...]
    fields_schema: tuple[FieldSchema, ...]
    reverse_transitions: tuple[TransitionDefinition, ...] = ()
    suggested_children: tuple[str, ...] = ()
    suggested_labels: tuple[str, ...] = ()

    def canonical_working_status(self) -> str:
        """Return the unique wip-category status name for this type.

        Used by ``start_work`` / ``start_next_work`` to default
        ``target_status`` for type-level callers. Raises
        ``AmbiguousTransitionError`` if multiple wip statuses exist (caller
        must specify ``target_status`` explicitly); raises
        ``InvalidTransitionError`` if the type has no wip statuses.
        """
        from filigree.types.api import AmbiguousTransitionError, InvalidTransitionError

        wip = [s.name for s in self.states if s.category == "wip"]
        if len(wip) == 1:
            return wip[0]
        if not wip:
            raise InvalidTransitionError(self.type, self.initial_state)
        raise AmbiguousTransitionError(self.type, wip)

    def reachable_working_status(self, current_status: str) -> str:
        """Return the unique wip-category target reachable from current_status.

        If the issue is already in a wip-category state, that status is the
        working target so handoff claims do not accidentally advance workflow.
        """
        from filigree.types.api import AmbiguousTransitionError, InvalidTransitionError

        categories = {state.name: state.category for state in self.states}
        if categories.get(current_status) == "wip":
            return current_status

        candidates: list[str] = []
        for transition in self.transitions:
            if transition.from_state == current_status and categories.get(transition.to_state) == "wip":
                candidates.append(transition.to_state)
        candidates = list(dict.fromkeys(candidates))

        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise InvalidTransitionError(self.type, current_status)
        raise AmbiguousTransitionError(self.type, candidates, current_status=current_status)


@dataclass(frozen=True)
class WorkflowPack:
    """A bundle of related type templates with relationships and guidance.

    All container fields are immutable: types and guide use MappingProxyType,
    relationships use tuples of MappingProxyType.
    """

    pack: str
    version: str
    display_name: str
    description: str
    types: MappingProxyType[str, TypeTemplate]
    requires_packs: tuple[str, ...]
    relationships: tuple[MappingProxyType[str, Any], ...]
    cross_pack_relationships: tuple[MappingProxyType[str, Any], ...]
    guide: MappingProxyType[str, Any] | None

    def __post_init__(self) -> None:
        # Auto-wrap plain dicts for callers that don't use MappingProxyType
        if isinstance(self.types, dict) and not isinstance(self.types, MappingProxyType):
            object.__setattr__(self, "types", MappingProxyType(self.types))
        if isinstance(self.guide, dict) and not isinstance(self.guide, MappingProxyType):
            object.__setattr__(self, "guide", MappingProxyType(self.guide))

        # Wrap relationship dicts inside tuples
        def _freeze_rels(rels: tuple[Any, ...]) -> tuple[MappingProxyType[str, Any], ...]:
            return tuple(MappingProxyType(r) if isinstance(r, dict) and not isinstance(r, MappingProxyType) else r for r in rels)

        if self.relationships and any(not isinstance(r, MappingProxyType) for r in self.relationships):
            object.__setattr__(self, "relationships", _freeze_rels(self.relationships))
        if self.cross_pack_relationships and any(not isinstance(r, MappingProxyType) for r in self.cross_pack_relationships):
            object.__setattr__(self, "cross_pack_relationships", _freeze_rels(self.cross_pack_relationships))


@dataclass(frozen=True)
class TransitionResult:
    """Result of validating a specific state transition."""

    allowed: bool
    enforcement: EnforcementLevel | None
    missing_fields: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TransitionOption:
    """A possible next state from the current state."""

    to: str
    category: StateCategory
    enforcement: EnforcementLevel | None
    requires_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    ready: bool


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating an issue against its template."""

    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        """Derived from errors — impossible to construct inconsistent state."""
        return len(self.errors) == 0


# ---------------------------------------------------------------------------
# TemplateRegistry
# ---------------------------------------------------------------------------


class TemplateRegistry:
    """Loads, caches, and queries workflow templates and packs.

    Templates are loaded once per instance and cached for the entire lifetime.
    The registry builds O(1) lookup caches for category mapping and
    transition validation.
    """

    # Size limits for custom templates (prevent DoS via huge templates)
    MAX_STATES = 50
    MAX_TRANSITIONS = 200
    MAX_FIELDS = 50

    def __init__(self) -> None:
        self._types: dict[str, TypeTemplate] = {}
        self._packs: dict[str, WorkflowPack] = {}
        self._category_cache: dict[str, dict[str, StateCategory]] = {}
        self._transition_cache: dict[str, dict[tuple[str, str], TransitionDefinition]] = {}
        self._reverse_transition_cache: dict[str, dict[tuple[str, str], TransitionDefinition]] = {}
        self._loaded = False

    # -- Parsing (from dict/JSON) -------------------------------------------

    @staticmethod
    def parse_type_template(raw: dict[str, Any]) -> TypeTemplate:
        """Parse a type template from a JSON-compatible dict.

        Args:
            raw: Dictionary with type template fields as defined in design Section 3.2.

        Returns:
            A frozen TypeTemplate instance.

        Raises:
            ValueError: If required fields are missing, invalid, or exceed size limits.
            KeyError: If required keys are missing from the dict.
        """
        # Validate type name format
        type_name = raw["type"]
        if not _NAME_PATTERN.match(type_name):
            msg = f"Invalid type name '{type_name}': must match ^[a-z][a-z0-9_]{{0,63}}$"
            raise ValueError(msg)

        # Defensive shape checks (must come before size limits to avoid TypeError on None)
        raw_states = raw.get("states")
        if not isinstance(raw_states, list):
            msg = f"Type '{type_name}': 'states' must be a list, got {type(raw_states).__name__}"
            raise ValueError(msg)
        if not raw_states:
            msg = f"Type '{type_name}': must have at least one state"
            raise ValueError(msg)
        for i, s in enumerate(raw_states):
            if not isinstance(s, dict) or "name" not in s or "category" not in s:
                msg = f"Type '{type_name}': state at index {i} must be a dict with 'name' and 'category'"
                raise ValueError(msg)

        # Defensive shape checks for transitions and fields_schema
        raw_transitions = raw.get("transitions", [])
        if raw_transitions is not None and not isinstance(raw_transitions, list):
            msg = f"Type '{type_name}': 'transitions' must be a list, got {type(raw_transitions).__name__}"
            raise ValueError(msg)
        if raw_transitions is None:
            raw_transitions = []
        for i, t in enumerate(raw_transitions):
            if not isinstance(t, dict):
                msg = f"Type '{type_name}': transition at index {i} must be a dict, got {type(t).__name__}"
                raise ValueError(msg)

        raw_reverse_transitions = raw.get("reverse_transitions", [])
        if raw_reverse_transitions is not None and not isinstance(raw_reverse_transitions, list):
            msg = f"Type '{type_name}': 'reverse_transitions' must be a list, got {type(raw_reverse_transitions).__name__}"
            raise ValueError(msg)
        if raw_reverse_transitions is None:
            raw_reverse_transitions = []
        for i, t in enumerate(raw_reverse_transitions):
            if not isinstance(t, dict):
                msg = f"Type '{type_name}': reverse_transition at index {i} must be a dict, got {type(t).__name__}"
                raise ValueError(msg)

        raw_fields = raw.get("fields_schema", [])
        if raw_fields is not None and not isinstance(raw_fields, list):
            msg = f"Type '{type_name}': 'fields_schema' must be a list, got {type(raw_fields).__name__}"
            raise ValueError(msg)
        if raw_fields is None:
            raw_fields = []
        for i, f in enumerate(raw_fields):
            if not isinstance(f, dict):
                msg = f"Type '{type_name}': field at index {i} must be a dict, got {type(f).__name__}"
                raise ValueError(msg)

        # Enforcement validation (filigree-9b9e45: only "hard"/"soft" are valid)
        valid_enforcement = {"hard", "soft"}
        for t in [*raw_transitions, *raw_reverse_transitions]:
            enforcement_val = t.get("enforcement")
            if enforcement_val not in valid_enforcement:
                allowed = ", ".join(sorted(valid_enforcement))
                msg = (
                    f"Type '{type_name}': transition "
                    f"{t.get('from')}->{t.get('to')} "
                    f"has invalid enforcement '{enforcement_val}' "
                    f"(must be one of: {allowed})"
                )
                raise ValueError(msg)

        # Size limit checks (prevent DoS via huge templates)
        if len(raw_states) > TemplateRegistry.MAX_STATES:
            msg = f"Type '{type_name}' has {len(raw_states)} states (max {TemplateRegistry.MAX_STATES})"
            raise ValueError(msg)
        total_transitions = len(raw_transitions) + len(raw_reverse_transitions)
        if total_transitions > TemplateRegistry.MAX_TRANSITIONS:
            msg = f"Type '{type_name}' has {total_transitions} transitions (max {TemplateRegistry.MAX_TRANSITIONS})"
            raise ValueError(msg)
        if len(raw_fields) > TemplateRegistry.MAX_FIELDS:
            msg = f"Type '{type_name}' has {len(raw_fields)} fields (max {TemplateRegistry.MAX_FIELDS})"
            raise ValueError(msg)

        logger.debug("Parsing template for type: %s", type_name)

        # StateDefinition.__post_init__ validates each state name format + category
        states = tuple(StateDefinition(name=s["name"], category=s["category"]) for s in raw_states)

        # Detect duplicate state names (filigree-eff214)
        seen_names: set[str] = set()
        for s in states:
            if s.name in seen_names:
                msg = f"Type '{type_name}': duplicate state name '{s.name}'"
                raise ValueError(msg)
            seen_names.add(s.name)

        transitions = tuple(
            TransitionDefinition(
                from_state=t["from"],
                to_state=t["to"],
                enforcement=t["enforcement"],
                requires_fields=tuple(t.get("requires_fields", [])),
            )
            for t in raw_transitions
        )
        reverse_transitions = tuple(
            TransitionDefinition(
                from_state=t["from"],
                to_state=t["to"],
                enforcement=t["enforcement"],
                requires_fields=tuple(t.get("requires_fields", [])),
            )
            for t in raw_reverse_transitions
        )

        # Detect duplicate (from_state, to_state) pairs (filigree-ab91b3, filigree-3e3f12)
        seen_transitions: set[tuple[str, str]] = set()
        for t in transitions:
            key = (t.from_state, t.to_state)
            if key in seen_transitions:
                msg = f"Type '{type_name}': duplicate transition '{t.from_state}' -> '{t.to_state}'"
                raise ValueError(msg)
            seen_transitions.add(key)
        seen_reverse_transitions: set[tuple[str, str]] = set()
        for t in reverse_transitions:
            key = (t.from_state, t.to_state)
            if key in seen_reverse_transitions:
                msg = f"Type '{type_name}': duplicate reverse_transition '{t.from_state}' -> '{t.to_state}'"
                raise ValueError(msg)
            seen_reverse_transitions.add(key)
        fields_schema = tuple(
            FieldSchema(
                name=f["name"],
                type=f["type"],
                description=f.get("description", ""),
                options=tuple(f.get("options", [])),
                default=f.get("default"),
                required_at=tuple(f.get("required_at", [])),
                pattern=f.get("pattern"),
                unique=f.get("unique", False),
            )
            for f in raw_fields
        )
        return TypeTemplate(
            type=type_name,
            display_name=raw["display_name"],
            description=raw.get("description", ""),
            pack=raw.get("pack", "custom"),
            states=states,
            initial_state=raw["initial_state"],
            transitions=transitions,
            fields_schema=fields_schema,
            reverse_transitions=reverse_transitions,
            suggested_children=tuple(raw.get("suggested_children", [])),
            suggested_labels=tuple(raw.get("suggested_labels", [])),
        )

    @staticmethod
    def validate_type_template(tpl: TypeTemplate) -> list[str]:
        """Validate a TypeTemplate for internal consistency.

        Returns:
            List of error messages. Empty list means valid.
        """
        errors: list[str] = []
        state_names = {s.name for s in tpl.states}

        # Detect duplicate state names (filigree-eff214)
        if len(state_names) != len(tpl.states):
            seen: set[str] = set()
            for st in tpl.states:
                if st.name in seen:
                    errors.append(f"duplicate state name '{st.name}'")
                seen.add(st.name)

        if tpl.initial_state not in state_names:
            errors.append(f"initial_state '{tpl.initial_state}' is not in states list")

        # Detect duplicate (from_state, to_state) pairs (filigree-ab91b3, filigree-3e3f12)
        seen_trans: set[tuple[str, str]] = set()
        for t in tpl.transitions:
            key = (t.from_state, t.to_state)
            if key in seen_trans:
                errors.append(f"duplicate transition '{t.from_state}' -> '{t.to_state}'")
            seen_trans.add(key)

        for t in tpl.transitions:
            if t.from_state not in state_names:
                errors.append(f"transition from_state '{t.from_state}' is not in states list")
            if t.to_state not in state_names:
                errors.append(f"transition to_state '{t.to_state}' is not in states list")
        seen_reverse_trans: set[tuple[str, str]] = set()
        for t in tpl.reverse_transitions:
            key = (t.from_state, t.to_state)
            if key in seen_reverse_trans:
                errors.append(f"duplicate reverse_transition '{t.from_state}' -> '{t.to_state}'")
            seen_reverse_trans.add(key)
            if t.from_state not in state_names:
                errors.append(f"reverse_transition from_state '{t.from_state}' is not in states list")
            if t.to_state not in state_names:
                errors.append(f"reverse_transition to_state '{t.to_state}' is not in states list")

        field_names = {f.name for f in tpl.fields_schema}
        for t in tpl.transitions:
            for rf in t.requires_fields:
                if rf not in field_names:
                    errors.append(f"transition {t.from_state}->{t.to_state} requires_fields '{rf}' not in fields_schema")
        for t in tpl.reverse_transitions:
            for rf in t.requires_fields:
                if rf not in field_names:
                    errors.append(f"reverse_transition {t.from_state}->{t.to_state} requires_fields '{rf}' not in fields_schema")

        for f in tpl.fields_schema:
            for ra in f.required_at:
                if ra not in state_names:
                    errors.append(f"field '{f.name}' required_at '{ra}' is not in states list")

        # Reachability: all states should be reachable from initial_state
        if tpl.initial_state in state_names:
            reachable: set[str] = set()
            queue = [tpl.initial_state]
            while queue:
                current = queue.pop(0)
                if current in reachable:
                    continue
                reachable.add(current)
                for t in tpl.transitions:
                    if t.from_state == current and t.to_state not in reachable:
                        queue.append(t.to_state)
            unreachable = state_names - reachable
            for s in sorted(unreachable):
                errors.append(f"state '{s}' is unreachable from initial_state '{tpl.initial_state}'")

        return errors

    @staticmethod
    def check_type_template_quality(tpl: TypeTemplate) -> list[str]:
        """Check a TypeTemplate for quality issues (non-blocking warnings).

        Returns:
            List of warning messages. These don't prevent registration.
        """
        warnings: list[str] = []
        state_names = {s.name for s in tpl.states}
        done_states = {s.name for s in tpl.states if s.category == "done"}
        from_states = {t.from_state for t in tpl.transitions}

        # Dead-end detection: non-done states should have at least one outgoing transition
        for s in sorted(state_names - done_states):
            if s not in from_states:
                cat = next(st.category for st in tpl.states if st.name == s)
                warnings.append(f"state '{s}' (category={cat}) has no outgoing transitions (dead end)")

        # Reverse-reachability: all non-done reachable states should be able
        # to reach at least one done-category state (filigree-d6ade2d45b).
        # Without this, states can be reachable from initial_state but stuck
        # in cycles (e.g. review⇄revise) with no exit to completion.
        if done_states:
            # BFS backwards from all done states
            can_reach_done: set[str] = set(done_states)
            queue_rev = list(done_states)
            while queue_rev:
                current = queue_rev.pop(0)
                for t in tpl.transitions:
                    if t.to_state == current and t.from_state not in can_reach_done:
                        can_reach_done.add(t.from_state)
                        queue_rev.append(t.from_state)
            # Only warn about non-done states that are reachable from initial
            # but cannot reach any done state
            reachable_fwd: set[str] = set()
            queue_fwd = [tpl.initial_state] if tpl.initial_state in state_names else []
            while queue_fwd:
                current = queue_fwd.pop(0)
                if current in reachable_fwd:
                    continue
                reachable_fwd.add(current)
                for t in tpl.transitions:
                    if t.from_state == current and t.to_state not in reachable_fwd:
                        queue_fwd.append(t.to_state)
            stuck = (reachable_fwd - done_states) - can_reach_done
            for s in sorted(stuck):
                warnings.append(f"state '{s}' cannot reach any done-category state (stuck in cycle)")

        # Done-states with outgoing transitions to other done states are
        # unreachable: close_issue() treats done-category as "already closed",
        # so done→done transitions can never fire.  Done→non-done transitions
        # (e.g. released→rolled_back) are fine — reachable via update_issue().
        for s in sorted(done_states & from_states):
            done_targets = [t.to_state for t in tpl.transitions if t.from_state == s and t.to_state in done_states]
            if done_targets:
                warnings.append(
                    f"done-category state '{s}' has transitions to other done states {done_targets} — "
                    f"these are unreachable because close_issue() rejects issues already in a done state"
                )

        return warnings

    # -- Registration (internal) --------------------------------------------

    def _register_type(self, tpl: TypeTemplate) -> None:
        """Register a type template and build caches."""
        logger.debug("Registering type: %s (pack=%s, %d states)", tpl.type, tpl.pack, len(tpl.states))
        self._types[tpl.type] = tpl

        # Build category cache -- O(1) lookup, atomic replacement
        self._category_cache[tpl.type] = {state.name: state.category for state in tpl.states}

        # Build transition cache -- O(1) lookup
        self._transition_cache[tpl.type] = {(t.from_state, t.to_state): t for t in tpl.transitions}
        self._reverse_transition_cache[tpl.type] = {(t.from_state, t.to_state): t for t in tpl.reverse_transitions}

    def _register_pack(self, pack: WorkflowPack) -> None:
        """Register a workflow pack."""
        logger.debug("Registering pack: %s (version=%s, %d types)", pack.pack, pack.version, len(pack.types))
        self._packs[pack.pack] = pack

    # -- Queries ------------------------------------------------------------

    def get_type(self, type_name: str) -> TypeTemplate | None:
        """Get a type template by name."""
        return self._types.get(type_name)

    def get_pack(self, pack_name: str) -> WorkflowPack | None:
        """Get a pack by name."""
        return self._packs.get(pack_name)

    def list_types(self) -> list[TypeTemplate]:
        """All types from enabled packs."""
        return list(self._types.values())

    def list_packs(self) -> list[WorkflowPack]:
        """All enabled packs."""
        return list(self._packs.values())

    def get_initial_state(self, type_name: str) -> str:
        """Initial state for a type. Raises ValueError if type is unknown."""
        tpl = self._types.get(type_name)
        if tpl is None:
            msg = (
                f"Unknown type '{type_name}': no workflow template registered. "
                f"Cannot determine initial state. Register a pack containing "
                f"this type or check for typos."
            )
            raise ValueError(msg)
        return tpl.initial_state

    def get_category(self, type_name: str, state: str) -> StateCategory | None:
        """Map a (type, state) pair to its category via O(1) cache."""
        type_cache = self._category_cache.get(type_name)
        if type_cache is None:
            return None
        return type_cache.get(state)

    def get_valid_states(self, type_name: str) -> list[str] | None:
        """Return list of valid state names for a type, or None if type unknown."""
        tpl = self._types.get(type_name)
        if tpl is None:
            return None
        return [s.name for s in tpl.states]

    def get_first_state_of_category(self, type_name: str, category: StateCategory) -> str | None:
        """Return the first state of a given category (array order)."""
        tpl = self._types.get(type_name)
        if tpl is None:
            return None
        for state in tpl.states:
            if state.category == category:
                return state.name
        return None

    def get_release_target(self, type_name: str, current_status: str) -> str | None:
        """Find an open-category state that should be the post-release status.

        For ``release_claim`` auto-revert (filigree-cb980eee0d, P1.3): when a
        claim is released on a wip-category issue, return the open-category
        predecessor whose forward transition targets ``current_status``. If
        multiple open predecessors exist, the first one in transition order
        wins; if none exists, returns ``None`` so the caller can leave the
        issue in its current status (no reverse target available).

        Returns ``None`` for done-category statuses (release on a done issue
        should not transition) and for unknown types.
        """
        tpl = self._types.get(type_name)
        if tpl is None:
            return None
        # Find which category the current status sits in.
        states_by_name = {s.name: s for s in tpl.states}
        current = states_by_name.get(current_status)
        if current is None or current.category != "wip":
            return None
        # Walk transitions in declaration order; first open-category predecessor wins.
        for transition in tpl.transitions:
            if transition.to_state != current_status:
                continue
            predecessor = states_by_name.get(transition.from_state)
            if predecessor is not None and predecessor.category == "open":
                return predecessor.name
        # No direct open predecessor — fallback to the template's initial_state
        # if it's open-category. This handles bug.verifying (only fixing→verifying)
        # by falling back to triage so the issue still rejoins discovery.
        initial = states_by_name.get(tpl.initial_state)
        if initial is not None and initial.category == "open":
            return initial.name
        return None

    # -- Validation ---------------------------------------------------------

    @staticmethod
    def _is_field_populated(value: Any) -> bool:
        """Check if a field value is considered populated.

        None, empty strings, whitespace-only strings, and empty
        collections (list, dict) are unpopulated.
        """
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip() != ""
        if isinstance(value, (list, dict)):
            return len(value) > 0
        return True

    def validate_transition(
        self,
        type_name: str,
        from_state: str,
        to_state: str,
        fields: dict[str, Any],
        *,
        backward: bool = False,
    ) -> TransitionResult:
        """Validate a state transition.

        Args:
            type_name: The issue type.
            from_state: Current state.
            to_state: Target state.
            fields: Current issue fields dict.
            backward: If True, validate against ``reverse_transitions`` and
                raise ``InvalidTransitionError`` when the reverse edge is
                undeclared; reverse edges enforce only their explicit
                ``requires_fields`` and do not inherit target ``required_at``
                gates.

        Returns:
            TransitionResult indicating whether the transition is allowed,
            its enforcement level, any missing required fields, and warnings.
        """
        tpl = self._types.get(type_name)
        if tpl is None:
            return TransitionResult(
                allowed=False,
                enforcement=None,
                missing_fields=(),
                warnings=(
                    f"Unknown type '{type_name}': no workflow template registered. "
                    f"Transition denied (fail-closed). Register a pack containing this type "
                    f"or check for typos.",
                ),
            )

        transition_map = self._reverse_transition_cache.get(type_name, {}) if backward else self._transition_cache.get(type_name, {})
        transition = transition_map.get((from_state, to_state))

        if transition is None:
            if backward:
                from filigree.types.api import InvalidTransitionError

                raise InvalidTransitionError(type_name, from_state, to_state=to_state, backward=True)
            # Transition not in table: REJECTED for known types
            return TransitionResult(
                allowed=False,
                enforcement=None,
                missing_fields=(),
                warnings=(
                    f"Transition '{from_state}' -> '{to_state}' is not in the standard workflow for '{type_name}'. "
                    f"Use get_valid_transitions() to see recommended transitions.",
                ),
            )

        # Check required fields for this transition
        missing = tuple(f for f in transition.requires_fields if not self._is_field_populated(fields.get(f)))

        # Forward workflow transitions also honor fields required at the target
        # state. Backward/escape edges only enforce their explicit
        # requires_fields contract so force-close preserves its historical
        # cleanup semantics instead of inheriting normal close gates.
        state_required = [] if backward else self.validate_fields_for_state(type_name, to_state, fields)
        all_missing = tuple(dict.fromkeys(list(missing) + state_required))  # dedupe, preserve order

        warnings: list[str] = []

        if transition.enforcement == "hard" and all_missing:
            return TransitionResult(
                allowed=False,
                enforcement="hard",
                missing_fields=all_missing,
                warnings=(),
            )

        if transition.enforcement == "soft" and all_missing:
            warnings.append(f"Missing recommended fields for '{to_state}': {', '.join(all_missing)}")

        return TransitionResult(
            allowed=True,
            enforcement=transition.enforcement,
            missing_fields=all_missing,
            warnings=tuple(warnings),
        )

    def get_valid_transitions(
        self,
        type_name: str,
        current_state: str,
        fields: dict[str, Any],
    ) -> list[TransitionOption]:
        """All valid transitions from current state with readiness info.

        Args:
            type_name: The issue type.
            current_state: Current state.
            fields: Current issue fields dict.

        Returns:
            List of TransitionOption with readiness and field requirement info.
        """
        tpl = self._types.get(type_name)
        if tpl is None:
            return []

        options: list[TransitionOption] = []
        for t in tpl.transitions:
            if t.from_state != current_state:
                continue

            # Check missing fields for this transition
            missing_trans = [f for f in t.requires_fields if not self._is_field_populated(fields.get(f))]
            missing_state = self.validate_fields_for_state(type_name, t.to_state, fields)
            all_missing = list(dict.fromkeys(missing_trans + missing_state))

            target_category = self._category_cache[type_name][t.to_state]
            # ready = True when all required fields are populated
            ready = len(all_missing) == 0

            options.append(
                TransitionOption(
                    to=t.to_state,
                    category=target_category,
                    enforcement=t.enforcement,
                    requires_fields=t.requires_fields,
                    missing_fields=tuple(all_missing),
                    ready=ready,
                )
            )

        return options

    def validate_fields_for_state(
        self,
        type_name: str,
        state: str,
        fields: dict[str, Any],
    ) -> list[str]:
        """Return fields required at this state but not yet populated.

        Args:
            type_name: The issue type.
            state: The state to check requirements for.
            fields: Current issue fields dict.

        Returns:
            List of field names that are required but missing.
        """
        tpl = self._types.get(type_name)
        if tpl is None:
            return []

        missing: list[str] = []
        for f in tpl.fields_schema:
            if state in f.required_at and not self._is_field_populated(fields.get(f.name)):
                missing.append(f.name)
        return missing

    # -- Loading ------------------------------------------------------------

    def load(self, filigree_dir: Path, *, enabled_packs: list[str] | None = None) -> None:
        """Load templates from all three layers (built-in, DB, filesystem overrides).

        Layer 1: Built-in packs from templates_data.BUILT_IN_PACKS
        Layer 2: Installed packs from .filigree/packs/*.json
        Layer 3: Project-local overrides from .filigree/templates/*.json

        Idempotent: second call is a no-op.

        Args:
            filigree_dir: Path to the .filigree/ directory.
            enabled_packs: Optional override for enabled packs. If None, reads from config.
        """
        if self._loaded:
            return

        import json as _json

        from filigree.templates_data import BUILT_IN_PACKS

        _default_packs = ["core", "planning", "release"]
        if enabled_packs is None:
            # Read enabled packs from config
            config_path = filigree_dir / "config.json"
            enabled_packs = _default_packs
            if config_path.exists():
                try:
                    config = _json.loads(config_path.read_text())
                    if not isinstance(config, dict):
                        raise ValueError("config.json must contain a JSON object")
                    enabled_packs = config.get("enabled_packs", _default_packs)
                except (_json.JSONDecodeError, ValueError) as exc:
                    logger.warning("Could not read config.json (%s) — using default enabled_packs", exc)

        # Validate enabled_packs is list[str] — strings would be split into chars
        if isinstance(enabled_packs, str):
            logger.warning("enabled_packs is a string ('%s'), wrapping in list", enabled_packs)
            enabled_packs = [enabled_packs]
        elif not isinstance(enabled_packs, list):
            logger.warning("enabled_packs has invalid type %s — using defaults", type(enabled_packs).__name__)
            enabled_packs = _default_packs
        else:
            non_str = [p for p in enabled_packs if not isinstance(p, str)]
            if non_str:
                logger.warning("Dropped non-string entries from enabled_packs: %s", non_str)
            enabled_packs = [p for p in enabled_packs if isinstance(p, str)]

        logger.info("Loading templates: enabled_packs=%s", enabled_packs)

        # Layer 1: Built-in packs
        for pack_name, pack_data in BUILT_IN_PACKS.items():
            if pack_name not in enabled_packs:
                logger.debug("Skipping disabled built-in pack: %s", pack_name)
                continue
            self._load_pack_data(pack_data)

        # Layer 2: Installed packs from .filigree/packs/*.json
        packs_dir = filigree_dir / "packs"
        if packs_dir.is_dir():
            for pack_file in sorted(packs_dir.glob("*.json")):
                try:
                    pack_data = _json.loads(pack_file.read_text())
                    if not isinstance(pack_data, dict):
                        raise TypeError(f"pack root must be an object, got {type(pack_data).__name__}")
                    pack_name = pack_data.get("pack", pack_file.stem)
                    if "pack" not in pack_data:
                        pack_data["pack"] = pack_name
                    if pack_name not in enabled_packs:
                        logger.debug("Skipping disabled installed pack: %s", pack_name)
                        continue
                    self._load_pack_data(pack_data)
                    logger.info("Loaded installed pack: %s from %s", pack_name, pack_file.name)
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("Skipping invalid pack file %s: %s", pack_file.name, exc)

        # Layer 3: Project-local overrides from .filigree/templates/*.json
        templates_dir = filigree_dir / "templates"
        if templates_dir.is_dir():
            for tpl_file in sorted(templates_dir.glob("*.json")):
                try:
                    raw = _json.loads(tpl_file.read_text())
                    tpl = self.parse_type_template(raw)
                    errors = self.validate_type_template(tpl)
                    if errors:
                        logger.warning("Skipping invalid template %s: %s", tpl_file.name, errors)
                        continue
                    quality_warnings = self.check_type_template_quality(tpl)
                    for qw in quality_warnings:
                        logger.warning("Quality: %s (local override): %s", tpl.type, qw)
                    self._register_type(tpl)  # Overwrites built-in with same name
                    logger.info("Loaded project-local template override: %s", tpl.type)
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("Skipping invalid template file %s: %s", tpl_file.name, exc)

        self._loaded = True
        logger.info("Template loading complete: %d types from %d packs", len(self._types), len(self._packs))

    def _load_pack_data(self, pack_data: dict[str, Any]) -> None:
        """Load a pack dict: register its types and the pack itself.

        Types that fail parsing or validation are skipped individually (logged
        as warnings). Successfully parsed types are collected in a staging dict
        first, then registered together, ensuring pack.types and registry._types
        see the same set of types.
        """
        pack_name = pack_data["pack"]

        # Phase 1: Parse all types into staging dict (no side effects)
        types_dict: dict[str, TypeTemplate] = {}
        raw_types = pack_data.get("types", {})
        if not isinstance(raw_types, dict):
            raise TypeError(f"pack {pack_name} types must be an object, got {type(raw_types).__name__}")
        for type_name, type_data in raw_types.items():
            try:
                tpl = self.parse_type_template(type_data)
                # Ensure the type is tagged with the actual pack name,
                # not the default "custom" from missing pack field in type data
                if tpl.pack != pack_name:
                    tpl = _dc_replace(tpl, pack=pack_name)
                errors = self.validate_type_template(tpl)
                if errors:
                    logger.warning("Skipping invalid type %s in pack %s: %s", type_name, pack_name, errors)
                    continue
                quality_warnings = self.check_type_template_quality(tpl)
                for qw in quality_warnings:
                    logger.warning("Quality: %s/%s: %s", pack_name, type_name, qw)
                types_dict[type_name] = tpl
            except (ValueError, KeyError) as exc:
                logger.warning("Skipping unparseable type %s in pack %s: %s", type_name, pack_name, exc)

        # Phase 2: Register all staged types atomically
        for tpl in types_dict.values():
            self._register_type(tpl)

        # Register the pack itself — wrap mutable containers for immutability
        raw_guide = pack_data.get("guide")
        if raw_guide is not None and not isinstance(raw_guide, dict):
            logger.warning("Pack %s has non-mapping guide (got %s), ignoring", pack_name, type(raw_guide).__name__)
            raw_guide = None
        pack = WorkflowPack(
            pack=pack_name,
            version=pack_data.get("version", "1.0"),
            display_name=pack_data.get("display_name", pack_name),
            description=pack_data.get("description", ""),
            types=MappingProxyType(types_dict),
            requires_packs=tuple(pack_data.get("requires_packs", [])),
            relationships=tuple(MappingProxyType(r) for r in pack_data.get("relationships", []) if isinstance(r, dict)),
            cross_pack_relationships=tuple(
                MappingProxyType(r) for r in pack_data.get("cross_pack_relationships", []) if isinstance(r, dict)
            ),
            guide=MappingProxyType(raw_guide) if raw_guide is not None else None,
        )
        self._register_pack(pack)
        logger.debug("Registered pack: %s (%d types)", pack_name, len(types_dict))
