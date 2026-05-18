from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_main_uses_bundled_claude_scan_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from filigree.scanner_scripts import claude_bug_hunt as mod

    captured: dict[str, object] = {}

    async def fake_run_scanner_pipeline(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(mod, "run_scanner_pipeline", fake_run_scanner_pipeline)

    assert mod.main() == 0
    assert captured["scan_source"] == "claude"


class TestRunClaudeCode:
    @pytest.mark.asyncio
    async def test_uses_prompt_cache_friendly_cli_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.scanner_scripts import claude_bug_hunt as mod

        captured: dict[str, object] = {}

        class FakeProcess:
            returncode: int | None = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"No concrete bug found in target file\n", b""

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                self.returncode = -9

            def terminate(self) -> None:
                self.returncode = -15

        async def fake_create_subprocess_exec(*cmd: str, **kwargs: object) -> FakeProcess:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        await mod.run_claude_code(
            prompt="static analysis prompt",
            output_path=tmp_path / "report.md",
            model="sonnet",
            repo_root=tmp_path,
            timeout=5,
        )

        cmd = captured["cmd"]
        assert isinstance(cmd, tuple)
        assert "--exclude-dynamic-system-prompt-sections" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd
