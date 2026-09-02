"""Secret-scan hygiene pins for Loomweave's ``LMWV-SEC-SECRET-DETECTED`` rule.

Loomweave's briefing scanner blocks every entity of a file that carries a
credential-looking literal. Two test files tripped it on fixtures, not secrets:

* the capabilities oracle's module docstring quoted a 64-hex sha256 pin on a line
  of its own, so the scanner's same-line digest-context skip (the
  ``DIGEST_CONTEXT_KEYWORDS`` rule in ``loomweave-scanner/src/patterns.rs``) did
  not fire and ``HighEntropyHex`` blocked all 22 entities of the module;
* ``tests/api/test_weft_auth.py`` assigns a fake federation token to ``TOKEN``,
  which the ``ContextualCredential`` keyword detector flags on the assignment
  regardless of the surrounding ``# noqa``; the only built-in suppression for a
  live assignment is the inline ``secret-scan: allow-this-line`` marker.

These plain-text checks keep both sites in the scanner-safe shape without
suppression files, and guard the one rule that must NOT be relaxed: the real
local ``.env`` token stays blocked (never baselined).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.federation import test_capabilities_wire_conformance_oracle as capabilities_oracle

REPO_ROOT = Path(__file__).resolve().parents[1]

# Loomweave's inline allow marker (``INLINE_ALLOW_MARKERS`` in patterns.rs) is a
# byte-substring match on the line.
INLINE_ALLOW_MARKER = "secret-scan: allow-this-line"

_DOTENV_BASELINE_KEY = re.compile(r'^\s*"?\.env"?\s*:')

# Loomweave's ``DIGEST_CONTEXT_KEYWORDS`` (patterns.rs): a 40/64-hex literal on
# a line naming one of these is a digest fixture, not a secret.
_DIGEST_CONTEXT_KEYWORDS = ("sha", "blake", "digest", "checksum", "fingerprint", "etag", "hash")
_DIGEST_HEX = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-fA-F])")


def test_sha256_pin_lines_name_their_digest_context() -> None:
    """Every line quoting the sha256 pin also names ``sha256`` on that SAME line,
    so Loomweave's digest-context skip recognises it as a fixture digest, not a
    high-entropy secret."""
    module_path = Path(capabilities_oracle.__file__)
    pin = capabilities_oracle.UPSTREAM_SHA256
    lines = [line for line in module_path.read_text().splitlines() if pin in line]
    assert lines, "the sha256 pin is no longer quoted in the capabilities oracle"
    for line in lines:
        assert "sha256" in line.lower(), f"sha256 pin quoted without same-line digest context: {line!r}"


def test_every_federation_digest_literal_names_its_context_on_the_same_line() -> None:
    """Generalisation of the pin above to every federation test module: any
    40-hex (git blob sha1) or 64-hex (sha256) literal must share its line with
    one of Loomweave's ``DIGEST_CONTEXT_KEYWORDS``, or the scanner blocks every
    entity of that module (the exact regression the helper-module pins hit)."""
    offending: list[str] = []
    for module_path in sorted((REPO_ROOT / "tests" / "federation").glob("*.py")):
        for lineno, line in enumerate(module_path.read_text().splitlines(), start=1):
            if _DIGEST_HEX.search(line) and not any(keyword in line.lower() for keyword in _DIGEST_CONTEXT_KEYWORDS):
                offending.append(f"{module_path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offending, "digest literals without same-line digest context:\n" + "\n".join(offending)


def test_weft_auth_fixture_token_carries_inline_allow_marker() -> None:
    weft_auth = REPO_ROOT / "tests" / "api" / "test_weft_auth.py"
    token_lines = [line for line in weft_auth.read_text().splitlines() if line.startswith("TOKEN = ")]
    assert len(token_lines) == 1, token_lines
    assert INLINE_ALLOW_MARKER in token_lines[0], token_lines[0]


def test_secrets_baseline_never_whitelists_dotenv() -> None:
    """``.env`` holds the real local federation token; its finding is a true
    positive and must stay blocked. If a secrets baseline ever appears, it must
    not carry a ``.env`` key."""
    baseline = REPO_ROOT / ".weft" / "loomweave" / "secrets-baseline.yaml"
    if not baseline.exists():
        return
    offending = [line for line in baseline.read_text().splitlines() if _DOTENV_BASELINE_KEY.match(line)]
    assert not offending, f".env must never be baselined: {offending}"
