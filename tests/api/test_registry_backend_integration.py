"""Integration coverage for ADR-014 registry-backend handshakes."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app


@contextmanager
def _live_clarion_read_api() -> Iterator[tuple[str, list[dict[str, list[str]]]]]:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            requests.append(query)
            assert parsed.path == "/api/v1/files"
            path = query.get("path", [""])[0]
            language = query.get("language", [""])[0]
            body = json.dumps(
                {
                    "entity_id": f"core:file:phase-d@{path}",
                    "content_hash": "sha256:phase-d",
                    "canonical_path": path,
                    "language": language,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


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
        with _live_clarion_read_api() as (base_url, requests):
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
                assert requests == [{"path": ["src/phase_d.py"], "language": ["python"]}]
                file_record = db.get_file_by_path("src/phase_d.py")
                assert file_record is not None
                assert file_record.id == "core:file:phase-d@src/phase_d.py"
                assert file_record.content_hash == "sha256:phase-d"
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
