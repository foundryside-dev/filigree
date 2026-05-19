"""Tests for the file registry backend boundary."""

from __future__ import annotations

import json
import logging
import socket
import threading
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from inspect import getdoc
from pathlib import Path
from typing import get_args, get_type_hints
from urllib.parse import parse_qs, urlparse

import pytest

from filigree.core import VALID_REGISTRY_BACKENDS, FiligreeDB
from filigree.models import FileRecord
from filigree.registry import ClarionRegistry, LocalRegistry, RegistryFileNotFoundError, RegistryUnavailableError, ResolvedFile
from filigree.types.core import ClarionConfig, EntityId, FileId, FileRecordDict, ProjectConfig, RegistryBackend


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


def test_project_config_uses_typed_clarion_config() -> None:
    hints = get_type_hints(ProjectConfig)

    assert hints["clarion"] is ClarionConfig
    assert set(get_type_hints(ClarionConfig)) == {"base_url", "timeout_seconds", "allow_local_fallback"}


def test_registry_backend_literal_is_shared_config_model_source_of_truth() -> None:
    project_hints = get_type_hints(ProjectConfig)
    file_record_hints = get_type_hints(FileRecord)
    file_record_dict_hints = get_type_hints(FileRecordDict)

    assert frozenset(get_args(RegistryBackend)) == VALID_REGISTRY_BACKENDS
    assert project_hints["registry_backend"] is RegistryBackend
    assert file_record_hints["registry_backend"] is RegistryBackend
    assert file_record_dict_hints["registry_backend"] is RegistryBackend


def test_file_record_documents_content_hash_backend_invariant() -> None:
    doc = getdoc(FileRecord)

    assert doc is not None
    assert "content_hash == ''" in doc
    assert "registry_backend == 'local'" in doc
    assert "Clarion" in doc
    assert "non-empty" in doc


def test_filigree_db_validates_programmatic_clarion_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown clarion setting"):
        FiligreeDB(
            tmp_path / "unknown.db",
            prefix="test",
            clarion_config={"base-url": "http://clarion.test"},  # type: ignore[typeddict-unknown-key]
        )

    with pytest.raises(ValueError, match="allow_local_fallback"):
        FiligreeDB(
            tmp_path / "bad-fallback.db",
            prefix="test",
            clarion_config={"allow_local_fallback": "yes"},  # type: ignore[typeddict-item]
        )


def test_registry_resolved_file_uses_branded_file_identity_types() -> None:
    hints = get_type_hints(ResolvedFile)

    assert hints["file_id"] == FileId | EntityId


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


@pytest.mark.parametrize("base_url", ["ftp://clarion.test", "http://", "localhost:9111", "http:///api"])
def test_clarion_registry_rejects_invalid_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        ClarionRegistry(base_url)


def test_clarion_registry_is_immutable() -> None:
    registry = ClarionRegistry("http://clarion.test/")

    assert registry.base_url == "http://clarion.test"
    with pytest.raises(FrozenInstanceError):
        registry.base_url = "http://other.test"  # type: ignore[misc]


def test_registry_unavailable_error_carries_structured_fields() -> None:
    error = RegistryUnavailableError(
        "registry unavailable",
        url="http://clarion.test/api/v1/files?path=src/main.py",
        path="src/main.py",
        cause_kind="network",
    )

    assert str(error) == "registry unavailable"
    assert error.url == "http://clarion.test/api/v1/files?path=src/main.py"
    assert error.path == "src/main.py"
    assert error.cause_kind == "network"


def test_clarion_registry_wraps_unreachable_backend() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    registry = ClarionRegistry(f"http://{host}:{port}", timeout_seconds=0.1)

    with pytest.raises(RegistryUnavailableError, match="/api/v1/files") as exc_info:
        registry.resolve_file("src/main.py", language="python")

    assert exc_info.value.url.startswith(f"http://{host}:{port}/api/v1/files")
    assert exc_info.value.path == "src/main.py"
    assert exc_info.value.cause_kind == "network"


def test_clarion_registry_distinguishes_unknown_file_from_unavailable() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_error(404, "not indexed")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        registry = ClarionRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)

        with pytest.raises(RegistryFileNotFoundError, match="HTTP 404 not indexed") as exc_info:
            registry.resolve_file("src/missing.py", language="python")

        assert exc_info.value.status_code == 404
        assert "/api/v1/files" in exc_info.value.url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


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

        with pytest.raises(RegistryUnavailableError, match="content_hash") as exc_info:
            registry.resolve_file("src/main.py", language="python")

        assert exc_info.value.url.startswith(f"http://127.0.0.1:{server.server_port}/api/v1/files")
        assert exc_info.value.path == "src/main.py"
        assert exc_info.value.cause_kind == "invalid_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_rejects_blank_content_hash() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(
                {
                    "entity_id": "core:file:abc123@src/main.py",
                    "content_hash": "",
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


def test_filigree_db_allow_local_fallback_tries_clarion_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolutions: list[str] = []

    class FakeClarionRegistry:
        def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
            self.base_url = base_url
            self.timeout_seconds = timeout_seconds

        def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
            resolutions.append(path)
            return {
                "file_id": "core:file:clarion-first@src/fallback.py",
                "content_hash": "sha256:clarion-first",
                "canonical_path": path,
                "language": language,
                "registry_backend": "clarion",
            }

        def is_displaced(self) -> bool:
            return True

    monkeypatch.setattr("filigree.core.ClarionRegistry", FakeClarionRegistry)
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry_backend="clarion",
        clarion_config={
            "base_url": "http://clarion.test",
            "timeout_seconds": 1,
            "allow_local_fallback": True,
        },
    )
    try:
        db.initialize()

        file_record = db.register_file("src/fallback.py", language="python")

        assert resolutions == ["src/fallback.py"]
        assert file_record.id == "core:file:clarion-first@src/fallback.py"
        assert file_record.content_hash == "sha256:clarion-first"
        assert file_record.registry_backend == "clarion"
        assert db.registry.is_displaced() is True
    finally:
        db.close()


def test_filigree_db_requires_explicit_clarion_base_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"clarion\.base_url"):
        FiligreeDB(tmp_path / "filigree.db", prefix="test", registry_backend="clarion")


def test_filigree_db_allow_local_fallback_uses_local_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolution_attempts: list[str] = []

    class FailingClarionRegistry:
        def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
            self.base_url = base_url
            self.timeout_seconds = timeout_seconds

        def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
            resolution_attempts.append(path)
            raise RegistryUnavailableError(
                "Clarion unavailable for test",
                url=f"{self.base_url}/api/v1/files",
                path=path,
                cause_kind="network",
            )

        def is_displaced(self) -> bool:
            return True

    monkeypatch.setattr("filigree.core.ClarionRegistry", FailingClarionRegistry)
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry_backend="clarion",
        clarion_config={
            "base_url": "http://clarion.test",
            "timeout_seconds": 0.1,
            "allow_local_fallback": True,
        },
    )
    try:
        db.initialize()

        with caplog.at_level(logging.WARNING, logger="filigree.core"):
            result = db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "src/fallback.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
            )

        assert resolution_attempts == ["src/fallback.py"]
        assert db.allow_local_fallback is True
        assert db.registry.is_displaced() is True
        file_record = db.get_file_by_path("src/fallback.py")
        assert file_record is not None
        assert file_record.id.startswith("test-f-")
        assert file_record.registry_backend == "local"
        event = db.conn.execute(
            "SELECT event_type, field, old_value, new_value FROM file_events WHERE file_id = ?",
            (file_record.id,),
        ).fetchone()
        assert event is not None
        assert event["event_type"] == "registry_local_fallback"
        assert event["field"] == "registry_backend"
        assert event["old_value"] == "clarion"
        assert event["new_value"] == "local"
        assert result["files_created"] == 1
        assert "Clarion registry backend unavailable; using local file registry fallback" in caplog.text
    finally:
        db.close()


def test_filigree_db_logs_hybrid_registry_state_on_startup(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db_path = tmp_path / "filigree.db"
    db = FiligreeDB(db_path, prefix="test")
    try:
        db.initialize()
        db.register_file("src/legacy.py")
    finally:
        db.close()

    with caplog.at_level(logging.WARNING, logger="filigree.core"):
        db = FiligreeDB(
            db_path,
            prefix="test",
            registry_backend="clarion",
            clarion_config={"base_url": "http://clarion.test"},
        )
        try:
            db.initialize()
        finally:
            db.close()

    records = [record for record in caplog.records if record.message == "file_registry_hybrid_state_detected"]
    assert records
    assert records[0].registry_backend == "clarion"
    assert records[0].local_file_records == 1


def test_filigree_db_rejects_injected_local_registry_for_clarion_backend(tmp_path: Path) -> None:
    registry = LocalRegistry(lambda: "test-f-1")

    with pytest.raises(ValueError, match="Injected registry displacement"):
        FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=registry,
            registry_backend="clarion",
        )


def test_filigree_db_rejects_injected_displaced_registry_for_local_backend(tmp_path: Path) -> None:
    registry = ClarionRegistry("http://127.0.0.1:9", timeout_seconds=0.1)

    with pytest.raises(ValueError, match="Injected registry displacement"):
        FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=registry,
            registry_backend="local",
        )
