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
from typing import Literal, cast, get_args, get_type_hints
from urllib.parse import parse_qs, urlparse

import pytest

from filigree.core import VALID_REGISTRY_BACKENDS, FiligreeDB
from filigree.models import FileRecord
from filigree.registry import (
    CLARION_BATCH_MAX_QUERIES,
    BatchQuery,
    ClarionRegistry,
    ClarionResolvedFile,
    LocalRegistry,
    LocalResolvedFile,
    RegistryBriefingBlockedError,
    RegistryFileNotFoundError,
    RegistryResolutionError,
    RegistryUnavailableError,
    ResolvedFile,
)
from filigree.types.core import ClarionConfig, ContentHash, EntityId, FileId, FileRecordDict, ProjectConfig, RegistryBackend


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
    assert set(get_type_hints(ClarionConfig)) == {"base_url", "timeout_seconds", "allow_local_fallback", "token_env"}


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


def test_filigree_db_reads_clarion_token_from_named_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT-2: ``ClarionConfig.token_env`` names the env var; the resolved
    token threads into ``ClarionRegistry.auth_token``."""
    from tests._fakes.clarion_http import clarion_stub

    monkeypatch.setenv("FILIGREE_TEST_LOOM_TOKEN", "live-token-value")
    with clarion_stub() as (base_url, state):
        state.required_token = "live-token-value"  # noqa: S105 — test fixture
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={
                "base_url": base_url,
                "timeout_seconds": 1,
                "token_env": "FILIGREE_TEST_LOOM_TOKEN",
            },
        )
        db.initialize()
        try:
            assert isinstance(db.registry, ClarionRegistry)
            assert db.registry.auth_token == "live-token-value"  # noqa: S105 — test fixture
            # Capability probe at startup sent the Bearer header.
            assert state.auth_headers_seen[0] == "Bearer live-token-value"
        finally:
            db.close()


def test_filigree_db_warns_when_clarion_token_env_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CONTRACT-2: ``token_env`` explicitly configured but env var empty →
    WARN log, no header sent, Clarion accepts on loopback."""
    from tests._fakes.clarion_http import clarion_stub

    monkeypatch.delenv("FILIGREE_TEST_LOOM_TOKEN_MISSING", raising=False)
    with clarion_stub() as (base_url, state):
        # required_token stays None — Clarion accepts unauthenticated on loopback.
        with caplog.at_level(logging.WARNING, logger="filigree.core"):
            db = FiligreeDB(
                tmp_path / "filigree.db",
                prefix="test",
                check_same_thread=False,
                registry_backend="clarion",
                clarion_config={
                    "base_url": base_url,
                    "timeout_seconds": 1,
                    "token_env": "FILIGREE_TEST_LOOM_TOKEN_MISSING",
                },
            )
            db.initialize()
        try:
            assert isinstance(db.registry, ClarionRegistry)
            assert db.registry.auth_token is None
            assert state.auth_headers_seen
            assert state.auth_headers_seen[0] is None
            warns = [r for r in caplog.records if "token_env" in r.getMessage()]
            assert warns, "expected token_env-empty WARN"
        finally:
            db.close()


def test_filigree_db_exceptional_context_exit_closes_registry(tmp_path: Path) -> None:
    class CloseableRegistry(LocalRegistry):
        closed = False

        def close(self) -> None:
            self.closed = True

    registry = CloseableRegistry(lambda: "test-f-0000000001")
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=registry)

    def raise_after_opening_connection() -> None:
        with db:
            db.initialize()
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        raise_after_opening_connection()

    assert registry.closed


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
    # ResolvedFile is a discriminated union (LocalResolvedFile | ClarionResolvedFile),
    # so the branded identity types live on the members, pinned to the backend.
    local_hints = get_type_hints(LocalResolvedFile)
    clarion_hints = get_type_hints(ClarionResolvedFile)

    assert local_hints["file_id"] is FileId
    assert local_hints["content_hash"] == Literal[""]
    assert local_hints["registry_backend"] == Literal["local"]

    assert clarion_hints["file_id"] is EntityId
    assert clarion_hints["content_hash"] is ContentHash
    assert clarion_hints["registry_backend"] == Literal["clarion"]


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


def test_clarion_registry_reuses_http_connection_for_contiguous_resolves() -> None:
    connections: list[tuple[str, int]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            assert parsed.path == "/api/v1/files"
            connections.append(self.client_address)
            query = parse_qs(parsed.query)
            path = query["path"][0]
            language = query["language"][0]
            body = json.dumps(
                {
                    "entity_id": f"core:file:abc123@{path}",
                    "content_hash": f"sha256:{path}",
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
        registry = ClarionRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)

        registry.resolve_file("src/one.py", language="python")
        registry.resolve_file("src/two.py", language="python")

        assert len(connections) == 2
        assert len(set(connections)) == 1
    finally:
        close = getattr(registry, "close", None)
        if callable(close):
            close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_follows_redirects_for_single_file_resolution() -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            requests.append(parsed.path)
            if parsed.path == "/api/v1/files":
                self.send_response(307)
                self.send_header("Location", f"/redirected{self.path}")
                self.end_headers()
                return
            if parsed.path == "/redirected/api/v1/files":
                query = parse_qs(parsed.query)
                path = query.get("path", [""])[0]
                language = query.get("language", [""])[0]
                body = json.dumps(
                    {
                        "entity_id": f"core:file:redirected@{path}",
                        "content_hash": f"sha256:{path}",
                        "canonical_path": path,
                        "language": language,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        registry = ClarionRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)

        resolved = registry.resolve_file("src/main.py", language="python")

        assert resolved["file_id"] == "core:file:redirected@src/main.py"
        assert requests == ["/api/v1/files", "/redirected/api/v1/files"]
    finally:
        close = getattr(registry, "close", None)
        if callable(close):
            close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_follows_redirects_for_batch_resolution() -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            requests.append(parsed.path)
            if parsed.path == "/api/v1/files/batch":
                self.send_response(307)
                self.send_header("Location", "/redirected/api/v1/files/batch")
                self.end_headers()
                return
            if parsed.path == "/redirected/api/v1/files/batch":
                length = int(self.headers.get("Content-Length", "0") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                resolved = [
                    {
                        "requested_path": query["path"],
                        "entity_id": f"core:file:redirected@{query['path']}",
                        "content_hash": f"sha256:{query['path']}",
                        "canonical_path": query["path"],
                        "language": query.get("language", ""),
                    }
                    for query in payload["queries"]
                ]
                body = json.dumps({"resolved": resolved, "not_found": [], "briefing_blocked": [], "errors": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        registry = ClarionRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)

        batch = registry.resolve_files_batch([BatchQuery(path="src/main.py", language="python")])

        assert batch["resolved"]["src/main.py"]["file_id"] == "core:file:redirected@src/main.py"
        assert requests == ["/api/v1/files/batch", "/redirected/api/v1/files/batch"]
    finally:
        close = getattr(registry, "close", None)
        if callable(close):
            close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_retries_transient_5xx_before_success(caplog: pytest.LogCaptureFixture) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            requests.append(parse_qs(parsed.query))
            if len(requests) == 1:
                self.send_error(503, "warming up")
                return
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
        caplog.set_level(logging.WARNING, logger="filigree.registry")
        registry = ClarionRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)

        resolved = registry.resolve_file("src/main.py", language="python")

        assert requests == [
            {"path": ["src/main.py"], "language": ["python"]},
            {"path": ["src/main.py"], "language": ["python"]},
        ]
        assert resolved["file_id"] == "core:file:abc123@src/main.py"
        retry_records = [
            record for record in caplog.records if record.message == "Retrying Clarion registry request after transient failure"
        ]
        assert len(retry_records) == 1
        retry_extra = vars(retry_records[0])
        assert retry_extra["attempt"] == 1
        assert retry_extra["next_attempt"] == 2
        assert retry_extra["cause_kind"] == "http_error"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("base_url", ["ftp://clarion.test", "http://", "localhost:9111", "http:///api"])
def test_clarion_registry_rejects_invalid_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        ClarionRegistry(base_url)


@pytest.mark.parametrize("timeout_seconds", [0, -1, False, "slow"])
def test_clarion_registry_rejects_invalid_timeout_seconds(timeout_seconds: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ClarionRegistry("http://clarion.test", timeout_seconds=timeout_seconds)  # type: ignore[arg-type]


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
    requests = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal requests
            requests += 1
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
        assert requests == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_local_registry_resolve_files_batch_returns_per_query_resolution() -> None:
    """CONTRACT-1: LocalRegistry mints local IDs for every query, no per-item failure paths."""
    issued = iter(f"local-{i:04d}" for i in range(100))

    def make_id() -> str:
        return next(issued)

    registry = LocalRegistry(make_id)
    batch = registry.resolve_files_batch(
        [BatchQuery(path="a.py", language="python"), BatchQuery(path="b.py", language=""), BatchQuery(path="a.py", language="python")]
    )

    # Deduplicated by path.
    assert set(batch["resolved"].keys()) == {"a.py", "b.py"}
    assert batch["not_found"] == []
    assert batch["briefing_blocked"] == []
    assert batch["errors"] == []


def test_clarion_registry_resolve_files_batch_chunks_at_256() -> None:
    """CONTRACT-1: 300 queries → 2 HTTP POSTs (chunks 256 + 44)."""
    from tests._fakes.clarion_http import clarion_stub

    paths = [f"src/file_{i:04d}.py" for i in range(300)]
    queries = [BatchQuery(path=p, language="python") for p in paths]
    with clarion_stub() as (base_url, state):
        registry = ClarionRegistry(base_url, timeout_seconds=2)

        batch = registry.resolve_files_batch(queries)

    assert len(batch["resolved"]) == 300
    assert len(state.batch_requests) == 2
    assert len(state.batch_requests[0]["queries"]) == CLARION_BATCH_MAX_QUERIES
    assert len(state.batch_requests[1]["queries"]) == 300 - CLARION_BATCH_MAX_QUERIES


def test_clarion_registry_resolve_files_batch_separates_resolved_and_briefing_blocked() -> None:
    """CONTRACT-1: structured response splits ``resolved`` from ``briefing_blocked``."""
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub() as (base_url, state):
        state.briefing_blocked_paths.add("src/secrets.py")
        registry = ClarionRegistry(base_url, timeout_seconds=2)

        batch = registry.resolve_files_batch(
            [
                BatchQuery(path="src/ok.py", language="python"),
                BatchQuery(path="src/secrets.py", language="python"),
            ]
        )

    assert set(batch["resolved"].keys()) == {"src/ok.py"}
    assert batch["briefing_blocked"] == ["src/secrets.py"]
    assert batch["errors"] == []


def test_clarion_registry_rejects_batch_response_missing_requested_path() -> None:
    """Every requested path must appear in exactly one batch result channel."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps({"resolved": [], "not_found": [], "briefing_blocked": [], "errors": []}).encode()
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

        with pytest.raises(RegistryUnavailableError, match="missing outcome") as exc_info:
            registry.resolve_files_batch([BatchQuery(path="src/missing-outcome.py", language="python")])

        assert exc_info.value.cause_kind == "invalid_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "resolved": [
                    {
                        "requested_path": "src/other.py",
                        "entity_id": "core:file:abc123@src/other.py",
                        "content_hash": "sha256:abc123",
                        "canonical_path": "src/other.py",
                        "language": "python",
                    }
                ],
                "not_found": [],
                "briefing_blocked": [],
                "errors": [],
            },
            "unexpected path",
        ),
        (
            {
                "resolved": [
                    {
                        "requested_path": "src/requested.py",
                        "entity_id": "core:file:abc123@src/requested.py",
                        "content_hash": "sha256:abc123",
                        "canonical_path": "src/requested.py",
                        "language": "python",
                    }
                ],
                "not_found": ["src/requested.py"],
                "briefing_blocked": [],
                "errors": [],
            },
            "multiple result channels",
        ),
    ],
)
def test_clarion_registry_rejects_batch_response_with_ambiguous_path_outcomes(
    payload: dict[str, object],
    message: str,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps(payload).encode()
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

        with pytest.raises(RegistryUnavailableError, match=message) as exc_info:
            registry.resolve_files_batch([BatchQuery(path="src/requested.py", language="python")])

        assert exc_info.value.cause_kind == "invalid_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("not_found", "src/missing.py", "not_found"),
        ("briefing_blocked", {"path": "src/secrets.py"}, "briefing_blocked"),
        ("errors", [{"requested_path": "src/x.py", "code": "ERR"}], "errors"),
    ],
)
def test_clarion_registry_rejects_malformed_batch_failure_channels(
    field: str,
    value: object,
    message: str,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload: dict[str, object] = {
                "resolved": [],
                "not_found": [],
                "briefing_blocked": [],
                "errors": [],
            }
            payload[field] = value
            body = json.dumps(payload).encode()
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

        with pytest.raises(RegistryUnavailableError, match=message) as exc_info:
            registry.resolve_files_batch([BatchQuery(path="src/x.py", language="python")])

        assert exc_info.value.cause_kind == "invalid_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_batch_http_403_briefing_blocked_bypasses_unavailable_fallback() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps({"error": "blocked", "code": "BRIEFING_BLOCKED"}).encode()
            self.send_response(403)
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

        with pytest.raises(RegistryBriefingBlockedError) as exc_info:
            registry.resolve_files_batch([BatchQuery(path="src/secrets.py", language="python")])

        assert exc_info.value.status_code == 403
        assert not isinstance(exc_info.value, RegistryUnavailableError)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_batch_http_400_is_resolution_error_not_unavailable() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps({"error": "bad request", "code": "BAD_BATCH"}).encode()
            self.send_response(400)
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

        with pytest.raises(RegistryResolutionError) as exc_info:
            registry.resolve_files_batch([BatchQuery(path="src/bad.py", language="python")])

        assert exc_info.value.status_code == 400
        assert not isinstance(exc_info.value, RegistryUnavailableError)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_sends_no_authorization_header_when_no_token_configured() -> None:
    """CONTRACT-2 baseline: with ``auth_token=None`` (default), no
    Authorization header is sent — Clarion's loopback bind accepts."""
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub() as (base_url, state):
        registry = ClarionRegistry(base_url, timeout_seconds=1)

        resolved = registry.resolve_file("src/x.py", language="python")

    assert resolved["registry_backend"] == "clarion"
    # capability probe is not invoked by ClarionRegistry itself; the only
    # request issued here was /api/v1/files, so exactly one header tracked.
    assert state.auth_headers_seen == [None]


def test_clarion_registry_sends_bearer_authorization_when_token_provided() -> None:
    """CONTRACT-2 happy path: token set → ``Authorization: Bearer <token>``
    on every outbound request."""
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub() as (base_url, state):
        state.required_token = "test-loom-token"  # noqa: S105 — test fixture
        registry = ClarionRegistry(base_url, timeout_seconds=1, auth_token="test-loom-token")  # noqa: S106 — test fixture

        resolved = registry.resolve_file("src/x.py", language="python")
        registry.resolve_file("src/y.py", language="python")

    assert resolved["registry_backend"] == "clarion"
    assert state.auth_headers_seen == ["Bearer test-loom-token", "Bearer test-loom-token"]


def test_clarion_registry_maps_401_to_registry_unavailable_with_auth_cause_kind() -> None:
    """CONTRACT-2: wrong token → Clarion returns 401 → ``RegistryUnavailableError``
    with ``cause_kind="auth"`` so fallback policy can engage uniformly."""
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub() as (base_url, state):
        state.required_token = "expected-token"  # noqa: S105 — test fixture
        registry = ClarionRegistry(base_url, timeout_seconds=1, auth_token="wrong-token")  # noqa: S106 — test fixture

        with pytest.raises(RegistryUnavailableError) as exc_info:
            registry.resolve_file("src/x.py", language="python")

    assert exc_info.value.cause_kind == "auth"
    assert "401" in str(exc_info.value)


def test_clarion_registry_raises_briefing_blocked_on_403_with_code() -> None:
    """CONTRACT-3: Clarion 1.0 returns 403 + ``{"code": "BRIEFING_BLOCKED"}``
    for paths it intentionally withholds. Filigree must raise
    ``RegistryBriefingBlockedError`` — a separate class that the fallback
    wrapper does NOT swallow.
    """
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub() as (base_url, state):
        state.briefing_blocked_paths.add("src/secrets.py")
        registry = ClarionRegistry(base_url, timeout_seconds=1)

        with pytest.raises(RegistryBriefingBlockedError) as exc_info:
            registry.resolve_file("src/secrets.py", language="python")

    assert exc_info.value.status_code == 403
    assert "briefing-blocked" in str(exc_info.value)
    # Inheritance chain: a caller that catches RegistryResolutionError still
    # sees this, but a caller that catches only RegistryUnavailableError
    # (the fallback wrapper) does NOT — which is the whole point.
    assert isinstance(exc_info.value, RegistryResolutionError)
    assert not isinstance(exc_info.value, RegistryUnavailableError)
    assert not isinstance(exc_info.value, RegistryFileNotFoundError)


def test_clarion_registry_treats_403_without_code_as_generic_resolution_error() -> None:
    """A bare 403 (no ``code: BRIEFING_BLOCKED`` body) must NOT be promoted
    to ``RegistryBriefingBlockedError`` — that would mis-attribute an
    auth/policy refusal as a briefing block.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"error": "access denied", "code": "FORBIDDEN"}).encode()
            self.send_response(403)
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

        with pytest.raises(RegistryResolutionError) as exc_info:
            registry.resolve_file("src/main.py", language="python")
        assert not isinstance(exc_info.value, RegistryBriefingBlockedError)
        assert exc_info.value.status_code == 403
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


def test_clarion_registry_wraps_invalid_entity_id_as_invalid_response() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(
                {
                    "entity_id": " ",
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

        with pytest.raises(RegistryUnavailableError, match="entity_id") as exc_info:
            registry.resolve_file("src/main.py", language="python")

        assert exc_info.value.path == "src/main.py"
        assert exc_info.value.cause_kind == "invalid_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_clarion_registry_wraps_batch_invalid_entity_id_as_invalid_response() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps(
                {
                    "resolved": [
                        {
                            "requested_path": "src/main.py",
                            "entity_id": " ",
                            "content_hash": "sha256:abc123",
                            "canonical_path": "src/main.py",
                            "language": "python",
                        }
                    ],
                    "not_found": [],
                    "briefing_blocked": [],
                    "errors": [],
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

        with pytest.raises(RegistryUnavailableError, match="entity_id") as exc_info:
            registry.resolve_files_batch([BatchQuery(path="src/main.py", language="python")])

        assert exc_info.value.cause_kind == "invalid_response"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_filigree_db_composes_clarion_registry_when_configured(tmp_path: Path) -> None:
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub() as (base_url, _state):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 1},
        )
        try:
            db.initialize()

            file_record = db.register_file("src/main.py", language="python")

            assert file_record.id == "core:file:stub@src/main.py"
            assert file_record.content_hash == "sha256:src/main.py"
            assert file_record.registry_backend == "clarion"
            assert db.registry.is_displaced() is True
        finally:
            db.close()


def test_filigree_db_allow_local_fallback_tries_clarion_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolutions: list[str] = []

    class FakeClarionRegistry:
        def __init__(self, base_url: str, *, timeout_seconds: float = 5, auth_token: str | None = None) -> None:
            self.base_url = base_url
            self.timeout_seconds = timeout_seconds
            self.auth_token = auth_token

        def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
            resolutions.append(path)
            return cast(
                ResolvedFile,
                {
                    "file_id": "core:file:clarion-first@src/fallback.py",
                    "content_hash": "sha256:clarion-first",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                },
            )

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
        def __init__(self, base_url: str, *, timeout_seconds: float = 5, auth_token: str | None = None) -> None:
            self.base_url = base_url
            self.timeout_seconds = timeout_seconds
            self.auth_token = auth_token

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
        # CONTRACT-1: scan-results now goes through resolve_files_batch, so
        # the fallback WARN message is the batch-resolve variant.
        assert "using local file registry fallback" in caplog.text
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
            # Test exercises hybrid-state detection logic on initialize(),
            # not the capability handshake — skip the probe so the
            # unreachable URL doesn't abort __init__.
            skip_clarion_capability_probe=True,
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
