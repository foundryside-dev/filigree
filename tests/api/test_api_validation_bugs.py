"""Tests for dashboard API response & validation bug cluster.

Covers:
- search endpoint returns page count instead of total count
- scan ingest returns inverted status codes (202 for no-findings, 200 for findings)
- findings endpoint passes unvalidated severity/status through cast()
- add_dependency endpoint accepts empty depends_on
- MCP add_comment missing _refresh_summary call
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app
from tests._db_factory import make_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bug_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """FiligreeDB for bug cluster tests."""
    db = make_db(tmp_path, check_same_thread=False)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
async def client(bug_db: FiligreeDB) -> AsyncClient:
    dash_module._db = bug_db
    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        dash_module._db = None


# ---------------------------------------------------------------------------
# Bug 1: search total is page count, not actual total
# ---------------------------------------------------------------------------


class TestSearchTotalCount:
    async def test_search_total_reflects_full_count_not_page_size(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Create more issues than the limit, verify total > len(results)."""
        # Create 5 issues with "widget" in the title
        for i in range(5):
            bug_db.create_issue(f"Widget component {i}")

        # Search with limit=2 — total should be 5, not 2
        resp = await client.get("/api/search", params={"q": "widget", "limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2
        assert data["total"] >= 5, f"total should reflect full match count, got {data['total']}"


# ---------------------------------------------------------------------------
# Bug 2: scan ingest returns inverted status codes
# ---------------------------------------------------------------------------


class TestScanIngestStatusCode:
    async def test_scan_with_findings_returns_200(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Scan ingest with findings should return 200 (processed)."""
        payload = {
            "scan_source": "test-scanner",
            "findings": [
                {
                    "path": "src/foo.py",
                    "rule_id": "R001",
                    "message": "Test finding",
                    "severity": "warning",
                }
            ],
        }
        resp = await client.post("/api/v1/scan-results", json=payload)
        assert resp.status_code == 200, f"Findings present: expected 200, got {resp.status_code}"

    async def test_scan_without_findings_returns_200(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Scan ingest with no findings should also return 200 (synchronous operation completed)."""
        payload = {"scan_source": "test-scanner", "findings": []}
        resp = await client.post("/api/v1/scan-results", json=payload)
        # 202 is wrong here — the operation completed synchronously
        assert resp.status_code == 200, f"No findings: expected 200, got {resp.status_code}"

    async def test_scan_missing_findings_field_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Missing findings field must be rejected as malformed, not treated as clean scan (9a9caabc08)."""
        payload = {"scan_source": "test-scanner"}  # no findings key at all
        resp = await client.post("/api/v1/scan-results", json=payload)
        assert resp.status_code == 400, f"Missing findings should return 400, got {resp.status_code}"
        body = resp.json()
        assert body.get("code") == "VALIDATION"


# ---------------------------------------------------------------------------
# Bug 3: findings endpoint accepts invalid severity/status without validation
# ---------------------------------------------------------------------------


class TestFindingsEnumValidation:
    async def test_invalid_severity_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Passing an invalid severity value should return 400, not silently return empty."""
        # Register a file first
        file_rec = bug_db.register_file("src/test.py")
        file_id = file_rec.id

        resp = await client.get(f"/api/files/{file_id}/findings", params={"severity": "banana"})
        assert resp.status_code == 400, f"Invalid severity should be rejected, got {resp.status_code}"

    async def test_invalid_finding_status_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Passing an invalid finding status should return 400, not silently return empty."""
        file_rec = bug_db.register_file("src/test.py")
        file_id = file_rec.id

        resp = await client.get(f"/api/files/{file_id}/findings", params={"status": "banana"})
        assert resp.status_code == 400, f"Invalid status should be rejected, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Bug 4: add_dependency accepts empty depends_on
# ---------------------------------------------------------------------------


class TestAddDependencyValidation:
    async def test_empty_depends_on_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Empty depends_on should return a clear 400 error, not a confusing KeyError."""
        issue = bug_db.create_issue("Test issue")
        resp = await client.post(
            f"/api/issue/{issue.id}/dependencies",
            json={"depends_on": ""},
        )
        assert resp.status_code == 400, f"Empty depends_on should be 400, got {resp.status_code}"

    async def test_missing_depends_on_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Missing depends_on key should return 400."""
        issue = bug_db.create_issue("Test issue")
        resp = await client.post(
            f"/api/issue/{issue.id}/dependencies",
            json={},
        )
        assert resp.status_code == 400, f"Missing depends_on should be 400, got {resp.status_code}"


# ---------------------------------------------------------------------------
# filigree-0ad97ea6e0: search routes leak SQLite OverflowError on huge offsets
# ---------------------------------------------------------------------------


class TestSearchOversizedOffset:
    """Both /api/search and /api/loom/search must reject offsets above
    SQLite's signed-int64 OFFSET bind limit with 400 VALIDATION, not 500."""

    async def test_classic_search_huge_offset_returns_400(self, client: AsyncClient) -> None:
        # 2**63 — one past SQLite's signed-int64 max
        resp = await client.get("/api/search", params={"q": "x", "offset": "9223372036854775808"})
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_loom_search_huge_offset_returns_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/loom/search", params={"q": "x", "offset": "9223372036854775808"})
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_classic_search_extreme_offset_still_clamps_negative(self, client: AsyncClient) -> None:
        """Lower-bound clamping is preserved (pinned by test_search_negative_offset_clamped)."""
        resp = await client.get("/api/search", params={"q": "x", "offset": -10})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# filigree-223d6a28ce: remove_dependency leaks WrongProjectError as 500
# ---------------------------------------------------------------------------


class TestRemoveDependencyForeignPrefix:
    """Both classic and loom remove-dependency routes must convert a
    foreign-prefix WrongProjectError into 400 VALIDATION, not 500."""

    async def test_classic_remove_dependency_foreign_prefix_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        # Use a fully-formed foreign-prefix ID. The DB's prefix is set by
        # the bug_db fixture (make_db default) — anything not starting with
        # that prefix will trip _check_id_prefix.
        foreign_a = "foreignproj-aaaaaaaa00"
        foreign_b = "foreignproj-bbbbbbbb00"
        resp = await client.request(
            "DELETE",
            f"/api/issue/{foreign_a}/dependencies/{foreign_b}",
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "project" in body["error"].lower()

    async def test_loom_remove_dependency_foreign_prefix_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        foreign_a = "foreignproj-aaaaaaaa00"
        foreign_b = "foreignproj-bbbbbbbb00"
        resp = await client.request(
            "DELETE",
            f"/api/loom/issues/{foreign_a}/dependencies/{foreign_b}",
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_classic_remove_dependency_same_prefix_missing_source_returns_404(
        self, bug_db: FiligreeDB, client: AsyncClient
    ) -> None:
        target = bug_db.create_issue("Target")
        resp = await client.request(
            "DELETE",
            f"/api/issue/{bug_db.prefix}-0000000000/dependencies/{target.id}",
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "NOT_FOUND", body

    async def test_loom_remove_dependency_same_prefix_missing_source_returns_200_idempotent(
        self, bug_db: FiligreeDB, client: AsyncClient
    ) -> None:
        """Loom DELETE is idempotent at the wire layer per
        tests/fixtures/contracts/loom/issues-dep-remove.json: a missing
        issue between two valid-prefix IDs returns 200 ``{"removed": false}``
        so a retried DELETE after a network glitch stays safe. Classic
        keeps the 404 behaviour above — no classic fixture pins that
        contract, and CLI/MCP surfaces still surface NOT_FOUND.
        """
        target = bug_db.create_issue("Target")
        resp = await client.request(
            "DELETE",
            f"/api/loom/issues/{bug_db.prefix}-0000000000/dependencies/{target.id}",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"removed": False}


# ---------------------------------------------------------------------------
# filigree-48e937cd3e: close routes accept non-string reason
# ---------------------------------------------------------------------------


class TestCloseReasonTypeValidation:
    """All close paths must reject non-string reason with 400 VALIDATION."""

    async def test_classic_close_reason_list_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Close reason list")
        resp = await client.post(
            f"/api/issue/{issue.id}/close",
            json={"reason": ["not", "a", "string"]},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "reason" in body["error"].lower()

    async def test_classic_close_reason_int_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Close reason int")
        resp = await client.post(
            f"/api/issue/{issue.id}/close",
            json={"reason": 42},
        )
        assert resp.status_code == 400, resp.text

    async def test_loom_close_reason_dict_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Close reason dict")
        resp = await client.post(
            f"/api/loom/issues/{issue.id}/close",
            json={"reason": {"nested": "object"}},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION"

    async def test_classic_batch_close_reason_list_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Batch close reason list")
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [issue.id], "reason": [1, 2, 3]},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION"

    async def test_classic_close_string_reason_still_works(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Sanity: a plain string reason still closes the issue."""
        issue = bug_db.create_issue("Happy reason")
        resp = await client.post(
            f"/api/issue/{issue.id}/close",
            json={"reason": "duplicate of #X"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["fields"]["close_reason"] == "duplicate of #X"


# ---------------------------------------------------------------------------
# Bug: /api/loom/changes since cursor not UTC-canonicalized (filigree-d808d8b70f)
# ---------------------------------------------------------------------------


class TestLoomChangesSinceUTCNormalization:
    """``/api/loom/changes`` must canonicalize ``since`` to UTC before
    SQLite text-compares it against stored ``created_at`` (which is stored as
    ``+00:00`` ISO). Otherwise an offset-bearing ``since`` representing the
    same instant returns different events than a UTC ``since``.
    """

    async def test_offset_bearing_since_matches_utc_equivalent(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        # Seed an event with a controlled created_at so we can probe the
        # boundary precisely.  Stored timestamps are ``+00:00`` ISO strings.
        issue = bug_db.create_issue("seed for changes")
        stored_at = "2026-01-15T15:30:00+00:00"
        bug_db.conn.execute(
            "UPDATE events SET created_at = ? WHERE issue_id = ?",
            (stored_at, issue.id),
        )
        bug_db.conn.commit()

        # ``since`` instant: 2026-01-15T15:31:00 UTC — strictly **after** the
        # stored event, so the event must NOT be returned regardless of how
        # the offset is expressed.
        utc_since = "2026-01-15T15:31:00+00:00"
        # Same instant, expressed with a -05:00 offset.
        offset_since = "2026-01-15T10:31:00-05:00"

        utc_resp = await client.get("/api/loom/changes", params={"since": utc_since})
        offset_resp = await client.get("/api/loom/changes", params={"since": offset_since})

        assert utc_resp.status_code == 200, utc_resp.text
        assert offset_resp.status_code == 200, offset_resp.text

        utc_items = utc_resp.json()["items"]
        offset_items = offset_resp.json()["items"]

        # Sanity: UTC form correctly excludes the stored event.
        assert utc_items == [], f"UTC since should exclude older event, got {utc_items}"

        # Bug fix: offset form must also exclude it (text-compare with
        # ``-05:00`` would otherwise incorrectly flag the stored ``+00:00``
        # event as newer than the cursor).
        assert offset_items == utc_items, f"Offset since must agree with UTC equivalent; utc={utc_items!r}, offset={offset_items!r}"


# ---------------------------------------------------------------------------
# Bug: /api/loom/changes accepts ?offset= but discards it (filigree-f0f47f5b9d)
# ---------------------------------------------------------------------------


class TestLoomChangesOffsetRejected:
    """The contract (``tests/fixtures/contracts/loom/changes.json``) declares
    the cursor is ``since``; ``offset`` is not exposed. The handler must
    therefore reject ``?offset=`` instead of silently discarding it.
    """

    async def test_offset_query_param_returns_400(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/loom/changes",
            params={"since": "2000-01-01T00:00:00+00:00", "offset": "10"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "offset" in body["error"].lower()

    async def test_offset_zero_also_rejected(self, client: AsyncClient) -> None:
        # offset is not part of this endpoint's surface — even an explicit
        # ``offset=0`` should be rejected, not silently accepted.
        resp = await client.get(
            "/api/loom/changes",
            params={"since": "2000-01-01T00:00:00+00:00", "offset": "0"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION"


class TestLoomChangesCompoundCursor:
    async def test_has_more_uses_since_cursor_not_next_offset(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        bug_db.create_issue("API cursor page A")
        bug_db.create_issue("API cursor page B")

        resp = await client.get(
            "/api/loom/changes",
            params={"since": "2000-01-01T00:00:00+00:00", "limit": "1"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["has_more"] is True
        assert "next_since" in body
        assert "next_event_id" in body
        assert "next_offset" not in body

    async def test_after_event_id_resumes_inside_same_timestamp_group(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("API same timestamp cursor")
        tied_timestamp = "2026-01-01T00:00:00+00:00"
        cursor = bug_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "title_changed", "import", tied_timestamp),
        ).lastrowid
        expected = bug_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "status_changed", "import", tied_timestamp),
        ).lastrowid
        bug_db.conn.commit()

        resp = await client.get(
            "/api/loom/changes",
            params={"since": tied_timestamp, "after_event_id": str(cursor), "limit": "1"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [item["event_id"] for item in body["items"]] == [expected]
        assert body["next_since"] == tied_timestamp
        assert body["next_event_id"] == expected


class TestLoomChangesHeartbeatFiltering:
    async def test_heartbeat_events_are_excluded_by_default(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("API heartbeat filtering")
        bug_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "heartbeat", "agent", "2026-01-01T00:00:00+00:00"),
        )
        expected = bug_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "status_changed", "agent", "2026-01-01T00:00:01+00:00"),
        ).lastrowid
        bug_db.conn.commit()

        resp = await client.get(
            "/api/loom/changes",
            params={"since": "2000-01-01T00:00:00+00:00"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        event_types = [item["event_type"] for item in body["items"]]
        assert "heartbeat" not in event_types
        assert expected in [item["event_id"] for item in body["items"]]


# ---------------------------------------------------------------------------
# filigree-8a117524ca: search routes leak FastAPI 422 envelope on malformed
# limit/offset (must produce flat {error, code} envelope at 400).
# ---------------------------------------------------------------------------


class TestSearchMalformedPaginationFlatEnvelope:
    """Both /api/search and /api/loom/search use FastAPI int coercion for
    limit/offset, which produces FastAPI's 422 ``{detail: [...]}`` envelope on
    malformed values — bypassing the flat ``{error, code}`` envelope contract
    pinned by tests/test_error_envelope_contract.py. Handlers must parse the
    query string themselves and return 400 VALIDATION.
    """

    async def test_classic_search_malformed_limit_returns_flat_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": "x", "limit": "foo"})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("code") == "VALIDATION", body
        assert "error" in body, body
        assert "detail" not in body, body  # FastAPI default envelope absent

    async def test_classic_search_malformed_offset_returns_flat_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": "x", "offset": "bar"})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("code") == "VALIDATION", body
        assert "detail" not in body, body

    async def test_loom_search_malformed_limit_returns_flat_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/loom/search", params={"q": "x", "limit": "foo"})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("code") == "VALIDATION", body
        assert "detail" not in body, body

    async def test_loom_search_malformed_offset_returns_flat_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/loom/search", params={"q": "x", "offset": "bar"})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("code") == "VALIDATION", body
        assert "detail" not in body, body


# ---------------------------------------------------------------------------
# filigree-d39c7bdc1e: PATCH issue handlers don't type-validate ``status``.
# Explicit JSON null collapses to "missing"; non-string is misclassified as
# INVALID_TRANSITION instead of VALIDATION.
# ---------------------------------------------------------------------------


class TestPatchStatusTypeValidation:
    async def test_classic_patch_status_null_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Status null")
        resp = await client.patch(f"/api/issue/{issue.id}", json={"status": None})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "status" in body["error"].lower()

    async def test_classic_patch_status_int_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Status int")
        resp = await client.patch(f"/api/issue/{issue.id}", json={"status": 42})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "status" in body["error"].lower()

    async def test_classic_patch_status_missing_is_no_change(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        """Sanity: omitting status keeps existing semantics (title-only update succeeds)."""
        issue = bug_db.create_issue("Status missing", priority=2)
        resp = await client.patch(f"/api/issue/{issue.id}", json={"title": "renamed"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "renamed"

    async def test_loom_patch_status_null_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Loom status null")
        resp = await client.patch(f"/api/loom/issues/{issue.id}", json={"status": None})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_loom_patch_status_int_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Loom status int")
        resp = await client.patch(f"/api/loom/issues/{issue.id}", json={"status": 42})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_classic_batch_update_status_int_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Batch status int")
        resp = await client.post(
            "/api/batch/update",
            json={"issue_ids": [issue.id], "status": 42},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_loom_batch_update_status_null_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Loom batch status null")
        resp = await client.post(
            "/api/loom/batch/update",
            json={"issue_ids": [issue.id], "status": None},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body


class TestBatchUpdateBodyValidation:
    async def test_classic_batch_update_fields_must_be_object_even_with_empty_ids(self, client: AsyncClient) -> None:
        resp = await client.post("/api/batch/update", json={"issue_ids": [], "fields": []})

        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "fields" in body["error"]

    async def test_loom_batch_update_assignee_must_be_string_even_with_empty_ids(self, client: AsyncClient) -> None:
        resp = await client.post("/api/loom/batch/update", json={"issue_ids": [], "assignee": 42})

        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "assignee" in body["error"]


class TestPatchParentIdNullValidation:
    async def test_classic_patch_parent_id_null_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        parent = bug_db.create_issue("Classic parent")
        child = bug_db.create_issue("Classic child", parent_id=parent.id)

        resp = await client.patch(f"/api/issue/{child.id}", json={"parent_id": None})

        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "parent_id" in body["error"]
        assert bug_db.get_issue(child.id).parent_id == parent.id

    async def test_loom_patch_parent_id_null_returns_400(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        parent = bug_db.create_issue("Loom parent")
        child = bug_db.create_issue("Loom child", parent_id=parent.id)

        resp = await client.patch(f"/api/loom/issues/{child.id}", json={"parent_id": None})

        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "parent_id" in body["error"]
        assert bug_db.get_issue(child.id).parent_id == parent.id

    async def test_classic_patch_parent_id_empty_string_still_clears(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        parent = bug_db.create_issue("Clear parent")
        child = bug_db.create_issue("Clear child", parent_id=parent.id)

        resp = await client.patch(f"/api/issue/{child.id}", json={"parent_id": ""})

        assert resp.status_code == 200, resp.text
        assert resp.json()["parent_id"] is None
        assert bug_db.get_issue(child.id).parent_id is None


class TestLoomIssueDetailIncludeFilesShape:
    async def test_include_files_uses_loom_file_assoc_shape(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Loom include files")
        file_record = bug_db.register_file("src/loom_file_shape.py")
        bug_db.add_file_association(file_record.id, issue.id, "bug_in")

        resp = await client.get(f"/api/loom/issues/{issue.id}", params={"include_files": "true"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["files"]) == 1
        assoc = body["files"][0]
        assert "assoc_id" in assoc
        assert "id" not in assoc
        assert assoc["file_id"] == file_record.id
        assert assoc["issue_id"] == issue.id


class TestLoomIssueEventsPagination:
    async def test_issue_events_offset_advances_page(self, bug_db: FiligreeDB, client: AsyncClient) -> None:
        issue = bug_db.create_issue("Loom events page")
        bug_db.conn.execute(
            "UPDATE events SET created_at = ? WHERE issue_id = ?",
            ("2025-01-01T00:00:00+00:00", issue.id),
        )
        first = bug_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "title_changed", "agent", "2026-01-01T00:00:00+00:00"),
        ).lastrowid
        second = bug_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "status_changed", "agent", "2026-01-01T00:00:01+00:00"),
        ).lastrowid
        bug_db.conn.commit()

        page1 = await client.get(f"/api/loom/issues/{issue.id}/events", params={"limit": "1"})
        page2 = await client.get(f"/api/loom/issues/{issue.id}/events", params={"limit": "1", "offset": "1"})

        assert page1.status_code == 200, page1.text
        assert page2.status_code == 200, page2.text
        assert [item["event_id"] for item in page1.json()["items"]] == [second]
        assert [item["event_id"] for item in page2.json()["items"]] == [first]
        assert page1.json()["next_offset"] == 1


# ---------------------------------------------------------------------------
# filigree-25b44e65e2: write routes map WrongProjectError to CONFLICT/NOT_FOUND
# instead of VALIDATION on claim/release/add-dependency/add-comment.
# ---------------------------------------------------------------------------


class TestWriteRoutesWrongProjectError:
    """All write routes must surface WrongProjectError as 400 VALIDATION
    (precedent: remove_dependency at dashboard_routes/issues.py:680-681).
    For comment routes, the read-style precheck (db.get_issue) ignores
    prefix and would otherwise mask the error as 404 NOT_FOUND.
    """

    _FOREIGN_A = "foreignproj-aaaaaaaa00"
    _FOREIGN_B = "foreignproj-bbbbbbbb00"

    async def test_classic_claim_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/issue/{self._FOREIGN_A}/claim",
            json={"assignee": "alice"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "project" in body["error"].lower()

    async def test_loom_claim_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/loom/issues/{self._FOREIGN_A}/claim",
            json={"assignee": "alice"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_classic_release_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(f"/api/issue/{self._FOREIGN_A}/release", json={})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_loom_release_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(f"/api/loom/issues/{self._FOREIGN_A}/release", json={})
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_classic_add_dependency_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/issue/{self._FOREIGN_A}/dependencies",
            json={"depends_on": self._FOREIGN_B},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_loom_add_dependency_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/loom/issues/{self._FOREIGN_A}/dependencies",
            json={"depends_on": self._FOREIGN_B},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_classic_add_comment_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/issue/{self._FOREIGN_A}/comments",
            json={"text": "hi"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_loom_add_comment_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/loom/issues/{self._FOREIGN_A}/comments",
            json={"text": "hi"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body

    async def test_same_prefix_missing_claim_still_404(self, client: AsyncClient) -> None:
        """Sanity: a same-project missing ID should still return NOT_FOUND."""
        resp = await client.post(
            "/api/issue/test-0000000000/claim",
            json={"assignee": "alice"},
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "NOT_FOUND", body

    async def test_http_400_for_foreign_id_does_not_leak_prefix(self, client: AsyncClient) -> None:
        """2.1.0 §1.2: untrusted HTTP responses must use ``safe_message``.

        Probing for "is project X open?" by trying ``X-something/claim``
        and pattern-matching the error body must not work — the
        envelope's ``error`` field is a generic string, neither the open
        DB's prefix (``test``) nor the offending id's prefix
        (``foreignproj``) appears.

        Renamed from the design's ``test_http_404_for_foreign_id_…`` because
        write routes return 400/VALIDATION (read-route 404 enforcement
        lands in §1.3 with its own pinning test).
        """
        resp = await client.post(
            f"/api/issue/{self._FOREIGN_A}/claim",
            json={"assignee": "alice"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        # safe_message contains generic wording, not project prefixes.
        assert "foreignproj" not in body["error"], body
        assert "test" not in body["error"].lower() or "this project" in body["error"].lower()
        # Concretely: the canonical safe wording must be present.
        from filigree.core import WrongProjectError

        assert body["error"] == WrongProjectError.SAFE_MESSAGE

    @pytest.mark.parametrize(
        ("method", "path", "json_body", "foreign_fragments"),
        [
            ("post", "/api/issue/foreignproj-aaaaaaaa00/reopen", {}, ("foreignproj", "test")),
            ("post", "/api/loom/issues/foreignproj-aaaaaaaa00/reopen", {}, ("foreignproj", "test")),
            (
                "post",
                "/api/issues",
                {"title": "bad parent", "parent_id": "foreignproj-aaaaaaaa00"},
                ("foreignproj", "test"),
            ),
            (
                "post",
                "/api/loom/issues",
                {"title": "bad parent", "parent_id": "foreignproj-aaaaaaaa00"},
                ("foreignproj", "test"),
            ),
        ],
    )
    async def test_create_and_reopen_wrong_project_errors_use_safe_message(
        self,
        client: AsyncClient,
        method: str,
        path: str,
        json_body: dict[str, str],
        foreign_fragments: tuple[str, ...],
    ) -> None:
        from filigree.core import WrongProjectError

        resp = await getattr(client, method)(path, json=json_body)

        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert body["error"] == WrongProjectError.SAFE_MESSAGE
        for fragment in foreign_fragments:
            assert fragment not in body["error"], body


class TestReleaseClaimErrorRouting:
    async def test_residual_value_error_is_validation_for_classic_and_loom_release(
        self,
        bug_db: FiligreeDB,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        issue = bug_db.create_issue("Release validation route", priority=2)

        def raise_validation(*_args: object, **_kwargs: object) -> object:
            raise ValueError("synthetic validation failure")

        monkeypatch.setattr(bug_db, "release_claim", raise_validation)

        for path in (f"/api/issue/{issue.id}/release", f"/api/loom/issues/{issue.id}/release"):
            resp = await client.post(path, json={"actor": "agent"})
            body = resp.json()
            assert resp.status_code == 400
            assert body["code"] == "VALIDATION"
            assert body["error"] == "synthetic validation failure"
