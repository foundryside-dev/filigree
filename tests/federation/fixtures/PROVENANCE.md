# Vendored SEI conformance oracle fixture

`sei-conformance-oracle.json` is a **verbatim copy** of the shared, normative
fixture that lives in Loomweave's repo:

    loomweave/docs/federation/fixtures/sei-conformance-oracle.json

It defines the six SEI conformance scenarios every Weft tool runs against a
reference Loomweave (Weft SEI conformance standard §8). It is vendored here so
Filigree's producer-side oracle can run without the Loomweave checkout present.

`tests/federation/test_sibling_drift.py::test_vendored_copy_matches_sibling[sei_conformance_oracle]`
guards against drift: when the Loomweave checkout is present it asserts the
vendored copy is byte-for-byte equal to Loomweave's. The checkout is located by
`tests/federation/_oracle.py::sibling_source` — `LOOMWEAVE_REPO` (the legacy
`CLARION_REPO` alias is still honoured) or, by default, the `loomweave` checkout
next to this repo. Set `FILIGREE_REQUIRE_LOOMWEAVE_REPO=1` to make an absent
checkout a hard failure instead of a skip. In CI that drift check executes in
the weekly `federation-drift` lane of `.github/workflows/ci.yml` (also
`workflow_dispatch` with `require_federation_drift=true`), which checks the
three siblings out and arms all three `FILIGREE_REQUIRE_<SIBLING>_REPO` envs;
the per-PR `loomweave-contract` job runs it skip-clean. If you update the
fixture, update it in Loomweave first, then re-copy here — never edit this copy
by hand.
