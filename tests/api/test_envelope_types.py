"""Structural tests for the 2.0 envelope TypedDicts."""

from __future__ import annotations

from typing import get_args, get_type_hints

from filigree.types.api import BatchFailure, BatchResponse, ListResponse, SlimIssue


def test_batch_response_fields() -> None:
    hints = get_type_hints(BatchResponse)
    assert "succeeded" in hints
    assert "failed" in hints
    assert "newly_unblocked" in hints  # NotRequired, still listed in hints


def test_batch_failure_fields() -> None:
    hints = get_type_hints(BatchFailure)
    # ``valid_transitions`` is NotRequired but get_type_hints still lists it.
    # Field name ``id`` was reverted from the Stage 1 ``item_id`` during
    # Stage 2B task 2b.0 to match the retired BatchFailureDetail's wire
    # shape and avoid a gratuitous wire-contract break.
    assert set(hints.keys()) == {"id", "error", "code", "valid_transitions"}


def test_list_response_fields() -> None:
    hints = get_type_hints(ListResponse)
    assert "items" in hints
    assert "has_more" in hints
    assert "next_offset" in hints  # NotRequired, still listed


def test_batch_response_is_generic() -> None:
    # A parameterized form should be usable
    inst: BatchResponse[SlimIssue] = {"succeeded": [], "failed": []}
    assert inst["succeeded"] == []


def test_error_code_enum_members() -> None:
    from filigree.types.api import ErrorCode

    # Exact 15-member set. SCHEMA_MISMATCH and INTERNAL were added so the
    # typed SchemaVersionMismatchError and the catch-all except-Exception
    # paths have dedicated codes rather than aliasing onto IO/VALIDATION.
    # FILE_REGISTRY_DISPLACED is the ADR-014 direct-registration
    # conflict code for projects whose file registry is owned by Clarion.
    # CLARION_REGISTRY_VERSION_MISMATCH is ADR-014 §4: emitted when
    # Clarion advertises an api_version this Filigree was not built for.
    # BRIEFING_BLOCKED is the Clarion 1.0 cross-product code (CONTRACT-3):
    # Clarion returns 403 + ``{"code": "BRIEFING_BLOCKED"}`` for files it
    # intentionally withholds; surfaces as HTTP 403 to the dashboard caller
    # and MUST NOT engage the local-registry fallback.
    expected = {
        "VALIDATION",
        "NOT_FOUND",
        "CONFLICT",
        "INVALID_TRANSITION",
        "PERMISSION",
        "NOT_INITIALIZED",
        "IO",
        "INVALID_API_URL",
        "FILE_REGISTRY_DISPLACED",
        "REGISTRY_UNAVAILABLE",
        "CLARION_REGISTRY_VERSION_MISMATCH",
        "BRIEFING_BLOCKED",
        "STOP_FAILED",
        "SCHEMA_MISMATCH",
        "INTERNAL",
    }
    assert {e.name for e in ErrorCode} == expected


def test_error_code_is_str_subclass() -> None:
    from filigree.types.api import ErrorCode

    assert ErrorCode.VALIDATION == "VALIDATION"
    assert isinstance(ErrorCode.VALIDATION, str)


def test_legacy_displaced_registry_code_maps_to_current_name() -> None:
    from filigree.types.api import LEGACY_CODE_TO_ERRORCODE, ErrorCode

    assert LEGACY_CODE_TO_ERRORCODE["FILIGREE_FILE_REGISTRY_DISPLACED"] is ErrorCode.FILE_REGISTRY_DISPLACED


def test_event_type_reversibility_classifier_is_total() -> None:
    from filigree.types.events import REVERSIBLE_EVENT_TYPES, EventType, is_reversible_event_type

    classified = {event_type: is_reversible_event_type(event_type) for event_type in get_args(EventType)}
    assert {event_type for event_type, reversible in classified.items() if reversible} == set(REVERSIBLE_EVENT_TYPES)
    assert classified["created"] is False
    assert classified["status_changed"] is True


def test_error_response_flat_shape() -> None:
    from filigree.types.api import ErrorCode, ErrorResponse

    # Without details
    err1: ErrorResponse = {"error": "nope", "code": ErrorCode.VALIDATION}
    assert err1["code"] == ErrorCode.VALIDATION

    # With details (optional field)
    err2: ErrorResponse = {
        "error": "conflict",
        "code": ErrorCode.CONFLICT,
        "details": {"issue_id": "abc", "current_assignee": "alice"},
    }
    assert err2["details"]["issue_id"] == "abc"


def test_error_response_has_no_legacy_fields() -> None:
    from typing import get_type_hints

    from filigree.types.api import ErrorResponse

    hints = get_type_hints(ErrorResponse)
    assert set(hints.keys()) == {"error", "code", "details"}


def test_schema_version_mismatch_error_shape() -> None:
    from filigree.types.api import SchemaVersionMismatchError

    exc = SchemaVersionMismatchError(installed=8, database=9)
    assert exc.installed == 8
    assert exc.database == 9
    assert "v8" in str(exc)
    assert "v9" in str(exc)


def test_transition_errors_exist() -> None:
    from filigree.types.api import (
        AmbiguousTransitionError,
        InvalidTransitionError,
    )

    exc1 = AmbiguousTransitionError("X", ["fixing", "reviewing"])
    assert "fixing" in str(exc1)
    assert isinstance(exc1, ValueError)

    exc2 = InvalidTransitionError("X", "confirmed", to_state="triage", backward=True)
    assert "confirmed" in str(exc2)
    enriched = exc2.with_valid_transitions([{"to": "open", "category": "open", "ready": True}])
    assert enriched is not exc2
    assert str(enriched) == str(exc2)
    assert enriched.backward is True
    assert enriched.valid_transitions == [{"to": "open", "category": "open", "ready": True}]


def test_invalid_transition_details_omits_uncomputed_transitions() -> None:
    from filigree.types.api import InvalidTransitionError, invalid_transition_details

    exc = InvalidTransitionError("X", "confirmed", to_state="closed")
    assert invalid_transition_details(exc) is None

    enriched = exc.with_valid_transitions([])
    assert invalid_transition_details(enriched) == {"valid_transitions": []}


def test_scan_ingest_response_loom_concrete_shape() -> None:
    """Regression: subclassing ``BatchResponse[str]`` did two bad things at
    runtime — it (1) left ``succeeded`` as ``list[~_T]`` instead of
    ``list[str]`` because TypedDict + ``Generic`` substitution is not
    preserved through subclassing, and (2) promoted ``newly_unblocked``
    from ``NotRequired`` to *required* in the subclass, exposing an
    impossible contract for scan ingestion (which never unblocks issues).

    Pin the concrete shape: exactly four required keys, ``succeeded``
    typed as ``list[str]``, no ``newly_unblocked``.
    """
    from filigree.generations.loom.types import ScanIngestResponseLoom, ScanStats

    hints = get_type_hints(ScanIngestResponseLoom)
    assert set(hints.keys()) == {"succeeded", "failed", "stats", "warnings"}
    assert hints["succeeded"] == list[str]
    assert hints["failed"] == list[BatchFailure]
    assert hints["stats"] is ScanStats
    assert hints["warnings"] == list[str]
    # newly_unblocked must NOT appear — scan ingest cannot unblock issues.
    assert "newly_unblocked" not in hints
    assert ScanIngestResponseLoom.__required_keys__ == {
        "succeeded",
        "failed",
        "stats",
        "warnings",
    }
    assert ScanIngestResponseLoom.__optional_keys__ == frozenset()


def test_batch_close_response_loom_succeeded_supports_full() -> None:
    """Regression: ``BatchCloseResponseLoom.succeeded`` was pinned to
    ``list[SlimIssueLoom]`` even though the C5 contract (and the handler
    in ``dashboard_routes/issues.py``) returns full ``IssueLoom`` items
    when ``response_detail=full``. ``newly_unblocked[]`` stays slim per
    the locked C5 rule (``docs/federation/contracts.md`` §C5).
    """
    from typing import get_args, get_origin

    from filigree.generations.loom.types import (
        BatchCloseResponseLoom,
        IssueLoom,
        SlimIssueLoom,
    )

    hints = get_type_hints(BatchCloseResponseLoom)
    assert set(hints.keys()) == {"succeeded", "failed", "newly_unblocked"}

    # succeeded is list[SlimIssueLoom | IssueLoom] — both projections legal.
    # PEP 604 unions surface as types.UnionType, while typing.Union[...] is
    # typing.Union; comparing get_args(elem) is robust to both.
    succeeded_t = hints["succeeded"]
    assert get_origin(succeeded_t) is list
    (elem_t,) = get_args(succeeded_t)
    assert set(get_args(elem_t)) == {SlimIssueLoom, IssueLoom}

    # newly_unblocked stays slim-only by C5 contract.
    # (Optionality is asserted at the wire-shape level by the parity test
    # in tests/util/test_generation_parity.py — `from __future__ import
    # annotations` strips PEP 655 ``NotRequired`` markers from
    # ``__optional_keys__`` so we cannot check optionality here.)
    assert hints["newly_unblocked"] == list[SlimIssueLoom]


def test_errorcode_to_http_status_is_exhaustive() -> None:
    """Every ErrorCode member maps to a sensible HTTP status.

    The function uses ``match`` + ``assert_never`` so adding a new member
    without extending the match fails mypy. This test complements that
    by verifying the runtime output shape at test-time (in case
    ``assert_never`` is disabled in production).
    """
    from filigree.types.api import ErrorCode, errorcode_to_http_status

    for code in ErrorCode:
        status = errorcode_to_http_status(code)
        assert isinstance(status, int)
        assert 400 <= status < 600, f"{code} maps to non-error status {status}"

    # Spot-check a few specific mappings that back-compat depends on.
    assert errorcode_to_http_status(ErrorCode.NOT_FOUND) == 404
    assert errorcode_to_http_status(ErrorCode.CONFLICT) == 409
    assert errorcode_to_http_status(ErrorCode.INVALID_TRANSITION) == 409
    assert errorcode_to_http_status(ErrorCode.VALIDATION) == 400
    assert errorcode_to_http_status(ErrorCode.PERMISSION) == 403
    assert errorcode_to_http_status(ErrorCode.NOT_INITIALIZED) == 503
    assert errorcode_to_http_status(ErrorCode.SCHEMA_MISMATCH) == 503
    assert errorcode_to_http_status(ErrorCode.REGISTRY_UNAVAILABLE) == 503
    assert errorcode_to_http_status(ErrorCode.INTERNAL) == 500
    assert errorcode_to_http_status(ErrorCode.IO) == 500
