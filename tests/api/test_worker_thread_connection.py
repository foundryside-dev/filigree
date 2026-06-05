"""Worker-thread DB connection isolation (CONTRACT-E).

The dashboard runs scan-results ingest and the clean-stale sweep on an asyncio
worker thread via ``asyncio.to_thread``. Every other DB handler runs synchronous
DB work inline on the event-loop thread. Both groups historically shared one
``sqlite3.Connection`` (opened ``check_same_thread=False``), so a worker-thread
write could interleave on the connection's single implicit transaction with an
event-loop write — silently committing partial work or discarding completed
work.

The fix: the worker paths borrow a PRIVATE connection via
``FiligreeDB.borrow_for_worker_thread`` so they never share a connection
cross-thread. Writer/writer contention is then mediated by SQLite's own file
locking (WAL + ``busy_timeout``), with no application-level lock. These tests
pin that mechanism and the connection-scoped invariant.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB, _LoomweaveLocalFallbackRegistry
from filigree.dashboard import create_app
from filigree.dashboard_routes.files import (
    _clean_stale_findings_on_private_conn,
    _ingest_scan_results_on_private_conn,
)
from tests._fakes.clarion_http import clarion_stub


@pytest.fixture
def api_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """Fresh DB with check_same_thread=False (the dashboard's open mode)."""
    d = FiligreeDB(tmp_path / "filigree.db", prefix="test", check_same_thread=False)
    d.initialize()
    yield d
    d.close()


@pytest.fixture
async def client(api_db: FiligreeDB) -> AsyncGenerator[AsyncClient, None]:
    dash_module._db = api_db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    dash_module._db = None


class TestBorrowForWorkerThread:
    """The borrow_for_worker_thread context manager itself."""

    async def test_yields_distinct_connection_through_to_thread(self, api_db: FiligreeDB) -> None:
        """Driven through asyncio.to_thread: the clone opens, uses, commits, and
        closes its OWN connection entirely on the worker thread, and the shared
        event-loop connection sees the committed result afterwards.
        """
        shared_conn = api_db.conn  # open the shared connection on the event-loop thread
        captured: dict[str, Any] = {}

        def worker() -> None:
            with api_db.borrow_for_worker_thread() as clone:
                captured["distinct"] = clone.conn is not shared_conn
                # A real write on the private connection (commits internally).
                clone.process_scan_results(
                    scan_source="ruff",
                    findings=[{"path": "a.py", "rule_id": "E501", "message": "long"}],
                )
            # The CM closed the private connection on exit (this thread).
            captured["closed_after_exit"] = clone._conn is None

        await asyncio.to_thread(worker)

        assert captured["distinct"] is True
        assert captured["closed_after_exit"] is True
        # The shared connection (event-loop thread) sees the committed write.
        assert api_db.get_file_by_path("a.py") is not None

    def test_clone_does_not_own_or_close_shared_registry(self, api_db: FiligreeDB) -> None:
        """Tearing down a borrowed clone must never close the shared Loomweave
        client — the clone borrows the registry by reference.
        """

        class _RegistrySpy:
            def __init__(self) -> None:
                self.closed = 0

            def close(self) -> None:
                self.closed += 1

        spy = _RegistrySpy()
        api_db.registry = spy  # type: ignore[assignment]

        assert api_db._owns_registry is True
        with api_db.borrow_for_worker_thread() as clone:
            assert clone.registry is api_db.registry  # shared by reference
            assert clone._owns_registry is False
        assert spy.closed == 0, "clone teardown must not close the shared registry"

        # Even an explicit close()/__del__ on a non-owning clone is a no-op for
        # the registry (guards the __del__ -> close -> _close_registry path).
        clone.close()
        assert spy.closed == 0

    def test_clone_close_leaves_parent_connection_usable(self, api_db: FiligreeDB) -> None:
        """The private connection is independent: closing the clone does not
        touch the parent's connection.
        """
        parent_conn = api_db.conn
        with api_db.borrow_for_worker_thread() as clone:
            assert clone.conn is not parent_conn
        # Parent connection is untouched and still usable.
        assert api_db.conn is parent_conn
        api_db.register_file("b.py", language="python")
        assert api_db.get_file_by_path("b.py") is not None

    async def test_clone_rebinds_loomweave_local_fallback_wrapper_to_private_conn(self, tmp_path: Path) -> None:
        """The fallback-wrapper-under-concurrent-ingest config — the exact
        ``SQLITE_MISUSE`` shape the connection-isolation fix targets.

        ``_rebind_local_id_factory_to_self`` has three arms: pure ``LocalRegistry``
        (covered by the other tests here), pure Loomweave (no rebind), and the
        ``_LoomweaveLocalFallbackRegistry`` wrapper. Only this last arm rebuilds the
        wrapper for the clone: the shared Loomweave ``_primary`` (an ``httpx.Client``)
        is kept BY REFERENCE — a non-owning clone must never close it — while the
        local-fallback half is rebound to the clone's own connection, so a
        worker-thread local-id mint (the fallback path under a Loomweave outage)
        uses the private connection, not the parent's shared one. A refactor that
        drops this ``elif`` arm would leave the clone sharing the parent's wrapper
        (and thus minting through the parent's connection) and regress silently."""
        with clarion_stub() as (base_url, _state):
            db = FiligreeDB(
                tmp_path / "filigree.db",
                prefix="test",
                check_same_thread=False,
                registry_backend="loomweave",
                loomweave_config={"base_url": base_url, "timeout_seconds": 0.5},
            )
            db.initialize()
            db.enable_local_registry_fallback()
        # Stub is down now; the rebind itself needs no network.
        assert isinstance(db.registry, _LoomweaveLocalFallbackRegistry)
        parent_wrapper = db.registry
        parent_primary = db.registry._primary
        parent_fallback = db.registry._fallback
        parent_base_url = db.registry._base_url

        captured: dict[str, Any] = {}

        def worker() -> None:
            with db.borrow_for_worker_thread() as clone:
                reg = clone.registry
                assert isinstance(reg, _LoomweaveLocalFallbackRegistry)
                captured["fresh_wrapper"] = reg is not parent_wrapper
                captured["primary_shared"] = reg._primary is parent_primary
                captured["fallback_rebound"] = reg._fallback is not parent_fallback
                captured["base_url"] = reg._base_url
                captured["clone_conn_distinct"] = clone.conn is not db.conn
                # Behavioural: the rebound fallback half mints a local id through
                # the clone's PRIVATE connection on this worker thread (the
                # cross-thread misuse path if it still closed over the parent).
                rec = reg._fallback.resolve_file("worker_mint.py", language="python")
                captured["minted_backend"] = rec["registry_backend"]
                captured["minted_local_prefix"] = rec["file_id"].startswith("test-f-")

        await asyncio.to_thread(worker)
        db.close()

        # Dropping the elif arm makes the clone keep the PARENT's wrapper:
        # fresh_wrapper/fallback_rebound flip False and this fails deterministically.
        assert captured["fresh_wrapper"] is True
        assert captured["fallback_rebound"] is True
        # The Loomweave HTTP client is shared by reference, never re-created.
        assert captured["primary_shared"] is True
        assert captured["base_url"] == parent_base_url
        assert captured["clone_conn_distinct"] is True
        # The rebound fallback factory mints a working local id on the clone's conn.
        assert captured["minted_backend"] == "local"
        assert captured["minted_local_prefix"] is True


class TestWorkerRunners:
    """The files.py worker-runner helpers handed to asyncio.to_thread."""

    async def test_ingest_runner_writes_via_private_conn(self, api_db: FiligreeDB) -> None:
        parsed = {
            "scan_source": "ruff",
            "findings": [{"path": "x.py", "rule_id": "E501", "message": "long"}],
        }
        result = await asyncio.to_thread(_ingest_scan_results_on_private_conn, api_db, parsed)
        assert result["findings_created"] == 1
        # Visible on the shared connection afterwards.
        f = api_db.get_file_by_path("x.py")
        assert f is not None

    async def test_clean_stale_runner_runs_via_private_conn(self, api_db: FiligreeDB) -> None:
        # Seed a finding, then re-ingest the SAME file without it (mark_unseen)
        # so the original transitions to unseen_in_latest; then sweep with
        # older_than_days=0.
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "x.py", "rule_id": "E501", "message": "long"}],
        )
        api_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "x.py", "rule_id": "E999", "message": "other"}],
            mark_unseen=True,
        )
        result = await asyncio.to_thread(
            _clean_stale_findings_on_private_conn,
            api_db,
            days=0,
            scan_source="ruff",
            actor="test",
        )
        assert result["findings_fixed"] >= 1


class TestConcurrentMixedWrites:
    """Confirmatory end-to-end: interleaved worker-path ingest and event-loop
    writes do not corrupt state. (Not a deterministic race repro — serialized
    sqlite3 mutexes individual calls, so the signal is final-state consistency,
    not thrown exceptions.)
    """

    async def test_interleaved_scan_results_and_patch_findings(self, client: AsyncClient, api_db: FiligreeDB) -> None:
        # Seed a finding we can PATCH concurrently with ingests.
        api_db.process_scan_results(
            scan_source="seed",
            findings=[{"path": "seed.py", "rule_id": "S1", "message": "seed"}],
        )
        f = api_db.get_file_by_path("seed.py")
        assert f is not None
        findings = api_db.get_findings_paginated(f.id)["results"]
        finding_id = findings[0]["id"]

        def scan_body(i: int) -> dict[str, Any]:
            return {
                "scan_source": f"src{i}",
                "findings": [{"path": f"f{i}.py", "rule_id": "R1", "message": "m"}],
            }

        ingests = [client.post("/api/v1/scan-results", json=scan_body(i)) for i in range(8)]
        patches = [
            client.patch(f"/api/files/{f.id}/findings/{finding_id}", json={"status": "acknowledged" if i % 2 else "open"}) for i in range(8)
        ]
        responses = await asyncio.gather(*ingests, *patches)

        assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
        # All ingested files persisted; the shared connection is healthy.
        for i in range(8):
            assert api_db.get_file_by_path(f"f{i}.py") is not None
