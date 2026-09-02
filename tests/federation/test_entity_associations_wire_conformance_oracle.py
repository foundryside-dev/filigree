"""Producer-side entity-associations wire conformance oracle.

Filigree is the PRODUCER of the ``GET /api/entity-associations?entity_id=…``
reverse-lookup response (ADR-029 / Weft G15): given an opaque Loomweave entity
ID, it returns every issue in the project bound to that entity, enriched with the
bound issue's lifecycle facts. Loomweave is the CONSUMER — its ``issues_for``
reverse-join calls that endpoint and ``parse_entity_associations_response``
(``loomweave-federation/src/filigree.rs``) deserializes the body. Both repos
vendor the same fixture byte-identical
(``filigree/tests/fixtures/contracts/entity-associations-response.json`` ==
``loomweave/docs/federation/fixtures/filigree-entity-associations-response.json``);
filigree is the authority for that body.

This module freezes filigree's produced wire to the committed golden + a
PRODUCER-SOURCE recheck that is NOT circular: it drives filigree's REAL
``GET /api/entity-associations`` route over the live ASGI app, seeds with values
this test owns (NOT the golden's), and asserts the produced per-row *key shape*
equals the golden's per-row key shape — plus the derivations the real enrichment
performs (``status`` → ``status_category``, and the four lifecycle/commit anchors
that are null for an open, un-claimed, un-closed issue). The structure comes from
real code (``db.list_associations_by_entity`` → ``_row_to_by_entity_association``
+ ``_resolve_status_category`` LEFT-JOIN enrichment, wrapped by the route into
``{"associations": [...]}``); the golden supplies the expected shape; the
self-owned seeds prove it is the live handler, not the golden echoed at itself.

Mirrors the federation oracle layout
(``test_scan_results_wire_conformance_oracle.py`` /
``test_suppression_filter_conformance_oracle.py``):

- **Layer 1 — byte-pin (default suite).** ``UPSTREAM_BLOB_SHA`` is the git-blob
  sha of the vendored golden; an unmarked test recomputes it from bytes, so any
  edit to the fixture reds immediately, in every CI run.
- **Producer wire oracle (the non-circular core, default suite).** Drives the
  REAL ``GET /api/entity-associations`` handler and asserts the produced shape
  ties to the golden's shape.

Filigree is the authority here, so — unlike the consumer oracles — there is NO
external Layer-2 drift-check (nothing upstream to point at). The *reverse* check —
that Loomweave's vendored copy is byte-identical to filigree's authority — lives
in ``test_sibling_drift.py`` (registry entry ``entity_associations``,
direction ``downstream``) so a drifted consumer copy is caught there.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app
from tests.federation._oracle import blob_sha, load_golden

pytestmark = pytest.mark.federation_contract

# The vendored authoritative entity-associations reverse-lookup response. Filigree
# is the producer/authority for this body; Loomweave vendors a byte-identical copy.
GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "entity-associations-response.json"

# Git-blob sha1 of the vendored golden (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_vendored_golden_byte_pin`` so any edit to the
# fixture reds in the default suite, on every CI run.
UPSTREAM_BLOB_SHA = "387979a54c3d8020369d0f16d04a617e1b6749e1"


def _golden_example_body(example_name: str) -> dict[str, Any]:
    golden = load_golden(GOLDEN_PATH)
    for example in golden["examples"]:
        if example["name"] == example_name:
            body: dict[str, Any] = example["response"]["body"]
            return body
    raise AssertionError(f"missing golden example {example_name}")


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_golden_byte_pin() -> None:
    """The vendored golden's bytes hash to the pinned git-blob sha.

    A single-byte edit to the fixture changes the sha and reds this test in the
    default suite — the cheapest possible drift tripwire, no sibling repo needed.
    """
    assert blob_sha(GOLDEN_PATH.read_bytes()) == UPSTREAM_BLOB_SHA


# ---------------------------------------------------------------------------
# Producer wire oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


async def _drive_reverse_lookup(db: FiligreeDB, entity_id: str) -> dict[str, Any]:
    """Issue the REAL ``GET /api/entity-associations`` over the live ASGI app.

    This is the exact route the consumer (Loomweave ``issues_for``) calls:
    ``api_list_associations_by_entity`` → ``db.list_associations_by_entity`` →
    ``_row_to_by_entity_association`` (the LEFT-JOIN lifecycle enrichment) wrapped
    into ``{"associations": [...]}``. ``dash_module._db`` is process-global mutable
    state shared with other tests, so the caller resets it in a ``finally``.
    """
    dash_module._db = db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/entity-associations", params={"entity_id": entity_id})
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
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    db.reconnect(check_same_thread=False)
    try:
        yield db
    finally:
        dash_module._db = None
        db.close()


async def test_real_handler_produces_golden_row_shape(producer_db: FiligreeDB) -> None:
    """Drive the REAL reverse-lookup handler and tie its produced row shape to the
    golden — seeding values this test OWNS, not the golden's, so the assertion can
    only pass because the live handler genuinely emits this key shape.

    Non-circular: the seeds (entity id, content hash, actor, issue id) are unique to
    this test; the golden contributes only the expected *key set* of a produced row.
    A producer that dropped, renamed, or stopped emitting any wire field — or that
    grew an un-sanctioned extra key — reds here, because the produced row's key set
    would no longer equal the golden row's key set.
    """
    golden_body = _golden_example_body("live_v27_reverse_lookup_200")
    golden_row = golden_body["associations"][0]
    golden_keys = set(golden_row)

    # Self-owned seeds — deliberately NOT the golden's values.
    entity_id = "loomweave:eid:ffffffffffffffffffffffffffffffff"
    content_hash = "oracle-producer-hash"
    actor = "wire-oracle"
    issue_id = "test-wireoracle1"

    # Seed an OPEN task issue with NO claim/close (so the lifecycle + commit anchors
    # the enrichment LEFT-JOINs are exercised in their null branch) and attach one
    # binding to our entity id through the real data layer.
    producer_db.conn.execute(
        "INSERT INTO issues (id, title, status, priority, type, created_at, updated_at, fields) "
        "VALUES (?, 'wire oracle issue', 'open', 2, 'task', '2026-06-24T00:00:00+00:00', "
        "'2026-06-24T00:00:00+00:00', '{}')",
        (issue_id,),
    )
    producer_db.conn.commit()
    producer_db.add_entity_association(issue_id, entity_id, content_hash=content_hash, entity_kind="function", actor=actor)

    body = await _drive_reverse_lookup(producer_db, entity_id)

    # The top-level envelope the consumer keys on.
    assert set(body) == {"associations"}
    assert len(body["associations"]) == 1
    produced_row = body["associations"][0]

    # The load-bearing assertion: the live handler's produced row carries EXACTLY
    # the golden's key set — no field dropped, none renamed, no un-sanctioned extra.
    assert set(produced_row) == golden_keys, (
        f"produced row key shape drifted from the golden:\n"
        f"  missing (golden has, producer dropped): {golden_keys - set(produced_row)}\n"
        f"  extra (producer added, golden lacks):   {set(produced_row) - golden_keys}"
    )

    # Our self-owned seeds round-tripped through the real producer.
    assert produced_row["entity_id"] == entity_id
    assert produced_row["loomweave_entity_id"] == entity_id
    assert produced_row["content_hash_at_attach"] == content_hash
    assert produced_row["attached_by"] == actor
    assert produced_row["entity_kind"] == "function"

    # Prove the LEFT-JOIN lifecycle enrichment actually RAN (not a static echo):
    # the seeded issue is an open task, so ``status``/``status_category`` derive to
    # ``open``, while the four un-claimed/un-closed lifecycle + commit anchors are
    # null — exactly the golden's open-issue projection.
    assert produced_row["status"] == "open"
    assert produced_row["status_category"] == "open"
    assert produced_row["claimed_at"] is None
    assert produced_row["closed_at"] is None
    assert produced_row["claim_commit"] is None
    assert produced_row["close_commit"] is None

    # The two producer-DERIVED enrichment defaults this oracle owns end-to-end
    # (so the file is self-contained on every producer-computed field, not silently
    # leaning on the api-suite to defeat circularity for these keys):
    #   * orphan_status — the seeded binding has no ``migration_orphaned_at``, so the
    #     ADR-017 non-orphaned case derives to ``unknown`` (deliberately NOT ``active``;
    #     a regression flipping that default would otherwise ship green through here).
    #   * freshness_status — the reverse-lookup passes no ``current_content_hash``, so
    #     the join-key freshness derives to ``unknown`` (the un-resolvable branch).
    assert produced_row["orphan_status"] == "unknown"
    assert produced_row["freshness_status"] == "unknown"
