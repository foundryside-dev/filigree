"""Tests for HTTP observation ingestion endpoints.

Covers:
- POST /api/v1/observations (classic alias)
- POST /api/weft/observations (loom generation)
- POST /api/observations (living surface alias)
- Input validation (missing summary, invalid priority, invalid line, non-project-relative paths)
- Idempotency (duplicate inserts return 200 and the existing mapped record)
- Replacement of expired duplicate observations (returns 201)
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app
from tests._db_factory import make_db


@pytest.fixture
def test_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """FiligreeDB for observation tests."""
    db = make_db(tmp_path, check_same_thread=False)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
async def client(test_db: FiligreeDB) -> AsyncClient:
    dash_module._db = test_db
    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        dash_module._db = None


class TestObservationIngest:
    async def test_loom_list_does_not_sweep_expired_observations(self, test_db: FiligreeDB, client: AsyncClient) -> None:
        obs = test_db.create_observation("expired HTTP scratchpad")
        test_db.conn.execute("UPDATE observations SET expires_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", obs["id"]))
        test_db.conn.commit()

        resp = await client.get("/api/weft/observations")

        assert resp.status_code == 200, resp.text
        assert resp.json()["items"] == []
        row = test_db.conn.execute("SELECT id FROM observations WHERE id = ?", (obs["id"],)).fetchone()
        assert row is not None
        audit_row = test_db.conn.execute("SELECT obs_id FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert audit_row is None

    async def test_classic_ingest_success(self, client: AsyncClient) -> None:
        payload = {
            "summary": "Classic test observation",
            "detail": "Some detailed notes here",
            "file_path": "src/module.py",
            "line": 42,
            "priority": 2,
            "actor": "reporter-1",
        }
        resp = await client.post("/api/v1/observations", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "observation_id" in data
        assert data["summary"] == "Classic test observation"
        assert data["detail"] == "Some detailed notes here"
        assert data["file_path"] == "src/module.py"
        assert data["line"] == 42
        assert data["priority"] == 2
        assert data["actor"] == "reporter-1"

    async def test_loom_ingest_success(self, client: AsyncClient) -> None:
        payload = {
            "summary": "Loom test observation",
            "detail": "More loom context",
            "file_path": "src/api.py",
            "line": 10,
            "priority": 3,
            "actor": "reporter-2",
        }
        resp = await client.post("/api/weft/observations", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "observation_id" in data
        assert data["summary"] == "Loom test observation"
        assert data["file_path"] == "src/api.py"
        assert data["line"] == 10
        assert data["priority"] == 3
        assert data["actor"] == "reporter-2"

    async def test_living_surface_ingest_success(self, client: AsyncClient) -> None:
        payload = {
            "summary": "Living surface test observation",
            "file_path": "src/main.py",
            "line": 1,
            "priority": 1,
        }
        resp = await client.post("/api/observations", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "observation_id" in data
        assert data["summary"] == "Living surface test observation"
        assert data["file_path"] == "src/main.py"
        assert data["line"] == 1
        assert data["priority"] == 1
        assert data["actor"] == "dashboard"  # default value

    async def test_validation_errors(self, client: AsyncClient) -> None:
        # 1. Missing summary
        resp = await client.post("/api/observations", json={"file_path": "src/main.py"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "summary" in resp.json()["error"]

        # 2. Empty summary
        resp = await client.post("/api/observations", json={"summary": "   ", "file_path": "src/main.py"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "empty" in resp.json()["error"].lower()

        # 3. Invalid priority
        resp = await client.post("/api/observations", json={"summary": "t", "priority": 10})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "priority" in resp.json()["error"].lower()

        # 4. Invalid line
        resp = await client.post("/api/observations", json={"summary": "t", "line": -5})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "line" in resp.json()["error"].lower()

        # 5. Non-project-relative file path
        resp = await client.post("/api/observations", json={"summary": "t", "file_path": "/absolute/path/file.py"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
        assert "project-relative" in resp.json()["error"].lower()

    async def test_idempotency_returns_200(self, client: AsyncClient) -> None:
        payload = {
            "summary": "Idempotent observation",
            "file_path": "src/common.py",
            "line": 100,
            "priority": 2,
            "actor": "agent-1",
        }
        # First call -> 201 Created
        resp1 = await client.post("/api/observations", json=payload)
        assert resp1.status_code == 201
        data1 = resp1.json()

        # Second call -> 200 OK with the exact same record
        resp2 = await client.post("/api/observations", json=payload)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data1["observation_id"] == data2["observation_id"]
        assert data1["summary"] == data2["summary"]

        # Third call with slightly different priority/actor -> still returns 200 with the original record (unchanged)
        payload_diff = payload.copy()
        payload_diff["priority"] = 4
        payload_diff["actor"] = "agent-2"
        resp3 = await client.post("/api/observations", json=payload_diff)
        assert resp3.status_code == 200
        data3 = resp3.json()
        assert data1["observation_id"] == data3["observation_id"]
        assert data3["priority"] == 2  # remains 2, not updated to 4
        assert data3["actor"] == "agent-1"

    async def test_expired_duplicate_replacement(self, test_db: FiligreeDB, client: AsyncClient) -> None:
        summary = "Expired observation"
        file_path = "src/expired.py"
        line = 5

        # 1. Insert an already expired duplicate directly into the database
        obs_id = test_db._generate_unique_id("observations", "obs")
        expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        created_at = (datetime.now(UTC) - timedelta(days=15)).isoformat()

        test_db.conn.execute(
            "INSERT INTO observations (id, summary, detail, file_id, file_path, line, "
            "source_issue_id, source_finding_id, priority, actor, created_at, expires_at) "
            "VALUES (?, ?, '', NULL, ?, ?, '', '', 2, 'reporter', ?, ?)",
            (obs_id, summary, file_path, line, created_at, expired_at),
        )
        test_db.conn.commit()

        # 2. Call the endpoint with the same dedup key
        payload = {"summary": summary, "file_path": file_path, "line": line, "priority": 3, "actor": "new-reporter"}
        resp = await client.post("/api/observations", json=payload)
        # Should return 201 Created (since the expired one was replaced)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["observation_id"] != obs_id  # new ID generated
        assert data["priority"] == 3
        assert data["actor"] == "new-reporter"

        # Verify that the old one was cleaned up/deleted
        old_row = test_db.conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
        assert old_row is None
