"""Byte-aware chunking of Loomweave batch POSTs (filigree-b57d4eb7d9).

Loomweave caps every ``/api/v1/*`` request body at 16 KiB (transport-level
``RequestBodyLimitLayer``; contracts.md §POST /api/v1/files/batch "max 16 KiB",
"413 n/a"). Chunking by the 256-query count alone let a 256-row chunk of real
paths (~25 KiB) trip HTTP 413, which the resolver turned into a whole-batch
``RegistryResolutionError`` and ``migrate-registry`` into every-row-unresolved.

These tests pin the fix: chunks are sized by serialized body bytes AND count,
a 413 splits the chunk instead of failing the batch, and a single query that
cannot fit is reported unresolved on its own.
"""

from __future__ import annotations

import json
from itertools import pairwise

import pytest

from filigree.registry import (
    LOOMWEAVE_BATCH_BODY_CAP_BYTES,
    LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE,
    LOOMWEAVE_BATCH_MAX_BODY_BYTES,
    LOOMWEAVE_BATCH_MAX_QUERIES,
    BatchQuery,
    LoomweaveRegistry,
    chunk_batch_queries,
)
from tests._fakes.clarion_http import clarion_stub

pytestmark = pytest.mark.federation_contract


def _wire_body_bytes(chunk: list[BatchQuery]) -> int:
    """Independent oracle: the exact bytes httpx puts on the wire for ``json=``.

    httpx 0.28 encodes ``json=`` with ``ensure_ascii=False`` and compact
    separators; the oracle re-derives that here rather than importing the
    implementation's own size helper.
    """
    body = {"queries": [{"path": q["path"], "language": q["language"]} for q in chunk]}
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _realistic_paths(count: int) -> list[str]:
    """Paths shaped like this repo's tracker rows (~60-100 bytes each, some non-ASCII)."""
    paths: list[str] = []
    for i in range(count):
        depth = ["src", "filigree", "cli_commands", f"subsystem_{i % 7:02d}", f"package_{i % 13:02d}"]
        name = f"implementation_detail_module_{i:04d}.py"
        if i % 50 == 0:
            name = f"módulo_ünïcode_{i:04d}.py"
        paths.append("/".join([*depth, name]))
    return paths


# ---------------------------------------------------------------------------
# (a) chunker: byte cap AND count cap, order preserved
# ---------------------------------------------------------------------------


def test_chunk_batch_queries_keeps_every_body_under_cap_and_preserves_order() -> None:
    """405 realistic paths → no chunk body over the cap, every query kept in order."""
    queries = [BatchQuery(path=p, language="python") for p in _realistic_paths(405)]

    chunks = chunk_batch_queries(queries)

    # Count-only chunking would produce 2 chunks (256 + 149) at ~25 KiB each;
    # the byte cap must split further.
    assert len(chunks) > 2
    assert [q for chunk in chunks for q in chunk] == queries
    for chunk in chunks:
        assert 1 <= len(chunk) <= LOOMWEAVE_BATCH_MAX_QUERIES
        assert _wire_body_bytes(chunk) <= LOOMWEAVE_BATCH_MAX_BODY_BYTES
    # Chunks are packed, not degenerate: every chunk but the last is full
    # enough that its successor's first query would have pushed it over.
    for chunk, following in pairwise(chunks):
        assert _wire_body_bytes([*chunk, following[0]]) > LOOMWEAVE_BATCH_MAX_BODY_BYTES or len(chunk) == LOOMWEAVE_BATCH_MAX_QUERIES


def test_chunk_batch_queries_still_honours_the_256_count_cap() -> None:
    """Tiny paths never exceed the byte cap, so the count cap must still bite."""
    queries = [BatchQuery(path=f"a{i}.py", language="") for i in range(600)]

    chunks = chunk_batch_queries(queries)

    assert [len(c) for c in chunks] == [256, 256, 88]
    assert [q for chunk in chunks for q in chunk] == queries


def test_chunk_batch_queries_measures_utf8_bytes_not_characters() -> None:
    """Non-ASCII paths are sent unescaped (ensure_ascii=False), so bytes > chars."""
    # 3-byte UTF-8 characters: 3000 chars = 9000 bytes per path.
    wide = "日" * 3000
    queries = [BatchQuery(path=f"{wide}_{i}.py", language="") for i in range(3)]

    chunks = chunk_batch_queries(queries)

    # Two such paths (18 KB) never share a chunk; by character count they would.
    assert [len(c) for c in chunks] == [1, 1, 1]


def test_chunk_batch_queries_isolates_a_single_over_cap_query() -> None:
    """A query that cannot fit under the cap alone becomes its own chunk (never dropped)."""
    giant = BatchQuery(path="x" * (LOOMWEAVE_BATCH_BODY_CAP_BYTES + 512), language="")
    small = [BatchQuery(path=f"src/small_{i}.py", language="python") for i in range(3)]
    queries = [small[0], giant, *small[1:]]

    chunks = chunk_batch_queries(queries)

    assert chunks == [[small[0]], [giant], small[1:]]


def test_chunk_batch_queries_empty_input() -> None:
    assert chunk_batch_queries([]) == []


def test_batch_body_constants_leave_headroom_under_the_contract_cap() -> None:
    assert LOOMWEAVE_BATCH_BODY_CAP_BYTES == 16 * 1024
    assert 0 < LOOMWEAVE_BATCH_MAX_BODY_BYTES < LOOMWEAVE_BATCH_BODY_CAP_BYTES


# ---------------------------------------------------------------------------
# (b) resolver against a fake that enforces the 16 KiB transport cap
# ---------------------------------------------------------------------------


def test_resolve_files_batch_never_sends_an_over_cap_body_and_resolves_everything() -> None:
    """405 realistic paths against a 16 KiB-capped Loomweave: zero 413s, all resolved."""
    paths = _realistic_paths(405)
    queries = [BatchQuery(path=p, language="python") for p in paths]
    with clarion_stub(max_body_bytes=LOOMWEAVE_BATCH_BODY_CAP_BYTES) as (base_url, state):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)

        batch = registry.resolve_files_batch(queries)

    assert state.rejected_body_bytes == []
    assert set(batch["resolved"]) == set(paths)
    assert batch["not_found"] == []
    assert batch["briefing_blocked"] == []
    assert batch["errors"] == []
    assert len(state.batch_requests) > 2, "count-only chunking would have sent 2 over-cap bodies"
    assert all(size <= LOOMWEAVE_BATCH_MAX_BODY_BYTES for size in state.batch_request_body_bytes)
    assert all(len(req["queries"]) <= LOOMWEAVE_BATCH_MAX_QUERIES for req in state.batch_requests)
    # Order on the wire matches the caller's order.
    assert [q["path"] for req in state.batch_requests for q in req["queries"]] == paths


def test_resolve_files_batch_small_batch_wire_shape_is_unchanged() -> None:
    """Under the cap a batch is ONE request whose body is byte-identical to before."""
    queries = [BatchQuery(path="src/a.py", language="python"), BatchQuery(path="src/b.py", language="")]
    with clarion_stub(max_body_bytes=LOOMWEAVE_BATCH_BODY_CAP_BYTES) as (base_url, state):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)
        registry.resolve_files_batch(queries)

    assert state.batch_requests == [{"queries": [{"path": "src/a.py", "language": "python"}, {"path": "src/b.py", "language": ""}]}]
    assert state.batch_request_body_bytes == [_wire_body_bytes(queries)]


# ---------------------------------------------------------------------------
# HTTP 413 handling: split, never fail the batch
# ---------------------------------------------------------------------------


def test_resolve_files_batch_splits_chunk_on_http_413_until_it_fits() -> None:
    """A Loomweave whose cap is tighter than ours answers 413; the resolver halves and retries."""
    paths = _realistic_paths(120)
    queries = [BatchQuery(path=p, language="python") for p in paths]
    tight_cap = 2048
    with clarion_stub(max_body_bytes=tight_cap) as (base_url, state):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)

        batch = registry.resolve_files_batch(queries)

    assert state.rejected_body_bytes, "the fake must have refused at least one over-cap chunk"
    assert set(batch["resolved"]) == set(paths)
    assert batch["errors"] == []
    assert batch["not_found"] == []
    assert batch["briefing_blocked"] == []
    assert all(size <= tight_cap for size in state.batch_request_body_bytes)
    # Every path crossed the wire exactly once in an ACCEPTED request.
    accepted = [q["path"] for req in state.batch_requests for q in req["queries"]]
    assert sorted(accepted) == sorted(paths)


def test_resolve_files_batch_reports_single_query_413_as_per_item_error() -> None:
    """A lone query Loomweave still refuses is unresolved on its own; siblings resolve."""
    stubborn = "src/" + "n" * 3000 + ".py"  # under our 16 KiB pre-check, over the fake's cap
    paths = ["src/ok_a.py", stubborn, "src/ok_b.py"]
    queries = [BatchQuery(path=p, language="python") for p in paths]
    with clarion_stub(max_body_bytes=2048) as (base_url, state):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)

        batch = registry.resolve_files_batch(queries)

    assert set(batch["resolved"]) == {"src/ok_a.py", "src/ok_b.py"}
    assert [err["requested_path"] for err in batch["errors"]] == [stubborn]
    (err,) = batch["errors"]
    assert err["code"] == LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE
    assert "413" in err["message"]
    assert batch["messages"][stubborn] == err["message"]
    assert state.rejected_body_bytes, "the stubborn query must have been tried on the wire"


# ---------------------------------------------------------------------------
# (c) a single pathological path over the contract cap
# ---------------------------------------------------------------------------


def test_resolve_files_batch_reports_pathological_over_cap_path_unresolved_and_resolves_the_rest() -> None:
    """One path that cannot fit under 16 KiB is reported unresolved; the other 404 resolve."""
    paths = _realistic_paths(404)
    pathological = "src/" + "p" * (LOOMWEAVE_BATCH_BODY_CAP_BYTES + 100) + ".py"
    paths.insert(200, pathological)
    queries = [BatchQuery(path=p, language="python") for p in paths]
    with clarion_stub(max_body_bytes=LOOMWEAVE_BATCH_BODY_CAP_BYTES) as (base_url, state):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)

        batch = registry.resolve_files_batch(queries)

    assert set(batch["resolved"]) == set(paths) - {pathological}
    assert [err["requested_path"] for err in batch["errors"]] == [pathological]
    (err,) = batch["errors"]
    assert err["code"] == LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE
    assert "16 KiB" in err["message"]
    assert pathological[:20] in err["message"]
    assert batch["messages"][pathological] == err["message"]
    # A body that provably cannot fit is never put on the wire.
    assert state.rejected_body_bytes == []
    assert all(q["path"] != pathological for req in state.batch_requests for q in req["queries"])
    assert all(size <= LOOMWEAVE_BATCH_MAX_BODY_BYTES for size in state.batch_request_body_bytes)


# ---------------------------------------------------------------------------
# identity resolve:batch shares the same /api/v1 transport cap
# ---------------------------------------------------------------------------


def test_resolve_locators_batch_chunks_by_body_bytes_under_the_transport_cap() -> None:
    """300 realistic locators (~90 B each) against a 16 KiB-capped Loomweave: all resolved."""
    locators = [f"core:file:{'h' * 12}@{p}" for p in _realistic_paths(300)]
    sei_by_locator = {loc: f"loomweave:eid:{i:040x}" for i, loc in enumerate(locators)}
    with clarion_stub(max_body_bytes=LOOMWEAVE_BATCH_BODY_CAP_BYTES, sei_supported=True, sei_by_locator=sei_by_locator) as (
        base_url,
        state,
    ):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)

        result = registry.resolve_locators_batch(locators)

    assert state.rejected_body_bytes == []
    assert result["resolved"] == sei_by_locator
    assert result["orphaned"] == []
    assert result["already_migrated"] == []
    assert len(state.identity_resolve_requests) > 2, "count-only chunking would have sent 2 over-cap bodies"
    assert all(size <= LOOMWEAVE_BATCH_MAX_BODY_BYTES for size in state.identity_resolve_request_body_bytes)
    assert [loc for req in state.identity_resolve_requests for loc in req] == locators


def test_resolve_locators_batch_splits_on_http_413() -> None:
    locators = [f"core:file:{'h' * 12}@{p}" for p in _realistic_paths(60)]
    sei_by_locator = {loc: f"loomweave:eid:{i:040x}" for i, loc in enumerate(locators)}
    with clarion_stub(max_body_bytes=1024, sei_supported=True, sei_by_locator=sei_by_locator) as (base_url, state):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)

        result = registry.resolve_locators_batch(locators)

    assert state.rejected_body_bytes
    assert result["resolved"] == sei_by_locator
    assert all(size <= 1024 for size in state.identity_resolve_request_body_bytes)


def test_resolve_entity_content_hashes_chunks_locators_by_body_bytes() -> None:
    """The closure-gate drift read goes through the same byte-aware chunker."""
    locators = [f"core:file:{'h' * 12}@{p}" for p in _realistic_paths(300)]
    sei_by_locator = {loc: f"loomweave:eid:{i:040x}" for i, loc in enumerate(locators)}
    with clarion_stub(max_body_bytes=LOOMWEAVE_BATCH_BODY_CAP_BYTES, sei_supported=True, sei_by_locator=sei_by_locator) as (
        base_url,
        state,
    ):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)

        result = registry.resolve_entity_content_hashes(locators)

    assert state.rejected_body_bytes == []
    assert set(result["resolved"]) == set(locators)
    assert result["unresolved"] == []
    assert len(state.identity_resolve_requests) > 2
    assert all(size <= LOOMWEAVE_BATCH_MAX_BODY_BYTES for size in state.identity_resolve_request_body_bytes)
