"""Pins for the shared Layer-2 oracle helpers (``tests/federation/_oracle.py``).

The eight consumer/producer wire oracles and the SEI oracle used to each carry
their own copy of the git-blob hasher, the golden loader, a ``/home/john/...``
sibling locator and a skip-clean drift test. Those now live once in ``_oracle``;
this module pins the helpers' contracts so a regression in the shared code reds
here rather than silently across nine oracles:

* ``blob_sha`` reproduces git's blob object id (verified against
  ``git hash-object --stdin``).
* ``load_golden`` caches BYTES, never a parsed object — every call returns a
  fresh dict, because the scan-results oracles rely on a pristine re-read after
  ``process_scan_results`` normalises finding dicts in place.
* ``sibling_source`` honours the per-sibling env override (plus the legacy
  ``CLARION_REPO`` alias for loomweave) and falls back to the portable
  next-to-this-checkout locator.
* ``arming_requested`` is the one documented parse of
  ``FILIGREE_REQUIRE_<SIBLING>_REPO`` — ``=0`` DISARMS (bare env truthiness used
  to arm it) and an unrecognised value raises so a typo cannot silently disarm.
* ``DRIFT_REGISTRY`` integrity, and a marker-completeness guard: every
  ``tests/federation/test_*.py`` contract module carries the module-level
  ``federation_contract`` mark the ``loomweave-contract`` CI job selects on —
  the tripwire for the failure mode where a new oracle file is silently omitted
  from the job.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from tests.federation._oracle import (
    DRIFT_REGISTRY,
    FEDERATION_FIXTURES_DIR,
    SIBLING_REPO_ENV,
    _golden_bytes,
    arming_env_name,
    arming_requested,
    blob_sha,
    load_golden,
    sibling_source,
)

pytestmark = pytest.mark.federation_contract

# Modules under tests/federation/ that are NOT Layer-2 contract oracles and so
# must NOT carry the marker (they belong to other CI lanes).
_NOT_CONTRACT_MODULES = {
    "test_sei_oracle_live_loomweave",  # live-serve integration lane (integration+slow)
}


# ---------------------------------------------------------------------------
# blob_sha
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # Trailing "sha1" keeps Loomweave's same-line digest-context skip happy
        # (a bare 40-hex literal reads as a high-entropy secret otherwise).
        (b"", "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"),  # git blob sha1 of b""
        (b"hello\n", "ce013625030ba8dba906f756967f9e9ca394464a"),  # git blob sha1 of b"hello\n"
    ],
    ids=["empty", "hello"],
)
def test_blob_sha_matches_git_hash_object(data: bytes, expected: str) -> None:
    """``blob_sha`` is git's blob object id (``git hash-object --stdin``)."""
    assert blob_sha(data) == expected


# ---------------------------------------------------------------------------
# load_golden — bytes cached, fresh object per call
# ---------------------------------------------------------------------------


def test_load_golden_parses_the_file_bytes(tmp_path: Path) -> None:
    golden = tmp_path / "g.json"
    golden.write_bytes(b'{"a": [1, 2], "b": {"c": "d"}}')
    assert load_golden(golden) == json.loads(golden.read_bytes())


def test_load_golden_returns_a_fresh_object_per_call(tmp_path: Path) -> None:
    """Mutating one load must not leak into the next — the cache holds bytes,
    not the parsed dict (the scan-results oracles re-read a pristine golden after
    in-place ingest normalisation)."""
    golden = tmp_path / "g.json"
    golden.write_bytes(b'{"findings": [{"severity": "HIGH"}]}')
    first = load_golden(golden)
    first["findings"][0]["severity"] = "mangled"
    first["extra"] = True
    second = load_golden(golden)
    assert second == {"findings": [{"severity": "HIGH"}]}
    assert second is not first
    assert second["findings"] is not first["findings"]


def test_load_golden_reads_the_file_once(tmp_path: Path) -> None:
    """A single disk read per session: the second load is a cache hit."""
    golden = tmp_path / "g.json"
    golden.write_bytes(b"{}")
    before = _golden_bytes.cache_info()
    load_golden(golden)
    load_golden(golden)
    after = _golden_bytes.cache_info()
    assert after.misses == before.misses + 1
    assert after.hits == before.hits + 1


# ---------------------------------------------------------------------------
# sibling_source — env override, legacy alias, portable fallback
# ---------------------------------------------------------------------------


def _clear_sibling_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for names in SIBLING_REPO_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)


def test_sibling_source_honours_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sibling_env(monkeypatch)
    relative = "tests/conformance/x.json"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"{}")
    monkeypatch.setenv("WARDLINE_REPO", str(tmp_path))
    assert sibling_source("wardline", relative, siblings_root=tmp_path / "nowhere") == target


def test_sibling_source_env_override_is_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit override that lacks the file yields None — it does NOT fall
    through to the next-to-checkout fallback (a wrong override must be visible,
    not papered over by a different checkout)."""
    _clear_sibling_env(monkeypatch)
    relative = "tests/conformance/x.json"
    root = tmp_path / "siblings"
    present = root / "wardline" / relative
    present.parent.mkdir(parents=True)
    present.write_bytes(b"{}")
    monkeypatch.setenv("WARDLINE_REPO", str(tmp_path / "override-without-file"))
    assert sibling_source("wardline", relative, siblings_root=root) is None


def test_sibling_source_falls_back_to_siblings_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sibling_env(monkeypatch)
    relative = "docs/federation/fixtures/x.json"
    root = tmp_path / "siblings"
    target = root / "loomweave" / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"{}")
    assert sibling_source("loomweave", relative, siblings_root=root) == target
    assert sibling_source("loomweave", "docs/missing.json", siblings_root=root) is None


def test_sibling_source_ignores_empty_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sibling_env(monkeypatch)
    relative = "x.json"
    root = tmp_path / "siblings"
    target = root / "legis" / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"{}")
    monkeypatch.setenv("LEGIS_REPO", "")
    assert sibling_source("legis", relative, siblings_root=root) == target


def test_sibling_source_honours_legacy_clarion_alias_at_lower_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``CLARION_REPO`` (the SEI oracle's documented override) still works for
    the loomweave sibling, but ``LOOMWEAVE_REPO`` wins when both are set."""
    _clear_sibling_env(monkeypatch)
    relative = "docs/federation/fixtures/x.json"
    legacy = tmp_path / "legacy"
    modern = tmp_path / "modern"
    for repo in (legacy, modern):
        (repo / relative).parent.mkdir(parents=True)
        (repo / relative).write_bytes(b"{}")

    monkeypatch.setenv("CLARION_REPO", str(legacy))
    assert sibling_source("loomweave", relative, siblings_root=tmp_path / "nowhere") == legacy / relative

    monkeypatch.setenv("LOOMWEAVE_REPO", str(modern))
    assert sibling_source("loomweave", relative, siblings_root=tmp_path / "nowhere") == modern / relative


def test_sibling_source_rejects_unknown_sibling(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        sibling_source("shuttle", "x.json", siblings_root=tmp_path)


# ---------------------------------------------------------------------------
# arming_requested — the one documented parse
# ---------------------------------------------------------------------------


def test_arming_env_name_is_per_sibling() -> None:
    assert arming_env_name("loomweave") == "FILIGREE_REQUIRE_LOOMWEAVE_REPO"
    assert arming_env_name("wardline") == "FILIGREE_REQUIRE_WARDLINE_REPO"
    assert arming_env_name("legis") == "FILIGREE_REQUIRE_LEGIS_REPO"


def test_arming_requested_is_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FILIGREE_REQUIRE_LOOMWEAVE_REPO", raising=False)
    assert arming_requested("loomweave") is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "False", "NO", " off "], ids=repr)
def test_arming_requested_disarms_on_falsey_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """``=0`` DISARMS — bare env truthiness used to arm on any non-empty value."""
    monkeypatch.setenv("FILIGREE_REQUIRE_LOOMWEAVE_REPO", value)
    assert arming_requested("loomweave") is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", " 1 ", "\ton\n"], ids=repr)
def test_arming_requested_arms_on_truthy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FILIGREE_REQUIRE_WARDLINE_REPO", value)
    assert arming_requested("wardline") is True


@pytest.mark.parametrize("value", ["maybe", "2", "yes please", "t"], ids=repr)
def test_arming_requested_rejects_unrecognised_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A typo must not silently disarm (or arm) the release gate."""
    monkeypatch.setenv("FILIGREE_REQUIRE_LEGIS_REPO", value)
    with pytest.raises(ValueError, match="FILIGREE_REQUIRE_LEGIS_REPO"):
        arming_requested("legis")


# ---------------------------------------------------------------------------
# DRIFT_REGISTRY integrity
# ---------------------------------------------------------------------------

_EXPECTED_REGISTRY_NAMES = {
    "capabilities",
    "loomweave_scan_results",
    "sei_conformance_oracle",
    "entity_associations",
    "weft_issue_detail",
    "wardline_finding_identity",
    "wardline_scan_results",
    "wardline_suppression_filter",
    "legis_signoff_binding",
}


def test_drift_registry_has_exactly_the_expected_entries() -> None:
    assert {e.name for e in DRIFT_REGISTRY} == _EXPECTED_REGISTRY_NAMES
    assert len(DRIFT_REGISTRY) == len(_EXPECTED_REGISTRY_NAMES)


def test_drift_registry_vendored_paths_exist_and_are_unique() -> None:
    for entry in DRIFT_REGISTRY:
        assert entry.vendored.is_file(), f"{entry.name}: vendored copy missing at {entry.vendored}"
    assert len({e.vendored for e in DRIFT_REGISTRY}) == len(DRIFT_REGISTRY)


def test_drift_registry_sibling_targets_are_unique_and_relative() -> None:
    pairs = [(e.sibling, e.relative) for e in DRIFT_REGISTRY]
    assert len(set(pairs)) == len(pairs)
    for entry in DRIFT_REGISTRY:
        assert entry.sibling in SIBLING_REPO_ENV, f"{entry.name}: unknown sibling {entry.sibling!r}"
        assert not Path(entry.relative).is_absolute(), f"{entry.name}: sibling-relative path must be relative"
        assert entry.direction in ("upstream", "downstream")


def test_drift_registry_covers_the_sei_oracle_fixture() -> None:
    """The pre-existing SEI oracle's drift check moved into the registry."""
    sei = next(e for e in DRIFT_REGISTRY if e.name == "sei_conformance_oracle")
    assert sei.vendored == FEDERATION_FIXTURES_DIR / "sei-conformance-oracle.json"
    assert sei.sibling == "loomweave"
    assert sei.direction == "upstream"


# ---------------------------------------------------------------------------
# Marker completeness guard
# ---------------------------------------------------------------------------


def _module_marks(module_name: str) -> set[str]:
    module = importlib.import_module(module_name)
    marks = getattr(module, "pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]
    return {m.name for m in marks}


def test_every_federation_contract_module_carries_the_marker() -> None:
    """The ``loomweave-contract`` CI job selects ``-m federation_contract``; a
    contract module that forgets the module-level mark silently drops out of the
    job (the failure mode ce7b9a0 fixed by hand for the sign-off oracle)."""
    federation_dir = Path(__file__).resolve().parent
    contract_modules = sorted(p.stem for p in federation_dir.glob("test_*.py") if p.stem not in _NOT_CONTRACT_MODULES)
    assert contract_modules, "no federation contract modules found"
    unmarked = [stem for stem in contract_modules if "federation_contract" not in _module_marks(f"tests.federation.{stem}")]
    assert not unmarked, f"federation contract modules missing `pytestmark = pytest.mark.federation_contract`: {unmarked}"


def test_live_lane_module_does_not_carry_the_marker() -> None:
    """The live-serve lane belongs to the ``live-loomweave`` job, not the contract job."""
    for stem in _NOT_CONTRACT_MODULES:
        assert "federation_contract" not in _module_marks(f"tests.federation.{stem}")
