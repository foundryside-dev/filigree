"""Classic API generation — pre-federation filigree HTTP surface.

Frozen per ADR-002 §1. URL layout is the existing ``/api/*`` surface
with one ``/api/v1/`` outlier (``POST /api/v1/scan-results``); see
``src/filigree/dashboard.py::_create_project_router`` for the routing
wiring and ``docs/federation/contracts.md`` for the stability guarantee.

Shape types for the classic generation live in
``filigree.types.api`` (the existing 1.x types); this subpackage is
present so the generation has a symmetric shape with weft and so any
future classic-only adapter work has a home.
"""
