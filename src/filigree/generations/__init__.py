"""Named API generations for filigree's HTTP surface.

Per ADR-002, filigree's HTTP surface exposes named API generations with
lifecycles decoupled from filigree's code-version cadence. Each generation
has its own subpackage here:

- ``filigree.generations.classic`` — pre-federation HTTP API, frozen at
  its existing URLs (mostly ``/api/*``, with one ``/api/v1/`` outlier).
- ``filigree.generations.weft`` — new in 2.0, at ``/api/loom/*``, using
  the unified ``BatchResponse`` / ``ListResponse`` envelopes, the closed
  ``ErrorCode`` enum, and the ``issue_id`` vocabulary.

Each subpackage contains:

- ``types.py`` — generation-specific TypedDicts (classic may re-export
  from ``filigree.types.api``; loom declares its own).
- ``adapters.py`` — thin, data-only shape transformations from internal
  domain objects to the generation's wire shape. Adapters have no
  business logic; if you find yourself writing an ``if generation ==``
  branch inside an adapter, the logic belongs in the handler.

See ``docs/federation/contracts.md`` for the consumer-facing contract
and ``docs/plans/2026-04-24-2.0-federation-work-package.md`` for the
implementation sequence.
"""
