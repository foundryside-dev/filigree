"""MCP verb plumbing for the commit anchor (warpline seam, contract B).

``close_issue``, ``claim_issue``, and ``start_work`` accept an optional
``commit`` input that threads through to the DB layer and persists as
``close_commit`` / ``claim_commit``.
"""

from __future__ import annotations

import os
import subprocess
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
async def test_close_with_sha_unknown_to_git_reports_unknown(mcp_db: FiligreeDB, tmp_path: Path) -> None:
    # merge-base exits 128 for an object git does not have: git could not
    # answer, so the result is unknown, not "unreachable".
    _init_repo_with_origin(tmp_path)
    issue = mcp_db.create_issue("close on a foreign sha", priority=2)

    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": "main@0123456789abcdef"}))

    assert "error" not in data, data
    assert "warnings" not in data
    got = _parse(await _handle_get_issue({"issue_id": issue.id}))
    assert got["close_commit_reachable"] == "unknown"


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
