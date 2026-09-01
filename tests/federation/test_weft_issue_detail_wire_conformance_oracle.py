"""Producer-side weft issue-detail wire conformance oracle.

Filigree is the PRODUCER/authority of the ``GET /api/weft/issues/{issue_id}``
issue-detail response (weft generation, ADR-002 / Phase C3). The route handler
``api_weft_get_issue`` (``filigree.dashboard_routes.issues``) projects the stored
issue through ``issue_to_weft`` (``filigree.generations.weft.adapters``) into the
``IssueWeft`` shape — the weft vocabulary renames the entity's OWN primary key
``id`` → ``issue_id`` while reference fields (``parent_id``, ``blocks``,
``blocked_by``, ``children``) keep their classic names. Federation consumers
(Loomweave) read this body; filigree is the sole source of truth for its shape,
which is why the golden lives here only — there is no externally-vendored consumer
copy to drift-check against (contrast the consumer oracles).

This module freezes filigree's produced wire to the committed golden + a
PRODUCER-SOURCE recheck that is NOT circular: it drives filigree's REAL
``GET /api/weft/issues/{issue_id}`` route over the live ASGI app, seeds with
values this test OWNS (NOT the golden's), and asserts the produced body's *key
shape* equals the golden body's key shape — plus the contract's load-bearing
``id`` → ``issue_id`` rename and the two producer-DERIVED projections
(``status`` → ``status_category``, and ``is_ready`` computed from blockers). The
structure comes from real code (``db.get_issue`` → ``issue_to_weft`` wrapped by
the route); the golden supplies the expected key set; the self-owned seeds prove
it is the live handler, not the golden echoed at itself.

Mirrors the federation oracle layout
(``test_entity_associations_wire_conformance_oracle.py`` /
``test_scan_results_wire_conformance_oracle.py``):

- **Layer 1 — byte-pin (default suite).** ``UPSTREAM_BLOB_SHA`` is the git-blob
  sha of the committed golden; an unmarked test recomputes it from bytes, so any
  edit to the fixture reds immediately, in every CI run.
- **Producer wire oracle (the non-circular core, default suite).** Drives the
  REAL ``GET /api/weft/issues/{issue_id}`` handler over the live ASGI app and
  asserts the produced body's shape ties to the golden's shape, with self-owned
  seeds defeating circularity.

Filigree is the authority here, so — unlike the consumer oracles — there is NO
external Layer-2 drift-check (nothing upstream to point at) and no reverse
consumer-copy recheck (Loomweave vendors no copy of this body).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app

# The committed authoritative weft issue-detail contract fixture. Filigree is the
# producer/authority for this body; no external consumer copy is vendored.
GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "weft" / "issues-get.json"

# Git-blob sha1 of the committed golden (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_golden_byte_pin`` so any edit to the fixture
# reds in the default suite, on every CI run.
UPSTREAM_BLOB_SHA = "a5561853c4192c7abe7bcc4fba64b07d4032a8a6"


def _blob_sha(data: bytes) -> str:
    """git's blob object id for ``data``: ``sha1(b"blob <len>\\0" + data)``.

    ``usedforsecurity=False`` is honest — this is content addressing (git's own
    object-id scheme), not a security primitive — and keeps ruff's S324 quiet
    without a per-line suppression.
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _load_golden() -> dict[str, Any]:
    golden: dict[str, Any] = json.loads(GOLDEN_PATH.read_text())
    return golden


def _golden_example_body(example_name: str) -> dict[str, Any]:
    golden = _load_golden()
    for example in golden["examples"]:
        if example["name"] == example_name:
            body: dict[str, Any] = example["response"]["body"]
            return body
    raise AssertionError(f"missing golden example {example_name}")


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_golden_byte_pin() -> None:
    """The committed golden's bytes hash to the pinned git-blob sha.

    A single-byte edit to the fixture changes the sha and reds this test in the
    default suite — the cheapest possible drift tripwire, no sibling repo needed.
    """
    assert _blob_sha(GOLDEN_PATH.read_bytes()) == UPSTREAM_BLOB_SHA


# ---------------------------------------------------------------------------
# Producer wire oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


async def _drive_issue_detail(db: FiligreeDB, issue_id: str) -> dict[str, Any]:
    """Issue the REAL ``GET /api/weft/issues/{issue_id}`` over the live ASGI app.

    This is the exact route the consumer (Loomweave) calls: ``api_weft_get_issue``
    → ``db.get_issue`` → ``issue_to_weft`` (the ``id`` → ``issue_id`` weft
    projection). ``dash_module._db`` is process-global mutable state shared with
    other tests, so the caller resets it in a ``finally``.
    """
    dash_module._db = db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/weft/issues/{issue_id}")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


@pytest.fixture
async def producer_db(tmp_path: Path) -> AsyncIterator[FiligreeDB]:
    """A fresh single-project DB wired into the dashboard's global slot.

    ``reconnect(check_same_thread=False)`` so the sync route handler may run in
    FastAPI's threadpool. Resets the shared ``dash_module._db`` slot and closes the
    DB on teardown so this oracle leaves no global state behind for sibling tests.
    """
    db = FiligreeDB(tmp_path / "filigree.db", prefix="genp")
    db.initialize()
    db.reconnect(check_same_thread=False)
    try:
        yield db
    finally:
        dash_module._db = None
        db.close()


async def test_real_handler_produces_golden_issue_detail_shape(producer_db: FiligreeDB) -> None:
    """Drive the REAL issue-detail handler and tie its produced body shape to the
    golden — seeding values this test OWNS, not the golden's, so the assertion can
    only pass because the live handler genuinely emits this key shape.

    Non-circular: the seeds (issue id, title, timestamps) are unique to this test
    and deliberately differ from the golden's example values; the golden contributes
    only the expected *key set* of the produced body. A producer that dropped,
    renamed, or stopped emitting any wire field — or that grew an un-sanctioned
    extra key — reds here, because the produced body's key set would no longer equal
    the golden body's key set.
    """
    golden_body = _golden_example_body("live_v_issue_detail_200")
    golden_keys = set(golden_body)

    # Self-owned seeds — deliberately NOT the golden's values (golden uses
    # ``genp-0123456789`` / ``golden issue detail`` / ``2026-01-02…``).
    issue_id = "genp-wireoracle1"
    title = "wire oracle seeded issue"
    created_at = "2026-06-24T00:00:00+00:00"

    # Seed an OPEN task issue with NO claim/close (so the lifecycle + commit anchors
    # are exercised in their null branch and ``is_ready`` is true — no open blockers).
    producer_db.conn.execute(
        "INSERT INTO issues (id, title, status, priority, type, created_at, updated_at, "
        "fields, description, notes, assignee) "
        "VALUES (?, ?, 'open', 2, 'task', ?, ?, '{}', '', '', '')",
        (issue_id, title, created_at, created_at),
    )
    producer_db.conn.commit()

    body = await _drive_issue_detail(producer_db, issue_id)

    # The load-bearing assertion: the live handler's produced body carries EXACTLY
    # the golden's key set — no field dropped, none renamed, no un-sanctioned extra.
    assert set(body) == golden_keys, (
        f"produced issue-detail key shape drifted from the golden:\n"
        f"  missing (golden has, producer dropped): {golden_keys - set(body)}\n"
        f"  extra (producer added, golden lacks):   {set(body) - golden_keys}"
    )

    # The contract's whole reason to exist (per the fixture shape_decl): the weft
    # vocabulary renames the entity's OWN primary key ``id`` → ``issue_id``. A
    # regression that leaked the classic ``id`` or dropped the rename reds here.
    assert "id" not in body
    assert body["issue_id"] == issue_id

    # Our self-owned seeds round-tripped through the real producer.
    assert body["title"] == title
    assert body["created_at"] == created_at
    assert body["updated_at"] == created_at
    assert body["type"] == "task"
    assert body["priority"] == 2

    # Prove the projection actually RAN (not a static echo): the two producer-DERIVED
    # fields. ``status_category`` derives from ``status`` (open → open) and
    # ``is_ready`` is computed from blockers (none seeded → true).
    assert body["status"] == "open"
    assert body["status_category"] == "open"
    assert body["is_ready"] is True

    # The four un-claimed/un-closed lifecycle + commit anchors are null — exactly the
    # golden's open-issue projection.
    assert body["claimed_at"] is None
    assert body["last_heartbeat_at"] is None
    assert body["claim_expires_at"] is None
    assert body["closed_at"] is None
    assert body["claim_commit"] is None
    assert body["close_commit"] is None

    # The reference fields that hold OTHER issues' ids keep their classic names
    # (only the entity's own primary key is renamed) and default empty here.
    assert body["parent_id"] is None
    assert body["blocks"] == []
    assert body["blocked_by"] == []
    assert body["children"] == []
    assert body["data_warnings"] == []


async def _drive_issue_detail_status(db: FiligreeDB, issue_id: str) -> tuple[int, dict[str, Any]]:
    """Issue the REAL ``GET /api/weft/issues/{issue_id}`` and return ``(status, body)``.

    The 200-path sibling (``_drive_issue_detail``) asserts ``status_code == 200``;
    this variant returns the status so the 404 error-envelope path can be asserted
    against the golden's ``error_not_found`` example.
    """
    dash_module._db = db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/weft/issues/{issue_id}")
    body: dict[str, Any] = resp.json()
    return resp.status_code, body


async def test_real_handler_produces_golden_not_found_envelope(producer_db: FiligreeDB) -> None:
    """Drive the REAL handler for an ABSENT issue id and tie the 404 envelope to
    the golden's ``error_not_found`` example shape.

    The 200 oracle (``test_real_handler_produces_golden_issue_detail_shape``)
    never requests a missing id, so the golden's ``error_not_found`` example was
    frozen only by the Layer-1 byte digest, never tied to the live handler. This
    closes that authority-vs-golden gap on the producer side: ``db.get_issue``
    raises ``KeyError`` for an unknown id and ``api_weft_get_issue`` maps it to
    ``_error_response(..., NOT_FOUND, 404)``. The golden contributes the expected
    *key set* + the ``code`` literal; the id is self-owned (NOT the golden's), so
    the assertion can only pass because the live handler genuinely emits this
    envelope, not the golden echoed at itself.
    """
    golden_404 = _golden_example_body("error_not_found")
    golden_404_keys = set(golden_404)

    # Self-owned absent id — deliberately NOT the golden's ``genp-deadbeef00``.
    absent_id = "genp-nosuchissue9"

    status, body = await _drive_issue_detail_status(producer_db, absent_id)

    assert status == 404, f"expected 404 for an absent issue id, got {status}: {body!r}"
    # Same load-bearing key-shape contract as the 200 path: the produced error
    # envelope carries EXACTLY the golden's error-envelope key set.
    assert set(body) == golden_404_keys, (
        f"produced 404 envelope key shape drifted from the golden:\n"
        f"  missing (golden has, producer dropped): {golden_404_keys - set(body)}\n"
        f"  extra (producer added, golden lacks):   {set(body) - golden_404_keys}"
    )
    # The error code literal is the load-bearing machine-readable field; it must
    # match the golden's declared value (consumers switch on ``code``).
    assert body["code"] == golden_404["code"] == "NOT_FOUND"
    # The message echoes the self-owned absent id (proves the live handler ran,
    # not a static golden echo whose id is ``genp-deadbeef00``).
    assert body["error"] == f"Issue not found: {absent_id}"
