"""Weft API generation — federation-era filigree HTTP surface.

Introduced in 2.0. URL layout is ``/api/weft/*``, using the unified
``BatchResponse`` / ``ListResponse`` envelopes, the closed ``ErrorCode``
enum, the ``issue_id`` vocabulary, and composed operations like
``start_work``. See ADR-002 for the lifecycle rules and
``docs/federation/contracts.md`` for the stability guarantee.

Phase B lands the scaffolding (this package + the first concrete
adapter for scan-results). Phase C1-C5 fills in the HTTP endpoints
that consume the adapters. Phase D forward-migrates MCP to the loom
vocabulary.
"""
