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

    def test_none_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("WEFT_FEDERATION_TOKEN", "FILIGREE_FEDERATION_API_TOKEN", "FILIGREE_API_TOKEN"):
            monkeypatch.delenv(v, raising=False)
        assert read_env_token() == ("", None)

    def test_deprecated_alias_warns_to_migrate(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """Auto-migration nudge: a deprecated alias is still honoured (soft
        fallback) but warns the operator to move to the canonical var."""
        monkeypatch.delenv(WEFT_FEDERATION_ENV_VAR, raising=False)
        monkeypatch.delenv("FILIGREE_API_TOKEN", raising=False)
        monkeypatch.setenv("FILIGREE_FEDERATION_API_TOKEN", "dep")
        with caplog.at_level("WARNING"):
            assert read_env_token() == ("dep", "FILIGREE_FEDERATION_API_TOKEN")
        assert any("DEPRECATED" in r.message and WEFT_FEDERATION_ENV_VAR in r.message for r in caplog.records)

    def test_canonical_does_not_warn_deprecation(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, "canon")
        with caplog.at_level("WARNING"):
            read_env_token()
        assert not any("DEPRECATED" in r.message for r in caplog.records)

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

    def test_rotate_overwrites_existing_with_fresh(self, tmp_path: Path) -> None:
        first = mint_token_file(tmp_path)
        rotated = mint_token_file(tmp_path, rotate=True)
        assert rotated != first  # genuinely new secret (no env pin)
        assert read_token_file(tmp_path) == rotated
        # Still 0600.
        mode = stat.S_IMODE((tmp_path / FEDERATION_TOKEN_FILENAME).stat().st_mode)
        assert mode == 0o600

    def test_rotate_realigns_stale_file_to_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The tier-2 lockout: a daemon pinned to the env while the file holds a
        different (stale) value. Rotation with the env set realigns the file TO the
        env, so same-host siblings reading the file converge on what the daemon
        enforces."""
        (tmp_path / FEDERATION_TOKEN_FILENAME).write_text("stale-file-tok\n")
        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, "env-pinned-tok")
        rotated = mint_token_file(tmp_path, rotate=True)
        assert rotated == "env-pinned-tok"
        assert read_token_file(tmp_path) == "env-pinned-tok"


class TestMintAndGuardFederationToken:
    """The daemon-boot guard against a *silently-open* serve (the mint write
    failed, so create_app would re-resolve to no-token and drop federation auth).
    """

    def test_persisted_mint_does_not_touch_env(self, tmp_path: Path) -> None:
        import os

        from filigree.dashboard import _mint_and_guard_federation_token

        pinned = _mint_and_guard_federation_token(tmp_path, allow_env_pin=True)
        # Mint persisted → tier-2 file resolves → no env fallback needed.
        assert pinned is False
        assert (tmp_path / FEDERATION_TOKEN_FILENAME).is_file()
        assert WEFT_FEDERATION_ENV_VAR not in os.environ

    def test_unpersisted_mint_pins_in_memory_token_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import os

        from filigree import dashboard

        # Simulate a write failure: mint returns a token but nothing lands on disk.
        monkeypatch.setattr("filigree.federation_token.mint_token_file", lambda _d: "in-mem-tok")
        try:
            pinned = dashboard._mint_and_guard_federation_token(tmp_path, allow_env_pin=True)
            # No env token + un-persisted mint → pin the in-memory token (tier 1)
            # so the daemon enforces, not silently-open.
            assert pinned is True
            assert os.environ.get(WEFT_FEDERATION_ENV_VAR) == "in-mem-tok"
            # ...and warn loudly so the sibling-unreadable token is visible.
            assert "could not persist the federation token" in capsys.readouterr().err
        finally:
            os.environ.pop(WEFT_FEDERATION_ENV_VAR, None)

    def test_server_mode_unpersisted_mint_does_not_pin_cross_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """F1: in server mode (allow_env_pin=False) a persist failure must NOT
        promote the home/server token to a tier-1 env pin — that would be accepted
        across every project scope. It warns loudly but does not pin."""
        import os

        from filigree import dashboard

        monkeypatch.setattr("filigree.federation_token.mint_token_file", lambda _d: "in-mem-tok")
        pinned = dashboard._mint_and_guard_federation_token(tmp_path, allow_env_pin=False)
        assert pinned is False
        assert WEFT_FEDERATION_ENV_VAR not in os.environ  # NOT promoted cross-project
        assert "could not persist the federation token" in capsys.readouterr().err  # still loud

    def test_env_token_present_no_fallback_even_if_unpersisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import os

        from filigree import dashboard

        monkeypatch.setenv(WEFT_FEDERATION_ENV_VAR, "operator-env-tok")
        monkeypatch.setattr("filigree.federation_token.mint_token_file", lambda _d: "in-mem-tok")
        pinned = dashboard._mint_and_guard_federation_token(tmp_path, allow_env_pin=True)
        # An operator env token already wins — the guard must not warn or clobber it.
        assert pinned is False
        assert os.environ[WEFT_FEDERATION_ENV_VAR] == "operator-env-tok"
        assert "could not persist" not in capsys.readouterr().err


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
