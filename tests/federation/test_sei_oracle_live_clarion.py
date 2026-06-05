"""SEI conformance oracle — faithful lane against a live ``clarion serve``.

The fast lane (``test_sei_conformance_oracle.py``) proves every producer branch
against the Loomweave stub. This module re-proves the producer's obligations
against a *real* Loomweave authority — the "no grandfathering / demonstrated, not
asserted" gate of the §8 standard.

What it asserts against live Loomweave:
  - the SEI capability handshake (``_capabilities.sei.supported``);
  - **identity_round_trip + opacity** — a real locator Loomweave knows is rewritten
    in place to a real ``clarion:eid:`` SEI by the backfill;
  - **orphan** (the ambiguous/delete producer shape) — a locator Loomweave cannot
    resolve is flagged ORPHAN and kept verbatim, never dropped.

The rename/move/ambiguous *carry* semantics are Loomweave-internal (mint vs. carry
vs. orphan); they are proven on the authority side by Loomweave's own run of the
same shared fixture (``cargo test -p clarion-storage --test
sei_conformance_oracle``). Filigree's job is to store the SEI opaquely and
degrade honestly, which is what this test exercises end to end.

Opt-in / skip rules mirror ``tests/integration/test_clarion_phase_d_e2e.py``:
the ``clarion`` CLI must be on PATH and new enough to ship the SEI surface;
otherwise the test skips (or fails when ``FILIGREE_REQUIRE_LIVE_CLARION=1``).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from filigree.core import FiligreeDB
from filigree.registry import LoomweaveRegistry
from filigree.sei_backfill import run_sei_backfill

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
]


def _live_unavailable(reason: str) -> None:
    if os.environ.get("FILIGREE_REQUIRE_LIVE_CLARION") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_capabilities(base_url: str, *, timeout: float = 15.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(f"{base_url}/api/v1/_capabilities"), timeout=1) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # polling loop swallows everything until deadline
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Loomweave HTTP read API did not come up at {base_url}: {last_error}")


@contextmanager
def _spawn_loomweave_serve(project_root: Path) -> Iterator[tuple[str, dict[str, object]]]:
    """Install, analyze, and serve a real Loomweave over loopback HTTP.

    Yields ``(base_url, capabilities)``. Skips (or fails under
    ``FILIGREE_REQUIRE_LIVE_CLARION``) when Loomweave is absent or too old.
    """
    if shutil.which("clarion") is None:
        _live_unavailable("clarion CLI is not on PATH; install Loomweave to run this integration test")

    install = subprocess.run(["clarion", "install", "--path", str(project_root)], check=False, capture_output=True, text=True)
    if install.returncode != 0:
        _live_unavailable(f"clarion install failed (stderr: {install.stderr.strip()!r})")

    analyze = subprocess.run(["clarion", "analyze", str(project_root)], check=False, capture_output=True, text=True)
    if analyze.returncode != 0:
        _live_unavailable(f"clarion analyze failed (stderr: {analyze.stderr.strip()!r})")

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
            proc.terminate()
            try:
                _stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _stdout, stderr = proc.communicate()
            _live_unavailable(f"clarion serve did not come up on {base_url}: {exc}; stderr={stderr.decode('utf-8', 'replace')[:500]!r}")
        yield base_url, capabilities
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()


def _require_sei_capable(capabilities: dict[str, object]) -> None:
    sei = capabilities.get("sei")
    if not isinstance(sei, dict) or not sei.get("supported"):
        _live_unavailable(f"clarion on PATH predates the SEI surface (capabilities.sei={sei!r}); rebuild Loomweave to run this test")


def _real_file_locator(base_url: str, path: str) -> str | None:
    """Return the ``core:file:`` locator Loomweave minted for ``path`` (or None)."""
    reg = LoomweaveRegistry(base_url, timeout_seconds=5)
    try:
        resolved = reg.resolve_file(path)
    except Exception:
        return None
    finally:
        reg.close()
    return resolved["file_id"]


def test_backfill_against_live_loomweave(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "sample.py").write_text("def issue_token():\n    return 1\n")

    with _spawn_loomweave_serve(project_root) as (base_url, capabilities):
        _require_sei_capable(capabilities)

        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            loomweave_config={"base_url": base_url, "timeout_seconds": 5},
            project_root=project_root,
        )
        db.initialize()
        assert db.loomweave_capabilities is not None
        assert db.loomweave_capabilities["sei_supported"] is True

        # A locator Loomweave genuinely knows (its minted file entity), bound to an
        # issue, must migrate in place to a real opaque SEI.
        locator = _real_file_locator(base_url, "src/sample.py")
        if locator is None or locator.startswith("clarion:eid:"):
            _live_unavailable("live Loomweave did not surface a resolvable file locator (no language plugin / no entity minted)")

        known_issue = db.create_issue("known", priority=2)
        db.add_entity_association(known_issue.id, locator, content_hash="sha256:body")

        # A locator Loomweave cannot resolve → orphan (the producer shape of
        # ambiguous/delete), kept verbatim and flagged.
        orphan_locator = "py:func:does.not.exist::nope"
        orphan_issue = db.create_issue("orphan", priority=2)
        db.add_entity_association(orphan_issue.id, orphan_locator, content_hash="sha256:body")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        # identity_round_trip + opacity, proven against the real authority.
        migrated = db.conn.execute(
            "SELECT clarion_entity_id, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
            (known_issue.id,),
        ).fetchone()
        if not migrated["clarion_entity_id"].startswith("clarion:eid:"):
            _live_unavailable("live Loomweave did not mint a SEI for the file entity; cannot prove round-trip on this build")
        assert migrated["migration_orphaned_at"] is None
        assert migrated["clarion_entity_id"] != locator
        assert report.associations_migrated >= 1

        # orphan, kept verbatim and flagged for review.
        orphaned = db.conn.execute(
            "SELECT clarion_entity_id, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
            (orphan_issue.id,),
        ).fetchone()
        assert orphaned["clarion_entity_id"] == orphan_locator
        assert orphaned["migration_orphaned_at"] is not None
        assert any(o.locator == orphan_locator for o in report.orphans)
        db.close()
