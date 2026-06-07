"""WorkflowMixin — template and workflow operations.

Extracted from core.py as part of the module architecture split.
Covers template access, status/parent validation, state resolution,
label validation, transition queries, and issue validation.

All methods access ``self.conn``, ``self.templates``, etc. via
Python's MRO when composed into ``FiligreeDB``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from filigree.db_base import DBMixinProtocol, StatusCategory, _now_iso
from filigree.types.workflow import (
    FieldSchemaInfo,
    StateInfo,
    TemplateInfo,
    TemplateListItem,
    TransitionInfo,
)

if TYPE_CHECKING:
    from filigree.templates import FieldSchema, TemplateRegistry, TransitionOption, ValidationResult


def _is_priority_like_label(label: str) -> bool:
    casefolded = label.casefold()
    if len(casefolded) == 2 and casefolded[0] == "p" and casefolded[1] in "01234":
        return True
    return ":" in label and label.split(":", 1)[0].casefold() == "priority"


def _build_builtin_category_maps() -> tuple[
    dict[tuple[str, str], StatusCategory],
    frozenset[str],
    frozenset[str],
]:
    """Derive ``(type, state) -> category`` and unambiguous state-name sets from bundled packs.

    Used as a correctness floor when the active template registry has no entry for
    a type — e.g. a pack was disabled after issues in that pack were created, or
    a bulk import predates pack registration. Name-only disambiguation only
    promotes states whose category is identical across every bundled type that
    declares them, so values like ``resolved`` (wip in ``incident``, done
    elsewhere) stay ambiguous and fall through to ``"open"``.
    """
    from filigree.templates_data import BUILT_IN_PACKS

    by_type_state: dict[tuple[str, str], StatusCategory] = {}
    by_state: dict[str, set[StatusCategory]] = {}

    for pack_data in BUILT_IN_PACKS.values():
        for type_name, type_data in pack_data.get("types", {}).items():
            for s in type_data.get("states", []):
                name: str = s["name"]
                cat: StatusCategory = s["category"]
                by_type_state[(type_name, name)] = cat
                by_state.setdefault(name, set()).add(cat)

    done_names = frozenset(s for s, cats in by_state.items() if cats == {"done"})
    wip_names = frozenset(s for s, cats in by_state.items() if cats == {"wip"})
    return by_type_state, done_names, wip_names


_BUILTIN_CATEGORY_BY_TYPE_STATE, _BUILTIN_UNAMBIGUOUS_DONE_NAMES, _BUILTIN_UNAMBIGUOUS_WIP_NAMES = _build_builtin_category_maps()


class WorkflowMixin(DBMixinProtocol):
    """Template and workflow operations for FiligreeDB.

    Inherits ``DBMixinProtocol`` for type-safe access to shared attributes
    (``self.conn``, ``self.db_path``, ``self.get_issue()``, etc.). Actual
    implementations provided by ``FiligreeDB`` at composition time via MRO.
    """

    _template_registry: TemplateRegistry | None  # narrowing needed by templates property

    def _filigree_metadata_dir(self) -> Path:
        """Return the project metadata/store directory even when the DB is relocated.

        Resolves to the store dir the DB was opened from (``.weft/filigree/`` or
        legacy ``.filigree/``), set by the anchor-aware constructors. Falls back
        to ``db_path.parent`` for bare ``FiligreeDB(...)`` constructions.
        """
        return self.meta_dir

    @property
    def templates(self) -> TemplateRegistry:
        """Lazy-loaded TemplateRegistry — created on first access.

        Uses runtime import to avoid circular dependency.
        Can be overridden via constructor injection for testing.
        """
        if self._template_registry is None:
            from filigree.templates import TemplateRegistry

            self._template_registry = TemplateRegistry()
            filigree_dir = self._filigree_metadata_dir()
            self._template_registry.load(filigree_dir, enabled_packs=self._enabled_packs_override)
        return self._template_registry

    # -- Templates -----------------------------------------------------------

    @staticmethod
    def _field_schema_to_info(f: FieldSchema) -> FieldSchemaInfo:
        """Convert a FieldSchema dataclass to a FieldSchemaInfo TypedDict."""
        info: FieldSchemaInfo = FieldSchemaInfo(name=f.name, type=f.type, description=f.description)
        if f.options:
            info["options"] = list(f.options)
        if f.default is not None:
            info["default"] = f.default
        if f.required_at:
            info["required_at"] = list(f.required_at)
        if f.pattern:
            info["pattern"] = f.pattern
        if f.unique:
            info["unique"] = f.unique
        return info

    def _seed_templates(self) -> None:
        """Seed built-in packs and type templates into the database."""
        from filigree.core import _seed_builtin_packs

        now = _now_iso()
        _seed_builtin_packs(self.conn, now)

    def reload_templates(self) -> None:
        """Clear the cached template registry so it reloads on next access.

        Also refreshes ``self.enabled_packs`` from config.json when no
        explicit override was provided at construction time.
        """
        self._template_registry = None
        if self._enabled_packs_override is None:
            self._refresh_enabled_packs()

    def _refresh_enabled_packs(self) -> None:
        """Re-read enabled_packs from config.json and update self.enabled_packs."""
        import json as _json

        _default_packs = ["core", "planning", "release"]
        config_path = self._filigree_metadata_dir() / "config.json"
        if not config_path.exists():
            self.enabled_packs = _default_packs
            return
        try:
            config = _json.loads(config_path.read_text())
        except (ValueError, OSError) as exc:
            msg = f"config.json exists but could not be parsed: {exc}"
            raise ValueError(msg) from exc
        if isinstance(config, dict):
            packs = config.get("enabled_packs", _default_packs)
            if isinstance(packs, list):
                self.enabled_packs = [p for p in packs if isinstance(p, str)]
                return
        self.enabled_packs = _default_packs

    def get_template(self, issue_type: str) -> TemplateInfo | None:
        """Get a template by type name from the registry."""
        tpl = self.templates.get_type(issue_type)
        if tpl is None:
            return None
        fields_schema = [self._field_schema_to_info(f) for f in tpl.fields_schema]
        return TemplateInfo(
            type=tpl.type,
            display_name=tpl.display_name,
            description=tpl.description,
            pack=tpl.pack,
            states=[StateInfo(name=s.name, category=s.category) for s in tpl.states],
            initial_state=tpl.initial_state,
            transitions=[
                TransitionInfo(
                    **{
                        "from": t.from_state,
                        "to": t.to_state,
                        "enforcement": t.enforcement,
                        "requires_fields": list(t.requires_fields),
                    }
                )
                for t in tpl.transitions
            ],
            reverse_transitions=[
                TransitionInfo(
                    **{
                        "from": t.from_state,
                        "to": t.to_state,
                        "enforcement": t.enforcement,
                        "requires_fields": list(t.requires_fields),
                    }
                )
                for t in tpl.reverse_transitions
            ],
            fields_schema=fields_schema,
        )

    def list_templates(self) -> list[TemplateListItem]:
        """List all registered templates via the registry (respects enabled_packs)."""
        result: list[TemplateListItem] = []
        for tpl in self.templates.list_types():
            fields = [self._field_schema_to_info(f) for f in tpl.fields_schema]
            result.append(
                TemplateListItem(
                    type=tpl.type,
                    display_name=tpl.display_name,
                    description=tpl.description,
                    fields_schema=fields,
                )
            )
        return sorted(result, key=lambda t: t["type"])

    def _validate_status(self, status: str, issue_type: str = "task") -> None:
        """Validate status against type-specific states from templates.

        Unknown types (no template) skip validation — permissive for custom types.
        """
        valid_states = self.templates.get_valid_states(issue_type)
        if valid_states is not None and status not in valid_states:
            msg = f"Invalid status '{status}' for type '{issue_type}'. Valid states: {', '.join(valid_states)}"
            raise ValueError(msg)

    def _validate_parent_id(self, parent_id: str | None) -> None:
        """Raise ValueError if parent_id does not reference an existing issue."""
        if parent_id is None:
            return
        exists = self.conn.execute("SELECT 1 FROM issues WHERE id = ?", (parent_id,)).fetchone()
        if exists is None:
            msg = f"parent_id '{parent_id}' does not reference an existing issue"
            raise ValueError(msg)

    def _get_states_for_category(self, category: str) -> list[str]:
        """Collect all state names that map to a category across enabled types.

        Returns deduplicated list. Empty if no types are registered.

        Note: this dedups by state name, so when two types share a state
        name with the same category (e.g. 'open') it appears once. It is
        unsafe for SQL category filtering when state names collide across
        categories (e.g. ``incident.resolved`` is wip, ``debt_item.resolved``
        is done) — use ``_get_type_states_for_category`` /
        ``_category_predicate_sql`` for that. (filigree-b55aa3191f)
        """
        seen: set[str] = set()
        states: list[str] = []
        for tpl in self.templates.list_types():
            for s in tpl.states:
                if s.category == category and s.name not in seen:
                    seen.add(s.name)
                    states.append(s.name)
        return states

    def _get_type_states_for_category(self, category: str) -> list[tuple[str, str]]:
        """Collect ``(type, state_name)`` pairs whose category matches.

        Type-aware companion to ``_get_states_for_category``. Used for SQL
        predicates that must respect the type when state names collide
        across categories (filigree-b55aa3191f).

        Mirrors the correctness floor in ``_infer_status_category``: types
        that are not in the active registry (e.g. their pack was disabled
        after issues were created) still contribute their bundled
        ``(type, state)`` pairs, so SQL category predicates agree with the
        Python-level ``_resolve_status_category`` for those rows
        (filigree-c9af813900). Without this, a hydrated blocker shows
        ``status_category="done"`` while readiness/hydration SQL still
        treats it as active.
        """
        active_types: set[str] = set()
        pairs: list[tuple[str, str]] = []
        for tpl in self.templates.list_types():
            active_types.add(tpl.type)
            for s in tpl.states:
                if s.category == category:
                    pairs.append((tpl.type, s.name))
        for (type_name, state_name), cat in _BUILTIN_CATEGORY_BY_TYPE_STATE.items():
            if type_name in active_types:
                continue
            if cat == category:
                pairs.append((type_name, state_name))
        return pairs

    def _category_predicate_sql(
        self,
        category: str,
        *,
        type_col: str,
        status_col: str,
        include_archived: bool = False,
    ) -> tuple[str, list[str]]:
        """Build a SQL fragment matching rows whose ``(type, status)`` maps to *category*.

        Returns ``(sql, params)``. ``include_archived`` ORs in the synthetic
        ``status='archived'`` terminal status — used only by blocker
        semantics, where ``archive_closed()`` rows must count as no-longer-
        blocking even though ``'archived'`` is not a workflow state.

        When no ``(type, state)`` pair matches the requested category (and
        ``include_archived`` is false), returns ``("0", [])`` — a SQL-valid
        always-false predicate — so callers do not need to special-case
        empty registries.
        """
        pairs = self._get_type_states_for_category(category)
        parts: list[str] = []
        params: list[str] = []
        if include_archived:
            parts.append(f"{status_col} = 'archived'")
        if pairs:
            ors = " OR ".join(f"({type_col} = ? AND {status_col} = ?)" for _ in pairs)
            parts.append(f"({ors})")
            for t, s in pairs:
                params.extend([t, s])
        if not parts:
            return ("0", [])
        if len(parts) == 1:
            return (parts[0], params)
        return ("(" + " OR ".join(parts) + ")", params)

    def _blocker_done_states(self) -> list[str]:
        """States that count as 'no longer blocking dependents'.

        This is the workflow's done states plus the synthetic ``'archived'``
        produced by ``archive_closed()``. Use this for readiness queries,
        ``blocked_by`` hydration, and virtual ``has:blockers`` resolution
        — anything that asks "is this blocker still active?". Use the
        plain ``_get_states_for_category("done")`` for archival selection
        (which must not include ``'archived'`` or it would re-archive
        already-archived rows) and for label semantics.
        """
        base = self._get_states_for_category("done") or ["closed"]
        if "archived" in base:
            return base
        return [*base, "archived"]

    @staticmethod
    def _infer_status_category(issue_type: str, status: str) -> StatusCategory:
        """Infer status category when the active registry lacks a matching template.

        Order:
          1. Exact ``(type, state)`` match in the bundled built-in packs. Preserves
             correctness when a pack was disabled after issues were created with it.
          2. State-name match that is unambiguously ``done`` or ``wip`` across every
             bundled type that declares it.
          3. ``"open"`` — permissive fallback for truly custom types.
        """
        cat = _BUILTIN_CATEGORY_BY_TYPE_STATE.get((issue_type, status))
        if cat is not None:
            return cat
        if status in _BUILTIN_UNAMBIGUOUS_DONE_NAMES:
            return "done"
        if status in _BUILTIN_UNAMBIGUOUS_WIP_NAMES:
            return "wip"
        return "open"

    def _resolve_status_category(self, issue_type: str, status: str) -> StatusCategory:
        """Resolve status category via template or fallback heuristic for unknown types.

        The name-only fallback in ``_infer_status_category`` is intended for
        types that are not in the active registry (disabled pack, custom type
        with no template). When the type *is* active but the state is
        undeclared, the row carries invalid template state — running the
        unambiguous-name path would silently classify e.g. a ``task`` with
        status ``released`` as done. Stay conservative and return ``"open"``
        for active-type / undeclared-state pairs; ``validate_issue()``
        surfaces the schema error (filigree-c9af813900).

        When ``archive_closed`` writes the synthetic ``"archived"`` status and
        no active template declares that state, classify it as ``"done"`` so
        ``is_ready``, ``status_category``, and every list/record consumer see
        archived rows as terminal. Explicit template categories take
        precedence, so projects can use a real workflow state named
        ``archived`` without being overridden.
        """
        cat = self.templates.get_category(issue_type, status)
        if cat is not None:
            return cat
        if status == "archived":
            return "done"
        if self.templates.get_type(issue_type) is not None:
            return "open"
        return self._infer_status_category(issue_type, status)

    # Namespace reservation — auto-tag and virtual namespaces are system-managed
    RESERVED_NAMESPACES_AUTO: frozenset[str] = frozenset(
        {
            "area",
            "severity",
            "scanner",
            "pack",
            "lang",
            "rule",
        }
    )
    RESERVED_NAMESPACES_VIRTUAL: frozenset[str] = frozenset({"age", "has"})
    RESERVED_NAMESPACES: frozenset[str] = RESERVED_NAMESPACES_AUTO | RESERVED_NAMESPACES_VIRTUAL

    def _reserved_label_names(self) -> set[str]:
        """Issue type names are reserved and cannot be used as free-form labels."""
        return {tpl.type.casefold() for tpl in self.templates.list_types()}

    def _validate_label_name(self, label: str, *, allow_priority_like: bool = False) -> str:
        """Normalize and validate a label before writing it."""
        if not isinstance(label, str):
            msg = "Label must be a string"
            raise ValueError(msg)
        normalized = label.strip()
        if not normalized:
            msg = "Label cannot be empty"
            raise ValueError(msg)
        # Reject control characters (would corrupt JSONL export line boundaries)
        if any(ord(c) < 32 or c == "\x7f" for c in normalized):
            msg = "Label contains control characters"
            raise ValueError(msg)
        if normalized.casefold() in self._reserved_label_names():
            msg = f"Label '{normalized}' is reserved as an issue type name; set the issue type explicitly instead."
            raise ValueError(msg)
        if not allow_priority_like and _is_priority_like_label(normalized):
            msg = (
                f"Label '{normalized}' conflicts with the priority field; set the numeric priority field "
                "or filter with --priority instead of using P0-P4 or priority:* labels."
            )
            raise ValueError(msg)
        # Check namespace reservation
        if ":" in normalized:
            ns = normalized.split(":", 1)[0].casefold()
            if ns in self.RESERVED_NAMESPACES_AUTO:
                msg = f"{ns}: is a system-managed auto-tag namespace. These labels are computed automatically."
                raise ValueError(msg)
            if ns in self.RESERVED_NAMESPACES_VIRTUAL:
                msg = f"{ns}: is a virtual namespace computed at query time. You can filter by it with --label but cannot add it manually."
                raise ValueError(msg)
        return normalized

    # -- Template-aware queries ----------------------------------------------

    def get_valid_transitions(self, issue_id: str) -> list[TransitionOption]:
        """Return valid next states for an issue with readiness info.

        Delegates to TemplateRegistry.get_valid_transitions() with the issue's
        current state and fields. Returns an empty list for unknown types.
        """
        issue = self.get_issue(issue_id)
        return self.templates.get_valid_transitions(issue.type, issue.status, issue.fields)

    def validate_issue(self, issue_id: str) -> ValidationResult:
        """Validate an issue against its template.

        Emits errors when the issue type has no active template or when the
        current status is not a declared state for that type — both are reachable
        via bulk import, migration, or a pack being disabled after issues were
        created. Also checks whether fields required at the current state are
        populated, surfaces upcoming transition requirements as warnings, and
        validates field values against pattern constraints.
        """
        from filigree.templates import ValidationResult, validate_field_pattern

        issue = self.get_issue(issue_id)
        tpl = self.templates.get_type(issue.type)
        if tpl is None:
            return ValidationResult(
                warnings=(),
                errors=(
                    f"Type '{issue.type}' has no active workflow template. "
                    f"The issue may belong to a disabled pack or an unregistered custom type.",
                ),
            )

        errors: list[str] = []
        declared_states = {s.name for s in tpl.states}
        if issue.status not in declared_states:
            errors.append(
                f"Status '{issue.status}' is not a declared state for type '{issue.type}'. "
                f"Valid states: {', '.join(sorted(declared_states))}."
            )

        warnings: list[str] = []

        # Check required_at fields for current state
        missing = self.templates.validate_fields_for_state(issue.type, issue.status, issue.fields)
        for field_name in missing:
            warnings.append(f"Field '{field_name}' is recommended at state '{issue.status}' for type '{issue.type}' but is not populated.")

        # Check field values against pattern constraints (surfaces non-compliant legacy data)
        for fs in tpl.fields_schema:
            value = issue.fields.get(fs.name)
            err = validate_field_pattern(fs, value)
            if err is not None:
                warnings.append(err)
            if fs.type == "enum" and fs.options and value is not None:
                str_value = str(value)
                if str_value and str_value not in fs.options:
                    warnings.append(f"Field '{fs.name}' value '{str_value}' is not a valid option. Valid options: {', '.join(fs.options)}")

        # Check upcoming requirements: fields needed for next transitions
        transitions = self.templates.get_valid_transitions(issue.type, issue.status, issue.fields)
        for t in transitions:
            if t.missing_fields:
                fields_str = ", ".join(t.missing_fields)
                warnings.append(f"Transition to '{t.to}' requires: {fields_str}")

        return ValidationResult(warnings=tuple(warnings), errors=tuple(errors))
