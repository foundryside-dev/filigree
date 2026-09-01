"""Tests for transport-bound actor identity resolution (ADR-012, schema v24)."""

from __future__ import annotations

import builtins

from filigree.actor_identity import actor_mismatch_warning, resolve_os_actor


def test_resolve_os_actor_returns_str_on_posix() -> None:
    # On the POSIX CI/dev host this resolves to the running user's name.
    result = resolve_os_actor()
    assert result is None or isinstance(result, str)
    assert result != ""  # never an empty string — None or a real name


def test_resolve_os_actor_returns_none_when_pwd_unavailable(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pwd":
            raise ModuleNotFoundError("No module named 'pwd'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert resolve_os_actor() is None  # does not raise


def test_logical_actor_alias_never_conflicts_with_os_provenance() -> None:
    assert actor_mismatch_warning("agent-x", "alice") is None
    assert actor_mismatch_warning("alice", "alice") is None
    assert actor_mismatch_warning("agent-x", None) is None
    assert actor_mismatch_warning("cli", "alice") is None
    assert actor_mismatch_warning("mcp", "alice") is None
