"""Tests for install.py — instructions, gitignore, doctor."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from filigree.core import (
    CONFIG_FILENAME,
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    WEFT_DIR_NAME,
    WEFT_MEMBER_SUBDIR,
    FiligreeDB,
    find_filigree_command,
)
from filigree.install import (
    _INSTR_FENCE_RE,
    FILIGREE_INSTRUCTIONS,
    FILIGREE_INSTRUCTIONS_MARKER,
    SKILL_NAME,
    CheckResult,
    _atomic_write_text,
    _find_filigree_mcp_command,
    _has_hook_command,
    _instructions_hash,
    _instructions_version,
    ensure_filigree_dir_gitignore,
    ensure_gitignore,
    inject_instructions,
    install_claude_code_hooks,
    install_claude_code_mcp,
    install_codex_mcp,
    install_codex_skills,
    install_skills,
    run_doctor,
)


class TestInjectInstructions:
    def test_create_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        ok, _msg = inject_instructions(target)
        assert ok
        assert target.exists()
        content = target.read_text()
        assert FILIGREE_INSTRUCTIONS_MARKER in content
        assert "Filigree Issue Tracker" in content

    def test_append_to_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        target.write_text("# Existing content\n\nSome stuff here.\n")
        ok, _msg = inject_instructions(target)
        assert ok
        content = target.read_text()
        assert "Existing content" in content
        assert FILIGREE_INSTRUCTIONS_MARKER in content

    def test_replace_existing_block(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        # Write initial instructions
        inject_instructions(target)
        # Replace again — should be idempotent
        ok, msg = inject_instructions(target)
        assert ok
        assert "Updated" in msg
        content = target.read_text()
        # The marker prefix appears once in the opening tag
        assert content.count(FILIGREE_INSTRUCTIONS_MARKER) == 1

    def test_versioned_marker_format(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        inject_instructions(target)
        content = target.read_text()
        version = _instructions_version()
        h = _instructions_hash()
        assert f"<!-- filigree:instructions:v{version}:{h} -->" in content
        assert "<!-- /filigree:instructions -->" in content

    def test_replace_malformed_block(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"Before\n{FILIGREE_INSTRUCTIONS_MARKER}\nsome old stuff without end marker")
        ok, _msg = inject_instructions(target)
        assert ok
        content = target.read_text()
        assert "Before" in content
        assert "<!-- /filigree:instructions -->" in content

    def test_end_marker_before_start_marker_does_not_corrupt(self, tmp_path: Path) -> None:
        """End marker appearing before start marker must not cause malformed output."""
        end_marker = "<!-- /filigree:instructions -->"
        target = tmp_path / "CLAUDE.md"
        # Craft content where end marker appears before start marker
        target.write_text(f"Preamble\n{end_marker}\nMiddle\n{FILIGREE_INSTRUCTIONS_MARKER}\nold content\n{end_marker}\nAfter\n")
        ok, _msg = inject_instructions(target)
        assert ok
        content = target.read_text()
        # Preamble and the stray end marker before start should be preserved
        assert "Preamble" in content
        # The "After" section should be preserved
        assert "After" in content
        # "old content" between the real markers must be replaced, not duplicated
        assert "old content" not in content
        # "Middle" (between stray end marker and real start) must appear exactly once
        assert content.count("Middle") == 1

    def test_malformed_block_repair_is_idempotent(self, tmp_path: Path) -> None:
        """Repeated runs on a block missing its end marker must converge to a single clean block.

        Regression: previously the first run preserved the orphan tail after the
        newly-inserted block, and subsequent runs treated the tail as user content
        and kept preserving it forever.
        """
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"Before preamble\n{FILIGREE_INSTRUCTIONS_MARKER}\nstale body line 1\nstale body line 2\n")
        inject_instructions(target)
        # Run again to prove no orphan tail is dragged through a second pass.
        inject_instructions(target)
        content = target.read_text()
        # Content before the start marker must be preserved.
        assert "Before preamble" in content
        # Exactly one opening marker, exactly one end marker.
        assert content.count(FILIGREE_INSTRUCTIONS_MARKER) == 1
        assert content.count("<!-- /filigree:instructions -->") == 1
        # Stale body must not survive repair.
        assert "stale body line 1" not in content
        assert "stale body line 2" not in content


class TestAtomicWriteEmptyGuard:
    """filigree-04bad2a2bf: the atomic writer must never truncate a user-visible
    file to 0 bytes. Every legitimate caller has non-empty content, so an empty
    or whitespace-only payload is corruption/logic-bug — refuse it loudly."""

    def test_refuses_empty_content_on_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        with pytest.raises(ValueError, match="empty"):
            _atomic_write_text(target, "")
        assert not target.exists()

    def test_refuses_whitespace_only_content(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        with pytest.raises(ValueError, match="empty"):
            _atomic_write_text(target, "   \n\t\n")
        assert not target.exists()

    def test_refuses_empty_and_preserves_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        original = "# My instructions\n\nImportant content.\n"
        target.write_text(original)
        with pytest.raises(ValueError, match="empty"):
            _atomic_write_text(target, "")
        # The populated file must be left exactly as it was.
        assert target.read_text() == original

    def test_does_not_leak_temp_file_on_refusal(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        with pytest.raises(ValueError, match="empty"):
            _atomic_write_text(target, "")
        # The guard fires before mkstemp, so no .tmp sidecar is left behind.
        assert list(tmp_path.iterdir()) == []


class TestInstructionWriteLock:
    """filigree-04bad2a2bf: inject_instructions serialises its read-modify-write
    across processes with a blocking exclusive lock under .filigree/."""

    def test_uses_blocking_exclusive_lock_when_project_initialised(self, tmp_path: Path) -> None:
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        target = tmp_path / "CLAUDE.md"
        with patch("filigree.install.portalocker.lock") as mock_lock:
            ok, _msg = inject_instructions(target)
        assert ok
        assert FILIGREE_INSTRUCTIONS_MARKER in target.read_text()
        # Locked exactly once, exclusively (blocking — never LOCK_NB).
        mock_lock.assert_called_once()
        import portalocker

        flags = mock_lock.call_args.args[1]
        assert flags & portalocker.LOCK_EX
        assert not (flags & portalocker.LOCK_NB)
        assert (tmp_path / FILIGREE_DIR_NAME / "instructions.lock").exists()

    def test_locks_under_weft_store_on_fresh_3_0_project(self, tmp_path: Path) -> None:
        """3.0 fresh layout: only ``.weft/filigree/`` exists, no ``.filigree/``.

        The lock must follow the resolved store dir, not a hardcoded
        ``.filigree/`` — otherwise the read-modify-write proceeds unlocked on
        every SessionStart of a normally-initialised 3.0 project
        (filigree-04bad2a2bf regression).
        """
        weft_store = tmp_path / WEFT_DIR_NAME / WEFT_MEMBER_SUBDIR
        weft_store.mkdir(parents=True)
        target = tmp_path / "CLAUDE.md"
        with patch("filigree.install.portalocker.lock") as mock_lock:
            ok, _msg = inject_instructions(target)
        assert ok
        assert FILIGREE_INSTRUCTIONS_MARKER in target.read_text()
        mock_lock.assert_called_once()
        # The real artifact lands in the weft store, and no legacy husk is forged.
        assert (weft_store / "instructions.lock").exists()
        assert not (tmp_path / FILIGREE_DIR_NAME).exists()

    def test_locks_under_weft_store_when_both_layouts_present(self, tmp_path: Path) -> None:
        """Migrated project: ``migrate_store_to_weft`` leaves the legacy
        ``.filigree/`` husk in place beside the new ``.weft/filigree/``.

        Mutual exclusion requires ONE deterministic lock location per project
        across all racing processes. resolve_store_dir's precedence puts the
        weft store ahead of the legacy dir, so the lock must land there — a
        naive "lock whichever dir exists" would let two processes split between
        ``.filigree/`` and ``.weft/filigree/`` and silently lose exclusion.
        """
        weft_store = tmp_path / WEFT_DIR_NAME / WEFT_MEMBER_SUBDIR
        weft_store.mkdir(parents=True)
        (tmp_path / FILIGREE_DIR_NAME).mkdir()  # legacy husk left by migration
        target = tmp_path / "CLAUDE.md"
        with patch("filigree.install.portalocker.lock") as mock_lock:
            ok, _msg = inject_instructions(target)
        assert ok
        mock_lock.assert_called_once()
        assert (weft_store / "instructions.lock").exists()
        assert not (tmp_path / FILIGREE_DIR_NAME / "instructions.lock").exists()

    def test_proceeds_unlocked_without_any_store_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        with patch("filigree.install.portalocker.lock") as mock_lock:
            ok, _msg = inject_instructions(target)
        assert ok
        assert FILIGREE_INSTRUCTIONS_MARKER in target.read_text()
        mock_lock.assert_not_called()

    def test_concurrent_injections_preserve_user_content(self, tmp_path: Path) -> None:
        """Serialised real-flock injections must not drop surrounding user text."""
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        target = tmp_path / "CLAUDE.md"
        target.write_text("# User heading\n\nUser body.\n")
        # Two sequential injections (the lock makes concurrent ones equivalent).
        inject_instructions(target)
        inject_instructions(target)
        content = target.read_text()
        assert "User heading" in content
        assert "User body." in content
        assert content.count(FILIGREE_INSTRUCTIONS_MARKER) == 1


_WARDLINE_BLOCK = "<!-- wardline:instructions:v1:abcd1234 -->\nwardline body line\n<!-- /wardline:instructions -->"
_END = "<!-- /filigree:instructions -->"


class TestForeignBlockSafety:
    """filigree-bcbd4d66fd: inject_instructions must never delete a co-resident
    sibling tool's block (wardline/legis) in a shared CLAUDE.md/AGENTS.md."""

    def test_malformed_filigree_preserves_following_foreign_block(self, tmp_path: Path) -> None:
        """The named repro: an UNCLOSED filigree block followed by a well-formed
        wardline block. The old truncate-to-EOF deleted the wardline block."""
        target = tmp_path / "CLAUDE.md"
        target.write_text(
            "Before user preamble.\n"
            "<!-- filigree:instructions:v2.9.0:deadbeef -->\n"
            "filigree workflow body\n"  # NO filigree end marker
            f"{_WARDLINE_BLOCK}\n"
        )
        ok, _msg = inject_instructions(target)
        assert ok
        content = target.read_text()
        # Wardline block survives intact.
        assert "wardline body line" in content
        assert content.count("<!-- wardline:instructions:v1:abcd1234 -->") == 1
        assert content.count("<!-- /wardline:instructions -->") == 1
        # User preamble survives; filigree block is now well-formed and singular.
        assert "Before user preamble." in content
        assert content.count(FILIGREE_INSTRUCTIONS_MARKER) == 1
        assert content.count(_END) == 1

    def test_shape2_sandwiched_foreign_block_preserved(self, tmp_path: Path) -> None:
        """Unclosed-first filigree / wardline / closed-later filigree: the
        well-formed `find` would jump the wardline block; bounded scan must not."""
        target = tmp_path / "CLAUDE.md"
        target.write_text(
            "<!-- filigree:instructions:v1:aaaaaaaa -->\n"
            "stale filigree body 1\n"  # first block unclosed
            f"{_WARDLINE_BLOCK}\n"
            "<!-- filigree:instructions:v1:aaaaaaaa -->\n"
            "stale filigree body 2\n"
            f"{_END}\n"
        )
        ok, _msg = inject_instructions(target)
        assert ok
        content = target.read_text()
        # The sandwiched wardline block is NOT swallowed.
        assert "wardline body line" in content
        assert content.count("<!-- wardline:instructions:v1:abcd1234 -->") == 1

    def test_uppercase_namespace_sibling_preserved(self, tmp_path: Path) -> None:
        """Refinement: an uppercase-namespaced sibling fence must still be seen as
        a boundary (case-insensitive), else it is truncated to EOF as before."""
        target = tmp_path / "CLAUDE.md"
        target.write_text(
            "<!-- filigree:instructions:v1:deadbeef -->\n"
            "filigree body\n"  # unclosed
            "<!-- Wardline:instructions:v1:abcd1234 -->\n"
            "uppercase sibling body\n"
            "<!-- /Wardline:instructions -->\n"
        )
        ok, _msg = inject_instructions(target)
        assert ok
        content = target.read_text()
        assert "uppercase sibling body" in content
        assert "Wardline:instructions" in content

    def test_well_formed_filigree_with_trailing_foreign_preserved(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        inject_instructions(target)  # well-formed filigree block
        target.write_text(target.read_text().rstrip("\n") + f"\n\n{_WARDLINE_BLOCK}\n")
        ok, _msg = inject_instructions(target)  # replace filigree in place
        assert ok
        content = target.read_text()
        assert "wardline body line" in content
        assert content.count("<!-- /wardline:instructions -->") == 1
        assert content.count(FILIGREE_INSTRUCTIONS_MARKER) == 1

    def test_foreign_block_before_filigree_preserved(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"{_WARDLINE_BLOCK}\n\n")
        inject_instructions(target)  # appends filigree after wardline
        ok, _msg = inject_instructions(target)  # replace filigree in place
        assert ok
        content = target.read_text()
        assert "wardline body line" in content
        assert content.count("<!-- /wardline:instructions -->") == 1
        assert content.count(FILIGREE_INSTRUCTIONS_MARKER) == 1

    def test_bounded_recovery_is_idempotent(self, tmp_path: Path) -> None:
        """Repeated runs over the malformed+foreign repro must converge and not
        accumulate separators or duplicate filigree blocks."""
        target = tmp_path / "CLAUDE.md"
        target.write_text(f"<!-- filigree:instructions:v9:deadbeef -->\nstale body\n{_WARDLINE_BLOCK}\n")
        inject_instructions(target)
        after_first = target.read_text()
        inject_instructions(target)
        after_second = target.read_text()
        assert after_first == after_second  # fixpoint reached
        assert after_second.count(FILIGREE_INSTRUCTIONS_MARKER) == 1
        assert after_second.count(_END) == 1
        assert "wardline body line" in after_second

    def test_instructions_body_contains_no_foreign_fence(self) -> None:
        """Refinement guard: the shipped filigree block must contain ONLY
        filigree-namespace instruction fences. A foreign-looking ':instructions'
        token in the body would create a spurious boundary and corrupt the common
        well-formed replace path on the next inject."""
        namespaces = {m.group(1).lower() for m in _INSTR_FENCE_RE.finditer(FILIGREE_INSTRUCTIONS)}
        assert namespaces <= {"filigree"}, f"instructions.md body has non-filigree fence(s): {namespaces}"


class TestInstructionsVersionFallback:
    def test_instructions_version_falls_back_to_package_version(self) -> None:
        """_instructions_version should fall back to filigree.__version__ when metadata is missing."""
        from importlib.metadata import PackageNotFoundError

        import filigree

        with (
            patch("filigree.install.importlib.metadata.version", side_effect=PackageNotFoundError("filigree")),
            patch.object(filigree, "__version__", "9.9.9-test"),
        ):
            assert _instructions_version() == "9.9.9-test"

    def test_import_install_module_without_metadata_does_not_raise(self) -> None:
        """Import/reload of filigree.install should not fail when package metadata is unavailable."""
        import importlib
        from importlib import metadata
        from importlib.metadata import PackageNotFoundError

        import filigree
        import filigree.install as install_mod

        real_version = metadata.version

        def _fake_version(dist_name: str) -> str:
            if dist_name == "filigree":
                raise PackageNotFoundError(dist_name)
            return real_version(dist_name)

        with (
            patch("importlib.metadata.version", side_effect=_fake_version),
            patch.object(filigree, "__version__", "9.9.9-test"),
        ):
            reloaded = importlib.reload(install_mod)
            assert "<!-- filigree:instructions:v9.9.9-test:" in reloaded.FILIGREE_INSTRUCTIONS

        # Avoid leaking patched module-level constants to later tests.
        importlib.reload(install_mod)


class TestAtomicWritePermissions:
    """Regression: _atomic_write_text must preserve permissions on existing files.

    `tempfile.mkstemp()` creates files with mode 0o600 by design. Naive
    write-temp + os.replace would leak that mode onto the destination,
    making CLAUDE.md / AGENTS.md / .gitignore owner-only after each install.
    """

    def test_inject_instructions_preserves_existing_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "CLAUDE.md"
        target.write_text("# pre-existing\n")
        os.chmod(target, 0o644)
        before = stat.S_IMODE(target.stat().st_mode)
        assert before == 0o644, "test setup failed to chmod 0o644"

        ok, _msg = inject_instructions(target)
        assert ok
        after = stat.S_IMODE(target.stat().st_mode)
        assert after == before, f"expected mode preserved as 0o644, got {oct(after)}"

    def test_inject_instructions_preserves_executable_mode(self, tmp_path: Path) -> None:
        """Even unusual modes (e.g. 0o755) must be preserved on rewrite."""
        target = tmp_path / "AGENTS.md"
        target.write_text("# pre-existing\n")
        os.chmod(target, 0o755)  # noqa: S103 — exercising mode preservation
        before = stat.S_IMODE(target.stat().st_mode)

        # Trigger a rewrite by injecting a second time (the marker is now present).
        inject_instructions(target)
        ok, _msg = inject_instructions(target)
        assert ok
        after = stat.S_IMODE(target.stat().st_mode)
        assert after == before, f"expected mode preserved as 0o755, got {oct(after)}"

    def test_ensure_gitignore_preserves_existing_mode(self, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n")
        os.chmod(gitignore, 0o644)
        before = stat.S_IMODE(gitignore.stat().st_mode)

        ok, _msg = ensure_gitignore(tmp_path)
        assert ok
        after = stat.S_IMODE(gitignore.stat().st_mode)
        assert after == before, f"expected mode preserved as 0o644, got {oct(after)}"

    def test_new_file_respects_umask(self, tmp_path: Path) -> None:
        """Newly created files should be world-readable per default umask, not 0o600."""
        target = tmp_path / "CLAUDE.md"
        old_umask = os.umask(0o022)
        try:
            ok, _msg = inject_instructions(target)
        finally:
            os.umask(old_umask)
        assert ok
        mode = stat.S_IMODE(target.stat().st_mode)
        # With umask 022, expect 0o644 (or at minimum: group/other readable).
        assert mode & 0o044, f"new file created with overly restrictive mode {oct(mode)}"


class TestEnsureGitignore:
    def test_create_gitignore(self, tmp_path: Path) -> None:
        ok, _msg = ensure_gitignore(tmp_path)
        assert ok
        assert (tmp_path / ".gitignore").exists()
        assert ".filigree/" in (tmp_path / ".gitignore").read_text()

    def test_append_to_existing(self, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n")
        ok, _msg = ensure_gitignore(tmp_path)
        assert ok
        content = gitignore.read_text()
        assert "*.pyc" in content
        assert ".filigree/" in content

    def test_already_present(self, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".weft/\n.filigree/\n")
        ok, msg = ensure_gitignore(tmp_path)
        assert ok
        assert "already" in msg

    def test_weft_store_ignored_when_only_filigree_present(self, tmp_path: Path) -> None:
        """A legacy-only ``.filigree/`` gitignore gains the ``.weft/`` rule too."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".filigree/\n")
        ok, msg = ensure_gitignore(tmp_path)
        assert ok
        content = gitignore.read_text()
        assert ".weft/" in content
        assert "Added .weft/" in msg

    def test_creates_both_rules_on_fresh_gitignore(self, tmp_path: Path) -> None:
        ok, _msg = ensure_gitignore(tmp_path)
        assert ok
        content = (tmp_path / ".gitignore").read_text()
        assert ".weft/" in content
        assert ".filigree/" in content

    def test_commented_pattern_not_treated_as_ignored(self, tmp_path: Path) -> None:
        """A `#.filigree/` comment must not count as an active ignore rule."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("#.filigree/\n")
        ok, _msg = ensure_gitignore(tmp_path)
        assert ok
        lines = gitignore.read_text().splitlines()
        # Active entry (not commented) must now exist.
        assert ".filigree/" in lines

    def test_negated_pattern_not_treated_as_ignored(self, tmp_path: Path) -> None:
        """A `!.filigree/` negation un-ignores; it must not count as an ignore rule."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("!.filigree/\n")
        ok, _msg = ensure_gitignore(tmp_path)
        assert ok
        lines = gitignore.read_text().splitlines()
        # Active entry must now exist alongside (or instead of) the negation.
        assert ".filigree/" in lines

    def test_subpath_substring_not_treated_as_ignored(self, tmp_path: Path) -> None:
        """Any non-root path that contains `.filigree/` as substring must not satisfy the check."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("src/some/.filigree/cache/\n")
        ok, _msg = ensure_gitignore(tmp_path)
        assert ok
        lines = gitignore.read_text().splitlines()
        # A real root-level entry must have been added.
        assert ".filigree/" in lines

    def test_root_anchored_pattern_accepted(self, tmp_path: Path) -> None:
        """`/.filigree/` is an anchored ignore rule — must be recognised as already present."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".weft/\n/.filigree/\n")
        ok, msg = ensure_gitignore(tmp_path)
        assert ok
        assert "already" in msg

    def test_bare_name_without_trailing_slash_accepted(self, tmp_path: Path) -> None:
        """`.filigree` (no trailing slash) matches the directory — must be recognised."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".weft/\n.filigree\n")
        ok, msg = ensure_gitignore(tmp_path)
        assert ok
        assert "already" in msg

    def test_later_negation_unignores_earlier_rule(self, tmp_path: Path) -> None:
        """`.filigree/` followed by `!.filigree/` un-ignores per gitignore semantics.

        The check must add a new active rule rather than treating the file
        as already ignored. Regression guard for GH PR #33 review #3.
        """
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".filigree/\n!.filigree/\n")
        ok, msg = ensure_gitignore(tmp_path)
        assert ok
        # Should NOT be treated as already-ignored — the negation cancelled it.
        assert "already" not in msg
        # New active rule must be appended.
        content = gitignore.read_text()
        assert content.count(".filigree/") >= 3  # original, negation, plus appended

    def test_negation_then_reignore_counts_as_ignored(self, tmp_path: Path) -> None:
        """`!.filigree/` followed by `.filigree/` re-ignores — last rule wins."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".weft/\n!.filigree/\n.filigree/\n")
        ok, msg = ensure_gitignore(tmp_path)
        assert ok
        assert "already" in msg


class TestEnsureFiligreeDirGitignore:
    """The nested ``.filigree/.gitignore`` keeps ephemeral runtime files out of
    commits when a project deliberately tracks its ``.filigree/`` dir (e.g. a
    demo or a team that commits its issue DB). See filigree-694f777d5c.
    """

    def _make_dir(self, tmp_path: Path) -> Path:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        return filigree_dir

    def test_creates_nested_gitignore(self, tmp_path: Path) -> None:
        filigree_dir = self._make_dir(tmp_path)
        ok, _msg = ensure_filigree_dir_gitignore(filigree_dir)
        assert ok
        nested = filigree_dir / ".gitignore"
        assert nested.exists()
        body = nested.read_text()
        # Ephemeral sidecars / logs / runtime state are excluded.
        for pattern in ("*.db-wal", "*.db-shm", "*.log", "context.md", "ephemeral.port"):
            assert pattern in body, f"expected {pattern!r} in nested .gitignore"

    def test_does_not_ignore_durable_files(self, tmp_path: Path) -> None:
        """Design decision: filigree.db and config.json are DURABLE — the nested
        file must not list them, so a tracked dir still commits the issue data.
        """
        filigree_dir = self._make_dir(tmp_path)
        ensure_filigree_dir_gitignore(filigree_dir)
        body = filigree_dir / ".gitignore"
        lines = {ln.strip() for ln in body.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")}
        assert DB_FILENAME not in lines
        assert CONFIG_FILENAME not in lines

    def test_git_check_ignore_semantics(self, tmp_path: Path) -> None:
        """Verify against *real* git: ephemeral ignored, durable tracked.

        Exercises the actual scenario — a project tracking its ``.filigree/``
        dir (no root rule), relying on the nested file to keep ephemerals out.
        """
        if not shutil.which("git"):
            pytest.skip("git not available")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        filigree_dir = self._make_dir(tmp_path)
        ensure_filigree_dir_gitignore(filigree_dir)

        def ignored(rel: str) -> bool:
            return (
                subprocess.run(
                    ["git", "check-ignore", "-q", rel],
                    cwd=tmp_path,
                ).returncode
                == 0
            )

        # Ephemeral → ignored
        assert ignored(f"{FILIGREE_DIR_NAME}/filigree.db-wal")
        assert ignored(f"{FILIGREE_DIR_NAME}/filigree.db-shm")
        assert ignored(f"{FILIGREE_DIR_NAME}/filigree.log")
        assert ignored(f"{FILIGREE_DIR_NAME}/context.md")
        assert ignored(f"{FILIGREE_DIR_NAME}/ephemeral.lock")
        assert ignored(f"{FILIGREE_DIR_NAME}/filigree.db.pre-v26-bak")
        # Durable → NOT ignored
        assert not ignored(f"{FILIGREE_DIR_NAME}/{DB_FILENAME}")
        assert not ignored(f"{FILIGREE_DIR_NAME}/{CONFIG_FILENAME}")

    def test_idempotent(self, tmp_path: Path) -> None:
        filigree_dir = self._make_dir(tmp_path)
        ensure_filigree_dir_gitignore(filigree_dir)
        ok, msg = ensure_filigree_dir_gitignore(filigree_dir)
        assert ok
        assert "already" in msg
        # No duplicate marker block.
        body = (filigree_dir / ".gitignore").read_text()
        assert body.count("*.db-wal") == 1

    def test_preserves_user_content(self, tmp_path: Path) -> None:
        filigree_dir = self._make_dir(tmp_path)
        nested = filigree_dir / ".gitignore"
        nested.write_text("# my own rule\nsecret-notes.txt\n")
        ok, _msg = ensure_filigree_dir_gitignore(filigree_dir)
        assert ok
        body = nested.read_text()
        assert "secret-notes.txt" in body
        assert "*.db-wal" in body


class TestRunDoctor:
    def test_healthy_project(self, filigree_project: Path) -> None:
        # Add .gitignore
        (filigree_project / ".gitignore").write_text(".filigree/\n")
        # Initialize DB schema version
        filigree_dir = filigree_project / FILIGREE_DIR_NAME
        d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="proj")
        d.initialize()
        d.close()

        results = run_doctor(filigree_project)
        # Should have at least a few passing checks
        passed = [r for r in results if r.passed]
        assert len(passed) >= 3

    def test_missing_filigree_dir(self, tmp_path: Path) -> None:
        results = run_doctor(tmp_path)
        assert any(not r.passed and "directory" in r.name.lower() for r in results)
        # Should short-circuit — can't proceed
        assert len(results) == 1

    def test_stale_context_md(self, filigree_project: Path) -> None:
        summary_path = filigree_project / FILIGREE_DIR_NAME / SUMMARY_FILENAME
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(str(summary_path), (old_time, old_time))
        results = run_doctor(filigree_project)
        context_check = next((r for r in results if "context" in r.name.lower()), None)
        assert context_check is not None
        assert not context_check.passed
        assert "stale" in context_check.message.lower()

    def test_schema_version_check(self, filigree_project: Path) -> None:
        """Doctor should report schema version."""
        filigree_dir = filigree_project / FILIGREE_DIR_NAME
        d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="proj")
        d.initialize()
        d.close()
        results = run_doctor(filigree_project)
        version_check = next((r for r in results if "schema" in r.name.lower()), None)
        assert version_check is not None
        assert version_check.passed

    def test_config_json_decode_error(self, filigree_project: Path) -> None:
        """Doctor should detect invalid config.json."""
        config_path = filigree_project / FILIGREE_DIR_NAME / CONFIG_FILENAME
        config_path.write_text("{invalid json!!!")
        results = run_doctor(filigree_project)
        config_check = next((r for r in results if r.name == "config.json"), None)
        assert config_check is not None
        assert not config_check.passed
        assert "Invalid JSON" in config_check.message

    def test_config_json_non_object(self, filigree_project: Path) -> None:
        """Doctor should reject config.json that is valid JSON but not an object."""
        config_path = filigree_project / FILIGREE_DIR_NAME / CONFIG_FILENAME
        config_path.write_text("[]")
        results = run_doctor(filigree_project)
        config_check = next((r for r in results if r.name == "config.json"), None)
        assert config_check is not None
        assert not config_check.passed
        assert "expected an object" in config_check.message

    def test_non_dict_mcp_json_does_not_crash(self, filigree_project: Path) -> None:
        """Doctor should handle .mcp.json containing a list instead of a dict."""
        mcp_path = filigree_project / ".mcp.json"
        mcp_path.write_text("[]")
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if "Claude Code MCP" in r.name), None)
        assert mcp_check is not None
        assert not mcp_check.passed

    def test_missing_config_json(self, filigree_project: Path) -> None:
        """Doctor should detect missing config.json."""
        config_path = filigree_project / FILIGREE_DIR_NAME / CONFIG_FILENAME
        config_path.unlink()
        results = run_doctor(filigree_project)
        config_check = next((r for r in results if r.name == "config.json"), None)
        assert config_check is not None
        assert not config_check.passed
        assert "Missing" in config_check.message

    def test_missing_db(self, filigree_project: Path) -> None:
        """Doctor should detect missing filigree.db."""
        db_path = filigree_project / FILIGREE_DIR_NAME / DB_FILENAME
        db_path.unlink()
        results = run_doctor(filigree_project)
        db_check = next((r for r in results if r.name == "filigree.db"), None)
        assert db_check is not None
        assert not db_check.passed
        assert "Missing" in db_check.message

    def test_db_error(self, filigree_project: Path) -> None:
        """Doctor should detect corrupted db.

        After GH PR #33 (schema-version read first), the message wording
        is "Cannot read schema version: ..." for files that aren't a
        valid sqlite database, and "Database error: ..." for failures
        on later queries. Either is acceptable; both must restate the
        ``corrupted / restore from backup`` guidance.
        """
        db_path = filigree_project / FILIGREE_DIR_NAME / DB_FILENAME
        # Overwrite with invalid data
        db_path.write_text("not a sqlite database")
        results = run_doctor(filigree_project)
        db_check = next((r for r in results if r.name == "filigree.db"), None)
        assert db_check is not None
        assert not db_check.passed
        assert "Database error" in db_check.message or "schema version" in db_check.message
        assert "corrupted" in (db_check.fix_hint or "")

    def test_missing_gitignore(self, filigree_project: Path) -> None:
        """Doctor should warn when .gitignore is missing."""
        results = run_doctor(filigree_project)
        gi_check = next((r for r in results if r.name == ".gitignore"), None)
        assert gi_check is not None
        assert not gi_check.passed
        assert "No .gitignore" in gi_check.message

    def test_gitignore_without_filigree(self, filigree_project: Path) -> None:
        """Doctor should warn when .gitignore doesn't include .filigree/."""
        (filigree_project / ".gitignore").write_text("*.pyc\n")
        results = run_doctor(filigree_project)
        gi_check = next((r for r in results if r.name == ".gitignore"), None)
        assert gi_check is not None
        assert not gi_check.passed
        assert ".filigree/ not in .gitignore" in gi_check.message

    def test_mcp_json_missing(self, filigree_project: Path) -> None:
        """Doctor should warn when .mcp.json is absent."""
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "No .mcp.json" in mcp_check.message

    def test_mcp_json_without_filigree(self, filigree_project: Path) -> None:
        """Doctor should warn when .mcp.json lacks filigree entry."""
        (filigree_project / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "filigree not in .mcp.json" in mcp_check.message

    def test_mcp_json_invalid(self, filigree_project: Path) -> None:
        """Doctor should warn when .mcp.json is invalid JSON."""
        (filigree_project / ".mcp.json").write_text("{bad json")
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "Invalid .mcp.json" in mcp_check.message

    def test_mcp_json_non_object_servers(self, filigree_project: Path) -> None:
        """Doctor should reject .mcp.json where mcpServers is not an object."""
        (filigree_project / ".mcp.json").write_text(json.dumps({"mcpServers": []}))
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "Invalid .mcp.json" in mcp_check.message

    def test_mcp_json_with_filigree(self, filigree_project: Path) -> None:
        """Doctor should pass when .mcp.json has filigree configured."""
        (filigree_project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"filigree": {"type": "stdio", "command": "filigree-mcp", "args": []}}})
        )
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert mcp_check.passed

    @pytest.mark.parametrize(
        "entry",
        [
            "should-be-an-object",
            True,
            ["filigree-mcp"],
            42,
        ],
    )
    def test_mcp_json_filigree_entry_must_be_dict(self, filigree_project: Path, entry: object) -> None:
        # filigree-466bcb6279: a truthy non-dict mcpServers.filigree used to
        # report "Configured in .mcp.json" because the schema check coerced
        # `command` to "" and fell through to the success branch.
        (filigree_project / ".mcp.json").write_text(json.dumps({"mcpServers": {"filigree": entry}}))
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "Invalid .mcp.json" in mcp_check.message

    @pytest.mark.parametrize(
        "entry",
        [
            {},
            {"type": "stdio"},
            {"type": "stdio", "command": ""},
            {"type": "stdio", "command": "filigree-mcp", "args": "not-a-list"},
            {"type": "stdio", "command": "filigree-mcp", "args": [123]},
            {"type": "streamable-http"},
            {"type": "streamable-http", "url": ""},
            {"type": "unknown-transport", "command": "filigree-mcp"},
        ],
    )
    def test_mcp_json_filigree_entry_schema_rejected(self, filigree_project: Path, entry: dict) -> None:
        # Companion to filigree-466bcb6279: dict entries that don't satisfy
        # either the stdio or streamable-http schema must be flagged invalid.
        (filigree_project / ".mcp.json").write_text(json.dumps({"mcpServers": {"filigree": entry}}))
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "Invalid .mcp.json" in mcp_check.message

    def test_mcp_json_streamable_http_passes(self, filigree_project: Path) -> None:
        (filigree_project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"filigree": {"type": "streamable-http", "url": "http://localhost:8377/mcp"}}})
        )
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert mcp_check.passed

    @pytest.mark.parametrize(
        ("label", "gitignore"),
        [
            ("comment-only", "# .filigree/ should be ignored\nbuild/\n"),
            ("negated-only", "!.filigree/\n"),
            ("substring-only", "src/.filigree/cache/\n"),
            ("comment-then-negation", ".filigree/\n!.filigree/\n"),
        ],
    )
    def test_gitignore_inactive_rules_do_not_pass(self, filigree_project: Path, label: str, gitignore: str) -> None:
        # filigree-bc5d2af1ef: doctor used naive substring matching that
        # accepted comments, negations, and non-root substrings as evidence
        # the project-root .filigree/ is ignored.
        (filigree_project / ".gitignore").write_text(gitignore)
        results = run_doctor(filigree_project)
        gi = next((r for r in results if r.name == ".gitignore"), None)
        assert gi is not None, label
        assert not gi.passed, f"{label}: should fail but passed"
        assert ".filigree/ not in .gitignore" in gi.message, label

    @pytest.mark.parametrize(
        ("label", "gitignore"),
        [
            ("plain rule", ".filigree/\n"),
            ("rooted rule", "/.filigree/\n"),
            ("no-trailing-slash", ".filigree\n"),
            ("with comments and other rules", "# header\n*.pyc\n.filigree/\n"),
            ("ignored then re-ignored", "!.filigree/\n.filigree/\n"),
        ],
    )
    def test_gitignore_active_rules_pass(self, filigree_project: Path, label: str, gitignore: str) -> None:
        # Control tests for filigree-bc5d2af1ef: real active rules still pass.
        (filigree_project / ".gitignore").write_text(gitignore)
        results = run_doctor(filigree_project)
        gi = next((r for r in results if r.name == ".gitignore"), None)
        assert gi is not None, label
        assert gi.passed, f"{label}: should pass but did not"

    def test_doctor_does_not_execute_hook_binary(self, filigree_project: Path) -> None:
        # filigree-e6828dcdb1: filigree doctor used to subprocess-run the
        # absolute interpreter path read from .claude/settings.json with
        # `-c "import filigree"`. A hostile repo could ship a settings.json
        # that points the SessionStart hook at an in-repo binary, getting
        # arbitrary code executed under the user running `filigree doctor`.
        repo_python = filigree_project / ".repo-python"
        marker = filigree_project / "EXECUTED"
        repo_python.write_text(f"#!/bin/sh\necho fired > {marker}\nexit 0\n")
        repo_python.chmod(0o755)

        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{repo_python} -m filigree session-context",
                            }
                        ]
                    }
                ]
            }
        }
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        run_doctor(filigree_project)

        assert not marker.exists(), "doctor must not execute hook-configured binaries"

    def test_codex_not_configured(self, filigree_project: Path) -> None:
        """Doctor should warn when ~/.codex/config.toml is absent."""
        home = filigree_project / "home"
        home.mkdir()
        with patch("filigree.install_support.doctor.Path.home", return_value=home):
            results = run_doctor(filigree_project)
        codex_check = next((r for r in results if r.name == "Codex MCP"), None)
        assert codex_check is not None
        assert not codex_check.passed

    def test_codex_configured(self, filigree_project: Path) -> None:
        """Doctor should pass when codex config has filigree."""
        home = filigree_project / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[mcp_servers.filigree]\ncommand = 'filigree-mcp'\nargs = []\n")
        with patch("filigree.install_support.doctor.Path.home", return_value=home):
            results = run_doctor(filigree_project)
        codex_check = next((r for r in results if r.name == "Codex MCP"), None)
        assert codex_check is not None
        assert codex_check.passed

    def test_codex_without_filigree(self, filigree_project: Path) -> None:
        """Doctor should warn when codex config exists but lacks filigree."""
        home = filigree_project / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[mcp_servers.other]\n")
        with patch("filigree.install_support.doctor.Path.home", return_value=home):
            results = run_doctor(filigree_project)
        codex_check = next((r for r in results if r.name == "Codex MCP"), None)
        assert codex_check is not None
        assert not codex_check.passed

    def test_codex_pinned_project_fails(self, filigree_project: Path) -> None:
        """Doctor should fail when Codex filigree entry still pins a project path."""
        home = filigree_project / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text('[mcp_servers.filigree]\ncommand = "filigree-mcp"\nargs = ["--project", "/tmp/other"]\n')
        with patch("filigree.install_support.doctor.Path.home", return_value=home):
            results = run_doctor(filigree_project)
        codex_check = next((r for r in results if r.name == "Codex MCP"), None)
        assert codex_check is not None
        assert not codex_check.passed
        assert "runtime project autodiscovery" in codex_check.message

    def test_codex_filigree_comment_does_not_count(self, filigree_project: Path) -> None:
        """Doctor should not treat commented filigree table text as configured."""
        home = filigree_project / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("# [mcp_servers.filigree]\n[mcp_servers.other]\n")
        with patch("filigree.install_support.doctor.Path.home", return_value=home):
            results = run_doctor(filigree_project)
        codex_check = next((r for r in results if r.name == "Codex MCP"), None)
        assert codex_check is not None
        assert not codex_check.passed
        assert "filigree not in ~/.codex/config.toml" in codex_check.message

    def test_codex_invalid_toml(self, filigree_project: Path) -> None:
        """Doctor should report invalid TOML instead of false configured state."""
        home = filigree_project / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[broken\n")
        with patch("filigree.install_support.doctor.Path.home", return_value=home):
            results = run_doctor(filigree_project)
        codex_check = next((r for r in results if r.name == "Codex MCP"), None)
        assert codex_check is not None
        assert not codex_check.passed
        assert "Invalid ~/.codex/config.toml" in codex_check.message

    def test_claude_md_missing(self, filigree_project: Path) -> None:
        """Doctor should warn when CLAUDE.md is absent."""
        results = run_doctor(filigree_project)
        claude_check = next((r for r in results if r.name == "CLAUDE.md"), None)
        assert claude_check is not None
        assert not claude_check.passed
        assert "File not found" in claude_check.message

    def test_claude_md_without_instructions(self, filigree_project: Path) -> None:
        """Doctor should warn when CLAUDE.md exists but has no instructions."""
        (filigree_project / "CLAUDE.md").write_text("# My Project\n")
        results = run_doctor(filigree_project)
        claude_check = next((r for r in results if r.name == "CLAUDE.md"), None)
        assert claude_check is not None
        assert not claude_check.passed
        assert "No filigree instructions" in claude_check.message

    def test_claude_md_with_instructions(self, filigree_project: Path) -> None:
        """Doctor should pass when CLAUDE.md has filigree instructions."""
        (filigree_project / "CLAUDE.md").write_text(f"# Project\n{FILIGREE_INSTRUCTIONS_MARKER}\n")
        results = run_doctor(filigree_project)
        claude_check = next((r for r in results if r.name == "CLAUDE.md"), None)
        assert claude_check is not None
        assert claude_check.passed

    def test_agents_md_without_instructions(self, filigree_project: Path) -> None:
        """Doctor should warn when AGENTS.md exists but has no instructions."""
        (filigree_project / "AGENTS.md").write_text("# Agents\n")
        results = run_doctor(filigree_project)
        agents_check = next((r for r in results if r.name == "AGENTS.md"), None)
        assert agents_check is not None
        assert not agents_check.passed

    def test_agents_md_with_instructions(self, filigree_project: Path) -> None:
        """Doctor should pass when AGENTS.md has filigree instructions."""
        (filigree_project / "AGENTS.md").write_text(f"# Agents\n{FILIGREE_INSTRUCTIONS_MARKER}\n")
        results = run_doctor(filigree_project)
        agents_check = next((r for r in results if r.name == "AGENTS.md"), None)
        assert agents_check is not None
        assert agents_check.passed

    def test_missing_context_md(self, filigree_project: Path) -> None:
        """Doctor should warn when context.md is missing."""
        summary_path = filigree_project / FILIGREE_DIR_NAME / SUMMARY_FILENAME
        summary_path.unlink()
        results = run_doctor(filigree_project)
        ctx_check = next((r for r in results if "context" in r.name.lower()), None)
        assert ctx_check is not None
        assert not ctx_check.passed
        assert "Missing" in ctx_check.message

    def test_git_status_check(self, filigree_project: Path) -> None:
        """Doctor should check git working tree when available."""
        # This test runs in a tmp dir that is not a git repo, so git check
        # should either not appear or report an issue. Just verify no crash.
        results = run_doctor(filigree_project)
        # Should complete without error
        assert len(results) >= 5


class TestFindFiligreeMcpCommand:
    @pytest.fixture(autouse=True)
    def _no_real_uv_tool(self, tmp_path: Path) -> Iterator[None]:
        """Prevent real uv tool install from interfering with tests."""
        with patch("filigree.install_support.integrations.Path.home", return_value=tmp_path):
            yield

    def test_found_on_path(self, tmp_path: Path) -> None:
        """When filigree-mcp is on PATH (no uv tool), return its path."""

        def _fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name == "filigree-mcp" else None

        with patch("filigree.install_support.integrations.shutil.which", side_effect=_fake_which):
            result = _find_filigree_mcp_command()
            assert result == "/usr/bin/filigree-mcp"

    def test_uv_tool_preferred_over_path(self, tmp_path: Path) -> None:
        """When uv tool is installed, prefer it over shutil.which result."""
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        (uv_bin / "filigree-mcp").touch()

        def _fake_which(name: str) -> str | None:
            return f"/some/venv/bin/{name}" if name == "filigree-mcp" else None

        with patch("filigree.install_support.integrations.shutil.which", side_effect=_fake_which):
            result = _find_filigree_mcp_command()
            assert result == str(uv_bin / "filigree-mcp")

    def test_fallback_to_sys_executable_sibling(self, tmp_path: Path) -> None:
        """When filigree-mcp not on PATH, look next to sys.executable."""
        fake_python = tmp_path / "python3"
        fake_python.touch()
        mcp_bin = tmp_path / "filigree-mcp"
        mcp_bin.touch()

        with (
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations.sys.executable", str(fake_python)),
        ):
            result = _find_filigree_mcp_command()
            assert result == str(mcp_bin)

    def test_fallback_to_sys_executable_sibling_windows_exe(self, tmp_path: Path) -> None:
        """When only filigree-mcp.exe is present, it should still be found."""
        fake_python = tmp_path / "python3"
        fake_python.touch()
        mcp_bin = tmp_path / "filigree-mcp.exe"
        mcp_bin.touch()

        with (
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations.sys.executable", str(fake_python)),
        ):
            result = _find_filigree_mcp_command()
            assert result == str(mcp_bin)

    def test_fallback_to_filigree_sibling(self, tmp_path: Path) -> None:
        """When filigree-mcp not on PATH or next to python, look next to filigree."""
        filigree_bin = tmp_path / "filigree"
        filigree_bin.touch()
        mcp_bin = tmp_path / "filigree-mcp"
        mcp_bin.touch()

        def fake_which(name: str) -> str | None:
            if name == "filigree":
                return str(filigree_bin)
            return None

        with (
            patch("filigree.install_support.integrations.shutil.which", side_effect=fake_which),
            patch("filigree.install_support.integrations.sys.executable", "/nonexistent/python3"),
        ):
            result = _find_filigree_mcp_command()
            assert result == str(mcp_bin)

    def test_default_fallback(self) -> None:
        """When nothing found, return 'filigree-mcp'."""
        with (
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations.sys.executable", "/nonexistent/python3"),
        ):
            result = _find_filigree_mcp_command()
            assert result == "filigree-mcp"

    def test_uv_tool_exe_preferred_on_windows_layout(self, tmp_path: Path) -> None:
        """Bug filigree-09d0dff729: uv-tool probe must also accept ``.exe``.

        On Windows the uv-tool binary is installed as ``filigree-mcp.exe``.
        Previously the uv-tool branch only probed the POSIX name, so the
        preferred absolute path was skipped and the resolver fell through
        to the bare-``filigree-mcp`` fallback.
        """
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        (uv_bin / "filigree-mcp.exe").touch()

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            result = _find_filigree_mcp_command()
            assert result == str(uv_bin / "filigree-mcp.exe")


class TestFindFiligreeCommand:
    @pytest.fixture(autouse=True)
    def _no_real_uv_tool(self, tmp_path: Path) -> Iterator[None]:
        """Prevent real uv tool install from interfering with tests."""
        with patch("filigree.core.Path.home", return_value=tmp_path):
            yield

    def test_found_on_path(self) -> None:
        """When filigree is on PATH (no uv tool), return single-element list."""
        with patch("filigree.core.shutil.which", return_value="/usr/local/bin/filigree"):
            result = find_filigree_command()
            assert result == ["/usr/local/bin/filigree"]

    def test_uv_tool_preferred_over_path(self, tmp_path: Path) -> None:
        """When uv tool is installed, prefer it over shutil.which result."""
        uv_bin = tmp_path / ".local" / "bin"
        uv_bin.mkdir(parents=True)
        candidate = uv_bin / "filigree"
        candidate.touch()
        candidate.chmod(0o755)

        with patch("filigree.core.shutil.which", return_value="/some/venv/bin/filigree"):
            result = find_filigree_command()
            assert result == [str(uv_bin / "filigree")]

    def test_fallback_to_sys_executable_sibling(self, tmp_path: Path) -> None:
        """When filigree not on PATH, look next to sys.executable."""
        fake_python = tmp_path / "python3"
        fake_python.touch()
        sibling = tmp_path / "filigree"
        sibling.touch()
        sibling.chmod(0o755)

        with (
            patch("filigree.core.shutil.which", return_value=None),
            patch("filigree.core.sys.executable", str(fake_python)),
        ):
            result = find_filigree_command()
            assert result == [str(sibling)]

    def test_default_fallback_uses_safe_path_module_invocation(self) -> None:
        """When nothing found, use ``python -P -m filigree`` to avoid cwd module shadowing."""
        with (
            patch("filigree.core.shutil.which", return_value=None),
            patch("filigree.core.sys.executable", "/nonexistent/python3"),
        ):
            result = find_filigree_command()
            assert result == ["/nonexistent/python3", "-P", "-m", "filigree"]

    def test_safe_path_fallback_does_not_execute_project_local_filigree(self, tmp_path: Path) -> None:
        """The fallback command must not let an untrusted project shadow the installed package."""
        project = tmp_path / "project"
        malicious_pkg = project / "filigree"
        malicious_pkg.mkdir(parents=True)
        marker = tmp_path / "module_hijacked"
        payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('hijacked')\nraise SystemExit(88)\n"
        (malicious_pkg / "__init__.py").write_text(payload)
        (malicious_pkg / "__main__.py").write_text(payload)

        with (
            patch("filigree.core.shutil.which", return_value=None),
            patch("filigree.core.os.access", return_value=False),
            patch("filigree.core.sys.executable", sys.executable),
        ):
            result = find_filigree_command()

        env = os.environ.copy()
        src_path = str(Path(__file__).resolve().parents[2] / "src")
        env["PYTHONPATH"] = src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
        proc = subprocess.run(
            [*result, "--help"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result[1:] == ["-P", "-m", "filigree"]
        assert proc.returncode == 0, proc.stderr
        assert "Usage:" in proc.stdout
        assert not marker.exists()


class TestInstallClaudeCodeMcp:
    def test_writes_mcp_json(self, tmp_path: Path) -> None:
        """Should write .mcp.json when claude CLI is not available."""
        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)
        assert ok
        mcp_json = tmp_path / ".mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text())
        assert "filigree" in data["mcpServers"]
        assert data["mcpServers"]["filigree"]["args"] == []

    def test_merges_with_existing_mcp_json(self, tmp_path: Path) -> None:
        """Should preserve existing entries in .mcp.json."""
        existing = {"mcpServers": {"other_tool": {"type": "stdio"}}}
        (tmp_path / ".mcp.json").write_text(json.dumps(existing))
        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)
        assert ok
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert "other_tool" in data["mcpServers"]
        assert "filigree" in data["mcpServers"]

    def test_handles_non_dict_mcp_json(self, tmp_path: Path) -> None:
        """Non-object .mcp.json should be backed up and reset, not crash."""
        (tmp_path / ".mcp.json").write_text("[]")
        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)
        assert ok
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert "filigree" in data["mcpServers"]

    def test_handles_non_dict_mcp_servers(self, tmp_path: Path) -> None:
        """mcpServers as a list should be replaced with {}, not crash."""
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": []}))
        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)
        assert ok
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert isinstance(data["mcpServers"], dict)
        assert "filigree" in data["mcpServers"]

    def test_handles_string_mcp_servers(self, tmp_path: Path) -> None:
        """mcpServers as a string should be replaced with {}, not crash."""
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": "bad"}))
        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)
        assert ok
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert isinstance(data["mcpServers"], dict)
        assert "filigree" in data["mcpServers"]


class TestInstallCodexMcp:
    def test_creates_codex_config(self, tmp_path: Path) -> None:
        """Should create ~/.codex/config.toml with filigree config."""
        home = tmp_path / "home"
        home.mkdir()
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        config = (home / ".codex" / "config.toml").read_text()
        assert "[mcp_servers.filigree]" in config

    def test_already_configured(self, tmp_path: Path) -> None:
        """Should detect when filigree is already configured."""
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[mcp_servers.filigree]\ncommand = 'filigree-mcp'\nargs = []\n")
        with (
            patch("filigree.install_support.integrations._find_filigree_mcp_command", return_value="filigree-mcp"),
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            ok, msg = install_codex_mcp(tmp_path)
        assert ok
        assert "Already configured" in msg

    def test_escapes_double_quotes_in_path(self, tmp_path: Path) -> None:
        """Paths with double quotes must produce valid TOML."""
        import tomllib

        # Use a project root whose name contains a double quote
        weird_root = tmp_path / 'proj"name'
        weird_root.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            ok, _msg = install_codex_mcp(weird_root)
        assert ok
        config_text = (home / ".codex" / "config.toml").read_text()
        # Must be parseable as valid TOML
        parsed = tomllib.loads(config_text)
        assert "filigree" in parsed["mcp_servers"]

    def test_replaces_header_with_inline_comment_without_duplication(self, tmp_path: Path) -> None:
        """Bug filigree-37b1452e59: TOML allows whitespace/inline comment between
        ``]`` and the line terminator. A bare-header regex left the old
        ``[mcp_servers.filigree]`` block in place and appended a second copy,
        making the file unparseable under tomllib's duplicate-table rule.
        """
        import tomllib

        home = tmp_path / "home"
        codex_dir = home / ".codex"
        config_path = codex_dir / "config.toml"
        home.mkdir()
        codex_dir.mkdir()
        config_path.write_text(
            "[mcp_servers.filigree] # pinned by laptop-setup script\n"
            'command = "/old/venv/bin/filigree-mcp"\n'
            'args = ["--project", "/srv/old"]\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "other-mcp"\n'
        )
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations._find_filigree_mcp_command", return_value="filigree-mcp"),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        text = config_path.read_text()
        # Duplicate table rule must hold — tomllib refuses otherwise
        parsed = tomllib.loads(text)
        assert parsed["mcp_servers"]["filigree"] == {"command": "filigree-mcp", "args": []}
        # The [mcp_servers.other] table must be left intact
        assert parsed["mcp_servers"]["other"] == {"command": "other-mcp"}
        # No stale project arg survived
        assert "/srv/old" not in text

    def test_replaces_header_with_trailing_whitespace(self, tmp_path: Path) -> None:
        """Trailing spaces/tabs after ``]`` are valid TOML and must not block replacement."""
        import tomllib

        home = tmp_path / "home"
        codex_dir = home / ".codex"
        config_path = codex_dir / "config.toml"
        home.mkdir()
        codex_dir.mkdir()
        # Trailing spaces after the header; note no comment
        config_path.write_text('[mcp_servers.filigree]   \ncommand = "/old/filigree-mcp"\nargs = []\n')
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations._find_filigree_mcp_command", return_value="filigree-mcp"),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        text = config_path.read_text()
        parsed = tomllib.loads(text)  # must not raise duplicate-table
        assert parsed["mcp_servers"]["filigree"]["command"] == "filigree-mcp"
        assert text.count("[mcp_servers.filigree]") == 1

    def test_replaces_existing_crlf_table_without_duplication(self, tmp_path: Path) -> None:
        """CRLF-terminated Codex configs should replace filigree in place."""
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        config_path = codex_dir / "config.toml"
        home.mkdir()
        codex_dir.mkdir()
        config_path.write_bytes(
            b"[mcp_servers.filigree]\r\n"
            b'command = "old-mcp"\r\n'
            b'args = ["--project", "/tmp/old"]\r\n'
            b"\r\n"
            b"[mcp_servers.other]\r\n"
            b'command = "other-mcp"\r\n'
        )
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations._find_filigree_mcp_command", return_value="filigree-mcp"),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        raw = config_path.read_bytes()
        assert raw.count(b"[mcp_servers.filigree]") == 1
        assert b'command = "old-mcp"' not in raw
        assert b'command = "filigree-mcp"\r\n' in raw
        assert b"args = []\r\n" in raw
        assert b"[mcp_servers.other]\r\n" in raw

    def test_replaces_quoted_key_header_without_duplication(self, tmp_path: Path) -> None:
        """Bug filigree-32fe96222e: TOML allows quoted keys in headers
        (``[mcp_servers."filigree"]``) and equivalent whitespace-around-dot
        forms. The bare-header regex missed these and appended a literal
        ``[mcp_servers.filigree]`` block, producing a duplicate-table file
        that tomllib refuses on the next install run.
        """
        import tomllib

        home = tmp_path / "home"
        codex_dir = home / ".codex"
        config_path = codex_dir / "config.toml"
        home.mkdir()
        codex_dir.mkdir()
        config_path.write_text(
            '[mcp_servers."filigree"]\n'
            'command = "/old/venv/bin/filigree-mcp"\n'
            'args = ["--project", "/srv/old"]\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "other-mcp"\n'
        )
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations._find_filigree_mcp_command", return_value="filigree-mcp"),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        text = config_path.read_text()
        parsed = tomllib.loads(text)
        assert parsed["mcp_servers"]["filigree"] == {"command": "filigree-mcp", "args": []}
        assert parsed["mcp_servers"]["other"] == {"command": "other-mcp"}
        assert "/srv/old" not in text

    def test_replaces_whitespace_around_dot_header_without_duplication(self, tmp_path: Path) -> None:
        """``[mcp_servers . filigree]`` is valid TOML equivalent to the bare form."""
        import tomllib

        home = tmp_path / "home"
        codex_dir = home / ".codex"
        config_path = codex_dir / "config.toml"
        home.mkdir()
        codex_dir.mkdir()
        config_path.write_text('[mcp_servers . filigree]\ncommand = "/old/filigree-mcp"\nargs = []\n')
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch("filigree.install_support.integrations._find_filigree_mcp_command", return_value="filigree-mcp"),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        text = config_path.read_text()
        parsed = tomllib.loads(text)  # must not raise duplicate-table
        assert parsed["mcp_servers"]["filigree"]["command"] == "filigree-mcp"


class TestInstallCodexMcpMalformedToml:
    """Bug filigree-d6bbbf: install_codex_mcp must fail on malformed TOML, not silently append."""

    def test_malformed_toml_returns_false(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[broken\nthis is not valid toml")
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            ok, msg = install_codex_mcp(tmp_path)
        assert not ok
        assert "malformed TOML" in msg

    def test_malformed_toml_does_not_modify_file(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        original = "[broken\nthis is not valid toml"
        (codex_dir / "config.toml").write_text(original)
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            install_codex_mcp(tmp_path)
        assert (codex_dir / "config.toml").read_text() == original


class TestInstallClaudeCodeHooks:
    MOCK_TOKENS = ["/mock/venv/bin/filigree"]  # noqa: RUF012
    MOCK_BIN = "/mock/venv/bin/filigree"

    def test_creates_settings_json(self, tmp_path: Path) -> None:
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert "hooks" in data
        cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        assert any("session-context" in c and self.MOCK_BIN in c for c in cmds)

    def test_merges_with_existing_settings(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {"someOtherKey": True}
        (claude_dir / "settings.json").write_text(json.dumps(existing))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        data = json.loads((claude_dir / "settings.json").read_text())
        assert data["someOtherKey"] is True
        assert "hooks" in data

    def test_idempotent(self, tmp_path: Path) -> None:
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)
            install_claude_code_hooks(tmp_path)
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        # session-context should appear exactly once
        session_cmds = [c for c in cmds if "session-context" in c]
        assert len(session_cmds) == 1

    def test_handles_corrupt_settings(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{corrupt json!!!")
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        # Backup should exist
        assert (claude_dir / "settings.json.bak").exists()

    def test_dashboard_hook_always_added(self, tmp_path: Path) -> None:
        """Dashboard hook is always added (dashboard is part of core)."""
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        assert any("ensure-dashboard" in c and self.MOCK_BIN in c for c in cmds)

    def test_safe_path_module_fallback_is_written_to_hooks(self, tmp_path: Path) -> None:
        """Installed hooks must preserve the safe-path fallback token sequence."""
        fallback = ["/usr/bin/python3", "-P", "-m", "filigree"]
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=fallback):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        session_cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        pre_tool_cmds = [h["command"] for m in data["hooks"]["PreToolUse"] for h in m["hooks"]]
        assert "/usr/bin/python3 -P -m filigree session-context" in session_cmds
        assert "/usr/bin/python3 -P -m filigree ensure-dashboard" in session_cmds
        assert "/usr/bin/python3 -P -m filigree ensure-dashboard" in pre_tool_cmds

    def test_upgrades_bare_to_absolute(self, tmp_path: Path) -> None:
        """Bare hook commands should be upgraded to resolved versions."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "filigree session-context", "timeout": 5000},
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, msg = install_claude_code_hooks(tmp_path)
        assert ok
        assert "Upgraded" in msg or "Registered" in msg
        data = json.loads((claude_dir / "settings.json").read_text())
        cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        assert f"{self.MOCK_BIN} session-context" in cmds

    def test_upgrades_stale_absolute_path(self, tmp_path: Path) -> None:
        """Old absolute-path hooks should be updated to current binary path."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        old_bin = "/old/venv/bin/filigree"
        hooks_list = [
            {
                "type": "command",
                "command": f"{old_bin} session-context",
                "timeout": 5000,
            },
        ]
        # Include dashboard hook too (dashboard is part of core)
        hooks_list.append(
            {
                "type": "command",
                "command": f"{old_bin} ensure-dashboard",
                "timeout": 5000,
            }
        )
        settings = {"hooks": {"SessionStart": [{"hooks": hooks_list}]}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, msg = install_claude_code_hooks(tmp_path)
        assert ok
        assert "Upgraded" in msg
        data = json.loads((claude_dir / "settings.json").read_text())
        cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        assert f"{self.MOCK_BIN} session-context" in cmds
        assert f"{old_bin} session-context" not in cmds

    def test_upgrades_module_form_to_current(self, tmp_path: Path) -> None:
        """python -m filigree commands should be recognized and upgraded."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/old/python -m filigree session-context",
                                "timeout": 5000,
                            },
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        data = json.loads((claude_dir / "settings.json").read_text())
        cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        assert f"{self.MOCK_BIN} session-context" in cmds
        assert "/old/python -m filigree session-context" not in cmds

    def test_spaces_in_path_properly_quoted(self, tmp_path: Path) -> None:
        """Paths with spaces must be shell-quoted so they round-trip correctly."""
        import shlex

        spaced_tokens = ["/path with spaces/python", "-m", "filigree"]
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=spaced_tokens):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for m in data["hooks"]["SessionStart"] for h in m["hooks"]]
        session_cmd = next(c for c in cmds if "session-context" in c)
        # shlex.split must recover the original tokens
        parsed = shlex.split(session_cmd)
        assert parsed[0] == "/path with spaces/python"
        assert parsed[1:] == ["-m", "filigree", "session-context"]


class TestInstallHooksMatcherIsolation:
    """Bug filigree-9fb21f2b4b: filigree SessionStart hooks must not be
    appended to a user block whose ``matcher`` scopes it to a subset of
    session sources (``resume``, ``clear``, ``compact``).
    """

    MOCK_TOKENS = ["/mock/venv/bin/filigree"]  # noqa: RUF012
    MOCK_BIN = "/mock/venv/bin/filigree"

    def _find_filigree_session_block(self, settings_path: Path) -> dict[str, object]:
        data = json.loads(settings_path.read_text())
        for block in data["hooks"]["SessionStart"]:
            for hook in block.get("hooks", []):
                cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                if "session-context" in cmd:
                    return block
        raise AssertionError("no filigree session-context hook found in settings")

    def test_does_not_inherit_user_resume_matcher(self, tmp_path: Path) -> None:
        """A user block with matcher=``resume`` (even one that casually
        mentions ``filigree`` in its command) must not adopt the new
        session-context hook. The hook belongs to a dedicated block with
        no matcher so it fires on cold startup too.
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "resume",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo resuming filigree session",
                                "timeout": 5000,
                            }
                        ],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)

        data = json.loads((claude_dir / "settings.json").read_text())
        blocks = data["hooks"]["SessionStart"]
        # User's resume block is preserved untouched.
        resume_block = next(b for b in blocks if b.get("matcher") == "resume")
        assert len(resume_block["hooks"]) == 1
        assert resume_block["hooks"][0]["command"] == "echo resuming filigree session"
        # Filigree hooks live in their own block with no matcher scoping.
        filigree_block = self._find_filigree_session_block(claude_dir / "settings.json")
        assert "matcher" not in filigree_block or filigree_block.get("matcher") in (None, "*")

    def test_reuses_existing_unscoped_filigree_block(self, tmp_path: Path) -> None:
        """An existing *unscoped* block that already holds a filigree
        session-context hook is the legitimate reuse target — adding
        ensure-dashboard should slot into it rather than create a second
        block.
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{self.MOCK_BIN} session-context",
                                "timeout": 5000,
                            }
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)

        data = json.loads((claude_dir / "settings.json").read_text())
        blocks = data["hooks"]["SessionStart"]
        # Only one block holds filigree session hooks, and both commands
        # ended up in it.
        filigree_blocks = [
            b
            for b in blocks
            if any("session-context" in h.get("command", "") or "ensure-dashboard" in h.get("command", "") for h in b.get("hooks", []))
        ]
        assert len(filigree_blocks) == 1
        cmds = [h["command"] for h in filigree_blocks[0]["hooks"]]
        assert any("session-context" in c for c in cmds)
        assert any("ensure-dashboard" in c for c in cmds)

    def test_does_not_reuse_scoped_block_holding_filigree_hook(self, tmp_path: Path) -> None:
        """Even if an existing block happens to hold a filigree hook, if
        its ``matcher`` scopes it to a subset of session sources, don't
        append more hooks there — they'd also inherit the scope.
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "resume",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{self.MOCK_BIN} session-context",
                                "timeout": 5000,
                            }
                        ],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)

        data = json.loads((claude_dir / "settings.json").read_text())
        blocks = data["hooks"]["SessionStart"]
        # A fresh unscoped block exists alongside the user's scoped one.
        unscoped_blocks = [b for b in blocks if "matcher" not in b or b.get("matcher") in (None, "*")]
        assert unscoped_blocks, "filigree hooks must land in an unscoped block"
        cmds = [h["command"] for b in unscoped_blocks for h in b.get("hooks", [])]
        # Bug filigree-48d6f0d8da: BOTH commands must be present in the unscoped
        # block — the install-time gate must not treat a scoped session-context
        # entry as authoritative, otherwise cold startup never triggers it.
        assert any("ensure-dashboard" in c for c in cmds)
        assert any("session-context" in c for c in cmds), (
            "session-context must land in the unscoped block too — the existing "
            "scoped 'resume' entry doesn't count as installed for cold startup."
        )
        # User's scoped block is left exactly as-is.
        resume_block = next(b for b in blocks if b.get("matcher") == "resume")
        assert len(resume_block["hooks"]) == 1
        assert resume_block["hooks"][0]["command"].endswith("session-context")


class TestInstallHooksPreToolUseRepair:
    """Bug filigree-83c52565d6: PreToolUse ensure-dashboard hooks must be
    repaired on reinstall after a binary move, not left stale with a
    substring match."""

    MOCK_TOKENS = ["/mock/venv/bin/filigree"]  # noqa: RUF012
    MOCK_BIN = "/mock/venv/bin/filigree"

    def _pre_tool_use_cmds(self, settings_path: Path) -> list[str]:
        data = json.loads(settings_path.read_text())
        return [h["command"] for m in data["hooks"]["PreToolUse"] for h in m["hooks"]]

    def test_stale_absolute_path_is_rewritten(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        old_bin = "/old/venv/bin/filigree"
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": f"{old_bin} session-context", "timeout": 5000},
                            {"type": "command", "command": f"{old_bin} ensure-dashboard", "timeout": 5000},
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "mcp__filigree__.*",
                        "hooks": [{"type": "command", "command": f"{old_bin} ensure-dashboard", "timeout": 5000}],
                    }
                ],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        cmds = self._pre_tool_use_cmds(claude_dir / "settings.json")
        assert f"{self.MOCK_BIN} ensure-dashboard" in cmds
        assert f"{old_bin} ensure-dashboard" not in cmds

    def test_bare_command_is_rewritten(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "mcp__filigree__.*",
                        "hooks": [{"type": "command", "command": "filigree ensure-dashboard", "timeout": 5000}],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)
        cmds = self._pre_tool_use_cmds(claude_dir / "settings.json")
        assert f"{self.MOCK_BIN} ensure-dashboard" in cmds
        assert "filigree ensure-dashboard" not in cmds

    def test_idempotent_when_already_current(self, tmp_path: Path) -> None:
        """Running install twice must not duplicate the PreToolUse entry."""
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)
            install_claude_code_hooks(tmp_path)
        cmds = self._pre_tool_use_cmds(tmp_path / ".claude" / "settings.json")
        # ensure-dashboard should appear exactly once in PreToolUse
        ensure_cmds = [c for c in cmds if "ensure-dashboard" in c]
        assert len(ensure_cmds) == 1

    def test_drifted_matcher_is_repaired(self, tmp_path: Path) -> None:
        """If matcher drifted away from mcp__filigree__.*, reinstall repairs it."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "wrong-matcher",
                        "hooks": [{"type": "command", "command": f"{self.MOCK_BIN} ensure-dashboard", "timeout": 5000}],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)
        data = json.loads((claude_dir / "settings.json").read_text())
        matchers = [m.get("matcher") for m in data["hooks"]["PreToolUse"]]
        assert "mcp__filigree__.*" in matchers
        assert "wrong-matcher" not in matchers

    def test_fresh_install_adds_pre_tool_use_with_scoped_matcher(self, tmp_path: Path) -> None:
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        pre_tool_use = data["hooks"]["PreToolUse"]
        assert len(pre_tool_use) == 1
        assert pre_tool_use[0]["matcher"] == "mcp__filigree__.*"
        assert pre_tool_use[0]["hooks"][0]["command"] == f"{self.MOCK_BIN} ensure-dashboard"

    def test_mixed_block_user_hook_keeps_original_matcher(self, tmp_path: Path) -> None:
        """Bug filigree-53fb9d5906: a PreToolUse block with both a user hook
        and filigree's ensure-dashboard hook must NOT have its matcher rewritten —
        that would silently demote the user hook from its original tool scope.
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "/usr/local/bin/user-bash-audit"},
                            {"type": "command", "command": f"{self.MOCK_BIN} ensure-dashboard", "timeout": 5000},
                        ],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)
        data = json.loads((claude_dir / "settings.json").read_text())
        pre_tool_use = data["hooks"]["PreToolUse"]

        # The user's Bash block must still be scoped to Bash and still contain
        # the user-bash-audit hook.
        bash_block = next(b for b in pre_tool_use if b.get("matcher") == "Bash")
        bash_cmds = [h["command"] for h in bash_block["hooks"]]
        assert "/usr/local/bin/user-bash-audit" in bash_cmds
        # Filigree's hook must be moved out of the user block (otherwise it
        # would still inherit the Bash matcher there).
        assert all("ensure-dashboard" not in c for c in bash_cmds), "ensure-dashboard must not remain inside the user's Bash-scoped block"
        # A dedicated mcp__filigree__.* block must hold ensure-dashboard.
        filigree_blocks = [b for b in pre_tool_use if b.get("matcher") == "mcp__filigree__.*"]
        assert len(filigree_blocks) == 1
        assert any("ensure-dashboard" in h.get("command", "") for h in filigree_blocks[0]["hooks"])

    def test_mixed_block_install_is_idempotent(self, tmp_path: Path) -> None:
        """Running install twice on the mixed-block scenario must converge
        to a stable shape — no oscillation, no drift.
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "/usr/local/bin/user-bash-audit"},
                            {"type": "command", "command": f"{self.MOCK_BIN} ensure-dashboard", "timeout": 5000},
                        ],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            install_claude_code_hooks(tmp_path)
            after_first = (claude_dir / "settings.json").read_text()
            install_claude_code_hooks(tmp_path)
            after_second = (claude_dir / "settings.json").read_text()
        assert after_first == after_second, "second install must produce byte-identical settings"


class TestHookCmdMatchesStrict:
    """Bug filigree-2ba60d9970: _hook_cmd_matches must reject arbitrary
    wrapper prefixes, accepting only bare/path form (single binary) and
    explicit module form (``<python> -m filigree <subcommand>``).
    """

    BARE = "filigree session-context"

    def test_exact_bare_matches(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches(self.BARE, self.BARE) is True

    def test_absolute_path_matches(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("/path/to/filigree session-context", self.BARE) is True

    def test_quoted_path_with_spaces_matches(self) -> None:
        import shlex

        from filigree.install_support.hooks import _hook_cmd_matches

        cmd = shlex.join(["/path with spaces/filigree"]) + " session-context"
        assert _hook_cmd_matches(cmd, self.BARE) is True

    def test_module_form_python3_matches(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("/usr/bin/python3 -m filigree session-context", self.BARE) is True

    def test_module_form_arbitrary_interpreter_matches(self) -> None:
        """`-m filigree` is the discriminator — any interpreter name should work
        (pypy3, python3.13t, /custom/wrapper, etc.). This is intentional.
        """
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("/opt/pypy3.10/bin/pypy3 -m filigree session-context", self.BARE) is True

    def test_echo_prefix_does_not_match(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("echo filigree session-context", self.BARE) is False

    def test_time_prefix_does_not_match(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("time filigree session-context", self.BARE) is False

    def test_env_prefix_does_not_match(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("env FOO=bar filigree session-context", self.BARE) is False

    def test_bash_wrapper_does_not_match(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("bash filigree session-context", self.BARE) is False

    def test_sudo_wrapper_does_not_match(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("sudo /path/to/filigree session-context", self.BARE) is False

    def test_module_form_wrong_module_does_not_match(self) -> None:
        from filigree.install_support.hooks import _hook_cmd_matches

        assert _hook_cmd_matches("python -m otherthing session-context", self.BARE) is False


class TestHasHookCommand:
    """Tests for _has_hook_command with malformed JSON structures."""

    def test_hooks_as_list(self) -> None:
        """settings.hooks as a list should return False, not crash."""
        from filigree.install import _has_hook_command

        assert _has_hook_command({"hooks": []}, "filigree session-context") is False

    def test_hooks_as_string(self) -> None:
        """settings.hooks as a string should return False, not crash."""
        from filigree.install import _has_hook_command

        assert _has_hook_command({"hooks": "bad"}, "filigree session-context") is False

    def test_session_start_as_string(self) -> None:
        """hooks.SessionStart as a string should return False, not crash."""
        from filigree.install import _has_hook_command

        assert _has_hook_command({"hooks": {"SessionStart": "bad"}}, "filigree session-context") is False

    def test_matcher_as_string(self) -> None:
        """Non-dict matcher entries should be skipped, not crash."""
        from filigree.install import _has_hook_command

        assert _has_hook_command({"hooks": {"SessionStart": ["bad"]}}, "filigree session-context") is False

    def test_hook_entry_as_string(self) -> None:
        """Non-dict hook entries within a matcher should be skipped."""
        from filigree.install import _has_hook_command

        settings = {"hooks": {"SessionStart": [{"hooks": ["bad"]}]}}
        assert _has_hook_command(settings, "filigree session-context") is False

    def test_non_dict_settings(self) -> None:
        """Non-dict settings should return False, not crash."""
        assert _has_hook_command([], "filigree session-context") is False  # type: ignore[arg-type]

    def test_matches_absolute_path_form(self) -> None:
        """Should detect '/path/to/filigree session-context' as a match."""
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": "/usr/local/bin/filigree session-context"}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is True

    def test_does_not_false_match_similar_command(self) -> None:
        """Should reject 'not-filigree session-context'."""
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": "not-filigree session-context"}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is False

    def test_matches_module_form(self) -> None:
        """Should detect 'python -m filigree session-context' as a match."""
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": "/usr/bin/python3 -m filigree session-context"}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is True

    def test_matches_safe_path_module_form(self) -> None:
        """Should detect the safe 'python -P -m filigree session-context' fallback as a match."""
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": "/usr/bin/python3 -P -m filigree session-context"}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is True

    def test_rejects_module_form_with_unsafe_extra_prefix(self) -> None:
        """Should still reject wrapper/prefix commands even if they contain '-P -m filigree'."""
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": "env FOO=bar python -P -m filigree session-context"}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is False

    def test_matches_quoted_path_with_spaces(self) -> None:
        """Should detect quoted paths containing spaces."""
        import shlex

        cmd = shlex.join(["/path with spaces/filigree"]) + " session-context"
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": cmd}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is True

    def test_matches_module_form_with_spaces(self) -> None:
        """Should detect quoted python -m form with spaces in python path."""
        import shlex

        cmd = shlex.join(["/Program Files/python", "-m", "filigree"]) + " session-context"
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": cmd}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is True

    def test_matches_safe_path_module_form_with_spaces(self) -> None:
        """Should detect quoted python -P -m form with spaces in python path."""
        import shlex

        cmd = shlex.join(["/Program Files/python", "-P", "-m", "filigree"]) + " session-context"
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": cmd}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is True

    def test_matches_windows_exe_form(self) -> None:
        """Should detect absolute filigree.exe paths as matches."""
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": "C:/tools/filigree.exe session-context"}]}]}}
        assert _has_hook_command(settings, "filigree session-context") is True


class TestInstallHooksMalformedStructure:
    """Tests for install_claude_code_hooks with malformed existing settings."""

    MOCK_TOKENS = ["/mock/venv/bin/filigree"]  # noqa: RUF012

    def test_hooks_key_is_list(self, tmp_path: Path) -> None:
        """Existing settings.hooks as a list should be replaced, not crash."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"hooks": []}))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        data = json.loads((claude_dir / "settings.json").read_text())
        assert isinstance(data["hooks"], dict)

    def test_session_start_is_string(self, tmp_path: Path) -> None:
        """Existing hooks.SessionStart as a string should be replaced, not crash."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": "bad"}}))
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=self.MOCK_TOKENS):
            ok, _msg = install_claude_code_hooks(tmp_path)
        assert ok
        data = json.loads((claude_dir / "settings.json").read_text())
        assert isinstance(data["hooks"]["SessionStart"], list)


class TestDoctorMalformedHooks:
    """Tests for run_doctor with malformed hooks in settings.json."""

    def test_hooks_as_list(self, filigree_project: Path) -> None:
        """Doctor should not crash when settings.hooks is a list."""
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text(json.dumps({"hooks": []}))
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert not hooks_check.passed

    def test_non_dict_settings_json(self, filigree_project: Path) -> None:
        """Doctor should not crash when settings.json is a list."""
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text("[]")
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert not hooks_check.passed


class TestDoctorHooksCheck:
    def test_passes_when_hooks_registered(self, filigree_project: Path) -> None:
        # Use a real path that exists so the binary path check passes
        mock_bin = str(filigree_project / "filigree")
        (filigree_project / "filigree").touch()
        with patch("filigree.install_support.hooks.find_filigree_command", return_value=[mock_bin]):
            install_claude_code_hooks(filigree_project)
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert hooks_check.passed

    def test_fails_when_settings_missing(self, filigree_project: Path) -> None:
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert not hooks_check.passed
        assert "No .claude/settings.json" in hooks_check.message

    def test_fails_when_hooks_absent(self, filigree_project: Path) -> None:
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"hooks": {}}))
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert not hooks_check.passed
        assert "session-context hook not found" in hooks_check.message


class TestDoctorMcpPathValidation:
    def test_stale_mcp_binary_detected(self, filigree_project: Path) -> None:
        """Doctor should detect nonexistent absolute path in .mcp.json command."""
        mcp_config = {
            "mcpServers": {
                "filigree": {
                    "type": "stdio",
                    "command": "/nonexistent/venv/bin/filigree-mcp",
                    "args": ["--project", str(filigree_project)],
                }
            }
        }
        (filigree_project / ".mcp.json").write_text(json.dumps(mcp_config))
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "Binary not found" in mcp_check.message

    def test_valid_mcp_binary_passes(self, filigree_project: Path) -> None:
        """Doctor should pass when MCP binary path exists."""
        fake_bin = filigree_project / "filigree-mcp"
        fake_bin.touch()
        mcp_config = {
            "mcpServers": {
                "filigree": {
                    "type": "stdio",
                    "command": str(fake_bin),
                    "args": ["--project", str(filigree_project)],
                }
            }
        }
        (filigree_project / ".mcp.json").write_text(json.dumps(mcp_config))
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert mcp_check.passed

    def test_stale_windows_mcp_binary_detected(self, filigree_project: Path) -> None:
        """Doctor should detect nonexistent Windows absolute path in .mcp.json command."""
        mcp_config = {
            "mcpServers": {
                "filigree": {
                    "type": "stdio",
                    "command": r"C:\stale\venv\Scripts\filigree-mcp.exe",
                    "args": ["--project", str(filigree_project)],
                }
            }
        }
        (filigree_project / ".mcp.json").write_text(json.dumps(mcp_config))
        results = run_doctor(filigree_project)
        mcp_check = next((r for r in results if r.name == "Claude Code MCP"), None)
        assert mcp_check is not None
        assert not mcp_check.passed
        assert "Binary not found" in mcp_check.message


class TestDoctorHookPathValidation:
    def test_stale_hook_binary_detected(self, filigree_project: Path) -> None:
        """Doctor should detect nonexistent absolute path in hook command."""
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/nonexistent/venv/bin/filigree session-context",
                                "timeout": 5000,
                            }
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert not hooks_check.passed
        assert "Binary not found" in hooks_check.message

    def test_valid_hook_binary_passes(self, filigree_project: Path) -> None:
        """Doctor should pass when hook binary path exists."""
        fake_bin = filigree_project / "filigree"
        fake_bin.touch()
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{fake_bin} session-context",
                                "timeout": 5000,
                            }
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert hooks_check.passed

    def test_quoted_path_with_spaces_passes(self, filigree_project: Path) -> None:
        """Doctor should extract binary correctly from quoted paths with spaces."""
        import shlex

        spaced_dir = filigree_project / "path with spaces"
        spaced_dir.mkdir()
        fake_bin = spaced_dir / "filigree"
        fake_bin.touch()
        cmd = shlex.join([str(fake_bin)]) + " session-context"
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": cmd, "timeout": 5000}]}]}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert hooks_check.passed

    def test_bare_command_hook_still_passes(self, filigree_project: Path) -> None:
        """Doctor should accept bare commands without path validation."""
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "filigree session-context",
                                "timeout": 5000,
                            }
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert hooks_check.passed

    def test_stale_windows_hook_binary_detected(self, filigree_project: Path) -> None:
        """Doctor should detect nonexistent Windows absolute path in hook command."""
        claude_dir = filigree_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"C:\\stale\\venv\\Scripts\\filigree.exe" session-context',
                                "timeout": 5000,
                            }
                        ]
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = run_doctor(filigree_project)
        hooks_check = next((r for r in results if r.name == "Claude Code hooks"), None)
        assert hooks_check is not None
        assert not hooks_check.passed
        assert "Binary not found" in hooks_check.message


class TestInstallSkills:
    def test_installs_skill_pack(self, tmp_path: Path) -> None:
        ok, _msg = install_skills(tmp_path)
        assert ok
        skill_md = tmp_path / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
        assert skill_md.exists()
        content = skill_md.read_text()
        assert "filigree-workflow" in content

    def test_overwrites_on_reinstall(self, tmp_path: Path) -> None:
        """Re-install should overwrite existing skill (picks up upgrades)."""
        install_skills(tmp_path)
        skill_md = tmp_path / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
        skill_md.write_text("stale content")
        install_skills(tmp_path)
        assert "filigree-workflow" in skill_md.read_text()

    def test_preserves_other_skills(self, tmp_path: Path) -> None:
        """Installing filigree skill should not touch other skills."""
        other_skill = tmp_path / ".claude" / "skills" / "other-skill"
        other_skill.mkdir(parents=True)
        (other_skill / "SKILL.md").write_text("other")
        install_skills(tmp_path)
        assert (other_skill / "SKILL.md").read_text() == "other"

    def test_includes_references(self, tmp_path: Path) -> None:
        install_skills(tmp_path)
        refs = tmp_path / ".claude" / "skills" / SKILL_NAME / "references"
        assert refs.is_dir()
        assert (refs / "workflow-patterns.md").exists()
        assert (refs / "team-coordination.md").exists()

    def test_includes_examples(self, tmp_path: Path) -> None:
        install_skills(tmp_path)
        examples = tmp_path / ".claude" / "skills" / SKILL_NAME / "examples"
        assert examples.is_dir()
        assert (examples / "sprint-plan.json").exists()

    def test_concurrent_installs_all_succeed(self, tmp_path: Path) -> None:
        """Regression: concurrent installs must not race on a shared staging dir.

        Two Claude Code sessions starting near-simultaneously both fire the
        SessionStart hook, which calls install_skills via the stale-skill
        auto-refresh path in hooks.py. With a deterministic ``.installing``
        sibling directory, concurrent calls would clobber each other's
        staging area; with per-call mkdtemp staging, they should all win.
        """
        import threading

        results: list[tuple[bool, str]] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(install_skills(tmp_path))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"concurrent install raised: {errors!r}"
        assert all(ok for ok, _ in results), f"some installs reported failure: {results!r}"
        # Final tree must be intact and parsable as a normal install.
        skill_md = tmp_path / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
        assert skill_md.exists()
        assert "filigree-workflow" in skill_md.read_text()
        # No staging leftovers.
        leftovers = list((tmp_path / ".claude" / "skills").glob(f"{SKILL_NAME}.*"))
        assert leftovers == [], f"staging directories left behind: {leftovers!r}"


class TestInstallCodexSkills:
    def test_installs_skill_pack(self, tmp_path: Path) -> None:
        ok, _msg = install_codex_skills(tmp_path)
        assert ok
        skill_md = tmp_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
        assert skill_md.exists()
        content = skill_md.read_text()
        assert "filigree-workflow" in content

    def test_overwrites_on_reinstall(self, tmp_path: Path) -> None:
        """Re-install should overwrite existing skill (picks up upgrades)."""
        install_codex_skills(tmp_path)
        skill_md = tmp_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
        skill_md.write_text("stale content")
        install_codex_skills(tmp_path)
        assert "filigree-workflow" in skill_md.read_text()

    def test_preserves_other_skills(self, tmp_path: Path) -> None:
        """Installing filigree skill should not touch other skills."""
        other_skill = tmp_path / ".agents" / "skills" / "other-skill"
        other_skill.mkdir(parents=True)
        (other_skill / "SKILL.md").write_text("other")
        install_codex_skills(tmp_path)
        assert (other_skill / "SKILL.md").read_text() == "other"

    def test_includes_references(self, tmp_path: Path) -> None:
        install_codex_skills(tmp_path)
        refs = tmp_path / ".agents" / "skills" / SKILL_NAME / "references"
        assert refs.is_dir()
        assert (refs / "workflow-patterns.md").exists()
        assert (refs / "team-coordination.md").exists()

    def test_includes_examples(self, tmp_path: Path) -> None:
        install_codex_skills(tmp_path)
        examples = tmp_path / ".agents" / "skills" / SKILL_NAME / "examples"
        assert examples.is_dir()
        assert (examples / "sprint-plan.json").exists()


class TestDoctorSkillsCheck:
    def test_passes_when_skill_installed(self, filigree_project: Path) -> None:
        install_skills(filigree_project)
        results = run_doctor(filigree_project)
        check = next((r for r in results if r.name == "Claude Code skills"), None)
        assert check is not None
        assert check.passed

    def test_fails_when_skill_missing(self, filigree_project: Path) -> None:
        results = run_doctor(filigree_project)
        check = next((r for r in results if r.name == "Claude Code skills"), None)
        assert check is not None
        assert not check.passed
        assert "not found" in check.message


class TestDoctorCodexSkillsCheck:
    def test_passes_when_skill_installed(self, filigree_project: Path) -> None:
        install_codex_skills(filigree_project)
        results = run_doctor(filigree_project)
        check = next((r for r in results if r.name == "Codex skills"), None)
        assert check is not None
        assert check.passed

    def test_fails_when_skill_missing(self, filigree_project: Path) -> None:
        results = run_doctor(filigree_project)
        check = next((r for r in results if r.name == "Codex skills"), None)
        assert check is not None
        assert not check.passed
        assert "not found" in check.message


class TestDoctorConnectionLeak:
    """Bug filigree-3bbc6f: run_doctor must close SQLite connection even on failure."""

    def test_connection_closed_on_db_error(self, filigree_project: Path) -> None:
        """If conn.execute() raises, conn.close() should still be called."""
        import sqlite3
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("table issues has no column named x")

        def fake_connect(*args: object, **kwargs: object) -> MagicMock:
            return mock_conn

        with patch("filigree.install_support.doctor.sqlite3.connect", side_effect=fake_connect):
            results = run_doctor(filigree_project)

        # Connection should have been closed despite the error
        mock_conn.close.assert_called_once()
        # Should report the DB error, not crash
        db_check = next((r for r in results if r.name == "filigree.db"), None)
        assert db_check is not None
        assert not db_check.passed


class TestInstallMcpServerMode:
    @pytest.fixture(autouse=True)
    def _isolate_server_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mint the server token into a temp dir, never the real ~/.config/filigree.
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", tmp_path / "_srvcfg")
        for _v in ("WEFT_FEDERATION_TOKEN", "FILIGREE_FEDERATION_API_TOKEN", "FILIGREE_API_TOKEN"):
            monkeypatch.delenv(_v, raising=False)

    def test_server_mode_writes_streamable_http(self, tmp_path: Path) -> None:
        project_root = tmp_path
        filigree_dir = project_root / ".filigree"
        filigree_dir.mkdir()
        config = {"prefix": "test", "mode": "server"}
        (filigree_dir / "config.json").write_text(json.dumps(config))

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(project_root, mode="server", server_port=8377)
        assert ok
        mcp = json.loads((project_root / ".mcp.json").read_text())
        server_config = mcp["mcpServers"]["filigree"]
        assert server_config["type"] == "streamable-http"
        assert server_config["url"] == "http://localhost:8377/mcp/?project=test"

    def test_server_mode_embeds_literal_project_token(self, tmp_path: Path) -> None:
        """Server-mode install embeds the LITERAL *project* federation token in the
        header — no ${ENV} indirection, no export. The URL is project-scoped
        (/mcp/?project=) and the daemon validates a scoped request against THAT
        project's token (weft-23574069a1), so the embedded value must be the
        project store-dir token — NOT the daemon home-store token."""
        from filigree.core import resolve_store_dir

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path, mode="server", server_port=8377)
        assert ok
        project_token = (resolve_store_dir(tmp_path) / "federation_token").read_text().strip()
        assert project_token
        auth = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["filigree"]["headers"]["Authorization"]
        assert auth == f"Bearer {project_token}"
        assert "${" not in auth  # literal, not an env-var reference
        # It must NOT be the daemon home-store token (the pre-fix behaviour).
        home_token_file = tmp_path / "_srvcfg" / "federation_token"
        if home_token_file.exists():
            assert project_token != home_token_file.read_text().strip()

    def test_ethereal_mode_writes_stdio(self, tmp_path: Path) -> None:
        project_root = tmp_path
        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(project_root, mode="ethereal")
        assert ok
        mcp = json.loads((project_root / ".mcp.json").read_text())
        server_config = mcp["mcpServers"]["filigree"]
        assert server_config.get("type") == "stdio" or "command" in server_config
        assert server_config["args"] == []

    def test_server_mode_mcp_json_is_owner_only(self, tmp_path: Path) -> None:
        """The server-mode .mcp.json carries the LITERAL federation token, so it
        must be written 0600 — matching the token file's own mode and making the
        module's "0600" claim honest (filigree-ebfc16a090)."""
        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path, mode="server", server_port=8377)
        assert ok
        mode = stat.S_IMODE((tmp_path / ".mcp.json").stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {mode:#o}"

    def test_server_mode_guards_mcp_json_in_gitignore(self, tmp_path: Path) -> None:
        """Server-mode install must add a .mcp.json gitignore guard so the embedded
        per-machine literal token never enters git history — a committed literal
        makes every other clone 401 (filigree-ebfc16a090)."""
        from filigree.install_support.gitignore import MCP_JSON_IGNORE_RULES, has_active_ignore

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path, mode="server", server_port=8377)
        assert ok
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert has_active_ignore(gitignore.read_text(), MCP_JSON_IGNORE_RULES)

    def test_ethereal_mode_does_not_guard_mcp_json(self, tmp_path: Path) -> None:
        """Ethereal mode writes a token-less stdio entry that is safe to commit;
        .mcp.json is a shared suite artifact, so filigree must NOT gitignore it in
        ethereal mode (guard is server-mode only) (filigree-ebfc16a090)."""
        from filigree.install_support.gitignore import MCP_JSON_IGNORE_RULES, has_active_ignore

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path, mode="ethereal")
        assert ok
        gitignore = tmp_path / ".gitignore"
        content = gitignore.read_text() if gitignore.exists() else ""
        assert not has_active_ignore(content, MCP_JSON_IGNORE_RULES)

    def test_server_mode_warns_when_mcp_json_already_tracked(self, tmp_path: Path) -> None:
        """If .mcp.json is already git-tracked, the gitignore guard is a no-op for
        it — the literal token still lands on the next commit. The install must
        surface that as a warning (filigree-ebfc16a090)."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}\n')
        subprocess.run(["git", "add", ".mcp.json"], cwd=tmp_path, check=True)

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, msg = install_claude_code_mcp(tmp_path, mode="server", server_port=8377)
        assert ok
        lower = msg.lower()
        assert "track" in lower
        assert "git rm --cached" in lower


class TestCheckResult:
    def test_passed_icon(self) -> None:
        r = CheckResult("test", True, "ok")
        assert r.icon == "OK"

    def test_failed_icon(self) -> None:
        r = CheckResult("test", False, "bad")
        assert r.icon == "!!"

    def test_fix_hint(self) -> None:
        r = CheckResult("test", False, "bad", fix_hint="Run: filigree init")
        assert r.fix_hint == "Run: filigree init"


def _setup_project(tmp_path: Path, mode: str = "ethereal") -> Path:
    """Helper to create a minimal filigree project."""
    filigree_dir = tmp_path / ".filigree"
    filigree_dir.mkdir()
    config = {"prefix": "test", "version": 1, "mode": mode}
    (filigree_dir / "config.json").write_text(json.dumps(config))
    from filigree.core import DB_FILENAME, FiligreeDB

    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="test")
    db.initialize()
    db.close()
    return filigree_dir


class TestDoctorModeChecks:
    def test_ethereal_checks_pid_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor in ethereal mode should check ephemeral.pid."""
        filigree_dir = _setup_project(tmp_path, mode="ethereal")
        # Write a stale PID (JSON format)
        (filigree_dir / "ephemeral.pid").write_text(json.dumps({"pid": 99999999, "cmd": "filigree"}))
        monkeypatch.chdir(tmp_path)

        results = run_doctor(project_root=tmp_path)
        names = [r.name for r in results]
        assert "Ephemeral PID" in names
        pid_result = next(r for r in results if r.name == "Ephemeral PID")
        assert not pid_result.passed  # stale PID

    def test_server_checks_daemon(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doctor in server mode should check daemon health."""
        _setup_project(tmp_path, mode="server")
        monkeypatch.chdir(tmp_path)

        config_dir = tmp_path / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        results = run_doctor(project_root=tmp_path)
        names = [r.name for r in results]
        assert "Server daemon" in names
        daemon_result = next(r for r in results if r.name == "Server daemon")
        assert not daemon_result.passed  # not running


class TestInstructionsSessionHint:
    def test_instructions_contain_session_context_hint(self) -> None:
        from filigree.install import _instructions_text

        text = _instructions_text()
        assert "filigree session-context" in text


# ===========================================================================
# Peripheral robustness fixes (from test_peripheral_fixes.py)
# Covers: TOML escaping, presence check, PackageNotFoundError, malformed .mcp.json
# ===========================================================================


class TestCodexTomlBackslash:
    def test_backslash_paths_escaped(self, tmp_path: Path) -> None:
        """Windows-style backslash paths must be escaped in TOML output."""
        home = tmp_path / "home"
        home.mkdir()
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch(
                "filigree.install_support.integrations._find_filigree_mcp_command",
                return_value="C:\\Program Files\\filigree\\filigree-mcp.exe",
            ),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        content = (home / ".codex" / "config.toml").read_text()
        # The raw TOML should have escaped backslashes
        assert "C:\\\\Program Files\\\\filigree\\\\filigree-mcp.exe" in content

    def test_unix_paths_unchanged(self, tmp_path: Path) -> None:
        """Unix paths (no backslashes) should be passed through unchanged."""
        home = tmp_path / "home"
        home.mkdir()
        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
            patch(
                "filigree.install_support.integrations._find_filigree_mcp_command",
                return_value="/usr/local/bin/filigree-mcp",
            ),
        ):
            ok, _msg = install_codex_mcp(tmp_path)
        assert ok
        content = (home / ".codex" / "config.toml").read_text()
        assert "/usr/local/bin/filigree-mcp" in content


class TestCodexTomlPresenceCheck:
    def test_filigree_extra_does_not_match(self, tmp_path: Path) -> None:
        """A TOML section [mcp_servers.filigree-extra] should NOT be mistaken for filigree."""
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        config = codex_dir / "config.toml"
        config.write_text('[mcp_servers.filigree-extra]\ncommand = "other"\n')

        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            ok, msg = install_codex_mcp(tmp_path)
        assert ok
        # Should have written a new filigree section (not returned "Already configured")
        assert "Already configured" not in msg
        content = config.read_text()
        assert "[mcp_servers.filigree]" in content

    def test_exact_filigree_detected(self, tmp_path: Path) -> None:
        """An existing [mcp_servers.filigree] section should be detected correctly."""
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        config = codex_dir / "config.toml"
        config.write_text('[mcp_servers.filigree]\ncommand = "filigree-mcp"\nargs = []\n')

        with (
            patch("filigree.install_support.integrations._find_filigree_mcp_command", return_value="filigree-mcp"),
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            ok, msg = install_codex_mcp(tmp_path)
        assert ok
        assert "Already configured" in msg

    def test_existing_pinned_project_is_reconfigured(self, tmp_path: Path) -> None:
        """A stale project-pinned filigree entry should be rewritten to autodiscovery."""
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        home.mkdir()
        codex_dir.mkdir()
        config = codex_dir / "config.toml"
        config.write_text('[mcp_servers.filigree]\ncommand = "filigree-mcp"\nargs = ["--project", "/tmp/other"]\n')

        with (
            patch("filigree.install_support.integrations.Path.home", return_value=home),
            patch("filigree.install_support.integrations.shutil.which", return_value=None),
        ):
            ok, msg = install_codex_mcp(tmp_path)
        assert ok
        assert "Already configured" not in msg
        content = config.read_text()
        assert "args = []" in content

    def test_server_mode_still_writes_stdio_autodiscovery(self, tmp_path: Path) -> None:
        """Server mode should not switch Codex to URL routing."""
        home = tmp_path / "home"
        home.mkdir()
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"prefix": "testproj", "mode": "server"}))

        with patch("filigree.install_support.integrations.Path.home", return_value=home):
            ok, _msg = install_codex_mcp(tmp_path, mode="server", server_port=9911)
        assert ok
        content = (home / ".codex" / "config.toml").read_text()
        assert 'command = "' in content
        assert "filigree-mcp" in content
        assert "args = []" in content
        assert "url =" not in content


class TestPackageNotFoundError:
    def test_import_falls_back_to_source_pyproject_when_metadata_missing(self) -> None:
        """Source-only execution recovers the real version from pyproject.toml."""
        import tomllib
        from importlib.metadata import PackageNotFoundError
        from pathlib import Path

        repo_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        expected = tomllib.loads(repo_pyproject.read_text(encoding="utf-8"))["project"]["version"]

        with patch(
            "importlib.metadata.version",
            side_effect=PackageNotFoundError("filigree"),
        ):
            import importlib

            import filigree

            importlib.reload(filigree)
            try:
                assert filigree.__version__ == expected
            finally:
                importlib.reload(filigree)

    def test_falls_back_to_dev_when_no_metadata_and_no_pyproject(self) -> None:
        """Final fallback to 0.0.0-dev when neither metadata nor pyproject exists."""
        import tomllib
        from importlib.metadata import PackageNotFoundError

        # Force _read_source_version to return None by making tomllib.loads fail.
        # Patching tomllib (module-global) survives importlib.reload, unlike
        # patching the bound function in filigree's namespace.
        with (
            patch(
                "importlib.metadata.version",
                side_effect=PackageNotFoundError("filigree"),
            ),
            patch.object(
                tomllib,
                "loads",
                side_effect=tomllib.TOMLDecodeError("forced", "", 0),
            ),
        ):
            import importlib

            import filigree

            importlib.reload(filigree)
            try:
                assert filigree.__version__ == "0.0.0-dev"
            finally:
                importlib.reload(filigree)

    def test_version_set_when_installed(self) -> None:
        """When package is installed, __version__ should be set from metadata."""
        import filigree

        # In our test environment, filigree should be installed
        assert filigree.__version__ is not None
        assert isinstance(filigree.__version__, str)

    def test_source_pyproject_wins_over_stale_installed_metadata(self) -> None:
        # When the source checkout is being imported but a stale `filigree`
        # dist-info is also installed in the environment, `__version__` must
        # reflect the source checkout — otherwise `filigree --version` and
        # `/api/health` advertise whatever older release happens to share the
        # `filigree` distribution name on this machine.
        import tomllib
        from pathlib import Path

        repo_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        expected = tomllib.loads(repo_pyproject.read_text(encoding="utf-8"))["project"]["version"]
        assert expected != "0.0.1-stale-fixture"

        with patch("importlib.metadata.version", return_value="0.0.1-stale-fixture"):
            import importlib

            import filigree

            importlib.reload(filigree)
            try:
                assert filigree.__version__ == expected
            finally:
                importlib.reload(filigree)


class TestMalformedMcpJson:
    def test_malformed_json_recovered(self, tmp_path: Path) -> None:
        """If .mcp.json contains invalid JSON, install should recover gracefully."""
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text("{this is not valid json!!!")

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)

        assert ok
        # The output file should now be valid JSON with filigree configured
        data = json.loads(mcp_json.read_text())
        assert "filigree" in data["mcpServers"]

    def test_malformed_json_backup_created(self, tmp_path: Path) -> None:
        """The corrupt .mcp.json should be backed up before overwriting."""
        mcp_json = tmp_path / ".mcp.json"
        corrupt_content = "{this is not valid json!!!"
        mcp_json.write_text(corrupt_content)

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            install_claude_code_mcp(tmp_path)

        backup = tmp_path / ".mcp.json.bak"
        assert backup.exists()
        assert backup.read_text() == corrupt_content

    def test_valid_json_preserved(self, tmp_path: Path) -> None:
        """Valid .mcp.json with existing entries should be preserved."""
        mcp_json = tmp_path / ".mcp.json"
        existing = {"mcpServers": {"other_tool": {"type": "stdio", "command": "other"}}}
        mcp_json.write_text(json.dumps(existing))

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)

        assert ok
        data = json.loads(mcp_json.read_text())
        assert "other_tool" in data["mcpServers"]
        assert "filigree" in data["mcpServers"]
        # No backup should be created for valid JSON
        assert not (tmp_path / ".mcp.json.bak").exists()

    def test_empty_json_file(self, tmp_path: Path) -> None:
        """An empty .mcp.json file should be handled gracefully."""
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text("")

        with patch("filigree.install_support.integrations.shutil.which", return_value=None):
            ok, _msg = install_claude_code_mcp(tmp_path)

        assert ok
        data = json.loads(mcp_json.read_text())
        assert "filigree" in data["mcpServers"]
