"""Unit tests for the 3-tier federation-token resolver + anchor mint."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from filigree.federation_token import (
    FEDERATION_TOKEN_FILE_SOURCE,
    FEDERATION_TOKEN_FILENAME,
    WEFT_FEDERATION_ENV_VAR,
    mint_token_file,
    read_env_token,
    read_token_file,
    resolve_federation_token,
)

_ALL_ENV = (
    WEFT_FEDERATION_ENV_VAR,
    "FILIGREE_FEDERATION_API_TOKEN",
    "FILIGREE_API_TOKEN",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)


class TestReadEnvToken:
    def test_canonical_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, "canon")
        monkeypatch.setenv("FILIGREE_API_TOKEN", "legacy")
        assert read_env_token() == ("canon", WEFT_FEDERATION_ENV_VAR)

    def test_deprecated_alias_used_when_canonical_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FILIGREE_FEDERATION_API_TOKEN", "dep")
        assert read_env_token() == ("dep", "FILIGREE_FEDERATION_API_TOKEN")

    def test_none_set(self) -> None:
        assert read_env_token() == ("", None)

    def test_blank_is_skipped_with_warning(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, "   ")
        with caplog.at_level("WARNING"):
            assert read_env_token() == ("", None)
        assert any("empty/whitespace" in r.message for r in caplog.records)


class TestReadTokenFile:
    def test_absent(self, tmp_path: Path) -> None:
        assert read_token_file(tmp_path) == ""

    def test_present_is_stripped(self, tmp_path: Path) -> None:
        (tmp_path / FEDERATION_TOKEN_FILENAME).write_text("  tok123\n")
        assert read_token_file(tmp_path) == "tok123"

    def test_non_utf8_file_returns_empty_per_contract(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A corrupt (non-UTF-8) token file must honour the "unreadable -> ''"
        contract, not raise UnicodeDecodeError. read_text() decodes UTF-8, and a
        UnicodeDecodeError is a ValueError (not an OSError) — so the catch must
        cover it. Fails closed (no token => auth off) with a warning, so a corrupt
        token is not silently mistaken for "auth disabled".
        """
        (tmp_path / FEDERATION_TOKEN_FILENAME).write_bytes(b"\xff\xfe\x00bad")
        assert read_token_file(tmp_path) == ""
        assert any("federation_token" in r.message or "token" in r.message.lower() for r in caplog.records)


class TestMintTokenFile:
    def test_mints_fresh_0600(self, tmp_path: Path) -> None:
        token = mint_token_file(tmp_path)
        path = tmp_path / FEDERATION_TOKEN_FILENAME
        assert token
        assert path.read_text().strip() == token
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_idempotent_reuse(self, tmp_path: Path) -> None:
        first = mint_token_file(tmp_path)
        second = mint_token_file(tmp_path)
        assert first == second

    def test_captures_existing_env_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # No file yet, but an env token is exported → the persisted file should
        # record THAT value so the file matches a daemon already on the env token.
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, "env-secret")
        assert mint_token_file(tmp_path) == "env-secret"
        assert (tmp_path / FEDERATION_TOKEN_FILENAME).read_text().strip() == "env-secret"

    def test_best_effort_on_unwritable_dir(self, tmp_path: Path) -> None:
        # store_dir's parent is a file → mkdir fails; mint must not raise and
        # still returns a usable token for this run.
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        token = mint_token_file(blocker / "store")
        assert token  # returned despite failing to persist


class TestResolveFederationToken:
    def test_env_wins_over_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / FEDERATION_TOKEN_FILENAME).write_text("file-tok\n")
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, "env-tok")
        assert resolve_federation_token(tmp_path) == ("env-tok", WEFT_FEDERATION_ENV_VAR)

    def test_file_used_when_no_env(self, tmp_path: Path) -> None:
        (tmp_path / FEDERATION_TOKEN_FILENAME).write_text("file-tok\n")
        assert resolve_federation_token(tmp_path) == ("file-tok", FEDERATION_TOKEN_FILE_SOURCE)

    def test_off_when_neither(self, tmp_path: Path) -> None:
        assert resolve_federation_token(tmp_path) == ("", None)

    def test_none_store_dir_env_only(self) -> None:
        assert resolve_federation_token(None) == ("", None)

    def test_resolve_is_read_only(self, tmp_path: Path) -> None:
        # Resolution must never create the token file (only boot/install/doctor mint).
        resolve_federation_token(tmp_path)
        assert not (tmp_path / FEDERATION_TOKEN_FILENAME).exists()
