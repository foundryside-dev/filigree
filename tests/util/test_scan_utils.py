"""Tests for bundled scanner utility helpers."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from filigree.core import FILIGREE_DIR_NAME, write_config
from filigree.scanner_scripts.scan_utils import (
    PROMPT_TEMPLATE,
    _analyse_files,
    _infer_rule_id,
    build_prompt_template,
    estimate_tokens,
    find_files,
    load_context,
    parse_findings,
    post_to_api,
    run_scanner_pipeline,
    severity_map,
)

# ── PROMPT_TEMPLATE ────────────────────────────────────────────────────


class TestPromptTemplate:
    """PROMPT_TEMPLATE keeps per-file values late for prompt-cache reuse."""

    def test_file_specific_values_follow_static_instructions(self) -> None:
        first_file_slot = PROMPT_TEMPLATE.index("{file_path}")
        static_output_contract = PROMPT_TEMPLATE.index("## Suggested Fix")

        assert first_file_slot > static_output_contract

    def test_rendered_prompts_share_static_prefix_across_target_files(self) -> None:
        context = "--- CLAUDE.md ---\nShared project guidance"
        prompt_a = PROMPT_TEMPLATE.format(file_path="/repo/src/filigree/a.py", context=context)
        prompt_b = PROMPT_TEMPLATE.format(file_path="/repo/src/filigree/b.py", context=context)

        common_prefix_len = 0
        for char_a, char_b in zip(prompt_a, prompt_b, strict=False):
            if char_a != char_b:
                break
            common_prefix_len += 1

        shared_prefix = prompt_a[:common_prefix_len]
        assert "Bug categories to check" in shared_prefix
        assert "## Suggested Fix" in shared_prefix
        assert "Shared project guidance" in shared_prefix

    def test_prompt_pack_adds_static_review_focus_before_file_path(self) -> None:
        prompt_template = build_prompt_template("security")
        rendered = prompt_template.format(file_path="/repo/src/app.py", context="context")

        assert "Review focus: security" in rendered
        assert "authentication" in rendered
        assert rendered.index("Review focus: security") < rendered.index("Target file:")

    def test_major_refactor_prompt_pack_combines_four_disciplines(self) -> None:
        prompt_template = build_prompt_template("major-refactor")

        assert "Review focus: solution-architecture" in prompt_template
        assert "Review focus: systems-thinking" in prompt_template
        assert "Review focus: python-engineering" in prompt_template
        assert "Review focus: quality-engineering" in prompt_template

    def test_comprehensive_prompt_pack_is_broader_than_major_refactor(self) -> None:
        prompt_template = build_prompt_template("comprehensive")
        major_refactor = build_prompt_template("major-refactor")

        assert prompt_template != major_refactor
        assert "Review focus: security" in prompt_template
        assert "Review focus: solution-architecture" in prompt_template
        assert "Review focus: system-interactions" in prompt_template
        assert "Review focus: python-engineering" in prompt_template
        assert "Review focus: quality-engineering" in prompt_template

    def test_system_interactions_distinguishes_interface_failures_from_systems_thinking(self) -> None:
        prompt_template = build_prompt_template("system-interactions")
        systems_thinking = build_prompt_template("systems-thinking")

        assert "Review focus: system-interactions" in prompt_template
        assert "cross-component" in prompt_template
        assert "integration contract" in prompt_template
        assert "stocks" in systems_thinking

    def test_frontend_prompt_packs_cover_css_javascript_and_typescript(self) -> None:
        css = build_prompt_template("css")
        javascript = build_prompt_template("javascript")
        typescript = build_prompt_template("typescript")

        assert "Review focus: css" in css
        assert "specificity" in css
        assert "Review focus: javascript" in javascript
        assert "event lifecycle" in javascript
        assert "Review focus: typescript" in typescript
        assert "type erasure" in typescript

    def test_infrastructure_and_system_language_prompt_packs(self) -> None:
        rust = build_prompt_template("rust")
        go = build_prompt_template("go")
        react = build_prompt_template("react")
        terraform = build_prompt_template("terraform")
        sql = build_prompt_template("sql")

        assert "Review focus: rust" in rust
        assert "ownership" in rust
        assert "Review focus: go" in go
        assert "goroutine" in go
        assert "Review focus: react" in react
        assert "hook" in react
        assert "Review focus: terraform" in terraform
        assert "state drift" in terraform
        assert "Review focus: sql" in sql
        assert "transaction" in sql


class TestRunScannerPipeline:
    async def test_default_root_infers_package_from_repo_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        project_root = tmp_path / "elspeth"
        package_root = project_root / "src" / "elspeth"
        package_root.mkdir(parents=True)
        (package_root / "target.py").write_text("x = 1\n")

        monkeypatch.chdir(project_root)
        monkeypatch.setattr(sys, "argv", ["scanner", "--dry-run", "--max-files", "1"])

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("dry-run must not execute scanner")

        rc = await run_scanner_pipeline(
            executor=fake_executor,
            scan_source="test",
            cli_tool="",
        )

        assert rc == 0
        assert "src/elspeth/target.py" in capsys.readouterr().out

    async def test_uses_current_working_directory_as_project_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        project_root = tmp_path / "project"
        src = project_root / "src"
        src.mkdir(parents=True)
        (src / "target.py").write_text("x = 1\n")

        monkeypatch.chdir(project_root)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", "src", "--dry-run", "--max-files", "1"])

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("dry-run must not execute scanner")

        rc = await run_scanner_pipeline(
            executor=fake_executor,
            scan_source="test",
            cli_tool="",
        )

        assert rc == 0
        assert "src/target.py" in capsys.readouterr().out

    async def test_default_api_url_uses_active_ephemeral_dashboard_port(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_root = tmp_path / "project"
        src = project_root / "src"
        filigree_dir = project_root / FILIGREE_DIR_NAME
        src.mkdir(parents=True)
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "tst", "version": 1})
        (filigree_dir / "ephemeral.port").write_text("9444\n")
        (src / "target.py").write_text("x = 1\n")
        post_calls: list[dict[str, Any]] = []

        monkeypatch.chdir(project_root)
        monkeypatch.setattr(
            sys,
            "argv",
            ["scanner", "--root", "src", "--file", "src/target.py", "--scan-run-id", "run-1"],
        )
        monkeypatch.setattr(
            "filigree.scanner_scripts.scan_utils.post_to_api",
            lambda **kwargs: post_calls.append(kwargs) or (True, ""),
        )

        async def fake_executor(**kwargs: object) -> None:
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(NO_BUG_MD, encoding="utf-8")

        rc = await run_scanner_pipeline(
            executor=fake_executor,
            scan_source="test",
            prompt_template=PROMPT_TEMPLATE,
        )

        assert rc == 0
        assert post_calls
        assert {call["api_url"] for call in post_calls} == {"http://localhost:9444"}

    async def test_rejects_invalid_prompt_pack(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "target.py").write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--prompt", "missing-pack"])

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("invalid prompt must stop before execution")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test")

        assert rc == 1
        assert "missing-pack" in capsys.readouterr().err

    async def test_rejects_invalid_batch_size(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "target.py").write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--batch-size", "0"])

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("invalid batch size must stop before execution")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test")

        assert rc == 1
        assert "--batch-size must be at least 1" in capsys.readouterr().err

    async def test_rejects_file_outside_scan_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "src"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", "src", "--file", "outside.py"])

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("invalid file must stop before execution")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test")

        assert rc == 1
        assert "outside scan root" in capsys.readouterr().err

    async def test_rejects_missing_cli_tool(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "target.py").write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", "."])
        monkeypatch.setattr("shutil.which", lambda _tool: None)

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("missing tool must stop before execution")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test", cli_tool="definitely-missing")

        assert rc == 1
        assert "`definitely-missing` not found" in capsys.readouterr().err

    async def test_successful_scan_prints_summary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--file", "target.py", "--no-ingest"])

        async def fake_executor(**kwargs: object) -> None:
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(SINGLE_FINDING_MD, encoding="utf-8")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test", prompt_template=PROMPT_TEMPLATE)

        assert rc == 0
        out = capsys.readouterr().out
        assert "Bug Hunt Summary (test)" in out
        assert "Defects found:  1" in out
        assert "P1: 1" in out

    async def test_unreadable_optional_context_file_does_not_abort_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        (tmp_path / "CLAUDE.md").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--file", "target.py", "--no-ingest"])
        executor_ran = False

        async def fake_executor(**kwargs: object) -> None:
            nonlocal executor_ran
            executor_ran = True
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(NO_BUG_MD, encoding="utf-8")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test", prompt_template=PROMPT_TEMPLATE)

        assert rc == 0
        assert executor_ran is True
        assert "Skipping unreadable context file CLAUDE.md" in capsys.readouterr().err

    async def test_executor_failure_returns_nonzero_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--file", "target.py", "--no-ingest"])

        async def fake_executor(**_kwargs: object) -> None:
            raise RuntimeError("scanner exploded")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test", prompt_template=PROMPT_TEMPLATE)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Failed:         1" in captured.out
        assert "FAIL target.py: scanner exploded" in captured.err

    async def test_blank_api_url_returns_cli_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--file", "target.py", "--api-url", "   "])

        async def fake_executor(**kwargs: object) -> None:
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(NO_BUG_MD, encoding="utf-8")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test", prompt_template=PROMPT_TEMPLATE)

        assert rc == 1
        assert "--api-url must be a non-empty URL" in capsys.readouterr().err

    async def test_missing_generated_report_returns_nonzero_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--file", "target.py", "--no-ingest"])

        async def fake_executor(**_kwargs: object) -> None:
            return None

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test", prompt_template=PROMPT_TEMPLATE)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Failed:         1" in captured.out
        assert "FAIL target.py: scanner did not generate report" in captured.err

    async def test_unreadable_generated_report_returns_nonzero_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["scanner", "--root", ".", "--file", "target.py", "--no-ingest"])

        async def fake_executor(**kwargs: object) -> None:
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"\xff")

        rc = await run_scanner_pipeline(executor=fake_executor, scan_source="test", prompt_template=PROMPT_TEMPLATE)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Failed:         1" in captured.out
        assert "FAIL reading report for target.py" in captured.err


class TestAnalyseFiles:
    async def test_cache_warmup_runs_first_file_before_parallel_batch(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        targets = [root / "a.py", root / "b.py", root / "c.py"]
        for target in targets:
            target.write_text("x = 1\n")
        output_dir = tmp_path / "reports"
        first_finished = asyncio.Event()
        non_first_started = False
        started: list[str] = []

        async def fake_executor(**kwargs: object) -> None:
            nonlocal non_first_started
            output_path = Path(kwargs["output_path"])
            started.append(output_path.name)
            if output_path.name == "a.py.md":
                await asyncio.sleep(0)
                first_finished.set()
            else:
                non_first_started = True
                assert first_finished.is_set()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(NO_BUG_MD, encoding="utf-8")

        stats = await _analyse_files(
            files=targets,
            output_dir=output_dir,
            root_dir=root,
            repo_root=root,
            model=None,
            batch_size=3,
            context="ctx",
            skip_existing=False,
            timeout=30,
            api_url="http://filigree.test",
            no_ingest=True,
            scan_run_id="run-1",
            scan_source="test",
            executor=fake_executor,
            prompt_template=PROMPT_TEMPLATE,
            cache_warmup=True,
        )

        assert stats["clean"] == 3
        assert non_first_started is True
        assert started == ["a.py.md", "b.py.md", "c.py.md"]

    async def test_ingests_findings_and_completes_scan_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "target.py"
        target.write_text("x = 1\n")
        output_dir = tmp_path / "reports"
        post_calls: list[dict[str, Any]] = []

        async def fake_executor(**kwargs: object) -> None:
            Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kwargs["output_path"]).write_text(SINGLE_FINDING_MD, encoding="utf-8")

        def fake_post_to_api(**kwargs: Any) -> tuple[bool, str]:
            post_calls.append(kwargs)
            return True, ""

        monkeypatch.setattr("filigree.scanner_scripts.scan_utils.post_to_api", fake_post_to_api)

        stats = await _analyse_files(
            files=[target],
            output_dir=output_dir,
            root_dir=root,
            repo_root=root,
            model=None,
            batch_size=1,
            context="ctx",
            skip_existing=False,
            timeout=30,
            api_url="http://filigree.test",
            no_ingest=False,
            scan_run_id="run-1",
            scan_source="test",
            executor=fake_executor,
            prompt_template=PROMPT_TEMPLATE,
        )

        assert stats["P1"] == 1
        assert stats["failed"] == 0
        assert stats["api_files_posted"] == 1
        assert stats["api_files_failed"] == 0
        assert len(post_calls) == 2
        assert post_calls[0]["complete_scan_run"] is False
        assert post_calls[0]["create_observations"] is True
        assert post_calls[1]["findings"] == []
        assert post_calls[1]["complete_scan_run"] is True
        assert "[1/1] target.py" in capsys.readouterr().err

    async def test_skip_existing_report_ingests_findings_before_completion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "target.py"
        target.write_text("x = 1\n")
        output_dir = tmp_path / "reports"
        report = output_dir / "target.py.md"
        report.parent.mkdir(parents=True)
        report.write_text(SINGLE_FINDING_MD, encoding="utf-8")
        post_calls: list[dict[str, Any]] = []

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("skip-existing must not execute scanner")

        def fake_post_to_api(**kwargs: Any) -> tuple[bool, str]:
            post_calls.append(kwargs)
            return True, ""

        monkeypatch.setattr("filigree.scanner_scripts.scan_utils.post_to_api", fake_post_to_api)

        stats = await _analyse_files(
            files=[target],
            output_dir=output_dir,
            root_dir=root,
            repo_root=root,
            model=None,
            batch_size=1,
            context="ctx",
            skip_existing=True,
            timeout=30,
            api_url="http://filigree.test",
            no_ingest=False,
            scan_run_id="run-1",
            scan_source="test",
            executor=fake_executor,
            prompt_template=PROMPT_TEMPLATE,
        )

        assert stats["P1"] == 1
        assert stats["api_files_posted"] == 1
        assert stats["api_files_failed"] == 0
        assert len(post_calls) == 2
        assert post_calls[0]["complete_scan_run"] is False
        assert post_calls[0]["create_observations"] is True
        assert post_calls[0]["findings"][0]["path"] == "target.py"
        assert post_calls[1]["findings"] == []
        assert post_calls[1]["complete_scan_run"] is True
        assert "[skip] target.py" in capsys.readouterr().err

    async def test_records_api_failures_for_finding_and_completion_posts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "target.py"
        target.write_text("x = 1\n")
        output_dir = tmp_path / "reports"

        async def fake_executor(**kwargs: object) -> None:
            Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kwargs["output_path"]).write_text(SINGLE_FINDING_MD, encoding="utf-8")

        monkeypatch.setattr("filigree.scanner_scripts.scan_utils.post_to_api", lambda **_kwargs: (False, "boom"))

        stats = await _analyse_files(
            files=[target],
            output_dir=output_dir,
            root_dir=root,
            repo_root=root,
            model=None,
            batch_size=1,
            context="ctx",
            skip_existing=False,
            timeout=30,
            api_url="http://filigree.test",
            no_ingest=False,
            scan_run_id="run-1",
            scan_source="test",
            executor=fake_executor,
            prompt_template=PROMPT_TEMPLATE,
        )

        assert stats["api_files_posted"] == 0
        assert stats["api_files_failed"] == 2
        err = capsys.readouterr().err
        assert "API error for target.py: boom" in err
        assert "API error completing scan run: boom" in err

    async def test_skip_existing_report_counts_clean_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "target.py"
        target.write_text("x = 1\n")
        output_dir = tmp_path / "reports"
        report = output_dir / "target.py.md"
        report.parent.mkdir(parents=True)
        report.write_text(NO_BUG_MD, encoding="utf-8")

        async def fake_executor(**_kwargs: object) -> None:
            raise AssertionError("skip-existing must not execute scanner")

        stats = await _analyse_files(
            files=[target],
            output_dir=output_dir,
            root_dir=root,
            repo_root=root,
            model=None,
            batch_size=1,
            context="ctx",
            skip_existing=True,
            timeout=30,
            api_url="http://filigree.test",
            no_ingest=True,
            scan_run_id="run-1",
            scan_source="test",
            executor=fake_executor,
            prompt_template=PROMPT_TEMPLATE,
        )

        assert stats["clean"] == 1
        assert stats["failed"] == 0
        assert "[skip] target.py" in capsys.readouterr().err

    async def test_executor_failure_and_unknown_report_are_summarized(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        failed_target = root / "failed.py"
        unknown_target = root / "unknown.py"
        failed_target.write_text("x = 1\n")
        unknown_target.write_text("y = 1\n")
        output_dir = tmp_path / "reports"

        async def fake_executor(**kwargs: object) -> None:
            output_path = Path(kwargs["output_path"])
            if output_path.name == "failed.py.md":
                raise RuntimeError("scanner exploded")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("## Summary\nBug without priority\n", encoding="utf-8")

        stats = await _analyse_files(
            files=[failed_target, unknown_target],
            output_dir=output_dir,
            root_dir=root,
            repo_root=root,
            model=None,
            batch_size=2,
            context="ctx",
            skip_existing=False,
            timeout=30,
            api_url="http://filigree.test",
            no_ingest=True,
            scan_run_id="run-1",
            scan_source="test",
            executor=fake_executor,
            prompt_template=PROMPT_TEMPLATE,
        )

        assert stats["failed"] == 1
        assert stats["unknown"] == 1
        err = capsys.readouterr().err
        assert "FAIL failed.py: scanner exploded" in err
        assert "[2/2] unknown.py" in err

    async def test_partial_ingest_failure_returns_nonzero_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1 of 2 findings POSTs fails: dropped findings must not be reported as
        # success. The old guard only fired when *every* post failed.
        project_root = tmp_path / "project"
        src = project_root / "src"
        src.mkdir(parents=True)
        (src / "a.py").write_text("x = 1\n")
        (src / "b.py").write_text("y = 1\n")

        monkeypatch.chdir(project_root)
        monkeypatch.setattr(
            sys,
            "argv",
            ["scanner", "--root", "src", "--batch-size", "1", "--scan-run-id", "run-1"],
        )

        findings_posts = 0

        def fake_post_to_api(**kwargs: Any) -> tuple[bool, str]:
            nonlocal findings_posts
            if kwargs.get("complete_scan_run") is True:
                return True, ""
            findings_posts += 1
            if findings_posts == 2:
                return False, "boom"
            return True, ""

        monkeypatch.setattr("filigree.scanner_scripts.scan_utils.post_to_api", fake_post_to_api)

        async def fake_executor(**kwargs: object) -> None:
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(SINGLE_FINDING_MD, encoding="utf-8")

        rc = await run_scanner_pipeline(
            executor=fake_executor,
            scan_source="test",
            prompt_template=PROMPT_TEMPLATE,
        )

        assert rc == 1

    async def test_completion_post_failure_returns_nonzero_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # All per-file POSTs succeed but the completion POST fails: the scan run
        # is left un-completed, so the pipeline must report failure.
        project_root = tmp_path / "project"
        src = project_root / "src"
        src.mkdir(parents=True)
        (src / "target.py").write_text("x = 1\n")

        monkeypatch.chdir(project_root)
        monkeypatch.setattr(
            sys,
            "argv",
            ["scanner", "--root", "src", "--scan-run-id", "run-1"],
        )

        def fake_post_to_api(**kwargs: Any) -> tuple[bool, str]:
            if kwargs.get("complete_scan_run") is True:
                return False, "boom"
            return True, ""

        monkeypatch.setattr("filigree.scanner_scripts.scan_utils.post_to_api", fake_post_to_api)

        async def fake_executor(**kwargs: object) -> None:
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(SINGLE_FINDING_MD, encoding="utf-8")

        rc = await run_scanner_pipeline(
            executor=fake_executor,
            scan_source="test",
            prompt_template=PROMPT_TEMPLATE,
        )

        assert rc == 1


# ── severity_map ───────────────────────────────────────────────────────


class TestSeverityMap:
    """severity_map() maps scanner-native severities to filigree severities."""

    def test_major_maps_to_high(self) -> None:
        assert severity_map("major") == "high"

    def test_minor_maps_to_medium(self) -> None:
        assert severity_map("minor") == "medium"

    def test_trivial_maps_to_low(self) -> None:
        assert severity_map("trivial") == "low"

    def test_critical_passes_through(self) -> None:
        assert severity_map("critical") == "critical"

    def test_high_passes_through(self) -> None:
        assert severity_map("high") == "high"

    def test_medium_passes_through(self) -> None:
        assert severity_map("medium") == "medium"

    def test_low_passes_through(self) -> None:
        assert severity_map("low") == "low"

    def test_info_passes_through(self) -> None:
        assert severity_map("info") == "info"

    def test_unknown_maps_to_info(self) -> None:
        assert severity_map("extreme") == "info"

    def test_case_insensitive(self) -> None:
        assert severity_map("MAJOR") == "high"
        assert severity_map("Minor") == "medium"

    def test_whitespace_stripped(self) -> None:
        assert severity_map("  major  ") == "high"


# ── parse_findings ─────────────────────────────────────────────────────


SINGLE_FINDING_MD = """\
## Summary
Off-by-one error in loop boundary

## Severity
- Severity: major
- Priority: P1

## Evidence
src/filigree/core.py:42 — `for i in range(len(items))` should be `range(len(items) - 1)`

## Root Cause Hypothesis
Classic fencepost error when iterating pairs.

## Suggested Fix
Change `range(len(items))` to `range(len(items) - 1)`.
"""

TWO_FINDINGS_MD = """\
## Summary
SQL injection in query builder

## Severity
- Severity: critical
- Priority: P0

## Evidence
src/filigree/dashboard.py:99 — f-string concatenation in SQL

## Root Cause Hypothesis
Direct string interpolation instead of parameterised query.

## Suggested Fix
Use `?` placeholders.

---

## Summary
Unclosed file handle in export

## Severity
- Severity: minor
- Priority: P2

## Evidence
src/filigree/core.py:200 — `open()` without context manager

## Root Cause Hypothesis
Missing `with` statement.

## Suggested Fix
Wrap in `with open(...) as f:`.
"""

NO_BUG_MD = "No concrete bug found in src/filigree/core.py"


class TestParseFindings:
    """parse_findings() extracts structured findings from markdown."""

    def test_empty_text_returns_empty(self) -> None:
        assert parse_findings("") == []
        assert parse_findings("   ") == []

    def test_no_bug_sentinel_returns_empty(self) -> None:
        assert parse_findings(NO_BUG_MD) == []

    def test_single_finding(self) -> None:
        findings = parse_findings(SINGLE_FINDING_MD, file_path="src/filigree/core.py")
        assert len(findings) == 1
        f = findings[0]
        assert f["path"] == "src/filigree/core.py"
        assert f["severity"] == "high"  # major → high via severity_map
        assert f["rule_id"] == "logic-error"  # "off-by-one" keyword
        assert "Off-by-one" in f["message"]
        assert f["suggestion"] == "Change `range(len(items))` to `range(len(items) - 1)`."
        assert f["line_start"] == 42

    def test_two_findings_separated_by_dashes(self) -> None:
        findings = parse_findings(TWO_FINDINGS_MD, file_path="test.py")
        assert len(findings) == 2
        assert findings[0]["severity"] == "critical"
        assert findings[0]["rule_id"] == "injection"  # "SQL" keyword
        assert findings[1]["severity"] == "medium"  # minor → medium
        assert findings[1]["rule_id"] == "resource-leak"  # "unclosed" keyword

    def test_root_cause_appended_to_message(self) -> None:
        findings = parse_findings(SINGLE_FINDING_MD, file_path="x.py")
        assert "Root cause:" in findings[0]["message"]

    def test_line_number_extracted_from_evidence(self) -> None:
        findings = parse_findings(SINGLE_FINDING_MD, file_path="x.py")
        assert findings[0]["line_start"] == 42

    def test_missing_evidence_no_line_start(self) -> None:
        md = "## Summary\nSome bug\n\n## Severity\n- Severity: minor\n"
        findings = parse_findings(md, file_path="x.py")
        assert len(findings) == 1
        assert "line_start" not in findings[0]

    def test_default_severity_when_missing(self) -> None:
        md = "## Summary\nSome bug without severity section\n"
        findings = parse_findings(md, file_path="x.py")
        assert len(findings) == 1
        assert findings[0]["severity"] == "info"


# ── _infer_rule_id ─────────────────────────────────────────────────────


class TestInferRuleId:
    """_infer_rule_id() maps free-text summaries to canonical rule IDs."""

    def test_logic_keywords(self) -> None:
        assert _infer_rule_id("Off-by-one logic error") == "logic-error"
        assert _infer_rule_id("Unreachable branch") == "logic-error"

    def test_resource_keywords(self) -> None:
        assert _infer_rule_id("Unclosed file handle") == "resource-leak"
        assert _infer_rule_id("Memory leak in pool") == "resource-leak"

    def test_race_keywords(self) -> None:
        assert _infer_rule_id("Race condition in async handler") == "race-condition"

    def test_injection_keywords(self) -> None:
        assert _infer_rule_id("SQL injection vulnerability") == "injection"
        assert _infer_rule_id("XSS in template") == "injection"

    def test_performance_keywords(self) -> None:
        assert _infer_rule_id("O(n² performance issue") == "performance"
        # "Blocking" contains "lock" which matches race-condition first
        # in keyword iteration order; use explicit "performance" keyword
        assert _infer_rule_id("Performance bottleneck in loop") == "performance"

    def test_api_keywords(self) -> None:
        assert _infer_rule_id("Deprecated API usage") == "api-misuse"

    def test_error_handling_keywords(self) -> None:
        assert _infer_rule_id("Bare exception handler") == "error-handling"

    def test_unknown_maps_to_other(self) -> None:
        assert _infer_rule_id("Something completely different") == "other"


# ── post_to_api ────────────────────────────────────────────────────────


class TestPostToApi:
    """post_to_api() POSTs findings to the filigree scan API."""

    def _mock_urlopen(self, *, status: int = 200, body: dict[str, Any] | None = None) -> MagicMock:
        """Create a mock for urllib.request.urlopen."""
        resp = MagicMock()
        resp.read.return_value = json.dumps(body or {"status": "ok"}).encode("utf-8")
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_success_returns_true(self) -> None:
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()):
            result = post_to_api(
                api_url="http://localhost:8377",
                scan_source="test",
                scan_run_id="run-1",
                findings=[{"path": "x.py", "rule_id": "other", "severity": "info", "message": "test"}],
            )
        assert result == (True, "")

    def test_sends_correct_payload(self) -> None:
        findings = [{"path": "x.py", "rule_id": "other", "severity": "info", "message": "test"}]
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()) as mock_open:
            post_to_api(
                api_url="http://localhost:8377",
                scan_source="codex",
                scan_run_id="run-42",
                findings=findings,
            )
            req = mock_open.call_args[0][0]
            assert req.full_url == "http://localhost:8377/api/scan-results"
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["scan_source"] == "codex"
            assert payload["scan_run_id"] == "run-42"
            assert payload["findings"] == findings
            assert req.get_header("Content-type") == "application/json"

    def test_http_error_returns_false(self) -> None:
        import urllib.error

        err = urllib.error.HTTPError(
            url="http://localhost:8377/api/v1/scan-results",
            code=500,
            msg="Internal Server Error",
            hdrs={},  # type: ignore[arg-type]
            fp=MagicMock(read=MagicMock(return_value=b"error")),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = post_to_api(
                api_url="http://localhost:8377",
                scan_source="test",
                scan_run_id="run-1",
                findings=[{"path": "x.py", "rule_id": "other", "severity": "info", "message": "test"}],
            )
        ok, detail = result
        assert ok is False
        assert "HTTP 500" in detail

    def test_url_error_returns_false(self) -> None:
        import urllib.error

        err = urllib.error.URLError("Connection refused")
        with patch("urllib.request.urlopen", side_effect=err):
            result = post_to_api(
                api_url="http://localhost:8377",
                scan_source="test",
                scan_run_id="run-1",
                findings=[{"path": "x.py", "rule_id": "other", "severity": "info", "message": "test"}],
            )
        ok, detail = result
        assert ok is False
        assert "Connection" in detail

    def test_api_warnings_logged(self) -> None:
        body = {"status": "ok", "warnings": ["severity coerced: extreme → info"]}
        with (
            patch("urllib.request.urlopen", return_value=self._mock_urlopen(body=body)),
            patch("filigree.scanner_scripts.scan_utils.logger") as mock_logger,
        ):
            post_to_api(
                api_url="http://localhost:8377",
                scan_source="test",
                scan_run_id="run-1",
                findings=[{"path": "x.py", "rule_id": "other", "severity": "info", "message": "test"}],
            )
            mock_logger.warning.assert_called()
            warn_call = mock_logger.warning.call_args
            assert "severity coerced" in str(warn_call)

    def test_http_error_body_read_failure_still_logs(self) -> None:
        """Bug filigree-4876d3: network failure reading error body should not crash."""
        import urllib.error

        err = urllib.error.HTTPError(
            url="http://localhost:8377/api/v1/scan-results",
            code=500,
            msg="Internal Server Error",
            hdrs={},  # type: ignore[arg-type]
            fp=MagicMock(read=MagicMock(side_effect=OSError("connection reset"))),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = post_to_api(
                api_url="http://localhost:8377",
                scan_source="test",
                scan_run_id="run-1",
                findings=[{"path": "x.py", "rule_id": "other", "severity": "info", "message": "test"}],
            )
        ok, detail = result
        assert ok is False  # Graceful degradation: returns error detail, doesn't crash
        assert "HTTP 500" in detail


# ── find_files ─────────────────────────────────────────────────────────


class TestFindFiles:
    """find_files() discovers source files with filtering and truncation."""

    def _make_tree(self, tmp_path: Path) -> Path:
        """Create a small file tree for testing."""
        root = tmp_path / "src"
        root.mkdir()
        (root / "a.py").write_text("# a")
        (root / "b.py").write_text("# b")
        (root / "c.py").write_text("# c")
        (root / "d.txt").write_text("data")
        (root / "test_e.py").write_text("# test")
        sub = root / "sub"
        sub.mkdir()
        (sub / "f.py").write_text("# f")
        # Excluded dir
        pycache = root / "__pycache__"
        pycache.mkdir()
        (pycache / "g.pyc").write_text("bytecode")
        return root

    def test_python_filter(self, tmp_path: Path) -> None:
        root = self._make_tree(tmp_path)
        files = find_files(root, file_type="python")
        names = {f.name for f in files}
        assert "a.py" in names
        assert "b.py" in names
        assert "d.txt" not in names
        assert "test_e.py" not in names  # test_ files excluded

    def test_all_filter(self, tmp_path: Path) -> None:
        root = self._make_tree(tmp_path)
        files = find_files(root, file_type="all")
        names = {f.name for f in files}
        assert "a.py" in names
        assert "d.txt" in names
        assert "test_e.py" in names
        assert "g.pyc" not in names  # binary excluded

    def test_max_files_truncation(self, tmp_path: Path) -> None:
        root = self._make_tree(tmp_path)
        files = find_files(root, file_type="python", max_files=2)
        assert len(files) == 2

    def test_max_files_zero_means_no_limit(self, tmp_path: Path) -> None:
        root = self._make_tree(tmp_path)
        all_files = find_files(root, file_type="python")
        limited = find_files(root, file_type="python", max_files=0)
        assert len(limited) == len(all_files)

    def test_exclude_dirs(self, tmp_path: Path) -> None:
        root = self._make_tree(tmp_path)
        sub = root / "sub"
        files = find_files(root, file_type="python", exclude_dirs={sub})
        names = {f.name for f in files}
        assert "f.py" not in names
        assert "a.py" in names

    def test_pycache_always_excluded(self, tmp_path: Path) -> None:
        root = self._make_tree(tmp_path)
        files = find_files(root, file_type="all")
        names = {f.name for f in files}
        assert "g.pyc" not in names

    def test_results_sorted(self, tmp_path: Path) -> None:
        root = self._make_tree(tmp_path)
        files = find_files(root, file_type="python")
        assert files == sorted(files)


# ── load_context ───────────────────────────────────────────────────────


class TestLoadContext:
    """load_context() loads CLAUDE.md and ARCHITECTURE.md from repo root."""

    def test_loads_existing_files(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Claude instructions")
        (tmp_path / "ARCHITECTURE.md").write_text("# Architecture")
        ctx = load_context(tmp_path)
        assert "Claude instructions" in ctx
        assert "Architecture" in ctx

    def test_missing_files_returns_empty(self, tmp_path: Path) -> None:
        ctx = load_context(tmp_path)
        assert ctx == ""

    def test_partial_files(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Only claude")
        ctx = load_context(tmp_path)
        assert "Only claude" in ctx
        assert "ARCHITECTURE" not in ctx


# ── estimate_tokens ────────────────────────────────────────────────────


class TestEstimateTokens:
    """estimate_tokens() provides rough token counts for cost estimation."""

    def test_basic_estimation(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("x" * 400)  # 400 chars → ~100 tokens
        tokens = estimate_tokens([f])
        assert tokens == (400 // 4) + 2000  # chars/4 + overhead

    def test_missing_file_uses_overhead(self, tmp_path: Path) -> None:
        f = tmp_path / "nonexistent.py"
        tokens = estimate_tokens([f])
        assert tokens == 2000  # just overhead

    def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("x" * 800)
        f2.write_text("y" * 400)
        tokens = estimate_tokens([f1, f2])
        expected = (800 // 4 + 2000) + (400 // 4 + 2000)
        assert tokens == expected
