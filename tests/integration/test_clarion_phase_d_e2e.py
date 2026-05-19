"""ADR-014 Phase D — end-to-end Filigree ↔ live Clarion HTTP read API.

Closes review item F-2: the existing
``tests/api/test_registry_backend_integration.py`` exercises only the
ThreadingHTTPServer stub. This file boots an actual ``clarion`` binary
on the loopback interface and verifies that ``POST /api/loom/scan-results``
threads Clarion's entity ID (``core:file:...``) into the stored
``file_records.id``.

The test is opt-in by environment because:
    1. The ``clarion`` CLI may not be on PATH in every contributor's
       workstation or CI lane.
    2. The Clarion build on PATH may predate the HTTP read API.

Skipping rules:
    - ``shutil.which("clarion") is None``: skip.
    - The installed binary does not accept ``serve --path`` or the spawned
      process fails to bind the configured HTTP port: skip with a clear
      reason.

Local-run instructions are in
``docs/federation/registry-backend-launch-runbook.md`` under "Fresh
Project Setup".
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("clarion") is None,
        reason="clarion CLI is not on PATH; install Clarion to run this integration test",
    ),
]


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_capabilities(base_url: str, *, timeout: float = 15.0) -> dict[str, object]:
    """Poll Clarion's ``_capabilities`` endpoint until it responds or we timeout."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(f"{base_url}/api/v1/_capabilities"), timeout=1) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # polling loop swallows everything until deadline
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Clarion HTTP read API did not come up at {base_url}: {last_error}")


@contextmanager
def _spawn_clarion_serve(project_root: Path) -> Iterator[str]:
    """Run ``clarion install`` then spawn ``clarion serve`` with HTTP enabled.

    Yields the loopback base URL Filigree should point at. The subprocess
    is terminated on context exit; closing its stdin shuts down the MCP
    stdio half cleanly, which in turn shuts the HTTP half.
    """
    install = subprocess.run(
        ["clarion", "install", "--path", str(project_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        pytest.skip(f"clarion install failed — installed binary may be too old for this test (stderr: {install.stderr.strip()!r})")

    port = _free_loopback_port()
    bind = f"127.0.0.1:{port}"
    (project_root / "clarion.yaml").write_text(f'version: 1\nserve:\n  http:\n    enabled: true\n    bind: "{bind}"\n')

    proc = subprocess.Popen(
        ["clarion", "serve", "--path", str(project_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://{bind}"
    try:
        try:
            capabilities = _wait_for_capabilities(base_url)
        except RuntimeError as exc:
            # Capture stderr so the skip explains what went wrong.
            proc.terminate()
            try:
                _stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _stdout, stderr = proc.communicate()
            pytest.skip(
                f"clarion serve did not start an HTTP listener on {base_url}: {exc}; "
                f"stderr={stderr.decode('utf-8', errors='replace')[:500]!r}"
            )
        # ADR-014 F-1 shape: ``api_version: int`` and ``instance_id: str``
        # must both be present. Older Clarion builds advertise a different
        # shape (e.g. ``{"version": "0.1"}``) — skip rather than fail in
        # that case, since the test's intent is "verify against a Clarion
        # build that ships the F-1 handshake."
        if not isinstance(capabilities.get("api_version"), int) or not isinstance(capabilities.get("instance_id"), str):
            pytest.skip(
                "clarion CLI on PATH predates ADR-014 F-1 (no api_version / instance_id "
                f"in /api/v1/_capabilities response: {capabilities!r}). Rebuild Clarion "
                "from a tip that includes the F-1 handshake to run this test."
            )
        yield base_url
    finally:
        # Closing stdin lets MCP shut down cleanly; if that hangs, escalate.
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


async def _post_scan_results(db: FiligreeDB, *, path: str) -> dict[str, object]:
    dash_module._db = db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/loom/scan-results",
            json={
                "scan_source": "ruff",
                "findings": [
                    {"path": path, "rule_id": "E501", "severity": "low", "message": "msg"},
                ],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()


async def test_filigree_resolves_file_identity_via_live_clarion_serve(tmp_path: Path) -> None:
    """End-to-end: real `clarion serve` HTTP read API resolves Filigree file IDs."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    source = project_root / "src" / "phase_d.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('phase d')\n")

    with _spawn_clarion_serve(project_root) as base_url:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 5},
            project_root=project_root,
        )
        db.initialize()
        try:
            assert db.clarion_instance_id is not None, "capability probe should have populated state"
            assert db.clarion_api_version is not None

            await _post_scan_results(db, path="src/phase_d.py")

            file_record = db.get_file_by_path("src/phase_d.py")
            assert file_record is not None
            assert file_record.id.startswith("core:file:"), f"Expected Clarion entity ID prefix, got {file_record.id!r}"
            assert file_record.registry_backend == "clarion"
            assert file_record.content_hash, "Clarion should have supplied a non-empty content_hash"
        finally:
            dash_module._db = None
            db.close()
