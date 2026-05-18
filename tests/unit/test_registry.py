"""Tests for the file registry backend boundary."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from filigree.core import FiligreeDB
from filigree.registry import ClarionRegistry, LocalRegistry, RegistryUnavailableError


def test_local_registry_resolves_file_with_local_identity() -> None:
    issued: list[str] = []

    def make_id() -> str:
        issued.append("called")
        return f"test-f-{len(issued):010d}"

    registry = LocalRegistry(make_id)

    resolved = registry.resolve_file("src/main.py", language="python", actor="tester")

    assert resolved == {
        "file_id": "test-f-0000000001",
        "content_hash": "",
        "canonical_path": "src/main.py",
        "language": "python",
        "registry_backend": "local",
    }
    assert registry.is_displaced() is False


def test_filigree_db_composes_local_registry_by_default(tmp_path: Path) -> None:
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    try:
        db.initialize()

        resolved = db.registry.resolve_file("src/main.py", language="python")

        assert resolved["file_id"].startswith("test-f-")
        assert resolved["registry_backend"] == "local"
        assert db.registry.is_displaced() is False
    finally:
        db.close()


def test_clarion_registry_resolves_file_via_http() -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            requests.append(parse_qs(parsed.query))
            assert parsed.path == "/api/v1/files"
            body = json.dumps(
                {
                    "entity_id": "core:file:abc123@src/main.py",
                    "content_hash": "sha256:abc123",
                    "canonical_path": "src/main.py",
                    "language": "python",
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
        registry = ClarionRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)

        resolved = registry.resolve_file("src/main.py", language="python", actor="tester")

        assert requests == [{"path": ["src/main.py"], "language": ["python"]}]
        assert resolved == {
            "file_id": "core:file:abc123@src/main.py",
            "content_hash": "sha256:abc123",
            "canonical_path": "src/main.py",
            "language": "python",
            "registry_backend": "clarion",
        }
        assert registry.is_displaced() is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_wraps_unreachable_backend() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    registry = ClarionRegistry(f"http://{host}:{port}", timeout_seconds=0.1)

    with pytest.raises(RegistryUnavailableError, match="/api/v1/files"):
        registry.resolve_file("src/main.py", language="python")


def test_clarion_registry_rejects_malformed_response() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"entity_id": "core:file:abc123@src/main.py"}).encode()
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
        registry = ClarionRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)

        with pytest.raises(RegistryUnavailableError, match="content_hash"):
            registry.resolve_file("src/main.py", language="python")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_filigree_db_composes_clarion_registry_when_configured(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(
                {
                    "entity_id": "core:file:configured@src/main.py",
                    "content_hash": "sha256:configured",
                    "canonical_path": "src/main.py",
                    "language": "python",
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
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry_backend="clarion",
        clarion_config={"base_url": f"http://127.0.0.1:{server.server_port}", "timeout_seconds": 1},
    )
    try:
        db.initialize()

        file_record = db.register_file("src/main.py", language="python")

        assert file_record.id == "core:file:configured@src/main.py"
        assert file_record.content_hash == "sha256:configured"
        assert file_record.registry_backend == "clarion"
        assert db.registry.is_displaced() is True
    finally:
        db.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_filigree_db_allow_local_fallback_uses_local_registry(tmp_path: Path) -> None:
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry_backend="clarion",
        clarion_config={
            "base_url": "http://127.0.0.1:9",
            "timeout_seconds": 0.1,
            "allow_local_fallback": True,
        },
    )
    try:
        db.initialize()

        result = db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/fallback.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
        )

        assert db.allow_local_fallback is True
        assert db.registry.is_displaced() is False
        file_record = db.get_file_by_path("src/fallback.py")
        assert file_record is not None
        assert file_record.id.startswith("test-f-")
        assert file_record.registry_backend == "local"
        assert result["files_created"] == 1
    finally:
        db.close()
