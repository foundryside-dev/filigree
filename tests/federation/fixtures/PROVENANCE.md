# Vendored SEI conformance oracle fixture

`sei-conformance-oracle.json` is a **verbatim copy** of the shared, normative
fixture that lives in Clarion's repo:

    clarion/docs/federation/fixtures/sei-conformance-oracle.json

It defines the six SEI conformance scenarios every Loom tool runs against a
reference Clarion (Loom SEI conformance standard §8). It is vendored here so
Filigree's producer-side oracle can run without the Clarion checkout present.

`test_sei_conformance_oracle.py::test_vendored_oracle_matches_clarion_source`
guards against drift: when the Clarion repo is present alongside this one, it
asserts the vendored copy is byte-for-byte equal to Clarion's. If you update the
fixture, update it in Clarion first, then re-copy here — never edit this copy
by hand.
