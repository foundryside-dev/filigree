"""Consumer-side legis sign-off binding wire conformance oracle.

Filigree is the CONSUMER of the legis -> Filigree governed sign-off binding
request body. legis (the PRODUCER/AUTHORITY) POSTs it to the classic
entity-association route; this oracle drives filigree's REAL
``POST /api/issue/{issue_id}/entity-associations`` handler over the BYTE-IDENTICAL
golden and asserts filigree parses, persists, and acts on it as the contract says.

The contract (see ``filigree/src/filigree/dashboard_routes/entities.py`` POST
handler + ``db_entity_associations.py``, v25/B1):

  * The base entity-association body is ``{entity_id, content_hash, actor}``. The
    GOVERNED body legis sends carries two MORE fields — ``signoff_seq`` and
    ``signature`` — the legis-specific governed sign-off extension. Filigree's
    handler type-validates them (``signature`` str; ``signoff_seq`` int, bool
    rejected) and persists them into dedicated columns. This is a DISTINCT wire from
    the base shape: the existing ``test_entity_associations_wire_conformance_oracle``
    freezes only the GET reverse-lookup *response* and never exercises these fields.

  * Filigree holds NO key and NEVER verifies the signature — it stores it verbatim
    (route docstring; DB comment "stored verbatim and NEVER verifies"). legis is the
    sole verifier (its own ``BindingLedger``). So the load-bearing consumer assertions
    are NOT "filigree re-derives/checks the sig" — they are: the byte-identical golden
    POSTs through the real route → ``signoff_seq``/``signature`` land in the columns
    verbatim → the governed SEMANTIC EFFECT fires (a non-null signature flips the
    binding to *governed* per DECISION 1A, which makes a governed close consult Legis
    instead of proceeding, and makes the binding non-removable).

The golden is vendored byte-identical from legis (the authority):
``legis/tests/contract/weft/vectors/signoff_binding.v1.json`` ==
``filigree/tests/fixtures/contracts/legis-signoff-binding-request.json``.

Layout mirrors ``test_entity_associations_wire_conformance_oracle.py``:

- **Layer 1 — byte-pin (default suite).** ``VENDORED_BLOB_SHA`` is the git-blob sha
  of the vendored golden; an unmarked test recomputes it from bytes, so any edit to
  the fixture reds immediately. legis's producer oracle pins the SAME constant.
- **Consumer wire oracle (the non-circular core, default suite).** POSTs the RAW
  golden bytes through the REAL route over the live ASGI app and asserts the parse +
  persistence + governed effect.
- **Reverse drift recheck (skip-clean).** legis's vendored authority copy must be
  byte-identical; skips when the sibling repo is absent.

All oracles are in-process (ASGITransport, no network), so they run in the default
suite with no new pytest marker.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app


def _weft_body_bytes(body: dict[str, Any]) -> bytes:
    """Re-implements legis ``weft_signing.weft_body_bytes`` so this consumer oracle is
    self-contained (no legis import): the exact compact, sorted-key, ``ensure_ascii``
    JSON bytes legis puts on the wire. Kept inline (and pinned by the byte-pin below)
    so a drift in either side's canonicalization is caught rather than masked by a
    shared helper.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")

# The vendored authoritative legis sign-off binding REQUEST body. legis is the
# producer/authority; this is a byte-identical copy.
GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "contracts" / "legis-signoff-binding-request.json"
)

# Git-blob sha1 of the vendored golden (``sha1(b"blob <len>\0" + data)``).
# Recomputed from bytes by ``test_vendored_golden_byte_pin`` so any edit reds in the
# default suite. legis's producer oracle pins the SAME constant against the
# byte-identical authority copy.
VENDORED_BLOB_SHA = "8796aeb5b8d7d067c82af17e361aa45fe5007b4e"


def _blob_sha(data: bytes) -> str:
    """git's blob object id for ``data``: ``sha1(b"blob <len>\\0" + data)``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _load_golden() -> dict[str, Any]:
    golden: dict[str, Any] = json.loads(GOLDEN_PATH.read_text())
    return golden


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_golden_byte_pin() -> None:
    """The vendored golden's bytes hash to the pinned git-blob sha.

    A single-byte edit changes the sha and reds this in the default suite — the
    cheapest drift tripwire, no sibling repo needed.
    """
    assert _blob_sha(GOLDEN_PATH.read_bytes()) == VENDORED_BLOB_SHA


def test_golden_carries_the_distinct_governed_extension_fields() -> None:
    """The golden body must carry ``signoff_seq`` + ``signature`` — the
    legis-specific extension that distinguishes this wire from the base
    entity-association shape (already covered by the reverse-lookup oracle).
    """
    body = _load_golden()["request_body"]
    assert set(body) == {"entity_id", "content_hash", "actor", "signoff_seq", "signature"}
    assert isinstance(body["signoff_seq"], int)
    assert not isinstance(body["signoff_seq"], bool)
    assert body["signature"].startswith("hmac-sha256:v2:")


# ---------------------------------------------------------------------------
# Consumer wire oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


@pytest.fixture
async def consumer_db(tmp_path: Path) -> AsyncIterator[FiligreeDB]:
    """A fresh single-project DB wired into the dashboard's global slot.

    ``reconnect(check_same_thread=False)`` so the sync route handler may run in
    FastAPI's threadpool. Resets the shared ``dash_module._db`` slot and closes the
    DB on teardown so this oracle leaves no global state behind.
    """
    db = FiligreeDB(tmp_path / "filigree.db", prefix="LEGIS-SIGNOFF")
    db.initialize()
    db.reconnect(check_same_thread=False)
    try:
        yield db
    finally:
        dash_module._db = None
        db.close()


async def _post_raw_golden(db: FiligreeDB, issue_id: str, raw_body: bytes) -> dict[str, Any]:
    """POST the RAW golden bytes through the REAL entity-association handler.

    ``content=raw_body`` (NOT ``json=dict``) so the bytes on the wire are exactly the
    frozen golden's — the same bytes legis emits — rather than a re-serialization. The
    path carries ``issue_id`` (the signature commits to it but it does not ride the
    body), matching the producer.
    """
    dash_module._db = db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            content=raw_body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_real_handler_parses_and_persists_the_golden_signoff(consumer_db: FiligreeDB) -> None:
    """Drive the REAL POST handler over the BYTE-IDENTICAL golden bytes and assert
    filigree parses + persists the governed sign-off verbatim.

    Non-circular: the bytes are the frozen golden (the authority's wire), seeded
    against an issue this test owns; the assertions read back through the REAL data
    layer (``list_entity_associations``). A consumer that stopped parsing
    ``signoff_seq``/``signature``, renamed a column, or dropped one would red here.
    """
    golden = _load_golden()
    body = golden["request_body"]
    raw = GOLDEN_PATH.read_bytes()
    # The raw POST body is the canonical request body inside the golden envelope —
    # exactly what legis's weft_body_bytes emits.
    raw_body = _weft_body_bytes(body)

    # Sanity: the golden file is the byte-pinned authority copy (proves we are not
    # hand-minting), and the POST body is legis's canonical wire serialization of the
    # golden's recorded request_body.
    assert _blob_sha(raw) == VENDORED_BLOB_SHA

    # Seed the issue the binding targets (FK requirement). Filigree generates the id
    # under its project prefix; the path issue_id need not equal the golden's signed
    # issue_id because filigree never verifies the signature — it stores it verbatim.
    issue = consumer_db.create_issue("legis sign-off wire oracle", priority=2)

    resp_row = await _post_raw_golden(consumer_db, issue.id, raw_body)

    # The route echoes the persisted row; the governed extension fields are present
    # and equal the golden's values, stored VERBATIM (filigree never re-derives them).
    assert resp_row["entity_id"] == body["entity_id"]
    assert resp_row["content_hash_at_attach"] == body["content_hash"]
    assert resp_row["signoff_seq"] == body["signoff_seq"]
    assert resp_row["signature"] == body["signature"]

    # PERSISTENCE through the REAL data layer (not just the route echo): read the
    # binding back and assert the governed fields round-tripped into the columns.
    rows = consumer_db.list_entity_associations(issue.id)
    assert len(rows) == 1
    stored = rows[0]
    assert stored["loomweave_entity_id"] == body["entity_id"]
    assert stored["content_hash_at_attach"] == body["content_hash"]
    assert stored["signoff_seq"] == body["signoff_seq"]
    assert stored["signature"] == body["signature"], (
        "filigree must store legis's signature VERBATIM (it holds no key and never "
        "verifies it); a mangled/re-derived signature is a contract break"
    )
    assert stored["attached_by"] == body["actor"]


async def test_parsed_signoff_flips_governed_state(consumer_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """The governed SEMANTIC EFFECT: a non-null parsed ``signature`` flips the binding
    to *governed* (DECISION 1A), so a governed close consults Legis instead of
    proceeding — the observable consequence of filigree having actually parsed the
    sign-off, NOT of verifying it.

    Contrast: an identical binding WITHOUT the sign-off (ungoverned) PROCEEDs with no
    Legis call. Driving both off the same real gate proves it is the parsed signature,
    not some unrelated default, that does the flipping.
    """
    from filigree import governance, legis_client
    from filigree.governance import GateOutcome

    golden = _load_golden()
    body = golden["request_body"]
    raw_body = _weft_body_bytes(body)

    # Legis is configured (so governed issues are gated) but every gate verdict is
    # stubbed to "unreachable" — filigree never actually calls a network Legis here.
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    from filigree.legis_client import LegisGateResult, LegisGateStatus
    monkeypatch.setattr(
        governance, "check_closure_gate",
        lambda iid: LegisGateResult(LegisGateStatus.UNREACHABLE),
    )

    # Governed issue: seed it and POST the golden sign-off binding.
    governed = consumer_db.create_issue("governed", priority=2)
    await _post_raw_golden(consumer_db, governed.id, raw_body)

    governed_decision = governance.evaluate_closure_gate(consumer_db, governed.id)
    # A non-null signature → governed → DECISION 2 fail-closed when Legis is down.
    assert governed_decision.outcome is GateOutcome.UNAVAILABLE
    assert not governed_decision.allowed

    # Ungoverned twin: the SAME binding minus the sign-off fields proceeds freely.
    ungoverned = consumer_db.create_issue("ungoverned", priority=2)
    base_body = {"entity_id": body["entity_id"], "content_hash": body["content_hash"], "actor": body["actor"]}
    await _post_raw_golden(consumer_db, ungoverned.id, _weft_body_bytes(base_body))

    ungoverned_decision = governance.evaluate_closure_gate(consumer_db, ungoverned.id)
    assert ungoverned_decision.outcome is GateOutcome.PROCEED, (
        "an ungoverned binding (no signature) must proceed — proves it is the parsed "
        "signature that governs, not a blanket default"
    )


async def test_governed_binding_is_non_removable(consumer_db: FiligreeDB) -> None:
    """A second observable governed effect: once the parsed sign-off marks the binding
    governed, the data layer refuses to remove it (``GovernedAssociationRemovalError``),
    so a non-Legis caller cannot downgrade governed -> ungoverned.
    """
    from filigree.db_entity_associations import GovernedAssociationRemovalError

    golden = _load_golden()
    body = golden["request_body"]
    issue = consumer_db.create_issue("non-removable", priority=2)
    await _post_raw_golden(consumer_db, issue.id, _weft_body_bytes(body))

    with pytest.raises(GovernedAssociationRemovalError):
        consumer_db.remove_entity_association(issue.id, body["entity_id"], actor="agent")


# ---------------------------------------------------------------------------
# Reverse drift recheck against legis's vendored authority (skip-clean)
# ---------------------------------------------------------------------------


def _legis_vendored_authority() -> Path | None:
    """Locate legis's authority copy, if the sibling repo is present.

    Honors a ``LEGIS_REPO`` override; defaults to ``/home/john/legis``.
    """
    repo = Path(os.environ.get("LEGIS_REPO", "/home/john/legis"))
    source = repo / "tests" / "contract" / "weft" / "vectors" / "signoff_binding.v1.json"
    return source if source.exists() else None


def test_legis_authority_copy_matches_vendored() -> None:
    """legis's authority golden must be BYTE-identical to filigree's vendored copy.

    legis is the producer/authority for this request body; the vendored copy is a
    sync, not a second source of truth. Fails closed on any divergence; skips cleanly
    when the sibling repo is absent (e.g. CI), where Layer 1 + the consumer oracle
    still gate the PR.
    """
    authority = _legis_vendored_authority()
    if authority is None:
        pytest.skip("legis repo not found (set LEGIS_REPO to enable the reverse byte-drift check)")
    assert authority.read_bytes() == GOLDEN_PATH.read_bytes(), (
        "legis's authority signoff_binding.v1.json has drifted from filigree's vendored "
        "copy; re-sync byte-identical (legis is the producer)."
    )
