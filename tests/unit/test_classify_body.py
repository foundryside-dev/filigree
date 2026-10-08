"""Unit tests for the shared call-outcome classifier."""

from __future__ import annotations

import pytest

from filigree.logging import classify_body


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": None, "code": None, "status": "ok"}, ("ok", None)),  # healthy mcp_status_get shape
        ({"error": "nope", "code": "NOT_FOUND"}, ("error", "NOT_FOUND")),
        ({"error": "bad", "code": "VALIDATION"}, ("validation", "VALIDATION")),
        ({"error": "x"}, ("error", None)),
        ({"isError": True}, ("error", None)),
        ({"result": "no_op"}, ("no_op", None)),
        ({"undone": False}, ("no_op", None)),
        ({"status": "empty"}, ("no_op", None)),
        ({"undone": True}, ("ok", None)),
        ({"issues": []}, ("ok", None)),
        ([1, 2], ("ok", None)),
        ("text", ("ok", None)),
    ],
)
def test_classify_body(body: object, expected: tuple[str, str | None]) -> None:
    assert classify_body(body) == expected
