"""MCP verb plumbing for the commit anchor (warpline seam, contract B).

``close_issue``, ``claim_issue``, and ``start_work`` accept an optional
``commit`` input that threads through to the DB layer and persists as
``close_commit`` / ``claim_commit``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from filigree import commit_reachability
from filigree.core import FiligreeDB, read_config, write_config
from filigree.mcp_tools.issues import (
    _handle_claim_issue,
    _handle_close_issue,
    _handle_get_issue,
    _handle_start_work,
)
from tests.mcp._helpers import _parse


def _anchors(db: FiligreeDB, issue_id: str) -> tuple[str | None, str | None]:
    row = db.conn.execute("SELECT claim_commit, close_commit FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return row["claim_commit"], row["close_commit"]


@pytest.mark.asyncio
async def test_close_issue_persists_commit(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("close via mcp", priority=2)
    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": "main@abc123"}))
    assert "error" not in data, data
    _, close_commit = _anchors(mcp_db, issue.id)
    assert close_commit == "main@abc123"
    # Read-side exposure: the public projection carries it.
    assert data["close_commit"] == "main@abc123"


@pytest.mark.asyncio
async def test_close_issue_without_commit_leaves_null(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("close no commit", priority=2)
    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done"}))
    assert "error" not in data, data
    _, close_commit = _anchors(mcp_db, issue.id)
    assert close_commit is None


@pytest.mark.asyncio
async def test_claim_issue_persists_commit(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("claim via mcp", priority=2)
    data = _parse(await _handle_claim_issue({"issue_id": issue.id, "assignee": "alice", "commit": "main@c0ffee"}))
    assert "error" not in data, data
    claim_commit, _ = _anchors(mcp_db, issue.id)
    assert claim_commit == "main@c0ffee"
    assert data["claim_commit"] == "main@c0ffee"


@pytest.mark.asyncio
async def test_start_work_persists_commit(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("start via mcp", priority=2)
    data = _parse(await _handle_start_work({"issue_id": issue.id, "assignee": "alice", "commit": "main@1234abcd"}))
    assert "error" not in data, data
    claim_commit, _ = _anchors(mcp_db, issue.id)
    assert claim_commit == "main@1234abcd"


# ---------------------------------------------------------------------------
# Reachability warning (Task 0.6, S2 prototype / X3): a close that cites a
# commit not reachable from origin/<integration_ref> still closes, but carries a
# ``warnings[]`` entry and records ``close_commit_reachable`` on the close
# event. Every repo here is a throwaway in tmp_path with a bare local "origin",
# so ``git fetch origin`` works offline.
# ---------------------------------------------------------------------------

_GIT_IDENT = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *_GIT_IDENT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return proc.stdout.strip()


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init_repo_with_origin(repo: Path) -> str:
    """Make *repo* a git repo whose ``origin`` is a bare sibling; push one commit to main."""
    origin = repo / "origin.git"
    _git(repo, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".git" / "info" / "exclude").write_text("origin.git/\n.filigree/\n")
    sha = _commit_file(repo, "a.txt", "a\n", "base")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    return sha


@pytest.fixture(autouse=True)
def _fresh_fetch_cache() -> None:
    commit_reachability.reset_fetch_cache()


@pytest.mark.asyncio
async def test_close_with_unreachable_commit_warns_but_closes(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    _init_repo_with_origin(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "side")
    side_sha = _commit_file(tmp_path, "b.txt", "b\n", "never pushed")
    issue = mcp_db.create_issue("close on an unpushed commit", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": f"side@{side_sha}"}))

    assert "error" not in data, data
    assert data["status_category"] == "done"  # never blocks in 3.x
    assert data["warnings"] == [f"commit_not_reachable_from_integration_ref: {side_sha} not in origin/main"]
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] is False


@pytest.mark.asyncio
async def test_close_with_reachable_commit_no_warning(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    base_sha = _init_repo_with_origin(tmp_path)
    issue = mcp_db.create_issue("close on a pushed commit", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": f"main@{base_sha[:9]}"}))

    assert "error" not in data, data
    assert "warnings" not in data
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] is True


@pytest.mark.asyncio
async def test_close_without_git_reports_unknown(mcp_db: FiligreeDB) -> None:
    # tmp_path (the project root) is not a git repository.
    issue = mcp_db.create_issue("close outside git", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": "main@deadbeefcafe"}))

    assert "error" not in data, data
    assert "warnings" not in data  # unknown never blames the commit
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] == "unknown"


@pytest.mark.asyncio
async def test_squash_merged_anchor_reports_unreachable_with_ref_named(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    # Review Focus 1: the work landed on main via a squash merge, so the cited
    # branch sha is NOT an ancestor of origin/main. That must be reported as
    # unreachable with both the sha and the ref named -- never silently passed.
    _init_repo_with_origin(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "feat")
    feat_sha = _commit_file(tmp_path, "f.txt", "feature\n", "feature work")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--squash", "feat")
    _git(tmp_path, "commit", "-q", "-m", "squash feat")
    _git(tmp_path, "push", "-q", "origin", "main")
    issue = mcp_db.create_issue("close on a squash-merged branch sha", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": f"feat@{feat_sha}"}))

    assert "error" not in data, data
    [warning] = data["warnings"]
    assert feat_sha in warning
    assert "origin/main" in warning
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] is False


@pytest.mark.asyncio
async def test_close_when_fetch_fails_reports_unknown(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    _init_repo_with_origin(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "side")
    side_sha = _commit_file(tmp_path, "b.txt", "b\n", "never pushed")
    _git(tmp_path, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    issue = mcp_db.create_issue("close with origin unreachable", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": f"side@{side_sha}"}))

    assert "error" not in data, data
    assert "warnings" not in data
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] == "unknown"


@pytest.mark.asyncio
async def test_close_with_sha_missing_after_fetch_reports_unreachable(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    # Fix round 1 (#2): after a successful fetch in a full (non-shallow) clone,
    # every commit reachable from origin/main is present locally. A sha git does
    # not have therefore cannot be an ancestor: merge-base's exit 128 is a
    # definite "not reachable", never a silent pass.
    _init_repo_with_origin(tmp_path)
    issue = mcp_db.create_issue("close on a foreign sha", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": "main@0123456789abcdef"}))

    assert "error" not in data, data
    assert data["warnings"] == ["commit_not_reachable_from_integration_ref: 0123456789abcdef not in origin/main"]
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] is False


@pytest.mark.asyncio
async def test_close_with_unpushed_sha_from_another_clone_reports_unreachable(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    # The motivating false-"shipped" case: the commit exists only in another
    # clone that never pushed it.
    _init_repo_with_origin(tmp_path)
    other = tmp_path / "other-clone"
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    foreign_sha = _commit_file(other, "x.txt", "x\n", "never pushed from the other clone")
    issue = mcp_db.create_issue("close on another clone's unpushed sha", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": f"feat@{foreign_sha}"}))

    assert "error" not in data, data
    assert data["warnings"] == [f"commit_not_reachable_from_integration_ref: {foreign_sha} not in origin/main"]


def _shallow_project(tmp_path: Path) -> tuple[Path, str, str]:
    """A shallow checkout of a 2-commit origin holding both commits as depth-1 roots.

    Returns (project, old_sha, head_sha); ``old_sha`` IS an ancestor of
    origin/main upstream, but the shallow history cannot show it.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    old_sha = _init_repo_with_origin(upstream)
    _git(upstream, "tag", "old", old_sha)
    head_sha = _commit_file(upstream, "b.txt", "b\n", "second")
    _git(upstream, "push", "-q", "origin", "main", "old")
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "remote", "add", "origin", f"file://{upstream / 'origin.git'}")
    _git(project, "fetch", "-q", "--depth", "1", "origin", "main")
    _git(project, "fetch", "-q", "--depth", "1", "origin", "refs/tags/old:refs/tags/old")
    assert _git(project, "rev-parse", "--is-shallow-repository") == "true"
    return project, old_sha, head_sha


def test_shallow_clone_exit1_at_boundary_reports_unknown(tmp_path: Path) -> None:
    # Fix round 1 (#3): both commits are shallow roots, so merge-base exits 1
    # although old_sha is reachable upstream. git cannot answer -> unknown.
    project, old_sha, _ = _shallow_project(tmp_path)
    assert subprocess.run(["git", "merge-base", "--is-ancestor", old_sha, "origin/main"], cwd=project, check=False).returncode == 1

    result = commit_reachability.check_commit_reachable(project, f"main@{old_sha}", "main")

    assert result.reachable == "unknown"
    assert result.warning is None


def test_shallow_clone_missing_object_reports_unknown(tmp_path: Path) -> None:
    project, _, _ = _shallow_project(tmp_path)
    result = commit_reachability.check_commit_reachable(project, "main@0123456789abcdef", "main")
    assert result.reachable == "unknown"


def test_shallow_clone_head_still_reachable(tmp_path: Path) -> None:
    project, _, head_sha = _shallow_project(tmp_path)
    assert commit_reachability.check_commit_reachable(project, f"main@{head_sha}", "main").reachable is True


class _FakeGit:
    """Scripted stand-in for ``subprocess.run`` keyed on the git subcommand."""

    def __init__(self, results: dict[str, tuple[int, str] | list[tuple[int, str]]]) -> None:
        # A list scripts successive calls (the last entry repeats).
        self.results = {k: list(v) if isinstance(v, list) else [v] for k, v in results.items()}
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        env = kwargs.get("env")
        assert isinstance(env, dict)
        self.envs.append(env)
        sub = argv[1] if argv[1] != "rev-parse" else argv[2].split("=")[0]
        script = self.results[sub]
        code, out = script.pop(0) if len(script) > 1 else script[0]
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    def count(self, sub: str) -> int:
        return sum(1 for a in self.calls if a[1] == sub)


def _fake_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_ambiguous_short_sha_reports_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit(
        {
            "fetch": (0, ""),
            "merge-base": (128, ""),
            "--is-shallow-repository": (0, "false\n"),
            "--disambiguate": (0, "abc1234aaaa\nabc1234bbbb\n"),
        }
    )
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    assert commit_reachability.check_commit_reachable(_fake_repo(tmp_path), "main@abc1234", "main").reachable == "unknown"


def test_unknown_shallowness_never_blames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit({"fetch": (0, ""), "merge-base": (1, ""), "--is-shallow-repository": (128, "")})
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    assert commit_reachability.check_commit_reachable(_fake_repo(tmp_path), "main@abc1234", "main").reachable == "unknown"


def test_failed_fetch_is_negatively_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix round 1 (#1): a dead origin costs one fetch timeout per window, not per close.
    fake = _FakeGit({"fetch": (128, "")})
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    clock = [1000.0]
    monkeypatch.setattr(commit_reachability.time, "monotonic", lambda: clock[0])
    repo = _fake_repo(tmp_path)

    for _ in range(3):
        assert commit_reachability.check_commit_reachable(repo, "main@abc1234", "main").reachable == "unknown"
    assert fake.count("fetch") == 1

    clock[0] += commit_reachability.FETCH_FAILURE_TTL_S + 1
    commit_reachability.check_commit_reachable(repo, "main@abc1234", "main")
    assert fake.count("fetch") == 2


def _seed_fetch_success(repo: Path) -> None:
    """Pretend a fetch of origin/main succeeded a moment ago (a cache hit)."""
    commit_reachability._fetch_cache[(str(repo.resolve()), "main")] = (time.monotonic(), True)


def test_cached_fetch_exit128_refetches_before_blaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix round 2 (#1): X was pushed from another clone moments ago; the cached
    # fetch predates it. Exit 128 must trigger one fresh fetch, after which X is
    # present and an ancestor -> True, never a persisted false "not reachable".
    fake = _FakeGit(
        {
            "fetch": (0, ""),
            "merge-base": [(128, ""), (0, "")],
            "--is-shallow-repository": (0, "false\n"),
            "--disambiguate": (0, ""),
        }
    )
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    repo = _fake_repo(tmp_path)
    _seed_fetch_success(repo)

    assert commit_reachability.check_commit_reachable(repo, "main@abc1234", "main").reachable is True
    assert fake.count("fetch") == 1


def test_cached_fetch_exit1_refetches_before_blaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit({"fetch": (0, ""), "merge-base": [(1, ""), (0, "")], "--is-shallow-repository": (0, "false\n")})
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    repo = _fake_repo(tmp_path)
    _seed_fetch_success(repo)

    assert commit_reachability.check_commit_reachable(repo, "main@abc1234", "main").reachable is True
    assert fake.count("fetch") == 1


def test_cached_fetch_still_unreachable_after_refetch_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit({"fetch": (0, ""), "merge-base": (1, ""), "--is-shallow-repository": (0, "false\n")})
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    repo = _fake_repo(tmp_path)
    _seed_fetch_success(repo)

    assert commit_reachability.check_commit_reachable(repo, "main@abc1234", "main").reachable is False
    assert fake.count("fetch") == 1  # exactly one fresh fetch, no loop


def test_cached_fetch_refetch_failure_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit(
        {
            "fetch": (128, ""),
            "merge-base": (128, ""),
            "--is-shallow-repository": (0, "false\n"),
            "--disambiguate": (0, ""),
        }
    )
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    repo = _fake_repo(tmp_path)
    _seed_fetch_success(repo)

    result = commit_reachability.check_commit_reachable(repo, "main@abc1234", "main")
    assert result.reachable == "unknown"
    assert result.warning is None


def test_fresh_fetch_exit1_does_not_refetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit({"fetch": (0, ""), "merge-base": (1, ""), "--is-shallow-repository": (0, "false\n")})
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)

    assert commit_reachability.check_commit_reachable(_fake_repo(tmp_path), "main@abc1234", "main").reachable is False
    assert fake.count("fetch") == 1


@pytest.mark.asyncio
async def test_mcp_close_with_commit_on_missing_issue_skips_git(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix round 2 (#2): a close that will 404 never waits on git.
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("reachability check must not run for a missing issue")

    monkeypatch.setattr(commit_reachability, "check_commit_reachable", _boom)
    data = _parse(await _handle_close_issue({"issue_id": "mcp-deadbeef00", "commit": "main@abc1234"}))
    assert data["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_mcp_close_with_commit_on_closed_issue_skips_git(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("already closed", priority=2)
    mcp_db.close_issue(issue.id)

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("reachability check must not run for an already-closed issue")

    monkeypatch.setattr(commit_reachability, "check_commit_reachable", _boom)
    data = _parse(await _handle_close_issue({"issue_id": issue.id, "commit": "main@abc1234"}))
    assert "error" in data


def test_git_env_is_hardened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix round 1 (#4): inherited GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE never
    # redirect the check; ssh can never prompt.
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    fake = _FakeGit({"fetch": (0, ""), "merge-base": (0, "")})
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)

    commit_reachability.check_commit_reachable(_fake_repo(tmp_path), "main@abc1234", "main")

    for env in fake.envs:
        assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"} & env.keys()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


def test_user_git_ssh_command_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /k")
    fake = _FakeGit({"fetch": (0, ""), "merge-base": (0, "")})
    monkeypatch.setattr(commit_reachability.subprocess, "run", fake)
    commit_reachability.check_commit_reachable(_fake_repo(tmp_path), "main@abc1234", "main")
    assert all(env["GIT_SSH_COMMAND"] == "ssh -i /k" for env in fake.envs)


def test_inherited_git_dir_does_not_redirect_real_check(mcp_db: FiligreeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_sha = _init_repo_with_origin(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repo"))
    assert commit_reachability.check_commit_reachable(tmp_path, f"main@{base_sha}", "main").reachable is True


@pytest.mark.asyncio
async def test_mcp_close_runs_reachability_off_the_event_loop(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fix round 1 (#1): a slow git must not stall the event loop.
    def _slow(*_a: object, **_k: object) -> commit_reachability.ReachabilityCheck:
        time.sleep(0.5)
        return commit_reachability.ReachabilityCheck(reachable=True, sha="abc1234", ref="origin/main")

    monkeypatch.setattr(commit_reachability, "check_commit_reachable", _slow)
    issue = mcp_db.create_issue("slow check", priority=2)
    order: list[str] = []

    async def _close() -> None:
        await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": "main@abc1234"})
        order.append("close")

    async def _tick() -> None:
        await asyncio.sleep(0.05)
        order.append("tick")

    await asyncio.gather(_close(), _tick())
    assert order == ["tick", "close"]
    assert mcp_db.get_issue(issue.id).status_category == "done"


@pytest.mark.asyncio
async def test_integration_ref_setting_is_honoured(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    _init_repo_with_origin(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "release")
    rel_sha = _commit_file(tmp_path, "r.txt", "r\n", "release work")
    _git(tmp_path, "push", "-q", "origin", "release")
    config = read_config(mcp_db.meta_dir)
    write_config(mcp_db.meta_dir, {**config, "integration_ref": "release"})
    issue = mcp_db.create_issue("close against a release branch", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": f"release@{rel_sha}"}))

    assert "error" not in data, data
    assert "warnings" not in data
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] is True


@pytest.mark.asyncio
async def test_issue_get_reachable_is_null_without_anchor(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("closed with no anchor", priority=2)
    await _handle_close_issue({"issue_id": issue.id, "reason": "done"})
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] is None


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ("main@abc1234", "abc1234"),
        ("codex/c16-lead-summaries@ecad149", "ecad149"),
        ("ABC1234DEF", "abc1234def"),
        ("main@abc123", None),  # 6 hex: too short
        ("main@--upload-pack=x", None),
        ("main@", None),
        ("", None),
    ],
)
def test_extract_sha(anchor: str, expected: str | None) -> None:
    assert commit_reachability.extract_sha(anchor) == expected


@pytest.mark.parametrize(
    ("ref", "ok"),
    [
        ("main", True),
        ("release/3.3.0", True),
        ("-main", False),
        ("--upload-pack=x", False),
        ("a..b", False),
        ("main.lock", False),
        ("main/", False),
        ("a@{1}", False),
        ("with space", False),
        ("", False),
    ],
)
def test_is_safe_ref(ref: str, ok: bool) -> None:
    assert commit_reachability.is_safe_ref(ref) is ok


def test_invalid_integration_ref_reports_unknown_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("git must not run for an unsafe ref")

    monkeypatch.setattr(commit_reachability.subprocess, "run", _boom)
    result = commit_reachability.check_commit_reachable(tmp_path, "main@abc1234", "--upload-pack=x")
    assert result.reachable == "unknown"
    assert result.warning is None
