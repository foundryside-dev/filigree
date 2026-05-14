from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


class TestResolveTargetFile:
    def test_resolve_relative_target_file(self, tmp_path: Path) -> None:
        from filigree.scanner_scripts import scan_utils as mod

        repo_root = tmp_path / "repo"
        root_dir = repo_root / "src"
        root_dir.mkdir(parents=True)
        target = root_dir / "a.py"
        target.write_text("x = 1\n")

        resolved = mod._resolve_target_file(repo_root=repo_root, root_dir=root_dir, file_arg="src/a.py")
        assert resolved == target.resolve()

    def test_reject_target_outside_root(self, tmp_path: Path) -> None:
        from filigree.scanner_scripts import scan_utils as mod

        repo_root = tmp_path / "repo"
        root_dir = repo_root / "src"
        root_dir.mkdir(parents=True)
        outside = repo_root / "outside.py"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("x = 1\n")

        with pytest.raises(ValueError, match="outside scan root"):
            mod._resolve_target_file(repo_root=repo_root, root_dir=root_dir, file_arg="outside.py")

    def test_reject_missing_target(self, tmp_path: Path) -> None:
        from filigree.scanner_scripts import scan_utils as mod

        repo_root = tmp_path / "repo"
        root_dir = repo_root / "src"
        root_dir.mkdir(parents=True)

        with pytest.raises(ValueError, match="does not exist"):
            mod._resolve_target_file(repo_root=repo_root, root_dir=root_dir, file_arg="src/missing.py")


class TestRunCodex:
    @pytest.mark.asyncio
    async def test_prompt_is_sent_over_stdin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.scanner_scripts import codex_bug_hunt as mod

        captured: dict[str, object] = {}

        class FakeProcess:
            returncode: int | None = 0
            received_input: bytes | None = None

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                self.received_input = input
                return b"", b""

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                self.returncode = -9

            def terminate(self) -> None:
                self.returncode = -15

        fake_process = FakeProcess()

        async def fake_create_subprocess_exec(*cmd: str, **kwargs: object) -> FakeProcess:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return fake_process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        prompt = "static analysis prompt\n" * 100
        await mod.run_codex(
            prompt=prompt,
            output_path=tmp_path / "report.md",
            model=None,
            repo_root=tmp_path,
            timeout=5,
        )

        cmd = captured["cmd"]
        assert isinstance(cmd, tuple)
        assert cmd[-1] == "-"
        assert prompt not in cmd

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["stdin"] is asyncio.subprocess.PIPE
        assert fake_process.received_input == prompt.encode("utf-8")
