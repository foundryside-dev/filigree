"""Pure helpers and constants shared across MCP tool modules.

This module has NO dependency on ``mcp_server`` module globals, so it can
be imported freely without triggering circular-import issues.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, TypeVar, cast

from mcp.types import TextContent

from filigree.issue_payloads import issue_to_ready, issue_to_slim
from filigree.models import Issue
from filigree.registry_errors import RegistryPublicError, registry_error_response

if TYPE_CHECKING:
    from filigree.core import FiligreeDB
from filigree.types.api import ErrorCode, ErrorResponse, ListResponse, ReadyIssue, SlimIssue, TransitionError, TransitionHint
from filigree.validation import sanitize_actor

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _parse_args(arguments: dict[str, Any], cls: type[_T]) -> _T:
    """Cast MCP arguments to a typed dict for static analysis.

    Safety: MCP SDK validates argument presence/types against JSON Schema
    before handler invocation. Core validates authoritatively. This cast()
    provides mypy type narrowing only — no runtime validation.
    """
    return cast(_T, arguments)


# Hard cap on list/search results (issues, observations) to keep MCP response
# size within token limits.  Callers can pass no_limit=true to bypass.
_MAX_LIST_RESULTS = 50
_MAX_SQLITE_OFFSET = 9_223_372_036_854_775_807
_MAX_SQLITE_OVERFETCH_LIMIT = _MAX_SQLITE_OFFSET - 1


def _text(content: object) -> list[TextContent]:
    if isinstance(content, str):
        return [TextContent(type="text", text=content)]
    return [TextContent(type="text", text=json.dumps(content, indent=2, default=str))]


def _registry_error_text(exc: RegistryPublicError, *, action: str) -> list[TextContent]:
    return _text(registry_error_response(exc, action=action))


def _slim_issue(issue: Issue) -> SlimIssue:
    """Return a lightweight dict for search result listings."""
    return issue_to_slim(issue)


def _ready_issue(
    issue: Issue,
    *,
    include_context: bool = False,
    parent_title: str | None = None,
    startable: bool = True,
    next_action: str | None = None,
) -> ReadyIssue:
    """Return a ready-queue item, keeping the default shape slim."""
    return issue_to_ready(
        issue,
        include_context=include_context,
        parent_title=parent_title,
        startable=startable,
        next_action=next_action,
    )


def _resolve_pagination(arguments: dict[str, Any]) -> tuple[int, int, list[TextContent] | None]:
    """Compute effective limit and offset for paginated MCP list/search tools.

    Handles the ``no_limit`` bypass and caps to ``_MAX_LIST_RESULTS``.
    The returned *effective_limit* is the user-visible page size; callers
    should overfetch by 1 (``limit=effective_limit + 1``) to detect ``has_more``.

    Validates ``no_limit``/``limit``/``offset`` types up front. Returns
    ``(0, 0, error_response)`` on malformed input so callers can short-circuit
    with a structured ``validation_error`` instead of letting ``TypeError``
    escape the MCP boundary (per filigree-772691017d).
    """
    no_limit = arguments.get("no_limit", False)
    if not isinstance(no_limit, bool):
        return 0, 0, _text(ErrorResponse(error="no_limit must be a boolean", code=ErrorCode.VALIDATION))

    requested_limit = arguments.get("limit", _MAX_LIST_RESULTS)
    limit_err = _validate_int_range(requested_limit, "limit", min_val=1, max_val=_MAX_SQLITE_OVERFETCH_LIMIT)
    if limit_err is not None:
        return 0, 0, limit_err

    offset = arguments.get("offset", 0)
    offset_err = _validate_int_range(offset, "offset", min_val=0, max_val=_MAX_SQLITE_OFFSET)
    if offset_err is not None:
        return 0, 0, offset_err

    if no_limit:  # noqa: SIM108 — expanded for readability per filigree-b1b414e36e
        effective_limit = requested_limit if "limit" in arguments else 10_000_000
    else:
        effective_limit = min(requested_limit, _MAX_LIST_RESULTS)

    return effective_limit, offset, None


def _apply_has_more(items: list[Any], effective_limit: int) -> tuple[list[Any], bool]:
    """Trim an overfetched result list and return ``(trimmed, has_more)``."""
    has_more = len(items) > effective_limit
    if has_more:
        items = items[:effective_limit]
    return items, has_more


def _list_response(items: list[Any], *, has_more: bool, next_offset: int | None = None) -> ListResponse[Any]:
    """Build a unified ``ListResponse[T]`` envelope for MCP list tools.

    Mirrors the loom HTTP ``list_response`` adapter:
    ``next_offset`` is present only when ``has_more`` is True. Defined here
    rather than reusing the loom adapter to keep the MCP surface free of
    generation-layer dependencies (per the operating principle "MCP reflects
    the living surface only", not "MCP imports loom").
    """
    body: ListResponse[Any] = {"items": items, "has_more": has_more}
    if has_more and next_offset is not None:
        body["next_offset"] = next_offset
    return body


def _validate_str(value: Any, name: str) -> list[TextContent] | None:
    """Return a validation error if *value* is not ``None`` and not a ``str``."""
    if value is not None and not isinstance(value, str):
        return _text(ErrorResponse(error=f"{name} must be a string", code=ErrorCode.VALIDATION))
    return None


def _validate_int_range(
    value: Any,
    name: str,
    min_val: int | None = None,
    max_val: int | None = None,
) -> list[TextContent] | None:
    """Return a validation error if *value* is not ``None`` and outside range.

    When *value* is ``None`` it is considered optional and passes.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return _text(ErrorResponse(error=f"{name} must be an integer", code=ErrorCode.VALIDATION))
    if min_val is not None and value < min_val:
        return _text(ErrorResponse(error=f"{name} must be >= {min_val}", code=ErrorCode.VALIDATION))
    if max_val is not None and value > max_val:
        return _text(ErrorResponse(error=f"{name} must be <= {max_val}", code=ErrorCode.VALIDATION))
    return None


def _validate_actor(value: Any) -> tuple[str, list[TextContent] | None]:
    """Sanitize actor, returning (cleaned, None) or ("", error_response)."""
    cleaned, err = sanitize_actor(value)
    if err:
        return ("", _text(ErrorResponse(error=err, code=ErrorCode.VALIDATION)))
    return (cleaned, None)


def _log_transition_enrichment_failure(issue_id: str, exc: Exception) -> None:
    if isinstance(exc, KeyError):
        logger.debug("Issue %s disappeared while enriching invalid-transition response", issue_id, exc_info=True)
        return
    logger.warning("Failed to enrich invalid-transition response for %s", issue_id, exc_info=True)


def _build_transition_error(
    tracker: FiligreeDB,
    issue_id: str,
    error: str,
    *,
    include_ready: bool = True,
    valid_transitions: list[TransitionHint] | None = None,
) -> TransitionError:
    """Build a structured error dict with valid-transition hints.

    Transition enrichment is best-effort: ``get_valid_transitions()`` re-reads
    the issue from SQLite, so a backend exception during error construction
    must not mask the caller's original invalid_transition payload (see
    filigree-55c5347992).
    """
    data: TransitionError = {"error": error, "code": ErrorCode.INVALID_TRANSITION}
    if valid_transitions is not None:
        data["valid_transitions"] = valid_transitions
        try:
            if not valid_transitions and tracker.get_issue(issue_id).status_category == "done":
                data["reopen_available"] = True
                data["hint"] = "Use reopen_issue to return this closed issue to the last non-done status before closure"
            else:
                data["hint"] = "Use get_valid_transitions to see allowed state changes"
        except Exception as exc:
            data["hint"] = "Use get_valid_transitions to see allowed state changes"
            _log_transition_enrichment_failure(issue_id, exc)
        return data
    try:
        transitions = tracker.get_valid_transitions(issue_id)
        if include_ready:
            data["valid_transitions"] = [{"to": t.to, "category": t.category, "ready": t.ready} for t in transitions]
        else:
            data["valid_transitions"] = [{"to": t.to, "category": t.category} for t in transitions]
        if not transitions and tracker.get_issue(issue_id).status_category == "done":
            data["reopen_available"] = True
            data["hint"] = "Use reopen_issue to return this closed issue to the last non-done status before closure"
        else:
            data["hint"] = "Use get_valid_transitions to see allowed state changes"
    except Exception as exc:
        # Enrichment is best-effort — must never mask the original error.
        _log_transition_enrichment_failure(issue_id, exc)
    return data
