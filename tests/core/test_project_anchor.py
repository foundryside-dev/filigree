"""Tests for .filigree.conf project anchor + prefix-mismatch guard.

Bug filigree-7840eae0bd: an agent in a directory with no .filigree/ silently
walks up into a parent's .filigree/ and writes tickets into the wrong DB.

The 2.0 fix introduces:
  * .filigree.conf — JSON marker file at the project root, the authoritative
    discovery anchor. Walk-up looks for this file (not the .filigree/ dir).
    Nested .filigree.conf overrides parent.
  * Read-only discovery — legacy installs (have .filigree/, no .filigree.conf)
    are still discoverable via :func:`find_filigree_anchor`, but discovery
    itself never writes. The conf is created only by explicit init/install
    paths so inspection commands work on read-only mounts.
  * WrongProjectError — every ID-taking method rejects IDs whose prefix
    doesn't match the DB's prefix.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from filigree.core import (
    CONF_FILENAME,
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    FiligreeDB,
    ForeignDatabaseError,
    ProjectNotInitialisedError,
    WrongProjectError,
    find_filigree_anchor,
    find_filigree_conf,
    find_filigree_root,
    read_conf,
    write_conf,
)
from filigree.db_schema import CURRENT_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Discovery: find_filigree_conf
# ---------------------------------------------------------------------------


class TestFindFiligreeConf:
    def test_finds_in_current_dir(self, tmp_path: Path) -> None:
        conf = tmp_path / CONF_FILENAME
        write_conf(conf, {"version": 1, "project_name": "p", "prefix": "p", "db": ".filigree/filigree.db"})
        assert find_filigree_conf(tmp_path) == conf

    def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        conf = tmp_path / CONF_FILENAME
        write_conf(conf, {"version": 1, "project_name": "p", "prefix": "p", "db": ".filigree/filigree.db"})
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        assert find_filigree_conf(sub) == conf

    def test_child_conf_overrides_parent(self, tmp_path: Path) -> None:
        """Nested .filigree.conf — the child claim wins for that subtree."""
        parent_conf = tmp_path / CONF_FILENAME
        write_conf(parent_conf, {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"})

        child_dir = tmp_path / "sub-project"
        child_dir.mkdir()
        child_conf = child_dir / CONF_FILENAME
        write_conf(child_conf, {"version": 1, "project_name": "inner", "prefix": "inner", "db": ".filigree/filigree.db"})

        # From inside child: child wins
        deep = child_dir / "src"
        deep.mkdir()
        assert find_filigree_conf(deep) == child_conf

        # From a sibling that's not under the child: parent wins
        sibling = tmp_path / "other"
        sibling.mkdir()
        assert find_filigree_conf(sibling) == parent_conf

    def test_raises_when_no_conf_anywhere(self, tmp_path: Path) -> None:
        """No .filigree.conf in tree → ProjectNotInitialisedError with init/doctor hint."""
        with pytest.raises(ProjectNotInitialisedError) as excinfo:
            find_filigree_conf(tmp_path)
        msg = str(excinfo.value)
        assert "filigree init" in msg
        assert "filigree doctor" in msg

    def test_strict_raises_for_legacy_only_install(self, tmp_path: Path) -> None:
        """``find_filigree_conf`` is strict: a bare legacy ``.filigree/`` does
        not satisfy it. Callers that need to tolerate legacy installs must use
        :func:`find_filigree_anchor` instead.
        """
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        with pytest.raises(ProjectNotInitialisedError):
            find_filigree_conf(tmp_path)
        # And — critically — discovery did NOT write the conf.
        assert not (tmp_path / CONF_FILENAME).exists()


# ---------------------------------------------------------------------------
# Conf file I/O
# ---------------------------------------------------------------------------


class TestConfIO:
    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        conf = tmp_path / CONF_FILENAME
        data = {"version": 1, "project_name": "demo", "prefix": "demo", "db": ".filigree/filigree.db"}
        write_conf(conf, data)
        assert read_conf(conf) == data

    def test_read_rejects_non_dict(self, tmp_path: Path) -> None:
        conf = tmp_path / CONF_FILENAME
        conf.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError, match="object"):
            read_conf(conf)

    def test_read_rejects_missing_required_keys(self, tmp_path: Path) -> None:
        conf = tmp_path / CONF_FILENAME
        conf.write_text(json.dumps({"version": 1}))  # no prefix, no db
        with pytest.raises(ValueError, match=r"prefix|db"):
            read_conf(conf)

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ({"prefix": "x", "db": []}, r"'db'"),
            ({"prefix": "x", "db": ""}, r"'db'"),
            ({"prefix": [], "db": "filigree.db"}, r"'prefix'"),
            ({"prefix": "", "db": "filigree.db"}, r"'prefix'"),
            ({"prefix": "x", "db": "filigree.db", "enabled_packs": "core"}, r"enabled_packs"),
            ({"prefix": "x", "db": "filigree.db", "enabled_packs": [1, 2]}, r"enabled_packs"),
        ],
        ids=["db-list", "db-empty", "prefix-list", "prefix-empty", "packs-string", "packs-non-string-items"],
    )
    def test_read_rejects_malformed_field_types(self, tmp_path: Path, payload: dict[str, object], match: str) -> None:
        """Bug filigree-0f0e76f4b6: type-check ``prefix``/``db``/``enabled_packs``
        in the validator instead of letting downstream raise ``TypeError``.
        """
        conf = tmp_path / CONF_FILENAME
        conf.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match=match):
            read_conf(conf)

    @pytest.mark.parametrize(
        "db_value",
        ["/tmp/escape.db", "../escape.db", "subdir/../../escape.db"],  # noqa: S108 — path strings, not real /tmp use
        ids=["absolute", "parent-traversal", "nested-traversal"],
    )
    def test_read_rejects_db_path_outside_project(self, tmp_path: Path, db_value: str) -> None:
        """Bug filigree-4a40b58dce: ``db`` must stay under the conf's directory.

        A crafted ``.filigree.conf`` with an absolute path or ``..`` traversal
        could otherwise cause ordinary CLI commands to open a SQLite database
        anywhere on the user's filesystem — silent injection via a checked-in
        config file.
        """
        conf = tmp_path / CONF_FILENAME
        conf.write_text(json.dumps({"prefix": "x", "db": db_value}))
        with pytest.raises(ValueError, match=r"'db'"):
            read_conf(conf)


# ---------------------------------------------------------------------------
# find_filigree_anchor — discovery that tolerates legacy installs without writing
# ---------------------------------------------------------------------------


class TestFindFiligreeAnchor:
    """``find_filigree_anchor`` is the discovery primitive for read-only
    contexts (inspection commands, MCP startup, ``filigree doctor``).

    It returns ``(project_root, conf_path_or_None)`` so callers can decide
    how to open the project — via :meth:`FiligreeDB.from_conf` for v2.0
    installs, or via :meth:`FiligreeDB.from_filigree_dir` for legacy ones —
    without ever requiring write access during discovery.
    """

    def test_returns_conf_path_for_v2_install(self, tmp_path: Path) -> None:
        conf = tmp_path / CONF_FILENAME
        write_conf(conf, {"version": 1, "project_name": "p", "prefix": "p", "db": ".filigree/filigree.db"})
        project_root, conf_path = find_filigree_anchor(tmp_path)
        assert project_root == tmp_path
        assert conf_path == conf

    def test_returns_none_conf_for_legacy_install(self, tmp_path: Path) -> None:
        """Legacy install: project_root is identified, conf_path is None."""
        legacy_dir = tmp_path / FILIGREE_DIR_NAME
        legacy_dir.mkdir()
        project_root, conf_path = find_filigree_anchor(tmp_path)
        assert project_root == tmp_path
        assert conf_path is None

    def test_does_not_write_during_legacy_discovery(self, tmp_path: Path) -> None:
        """Regression: discovery must be read-only.

        Previously :func:`find_filigree_conf` backfilled ``.filigree.conf`` on
        legacy installs during the walk, which broke read-only mounts and
        inspection-only commands. Discovery now never writes.
        """
        legacy_dir = tmp_path / FILIGREE_DIR_NAME
        legacy_dir.mkdir()
        (legacy_dir / "config.json").write_text(json.dumps({"prefix": "legacy", "name": "Legacy", "version": 1}))
        FiligreeDB(legacy_dir / DB_FILENAME, prefix="legacy").initialize()

        find_filigree_anchor(tmp_path)
        find_filigree_root(tmp_path)
        assert not (tmp_path / CONF_FILENAME).exists()

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="POSIX-only; root bypasses dir mode bits",
    )
    def test_legacy_install_discoverable_on_readonly_mount(self, tmp_path: Path) -> None:
        """The motivating regression: discovery must not fail with PermissionError
        when the project root is read-only and a pre-2.0 ``.filigree/`` exists.
        """
        legacy_dir = tmp_path / FILIGREE_DIR_NAME
        legacy_dir.mkdir()
        FiligreeDB(legacy_dir / DB_FILENAME, prefix="legacy").initialize()

        # Drop write permission on the project root (simulates a read-only checkout).
        original_mode = tmp_path.stat().st_mode
        os.chmod(tmp_path, 0o555)  # noqa: S103 — read-only mode is the test scenario
        try:
            project_root, conf_path = find_filigree_anchor(tmp_path)
        finally:
            os.chmod(tmp_path, original_mode)

        assert project_root == tmp_path
        assert conf_path is None  # legacy — no write attempted
        assert not (tmp_path / CONF_FILENAME).exists()

    def test_child_anchor_wins_over_legacy_ancestor(self, tmp_path: Path) -> None:
        """A child ``.filigree.conf`` takes precedence even when an ancestor
        has a legacy ``.filigree/`` dir — closer-first walk-up.
        """
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        child = tmp_path / "sub"
        child.mkdir()
        child_conf = child / CONF_FILENAME
        write_conf(child_conf, {"version": 1, "project_name": "c", "prefix": "c", "db": ".filigree/filigree.db"})

        project_root, conf_path = find_filigree_anchor(child)
        assert project_root == child
        assert conf_path == child_conf

    def test_raises_when_neither_anchor_anywhere(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectNotInitialisedError):
            find_filigree_anchor(tmp_path)


# ---------------------------------------------------------------------------
# Foreign-database detection: refuse to latch onto an ancestor project's DB
# when the cwd is inside a git repo that has no anchor of its own.
# ---------------------------------------------------------------------------


class TestForeignDatabaseDetection:
    """When filigree is installed globally, a naïve walk-up lets an LLM in a
    directory with no ``.filigree.conf`` silently open whichever parent
    project's database it finds. The fix is a runtime guard: if discovery
    walks past a ``.git/`` boundary before finding an anchor, refuse with a
    ``ForeignDatabaseError`` whose message tells the caller to run
    ``filigree init`` (and restart MCP) in the current project.
    """

    def test_refuses_when_git_sits_below_ancestor_conf(self, tmp_path: Path) -> None:
        """cwd is inside its own git repo; the ancestor has the conf."""
        # Ancestor: foreign project with .filigree.conf
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        # Inner: separate git repo, no conf of its own
        inner = tmp_path / "inner-repo"
        inner.mkdir()
        (inner / ".git").mkdir()

        with pytest.raises(ForeignDatabaseError) as excinfo:
            find_filigree_anchor(inner)

        exc = excinfo.value
        assert exc.cwd == inner.resolve()
        assert exc.found_anchor == tmp_path / CONF_FILENAME
        assert exc.git_boundary == inner.resolve()
        # The message carries actionable guidance for the LLM.
        msg = str(exc)
        assert "filigree init" in msg
        assert "MCP" in msg or "mcp" in msg
        assert str(inner.resolve()) in msg

    def test_refuses_for_legacy_ancestor_beyond_git_boundary(self, tmp_path: Path) -> None:
        """Same refusal applies to legacy ``.filigree/`` ancestors."""
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        inner = tmp_path / "inner-repo"
        inner.mkdir()
        (inner / ".git").mkdir()

        with pytest.raises(ForeignDatabaseError):
            find_filigree_anchor(inner)

    def test_refuses_from_deep_subdir_past_git_boundary(self, tmp_path: Path) -> None:
        """Walk-up from several levels deep still detects the boundary."""
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        inner = tmp_path / "repo"
        inner.mkdir()
        (inner / ".git").mkdir()
        deep = inner / "src" / "pkg"
        deep.mkdir(parents=True)

        with pytest.raises(ForeignDatabaseError) as excinfo:
            find_filigree_anchor(deep)
        # The git_boundary is the inner repo, not the deeper subdir.
        assert excinfo.value.git_boundary == inner.resolve()

    def test_allows_conf_at_same_level_as_git(self, tmp_path: Path) -> None:
        """Monorepo case: conf sits at the git root — no boundary crossed."""
        (tmp_path / ".git").mkdir()
        conf = tmp_path / CONF_FILENAME
        write_conf(conf, {"version": 1, "project_name": "p", "prefix": "p", "db": ".filigree/filigree.db"})

        project_root, conf_path = find_filigree_anchor(tmp_path)
        assert project_root == tmp_path
        assert conf_path == conf

    def test_allows_conf_at_git_root_from_subdir(self, tmp_path: Path) -> None:
        """Walking up from inside the repo finds conf at the git root, OK."""
        (tmp_path / ".git").mkdir()
        conf = tmp_path / CONF_FILENAME
        write_conf(conf, {"version": 1, "project_name": "p", "prefix": "p", "db": ".filigree/filigree.db"})
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)

        project_root, conf_path = find_filigree_anchor(sub)
        assert project_root == tmp_path
        assert conf_path == conf

    def test_allows_walk_up_when_no_git_in_ancestry(self, tmp_path: Path) -> None:
        """No ``.git/`` anywhere → no boundary to enforce; walk-up is allowed."""
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "p", "prefix": "p", "db": ".filigree/filigree.db"},
        )
        sub = tmp_path / "sub"
        sub.mkdir()

        project_root, _conf_path = find_filigree_anchor(sub)
        assert project_root == tmp_path

    def test_find_filigree_conf_also_enforces_boundary(self, tmp_path: Path) -> None:
        """The strict variant (``find_filigree_conf``) must apply the same guard."""
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        inner = tmp_path / "repo"
        inner.mkdir()
        (inner / ".git").mkdir()

        with pytest.raises(ForeignDatabaseError):
            find_filigree_conf(inner)

    def test_git_file_submodule_is_also_a_boundary(self, tmp_path: Path) -> None:
        """A git submodule has ``.git`` as a file, not a directory — still a boundary."""
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        submodule = tmp_path / "submodule"
        submodule.mkdir()
        (submodule / ".git").write_text("gitdir: ../.git/modules/submodule\n")

        with pytest.raises(ForeignDatabaseError):
            find_filigree_anchor(submodule)

    def test_foreign_database_error_is_project_not_initialised(self, tmp_path: Path) -> None:
        """Existing generic handlers that catch ``ProjectNotInitialisedError``
        (and transitively ``FileNotFoundError``) continue to work — callers
        opt in to the richer behaviour by catching ``ForeignDatabaseError``
        specifically.
        """
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        inner = tmp_path / "inner"
        inner.mkdir()
        (inner / ".git").mkdir()

        with pytest.raises(ProjectNotInitialisedError):
            find_filigree_anchor(inner)
        with pytest.raises(FileNotFoundError):
            find_filigree_anchor(inner)

    def test_safe_message_omits_absolute_paths(self, tmp_path: Path) -> None:
        """``safe_message`` is the wire-exposed variant used by HTTP/MCP
        responses. It must not embed the cwd, anchor, or git-boundary
        absolute paths — those leak the user's directory layout.
        ``str(exc)`` keeps the rich diagnostic for CLI/stderr/doctor.
        """
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        inner = tmp_path / "inner"
        inner.mkdir()
        (inner / ".git").mkdir()

        with pytest.raises(ForeignDatabaseError) as excinfo:
            find_filigree_anchor(inner)
        exc = excinfo.value
        # Rich str() — still contains the paths for CLI use.
        assert str(inner.resolve()) in str(exc)
        # safe_message — no absolute paths from this fixture.
        assert str(inner.resolve()) not in exc.safe_message
        assert str(tmp_path.resolve()) not in exc.safe_message
        # But the diagnostic intent is preserved.
        assert "Refusing to latch" in exc.safe_message
        assert "filigree doctor" in exc.safe_message


class TestGitWorktreeDiscovery:
    """Git linked worktrees place a ``.git`` *file* (not directory) at the
    worktree root pointing at ``<main_repo>/.git/worktrees/<name>/``. Naïve
    walk-up discovery would treat that file as a project boundary and raise
    ``ForeignDatabaseError`` even though the worktree belongs to the same
    project as the main worktree. Discovery must recognise the worktree
    pointer and redirect to the main worktree root.
    """

    @staticmethod
    def _make_main_repo(tmp_path: Path, *, with_anchor: bool = True) -> Path:
        """Set up a main repo with ``.git/``, optionally with a filigree anchor."""
        repo = tmp_path / "main-repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        if with_anchor:
            write_conf(
                repo / CONF_FILENAME,
                {"version": 1, "project_name": "main", "prefix": "main", "db": ".filigree/filigree.db"},
            )
            (repo / FILIGREE_DIR_NAME).mkdir()
        return repo

    @staticmethod
    def _make_worktree(main_repo: Path, worktree_root: Path, name: str) -> Path:
        """Create a linked-worktree skeleton: ``.git`` file + main-repo bookkeeping.

        Matches git's on-disk layout (verified against real ``git worktree
        add``):

        - ``<worktree>/.git`` is a file containing ``gitdir: <admin>``.
        - ``<admin>/gitdir`` (the back-pointer) contains the absolute path
          to ``<worktree>/.git`` — used by
          :func:`_resolve_to_main_worktree` to verify the worktree pointer
          bidirectionally and reject spoofed or stale pointers.
        """
        wt_admin = main_repo / ".git" / "worktrees" / name
        wt_admin.mkdir(parents=True)
        worktree_root.mkdir(parents=True, exist_ok=True)
        wt_git_file = worktree_root / ".git"
        wt_git_file.write_text(f"gitdir: {wt_admin}\n")
        # Back-pointer that real ``git worktree add`` writes — load-bearing
        # for the bidirectional verification in _resolve_to_main_worktree.
        (wt_admin / "gitdir").write_text(f"{wt_git_file}\n")
        return worktree_root

    def test_worktree_inside_main_repo_finds_main_anchor(self, tmp_path: Path) -> None:
        """``main-repo/.worktrees/feature-x/`` resolves to main-repo's anchor."""
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, main / ".worktrees" / "feature-x", "feature-x")

        project_root, conf_path = find_filigree_anchor(wt)
        assert project_root == main
        assert conf_path == main / CONF_FILENAME

    def test_worktree_outside_main_repo_finds_main_anchor(self, tmp_path: Path) -> None:
        """Worktree placed as a sibling of the repo still resolves correctly."""
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, tmp_path / "sibling-worktree", "sibling")

        project_root, conf_path = find_filigree_anchor(wt)
        assert project_root == main
        assert conf_path == main / CONF_FILENAME

    def test_deep_subdir_inside_worktree_resolves(self, tmp_path: Path) -> None:
        """Walk-up from several levels deep inside a worktree still works."""
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, main / ".worktrees" / "feature-x", "feature-x")
        deep = wt / "src" / "pkg"
        deep.mkdir(parents=True)

        project_root, conf_path = find_filigree_anchor(deep)
        assert project_root == main
        assert conf_path == main / CONF_FILENAME

    def test_find_filigree_conf_also_resolves_worktree(self, tmp_path: Path) -> None:
        """The strict variant must apply the same redirect."""
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, tmp_path / "wt", "wt")

        assert find_filigree_conf(wt) == main / CONF_FILENAME

    def test_worktree_of_uninitialised_project_raises_not_initialised(self, tmp_path: Path) -> None:
        """Worktree of a repo with no anchor must raise ``ProjectNotInitialisedError``,
        not ``ForeignDatabaseError`` — the boundary guard fired spuriously before.
        """
        main = self._make_main_repo(tmp_path, with_anchor=False)
        wt = self._make_worktree(main, tmp_path / "wt", "wt")

        with pytest.raises(ProjectNotInitialisedError) as excinfo:
            find_filigree_anchor(wt)
        # Specifically NOT a ForeignDatabaseError — there is no foreign DB here.
        assert not isinstance(excinfo.value, ForeignDatabaseError)

    def test_submodule_still_a_boundary(self, tmp_path: Path) -> None:
        """Submodules use a ``.git`` file too, but with ``modules/`` not
        ``worktrees/`` — must remain a project boundary.
        """
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        submodule = tmp_path / "submodule"
        submodule.mkdir()
        (submodule / ".git").write_text("gitdir: ../.git/modules/submodule\n")

        with pytest.raises(ForeignDatabaseError):
            find_filigree_anchor(submodule)

    def test_relative_gitdir_in_worktree_pointer(self, tmp_path: Path) -> None:
        """Some git versions write a relative ``gitdir:`` path. Must still resolve."""
        main = self._make_main_repo(tmp_path)
        wt_admin = main / ".git" / "worktrees" / "rel"
        wt_admin.mkdir(parents=True)
        wt = main / ".worktrees" / "rel"
        wt.mkdir(parents=True)
        wt_git = wt / ".git"
        # Relative pointer: from wt/.git up to main/.git/worktrees/rel
        rel = os.path.relpath(wt_admin, wt)
        wt_git.write_text(f"gitdir: {rel}\n")
        # Back-pointer that real git writes — absolute path to wt/.git.
        (wt_admin / "gitdir").write_text(f"{wt_git}\n")

        project_root, _ = find_filigree_anchor(wt)
        assert project_root == main

    def test_nested_conf_inside_worktree_wins_over_main(self, tmp_path: Path) -> None:
        """A nested ``.filigree.conf`` inside a worktree subtree must still win.

        Codex review #39: the redirect must not skip past closer anchors in
        the worktree's own subtree, otherwise sub-projects inside a worktree
        get routed to the main repo's DB instead of their own.
        """
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, tmp_path / "wt", "wt")
        # Nested sub-project inside the worktree, with its own conf.
        nested = wt / "subproject"
        nested.mkdir()
        nested_conf = nested / CONF_FILENAME
        write_conf(
            nested_conf,
            {"version": 1, "project_name": "nested", "prefix": "nested", "db": ".filigree/filigree.db"},
        )

        # From inside the nested sub-project: nested wins, not main.
        project_root, conf_path = find_filigree_anchor(nested / "src")
        assert project_root == nested
        assert conf_path == nested_conf

        # Strict resolver agrees.
        assert find_filigree_conf(nested / "src") == nested_conf

        # And from the worktree root itself (above nested): we redirect to
        # main (no nested anchor in this direct ancestry).
        project_root, _ = find_filigree_anchor(wt)
        assert project_root == main

    def test_nested_legacy_dir_inside_worktree_wins_over_main(self, tmp_path: Path) -> None:
        """A nested legacy ``.filigree/`` inside a worktree subtree also wins."""
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, tmp_path / "wt", "wt")
        nested = wt / "legacy-sub"
        nested.mkdir()
        (nested / FILIGREE_DIR_NAME).mkdir()

        project_root, conf_path = find_filigree_anchor(nested / "deep" / "code")
        assert project_root == nested
        assert conf_path is None

    def test_foreign_database_error_reports_original_cwd(self, tmp_path: Path) -> None:
        """When the redirect resolves a worktree and the *main* worktree itself
        lives inside a foreign project, ``ForeignDatabaseError.cwd`` must report
        the caller's original CWD (inside the worktree), not the redirected
        main worktree root — otherwise the diagnostic points at the wrong place.
        """
        # Outer project has its own .filigree.conf
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        # Main repo nested inside the outer project, with no anchor of its own.
        main = self._make_main_repo(tmp_path / "main", with_anchor=False)
        # Worktree of the main repo.
        wt = self._make_worktree(main, tmp_path / "wt", "wt")
        wt_subdir = wt / "src"
        wt_subdir.mkdir()

        with pytest.raises(ForeignDatabaseError) as excinfo:
            find_filigree_anchor(wt_subdir)
        # The redirect resolved to ``main``; the boundary is ``main`` itself;
        # but ``cwd`` must point at the caller's original location.
        assert excinfo.value.cwd == wt_subdir.resolve()
        assert excinfo.value.git_boundary == main.resolve()

    def test_malformed_git_file_is_left_alone(self, tmp_path: Path) -> None:
        """A ``.git`` file with no ``gitdir:`` line falls back to start-unchanged
        behaviour — the existing walk-up handles whatever ancestry exists.
        """
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        weird = tmp_path / "weird"
        weird.mkdir()
        (weird / ".git").write_text("# not a worktree pointer\n")

        # ``.git`` is a file, but not a worktree pointer — the existing
        # boundary logic still treats it as a boundary and refuses.
        with pytest.raises(ForeignDatabaseError):
            find_filigree_anchor(weird)

    def test_empty_gitdir_value_is_left_alone(self, tmp_path: Path) -> None:
        """A ``.git`` file with an empty ``gitdir:`` value must not redirect.

        Otherwise ``Path("").resolve()`` returns cwd, which made the prior
        behaviour cwd-dependent (worked by accident because cwd was never
        named ``worktrees/<...>``).
        """
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        weird = tmp_path / "weird"
        weird.mkdir()
        (weird / ".git").write_text("gitdir:   \n")

        with pytest.raises(ForeignDatabaseError):
            find_filigree_anchor(weird)

    def test_crlf_gitdir_line_resolves(self, tmp_path: Path) -> None:
        """Some editors / git ports write CRLF line endings. ``splitlines``
        handles both, but pin the behaviour explicitly.
        """
        main = self._make_main_repo(tmp_path)
        wt_admin = main / ".git" / "worktrees" / "crlf"
        wt_admin.mkdir(parents=True)
        wt = tmp_path / "wt-crlf"
        wt.mkdir(parents=True)
        wt_git = wt / ".git"
        wt_git.write_text(f"gitdir: {wt_admin}\r\n")
        (wt_admin / "gitdir").write_text(f"{wt_git}\r\n")

        project_root, _ = find_filigree_anchor(wt)
        assert project_root == main

    def test_gitdir_value_with_null_byte_is_left_alone(self, tmp_path: Path) -> None:
        """A ``gitdir:`` value containing a NUL byte raises ``ValueError`` from
        ``Path.resolve()`` — must be caught and fall back to start.
        """
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"},
        )
        weird = tmp_path / "weird"
        weird.mkdir()
        (weird / ".git").write_text("gitdir: /tmp/foo\x00bar\n")

        # Must not crash — falls back, then the .git file is a boundary.
        with pytest.raises(ForeignDatabaseError):
            find_filigree_anchor(weird)

    def test_stale_worktree_admin_dir_does_not_redirect(self, tmp_path: Path) -> None:
        """A ``.git`` file pointing at a worktree admin dir that no longer
        exists must not redirect — the admin dir might have been deleted
        manually (``git worktree remove --force`` followed by partial
        cleanup), or the worktree was restored from backup without its
        admin dir. The pointer is stale; treating it as a valid redirect
        would route writes to a possibly-unrelated repo.
        """
        main = self._make_main_repo(tmp_path)
        wt = tmp_path / "stale-wt"
        wt.mkdir(parents=True)
        # Point at an admin dir that doesn't exist.
        (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'ghost'}\n")

        # No nested anchor, no foreign DB — the .git file falls back to
        # being treated as a boundary, and walk-up has no other anchor.
        with pytest.raises(ProjectNotInitialisedError) as excinfo:
            find_filigree_anchor(wt)
        assert not isinstance(excinfo.value, ForeignDatabaseError)

    def test_missing_back_pointer_does_not_redirect(self, tmp_path: Path) -> None:
        """Admin dir exists but has no ``gitdir`` back-pointer file — must
        not redirect. Real ``git worktree add`` always writes one.
        """
        main = self._make_main_repo(tmp_path)
        wt_admin = main / ".git" / "worktrees" / "no-back-pointer"
        wt_admin.mkdir(parents=True)
        wt = tmp_path / "wt-no-bp"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {wt_admin}\n")
        # Deliberately do NOT write wt_admin/gitdir.

        with pytest.raises(ProjectNotInitialisedError) as excinfo:
            find_filigree_anchor(wt)
        assert not isinstance(excinfo.value, ForeignDatabaseError)

    def test_spoofed_worktree_pointer_does_not_redirect(self, tmp_path: Path) -> None:
        """An attacker-controlled ``.git`` file pointing at a victim project's
        admin dir must not redirect — the victim's back-pointer points at
        the victim's worktree, not at the attacker's. This closes the
        confused-deputy that 2.0.3's redirect would otherwise have opened.
        """
        # Victim project: a real main repo + a real worktree of its own.
        victim_main = self._make_main_repo(tmp_path / "victim-main")
        victim_wt = self._make_worktree(victim_main, tmp_path / "victim-wt", "real")
        assert victim_wt  # silence unused-var; the back-pointer in the admin dir matters.

        # Attacker: a directory containing a .git file that points at the
        # victim's admin directory. No real worktree of victim — just a
        # spoofed pointer trying to ride the redirect into victim_main's DB.
        attacker = tmp_path / "attacker-clone"
        attacker.mkdir()
        (attacker / ".git").write_text(f"gitdir: {victim_main / '.git' / 'worktrees' / 'real'}\n")

        # Bidirectional check fires: victim's back-pointer doesn't point
        # at attacker's .git file, so redirect is refused. With no other
        # anchor in attacker's ancestry, ProjectNotInitialisedError.
        with pytest.raises(ProjectNotInitialisedError) as excinfo:
            find_filigree_anchor(attacker)
        assert not isinstance(excinfo.value, ForeignDatabaseError)

    def test_nested_legacy_dir_does_not_block_strict_redirect(self, tmp_path: Path) -> None:
        """A nested legacy ``.filigree/`` inside a worktree must NOT suppress
        the redirect for strict ``find_filigree_conf`` — strict callers
        ignore legacy dirs, so a stray legacy dir shouldn't make them raise
        a spurious ForeignDatabaseError when the main repo has a conf.

        Regression for the asymmetric-anchor-predicate bug:
        ``_resolve_to_main_worktree`` accepted both anchor types as a
        "don't redirect past me" marker, but ``find_filigree_conf``
        only recognises ``.filigree.conf`` — so the legacy dir blocked
        the redirect, then strict walk-up ignored the legacy dir, hit
        the worktree ``.git`` as a boundary, and raised the wrong error.
        """
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, tmp_path / "wt", "wt")
        # A stray legacy dir inside the worktree (no conf).
        nested = wt / "legacy-sub"
        nested.mkdir()
        (nested / FILIGREE_DIR_NAME).mkdir()

        # Strict find_filigree_conf should still redirect to main's conf —
        # the legacy dir is invisible to it.
        assert find_filigree_conf(nested / "src") == main / CONF_FILENAME
        # And the tolerant find_filigree_anchor still honours the nested
        # legacy dir as a closer anchor.
        project_root, conf_path = find_filigree_anchor(nested / "src")
        assert project_root == nested
        assert conf_path is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_unreadable_git_file_does_not_redirect(self, tmp_path: Path) -> None:
        """A ``.git`` file we can't read falls back to start. Must not
        crash, must not silently open the main worktree's DB on a guess.
        """
        if os.geteuid() == 0:
            pytest.skip("root bypasses permission checks")
        main = self._make_main_repo(tmp_path)
        wt = self._make_worktree(main, tmp_path / "wt", "wt")
        wt_git = wt / ".git"
        original_mode = wt_git.stat().st_mode
        wt_git.chmod(0o000)
        try:
            # Permission denied on the worktree's .git — falls back, walk-up
            # then sees no other anchor in this subtree, fails with
            # ForeignDatabaseError (the .git file still exists() and triggers
            # the boundary guard even though we can't read it).
            with pytest.raises(ProjectNotInitialisedError):
                find_filigree_anchor(wt)
        finally:
            wt_git.chmod(original_mode)


# ---------------------------------------------------------------------------
# FiligreeDB.from_project / from_conf — discovery integration
# ---------------------------------------------------------------------------


class TestFindFiligreeRoot:
    """``find_filigree_root`` is a back-compat helper. Its historical contract
    is to return the project's ``.filigree/`` directory — every caller in the
    repo concatenates ``SUMMARY_FILENAME`` / ``ephemeral.pid`` / ``DB_FILENAME``
    onto it, or does ``.parent`` to derive the project root.

    Returning ``db_path.parent`` (the previous v2.0 behaviour) silently
    misroutes those callers when the conf's ``db`` field points elsewhere.
    """

    def test_returns_dotfiligree_dir_for_default_layout(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "p", "prefix": "p", "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"},
        )
        assert find_filigree_root(tmp_path) == filigree_dir

    def test_returns_dotfiligree_dir_when_db_points_elsewhere(self, tmp_path: Path) -> None:
        """Custom ``db`` location must not alter ``find_filigree_root``'s contract.

        Regression: returning ``db_path.parent`` made ``mcp_server._run`` reopen
        ``storage/filigree.db`` instead of the configured ``storage/track.db``,
        and made ``install`` derive the wrong project root via ``.parent``.
        """
        (tmp_path / "storage").mkdir()
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "p", "prefix": "p", "db": "storage/track.db"},
        )
        assert find_filigree_root(tmp_path) == tmp_path / FILIGREE_DIR_NAME


class TestFromConf:
    def test_from_conf_opens_db_at_relative_path(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        conf = tmp_path / CONF_FILENAME
        write_conf(
            conf,
            {"version": 1, "project_name": "p", "prefix": "p", "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"},
        )

        db = FiligreeDB.from_conf(conf)
        try:
            assert db.prefix == "p"
            assert db.db_path == filigree_dir / DB_FILENAME
        finally:
            db.close()

    def test_from_conf_uses_config_enabled_packs_when_conf_omits_them(self, tmp_path: Path) -> None:
        """The conf anchors the DB, while config.json remains the project pack source."""
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"prefix": "p", "version": 1, "enabled_packs": ["core"]}))
        conf = tmp_path / CONF_FILENAME
        write_conf(
            conf,
            {"version": 1, "project_name": "p", "prefix": "p", "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"},
        )

        db = FiligreeDB.from_conf(conf)
        try:
            assert db.enabled_packs == ["core"]
            assert db.list_issues(type="release") == []
        finally:
            db.close()


class TestFactoriesCloseConnOnInitFailure:
    """Regression: ``from_filigree_dir`` / ``from_conf`` must close the
    lazily-opened SQLite connection if ``initialize()`` raises.

    Bug filigree-3449322141: ``initialize()``'s first statement opens the
    connection via ``get_schema_version()`` → ``self.conn``. If it then
    raises (e.g. schema version newer than this build supports), the
    classmethod exits before ``return db``, so the caller never receives a
    handle to ``close()``. The connection — and its WAL/SHM sidecar files —
    leak until the interpreter exits or GC collects the instance.

    Fix: the factories wrap ``initialize()`` in try/except, call
    ``db.close()`` on failure, and re-raise.
    """

    def _poison_db_with_future_schema(self, db_path: Path) -> None:
        """Create a SQLite file with a schema version newer than supported."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()

    def test_from_filigree_dir_closes_conn_when_initialize_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        self._poison_db_with_future_schema(filigree_dir / DB_FILENAME)

        created: list[FiligreeDB] = []
        original_init = FiligreeDB.__init__

        def capturing_init(self: FiligreeDB, *args: object, **kwargs: object) -> None:
            original_init(self, *args, **kwargs)  # type: ignore[arg-type]
            created.append(self)

        monkeypatch.setattr(FiligreeDB, "__init__", capturing_init)

        with pytest.raises(ValueError, match="newer than this version"):
            FiligreeDB.from_filigree_dir(filigree_dir)

        assert len(created) == 1, "expected a single FiligreeDB instance to be constructed"
        assert created[0]._conn is None, "from_filigree_dir must close the SQLite connection when initialize() raises"

    def test_from_conf_closes_conn_when_initialize_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        self._poison_db_with_future_schema(filigree_dir / DB_FILENAME)
        conf = tmp_path / CONF_FILENAME
        write_conf(
            conf,
            {"version": 1, "project_name": "p", "prefix": "p", "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"},
        )

        created: list[FiligreeDB] = []
        original_init = FiligreeDB.__init__

        def capturing_init(self: FiligreeDB, *args: object, **kwargs: object) -> None:
            original_init(self, *args, **kwargs)  # type: ignore[arg-type]
            created.append(self)

        monkeypatch.setattr(FiligreeDB, "__init__", capturing_init)

        with pytest.raises(ValueError, match="newer than this version"):
            FiligreeDB.from_conf(conf)

        assert len(created) == 1, "expected a single FiligreeDB instance to be constructed"
        assert created[0]._conn is None, "from_conf must close the SQLite connection when initialize() raises"


class TestFromFiligreeDirLegacyPrefixFallback:
    """Regression: legacy installs with no (or malformed) config.json must
    not silently open with ``prefix="filigree"``.

    Bug filigree-fda0e2a340: ``read_config`` returned a hardcoded default of
    ``prefix="filigree"`` when config.json was missing or lacked a ``prefix``
    key, and ``from_filigree_dir`` adopted it. A legacy project initialised
    by directory name (the pre-v2 behaviour) ended up mislabelled so every
    write to its own issues raised ``WrongProjectError``.

    Fix: when an explicit prefix is not present, fall back to the project
    directory's own name (``filigree_dir.parent.name``), mirroring
    ``filigree init``'s default.
    """

    def test_missing_config_uses_project_dir_name_as_prefix(self, tmp_path: Path) -> None:
        project_root = tmp_path / "myproj"
        project_root.mkdir()
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        # No config.json, no .filigree.conf — the mis-handled legacy path.
        # Pre-populate a DB with an issue under the project-name prefix so
        # the bad default would silently diverge.
        seed = FiligreeDB(filigree_dir / DB_FILENAME, prefix="myproj")
        seed.initialize()
        seed_issue = seed.create_issue("seed")
        seed.close()

        db = FiligreeDB.from_filigree_dir(filigree_dir)
        try:
            assert db.prefix == "myproj"
            # A mutation on the seeded issue must succeed, not raise
            # WrongProjectError — this is the symptom in the bug report.
            db.update_issue(seed_issue.id, title="renamed")
        finally:
            db.close()

    def test_config_missing_prefix_key_uses_dir_name(self, tmp_path: Path) -> None:
        """config.json exists but omits the ``prefix`` key (partial/corrupt)."""
        project_root = tmp_path / "widget-tracker"
        project_root.mkdir()
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"version": 1}))

        db = FiligreeDB.from_filigree_dir(filigree_dir)
        try:
            assert db.prefix == "widget-tracker"
        finally:
            db.close()

    def test_config_with_explicit_prefix_still_wins(self, tmp_path: Path) -> None:
        """Config-provided prefix must override any directory-name fallback."""
        project_root = tmp_path / "dirname-ignored"
        project_root.mkdir()
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"prefix": "explicit", "version": 1}))

        db = FiligreeDB.from_filigree_dir(filigree_dir)
        try:
            assert db.prefix == "explicit"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Prefix-mismatch guard
# ---------------------------------------------------------------------------


@pytest.fixture
def db_p(tmp_path: Path) -> FiligreeDB:
    """Fresh FiligreeDB with prefix='alpha'."""
    d = FiligreeDB(tmp_path / "filigree.db", prefix="alpha")
    d.initialize()
    return d


class TestWrongProjectErrorOnWrites:
    """The prefix-mismatch guard is enforced on **write** operations only.

    Reads (get_issue, get_comments, get_issue_files, get_issue_events,
    get_valid_transitions, validate_issue) intentionally do *not* enforce —
    they return KeyError / empty results as before. Migration and other
    cross-prefix read scenarios depend on this.

    Writes always enforce: an agent that climbed into the wrong DB and tries
    to mutate a foreign-prefix ticket gets WrongProjectError.
    """

    def test_update_issue_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError) as excinfo:
            db_p.update_issue("beefdata-abc123", title="x")
        msg = str(excinfo.value)
        assert "alpha" in msg
        assert "beefdata" in msg
        db_p.close()

    def test_close_issue_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.close_issue("beefdata-abc123")
        db_p.close()

    def test_reopen_issue_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.reopen_issue("beefdata-abc123")
        db_p.close()

    def test_claim_issue_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.claim_issue("beefdata-abc123", assignee="me")
        db_p.close()

    def test_release_claim_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.release_claim("beefdata-abc123")
        db_p.close()

    def test_add_comment_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.add_comment("beefdata-abc123", "hi")
        db_p.close()

    def test_add_label_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.add_label("beefdata-abc123", "foo")
        db_p.close()

    def test_remove_label_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.remove_label("beefdata-abc123", "foo")
        db_p.close()

    def test_add_dependency_with_wrong_prefix_raises_for_either_side(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.add_dependency("beefdata-abc", "alpha-xyz")
        with pytest.raises(WrongProjectError):
            db_p.add_dependency("alpha-xyz", "beefdata-abc")
        db_p.close()

    def test_remove_dependency_with_wrong_prefix_raises(self, db_p: FiligreeDB) -> None:
        with pytest.raises(WrongProjectError):
            db_p.remove_dependency("beefdata-abc", "alpha-xyz")
        db_p.close()

    def test_create_issue_unaffected(self, db_p: FiligreeDB) -> None:
        """create_issue assigns the prefix from the DB — no caller-supplied ID to check."""
        issue = db_p.create_issue("Test")
        assert issue.id.startswith("alpha-")
        db_p.close()

    def test_correct_prefix_passes(self, db_p: FiligreeDB) -> None:
        """Sanity: the prefix check doesn't reject legitimate IDs."""
        issue = db_p.create_issue("Test")
        fetched = db_p.get_issue(issue.id)
        assert fetched.id == issue.id
        db_p.close()

    def test_hyphenated_prefix_does_not_trip_guard(self, tmp_path: Path) -> None:
        """A project whose prefix contains a hyphen (e.g. ``my-app``) must still
        be able to mutate its own issues.

        Regression: ``filigree init`` defaults the prefix to ``cwd.name``; a
        repo checked out as ``my-app/`` generates IDs like ``my-app-abc1234567``.
        Splitting the ID on the first ``-`` returned ``my`` and falsely tripped
        WrongProjectError, leaving such projects effectively read-only.
        """
        db = FiligreeDB(tmp_path / "filigree.db", prefix="my-app")
        db.initialize()
        try:
            issue = db.create_issue("Test")
            assert issue.id.startswith("my-app-")
            # Each of these would have raised WrongProjectError pre-fix.
            db.update_issue(issue.id, title="renamed")
            db.add_label(issue.id, "needs-review")
            db.add_comment(issue.id, "still working")
            db.close_issue(issue.id)
        finally:
            db.close()

    def test_foreign_prefix_that_extends_local_prefix_raises(self, tmp_path: Path) -> None:
        """A DB with prefix 'a' must reject issue IDs whose real prefix is 'a-b'."""
        db = FiligreeDB(tmp_path / "filigree.db", prefix="a")
        db.initialize()
        issue_id = "a-b-1234567890"
        now = "2026-01-01T00:00:00+00:00"
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (issue_id, "foreign", "open", 2, "task", "", now, now, "", "", "{}"),
        )
        db.conn.commit()
        try:
            with pytest.raises(WrongProjectError, match=r"a-b|a"):
                db.update_issue(issue_id, title="mutated")
            assert db.get_issue(issue_id).title == "foreign"
        finally:
            db.close()


class TestReadsDoNotEnforcePrefix:
    """Read methods deliberately allow cross-prefix lookups (return KeyError)."""

    def test_get_issue_foreign_prefix_returns_keyerror(self, db_p: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db_p.get_issue("beefdata-abc123")
        db_p.close()

    def test_get_comments_foreign_prefix_returns_empty(self, db_p: FiligreeDB) -> None:
        # No row exists; should just return [] without error.
        assert db_p.get_comments("beefdata-abc123") == []
        db_p.close()
