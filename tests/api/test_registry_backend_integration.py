"""Integration coverage for ADR-014 registry-backend handshakes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app
from tests._fakes.clarion_http import clarion_stub


async def _post_scan_results(db: FiligreeDB) -> dict[str, object]:
    dash_module._db = db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        schema_response = await client.get("/api/files/_schema")
        assert schema_response.status_code == 200
        schema = schema_response.json()
        assert schema["config_flags"]["registry_backend"] == db.registry_backend
        assert schema["config_flags"]["registry_backend_features"] == ["local", "clarion"]

        ingest_response = await client.post(
            "/api/loom/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [
                    {"path": "src/phase_d.py", "rule_id": "E501", "severity": "low", "message": "msg"},
                ],
            },
        )
        assert ingest_response.status_code == 200, ingest_response.text
        return ingest_response.json()


@pytest.mark.parametrize("registry_backend", ["local", "clarion"])
async def test_loom_scan_results_resolves_file_identity_over_registry_backends(tmp_path: Path, registry_backend: str) -> None:
    if registry_backend == "clarion":
        with clarion_stub() as (base_url, state):
            db = FiligreeDB(
                tmp_path / "filigree.db",
                prefix="test",
                check_same_thread=False,
                registry_backend="clarion",
                clarion_config={"base_url": base_url, "timeout_seconds": 1},
            )
            db.initialize()
            try:
                result = await _post_scan_results(db)

                assert len(result["succeeded"]) == 1
                assert str(result["succeeded"][0]).startswith("test-sf-")
                # CONTRACT-1: scan-results now batches unfamiliar paths into
                # a single POST /api/v1/files/batch, not N GET /api/v1/files.
                assert state.file_requests == []
                assert state.batch_requests == [{"queries": [{"path": "src/phase_d.py", "language": "python"}]}]
                file_record = db.get_file_by_path("src/phase_d.py")
                assert file_record is not None
                assert file_record.id == "core:file:stub@src/phase_d.py"
                assert file_record.content_hash == "sha256:src/phase_d.py"
                assert file_record.registry_backend == "clarion"
            finally:
                dash_module._db = None
                db.close()
        return

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", check_same_thread=False)
    db.initialize()
    try:
        result = await _post_scan_results(db)

        assert len(result["succeeded"]) == 1
        assert str(result["succeeded"][0]).startswith("test-sf-")
        file_record = db.get_file_by_path("src/phase_d.py")
        assert file_record is not None
        assert file_record.id.startswith("test-f-")
        assert file_record.content_hash == ""
        assert file_record.registry_backend == "local"
    finally:
        dash_module._db = None
        db.close()


async def test_loom_scan_results_falls_back_to_local_when_clarion_goes_down(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-014 §7: clarion-mode + allow_local_fallback=true + Clarion-down at write time.

    Exercise the recovery path end-to-end through ``FiligreeDB``:
    - Stub Clarion is up at startup so the capability probe succeeds.
    - Stub is shut down before the scan-results POST.
    - The auto-create succeeds via ``LocalRegistry``.
    - A ``registry_local_fallback`` event is written.
    - A WARN log carries ``cause_kind`` and the failing URL.

    Without this test the ``_ClarionLocalFallbackRegistry`` wrapper is only
    unit-tested in isolation; this asserts it is actually wired through
    ``FiligreeDB`` at the HTTP boundary.
    """
    with clarion_stub() as (base_url, _state):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={
                "base_url": base_url,
                "timeout_seconds": 0.5,
                "allow_local_fallback": True,
            },
        )
        db.initialize()
    # Stub is now shut down — subsequent /api/v1/files calls will fail.
    try:
        with caplog.at_level(logging.WARNING, logger="filigree.core"):
            result = await _post_scan_results(db)

        assert len(result["succeeded"]) == 1
        file_record = db.get_file_by_path("src/phase_d.py")
        assert file_record is not None
        # Fallback path: file_id minted locally, registry_backend recorded as 'local'.
        assert file_record.id.startswith("test-f-")
        assert file_record.registry_backend == "local"

        # Audit event was written.
        event = db.conn.execute(
            "SELECT event_type, field, old_value, new_value FROM file_events WHERE file_id = ? AND event_type = 'registry_local_fallback'",
            (file_record.id,),
        ).fetchone()
        assert event is not None
        assert event["field"] == "registry_backend"
        assert event["old_value"] == "clarion"
        assert event["new_value"] == "local"

        # WARN log carries the network cause_kind and the failing URL.
        fallback_records = [r for r in caplog.records if "using local file registry fallback" in r.getMessage()]
        assert fallback_records, "expected fallback WARN log"
        last = fallback_records[-1]
        assert getattr(last, "cause_kind", None) == "network"
        assert base_url in getattr(last, "url", "")
    finally:
        dash_module._db = None
        db.close()


async def test_loom_scan_results_does_not_block_event_loop_for_other_handlers(tmp_path: Path) -> None:
    """CONTRACT-E: a slow scan-results POST must not block the event loop;
    OTHER endpoints (here ``GET /api/scan-runs``) must complete during the
    Clarion HTTP wait.

    This test verifies the responsiveness property by interleaving a fast read
    endpoint with a slow scan-results POST. Parallelism between two concurrent
    scan-results POSTs is verified separately by
    ``test_concurrent_loom_scan_results_run_in_parallel``.
    """
    import asyncio
    import http.server
    import json as jsonmod
    import threading
    import time
    from urllib.parse import urlparse as _urlparse

    latency_seconds = 0.3

    class LatentClarionHandler(http.server.BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = jsonmod.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = _urlparse(self.path)
            if parsed.path == "/api/v1/_capabilities":
                self._send_json(
                    200,
                    {"registry_backend": True, "file_registry": True, "api_version": 1, "instance_id": "latent"},
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            time.sleep(latency_seconds)
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = jsonmod.loads(raw.decode("utf-8")) if raw else {}
            queries = payload.get("queries", [])
            resolved = [
                {
                    "requested_path": q["path"],
                    "entity_id": f"core:file:stub@{q['path']}",
                    "content_hash": f"sha256:{q['path']}",
                    "canonical_path": q["path"],
                    "language": q.get("language", ""),
                }
                for q in queries
            ]
            self._send_json(200, {"resolved": resolved, "not_found": [], "briefing_blocked": [], "errors": []})

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LatentClarionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 5},
        )
        db.initialize()
        try:
            dash_module._db = db
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                scan_body = {
                    "scan_source": "ruff",
                    "findings": [{"path": "src/a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
                }
                scan_task = asyncio.create_task(client.post("/api/loom/scan-results", json=scan_body))
                # Tiny yield so scan-results starts its HTTP wait, then issue
                # an unrelated GET — it must complete well before scan-results does.
                await asyncio.sleep(0.05)
                get_start = time.monotonic()
                runs_resp = await client.get("/api/scan-runs")
                get_elapsed = time.monotonic() - get_start
                scan_resp = await scan_task
            assert scan_resp.status_code == 200, scan_resp.text
            assert runs_resp.status_code == 200
            # The GET must NOT have to wait the full latency. Serialized at
            # the event loop → get_elapsed >= latency_seconds. Properly
            # to_threaded → get_elapsed is a few ms.
            assert get_elapsed < latency_seconds, (
                f"GET /api/scan-runs blocked on scan-results event loop: {get_elapsed:.3f}s >= latency {latency_seconds}s"
            )
        finally:
            dash_module._db = None
            db.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


async def test_concurrent_loom_scan_results_run_in_parallel(tmp_path: Path) -> None:
    """Two concurrent scan-results POSTs overlap their Clarion HTTP round-trips
    instead of serialising.

    This is the parallelism half of filigree-d4237f486f. The correctness half
    (no cross-thread shared-connection race) was closed earlier by
    ``borrow_for_worker_thread``; the residual ``_SCAN_RESULTS_LOCK`` that
    serialised the two worker WRITE paths has been removed. Each call does its
    Clarion batch resolution (the slow part) BEFORE opening any write
    transaction, so with no app-level lock the two resolutions overlap and the
    tiny write windows serialise only briefly at the WAL writer lock
    (``busy_timeout`` absorbs the wait — no SQLITE_BUSY/IntegrityError).

    Overlap is proven deterministically rather than by wall-clock: a
    ``threading.Barrier(2)`` inside the stub Clarion resolve handler forces BOTH
    POSTs' batch resolutions to be in flight at the same instant before either
    is allowed to return. If the app still serialised the two paths (the old
    lock behaviour), only one resolution would ever reach the barrier, it would
    time out, break, and both POSTs would fail the 200 assertion — no flaky
    timing threshold involved.
    """
    import asyncio
    import http.server
    import json as jsonmod
    import threading
    from urllib.parse import urlparse as _urlparse

    # Rendezvous for the two scan-results POSTs' Clarion batch resolutions. The
    # capabilities probe is a GET, so exactly two requests hit do_POST — 2 is
    # both the floor we require and the ceiling that can be reached.
    resolve_barrier = threading.Barrier(2, timeout=5)
    barrier_broken: list[str] = []

    class LatentClarionHandler(http.server.BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = jsonmod.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = _urlparse(self.path)
            if parsed.path == "/api/v1/_capabilities":
                self._send_json(
                    200,
                    {"registry_backend": True, "file_registry": True, "api_version": 1, "instance_id": "latent"},
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = jsonmod.loads(raw.decode("utf-8")) if raw else {}
            queries = payload.get("queries", [])
            # Both resolutions must be concurrently in flight to pass the barrier.
            # Serialised execution leaves the first waiter to time out and break
            # the barrier; record it so the failure reads clearly.
            try:
                resolve_barrier.wait()
            except threading.BrokenBarrierError:
                barrier_broken.append(self.path)
                self.send_response(503)
                self.end_headers()
                return
            resolved = [
                {
                    "requested_path": q["path"],
                    "entity_id": f"core:file:stub@{q['path']}",
                    "content_hash": f"sha256:{q['path']}",
                    "canonical_path": q["path"],
                    "language": q.get("language", ""),
                }
                for q in queries
            ]
            self._send_json(200, {"resolved": resolved, "not_found": [], "briefing_blocked": [], "errors": []})

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LatentClarionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 5},
        )
        db.initialize()
        try:
            dash_module._db = db
            app = create_app()
            transport = ASGITransport(app=app)

            def scan_body(run: int) -> dict[str, Any]:
                # Distinct paths per POST so each resolution + write window is
                # non-trivial and the two POSTs touch different file rows.
                return {
                    "scan_source": f"src{run}",
                    "findings": [{"path": f"src/run{run}_f{i}.py", "rule_id": "R1", "severity": "low", "message": "m"} for i in range(5)],
                }

            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp_a, resp_b = await asyncio.gather(
                    client.post("/api/loom/scan-results", json=scan_body(0)),
                    client.post("/api/loom/scan-results", json=scan_body(1)),
                )

            # The barrier was satisfied: both resolutions overlapped. A serialised
            # path would have broken it (recorded below) and failed these 200s.
            assert not barrier_broken, f"scan-results serialised — barrier timed out for {barrier_broken}"
            # No IntegrityError / SQLITE_BUSY surfaced under concurrent writers.
            assert resp_a.status_code == 200, resp_a.text
            assert resp_b.status_code == 200, resp_b.text
            # Both POSTs' files persisted.
            for run in range(2):
                for i in range(5):
                    assert db.get_file_by_path(f"src/run{run}_f{i}.py") is not None
        finally:
            dash_module._db = None
            db.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


async def test_scan_ingest_adopts_committed_id_on_local_minted_id_collision(tmp_path: Path) -> None:
    """C1 regression (db_files.py:977-982). Under concurrent same-path ingest in
    local / Clarion-fallback mode, ``LocalRegistry.resolve_file`` mints a *fresh*
    arbitrary id per call (registry.py). When two ingests pre-resolve the same
    new path before either writes, they mint different ids; the winner commits
    and the loser must adopt the committed row's id — the minted id is arbitrary,
    so the mismatch is NOT registry drift and must not raise the
    ``run migrate-registry`` ValueError (which surfaces as a spurious HTTP 400).

    Deterministic white-box reproduction of the ``existing_file`` first-SELECT
    site: a row is already committed under ``id_A``; a second ingest arrives
    pre-resolved to a different local ``id_B`` for the same path.
    """
    from filigree.registry import LocalResolvedFile
    from filigree.types.files import ScanIngestResult

    def _fresh_stats() -> ScanIngestResult:
        return ScanIngestResult(
            files_created=0,
            files_updated=0,
            findings_created=0,
            findings_updated=0,
            new_finding_ids=[],
            observations_created=0,
            observations_failed=0,
            warnings=[],
        )

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", check_same_thread=False)
    db.initialize()
    try:
        path = "src/raced.py"
        now = "2026-06-02T00:00:00Z"
        winner = LocalResolvedFile(
            file_id="test-f-winner", content_hash="", canonical_path=path, language="python", registry_backend="local"
        )
        loser = LocalResolvedFile(file_id="test-f-loser", content_hash="", canonical_path=path, language="python", registry_backend="local")

        # Winner commits the row first.
        fid_a = db._upsert_file_record(
            path=path,
            language="python",
            infer_language=False,
            now=now,
            stats=_fresh_stats(),
            counted_file_ids=set(),
            actor="t",
            resolved_file=winner,
        )
        db.conn.commit()
        assert fid_a == "test-f-winner"

        # Loser pre-resolved a *different* local id for the same path; the row is
        # already present. Pre-fix this raised ValueError(...migrate-registry...);
        # post-fix it adopts the committed id.
        fid_b = db._upsert_file_record(
            path=path,
            language="python",
            infer_language=False,
            now=now,
            stats=_fresh_stats(),
            counted_file_ids=set(),
            actor="t",
            resolved_file=loser,
        )
        assert fid_b == "test-f-winner", "loser must adopt the committed row's id, not raise"

        # The committed row is untouched: still exactly one row under id_A.
        rows = db.conn.execute("SELECT id FROM file_records WHERE path = ?", (path,)).fetchall()
        assert [r["id"] for r in rows] == ["test-f-winner"]
    finally:
        db.close()


async def test_concurrent_same_path_local_scan_results_do_not_raise(tmp_path: Path) -> None:
    """C1 regression (end-to-end). Two concurrent scan-results POSTs for the
    *same* new path in local mode must both return 200 and converge on a single
    file row — no spurious HTTP 400 ``run migrate-registry``.

    A ``threading.Barrier`` patched into ``LocalRegistry.resolve_files_batch``
    forces both worker threads to finish minting their (distinct) local ids
    inside the pre-resolve window — exactly the production structure (resolution
    happens before the write transaction opens) — before either writes. This
    deterministically forces the same-path INSERT collision the removed
    ``_SCAN_RESULTS_LOCK`` used to prevent. The barrier carries a timeout so a
    patch that fails to bind surfaces as a loud failure, not a hang.
    """
    import asyncio
    import threading

    from filigree.registry import LocalRegistry

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", check_same_thread=False)
    db.initialize()

    barrier = threading.Barrier(2, timeout=5)
    original_batch = LocalRegistry.resolve_files_batch

    def barriered_batch(self: LocalRegistry, queries: Any, *, actor: str = "") -> Any:
        result = original_batch(self, queries, actor=actor)
        # Both POSTs must finish minting before either opens its write window.
        barrier.wait()
        return result

    LocalRegistry.resolve_files_batch = barriered_batch  # type: ignore[method-assign]
    try:
        dash_module._db = db
        app = create_app()
        transport = ASGITransport(app=app)

        def scan_body(run: int) -> dict[str, Any]:
            # Same path across both POSTs; distinct scan_source so finding-level
            # dedup does not mask the file-row race under test.
            return {
                "scan_source": f"ruff{run}",
                "findings": [{"path": "src/raced.py", "rule_id": "E501", "severity": "low", "message": "m"}],
            }

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp_a, resp_b = await asyncio.gather(
                client.post("/api/loom/scan-results", json=scan_body(0)),
                client.post("/api/loom/scan-results", json=scan_body(1)),
            )

        assert resp_a.status_code == 200, resp_a.text
        assert resp_b.status_code == 200, resp_b.text

        # Exactly one row for the contended path; both POSTs agree on its id.
        rows = db.conn.execute("SELECT id FROM file_records WHERE path = ?", ("src/raced.py",)).fetchall()
        assert len(rows) == 1, f"expected a single converged row, got {[r['id'] for r in rows]}"
        converged_id = rows[0]["id"]
        assert db.get_file_by_path("src/raced.py") is not None

        # The adopted id flows downstream: both POSTs' findings attached to the
        # single converged row (not to the loser's discarded minted id).
        scan_sources = {
            r["scan_source"]
            for r in db.conn.execute("SELECT DISTINCT scan_source FROM scan_findings WHERE file_id = ?", (converged_id,)).fetchall()
        }
        assert scan_sources == {"ruff0", "ruff1"}, scan_sources
    finally:
        LocalRegistry.resolve_files_batch = original_batch  # type: ignore[method-assign]
        dash_module._db = None
        db.close()


async def test_loom_scan_results_makes_single_batch_call_for_300_findings(tmp_path: Path) -> None:
    """CONTRACT-1: a scan-results POST with 300 unfamiliar paths makes exactly
    ``ceil(300/256) = 2`` batch HTTP calls (not 300 GET calls)."""
    findings = [{"path": f"src/file_{i:04d}.py", "rule_id": "E501", "severity": "low", "message": "msg"} for i in range(300)]
    with clarion_stub() as (base_url, state):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 5},
        )
        db.initialize()
        try:
            dash_module._db = db
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/loom/scan-results",
                    json={"scan_source": "ruff", "findings": findings},
                )
            assert response.status_code == 200, response.text
            assert len(state.file_requests) == 0  # zero single-file GETs
            assert len(state.batch_requests) == 2  # two POST batches
            assert len(state.batch_requests[0]["queries"]) == 256
            assert len(state.batch_requests[1]["queries"]) == 300 - 256
        finally:
            dash_module._db = None
            db.close()


async def test_loom_scan_results_briefing_blocked_path_bypasses_fallback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CONTRACT-3: a briefing-blocked path (Clarion 1.0 returns 403 +
    ``code: BRIEFING_BLOCKED``) must NOT fall back to local, even with
    ``allow_local_fallback=true``. Falling back would silently re-attach
    the secret-bearing file under a local file_id, defeating the briefing
    block.

    Asserts the scan-results POST returns 403 with ``ErrorCode.BRIEFING_BLOCKED``,
    no ``file_records`` row is created for the blocked path, and no
    ``registry_local_fallback`` event is written.
    """
    with clarion_stub() as (base_url, state):
        state.briefing_blocked_paths.add("src/phase_d.py")
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={
                "base_url": base_url,
                "timeout_seconds": 1,
                "allow_local_fallback": True,
            },
        )
        db.initialize()
        try:
            dash_module._db = db
            app = create_app()
            transport = ASGITransport(app=app)
            with caplog.at_level(logging.WARNING, logger="filigree.core"):
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/api/loom/scan-results",
                        json={
                            "scan_source": "ruff",
                            "findings": [
                                {"path": "src/phase_d.py", "rule_id": "E501", "severity": "low", "message": "msg"},
                            ],
                        },
                    )

            assert response.status_code == 403, response.text
            body = response.json()
            assert body["code"] == "BRIEFING_BLOCKED"
            assert "briefing-blocked" in body["error"]

            # No file row was created — the briefing block stuck.
            assert db.get_file_by_path("src/phase_d.py") is None

            # No fallback engaged: no fallback WARN, no registry_local_fallback event.
            fallback_records = [r for r in caplog.records if "using local file registry fallback" in r.getMessage()]
            assert not fallback_records, "fallback wrapper must not engage on briefing-blocked"
            fallback_events = db.conn.execute("SELECT 1 FROM file_events WHERE event_type = 'registry_local_fallback'").fetchone()
            assert fallback_events is None
        finally:
            dash_module._db = None
            db.close()
