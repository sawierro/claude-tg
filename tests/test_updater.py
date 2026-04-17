"""Tests for bot.updater — git fetch/pull wrappers.

We don't hit the real git repo; instead we monkeypatch the async _run_git
helper to return fixed stdout/stderr pairs.
"""
from __future__ import annotations

import pytest

from bot import updater


@pytest.fixture
def fake_git(monkeypatch):
    calls = []

    async def _fake(*args, timeout: int = 30):
        calls.append(args)
        key = " ".join(args)
        responses = fake_git.responses
        if key in responses:
            return responses[key]
        return (0, "", "")

    fake_git.responses = {}  # type: ignore[attr-defined]
    fake_git.calls = calls  # type: ignore[attr-defined]
    monkeypatch.setattr(updater, "_run_git", _fake)
    return fake_git


@pytest.mark.asyncio
async def test_is_git_repo_true(fake_git):
    fake_git.responses["rev-parse --git-dir"] = (0, ".git", "")
    assert await updater.is_git_repo() is True


@pytest.mark.asyncio
async def test_is_git_repo_false(fake_git):
    fake_git.responses["rev-parse --git-dir"] = (128, "", "not a repo")
    assert await updater.is_git_repo() is False


@pytest.mark.asyncio
async def test_current_branch(fake_git):
    fake_git.responses["rev-parse --abbrev-ref HEAD"] = (0, "main", "")
    assert await updater.current_branch() == "main"


@pytest.mark.asyncio
async def test_current_commit(fake_git):
    fake_git.responses["rev-parse --short HEAD"] = (0, "abc1234", "")
    assert await updater.current_commit() == "abc1234"


@pytest.mark.asyncio
async def test_is_working_tree_dirty_yes(fake_git):
    fake_git.responses["status --porcelain"] = (0, " M foo.py\n?? new.txt", "")
    assert await updater.is_working_tree_dirty() is True


@pytest.mark.asyncio
async def test_is_working_tree_dirty_no(fake_git):
    fake_git.responses["status --porcelain"] = (0, "", "")
    assert await updater.is_working_tree_dirty() is False


@pytest.mark.asyncio
async def test_pending_commits_parses_lines(fake_git):
    fake_git.responses["log HEAD..origin/main -20 --pretty=format:%h %s"] = (
        0,
        "abc1111 feat: thing\ndef2222 fix: bug\nghi3333 docs: update",
        "",
    )
    commits = await updater.pending_commits("main")
    assert len(commits) == 3
    assert commits[0].startswith("abc1111")
    assert commits[-1].startswith("ghi3333")


@pytest.mark.asyncio
async def test_pending_commits_empty_when_up_to_date(fake_git):
    fake_git.responses["log HEAD..origin/main -20 --pretty=format:%h %s"] = (0, "", "")
    assert await updater.pending_commits("main") == []


@pytest.mark.asyncio
async def test_fetch_reports_success(fake_git):
    fake_git.responses["fetch --quiet"] = (0, "", "")
    ok, err = await updater.fetch()
    assert ok is True
    assert err == ""


@pytest.mark.asyncio
async def test_fetch_reports_failure(fake_git):
    fake_git.responses["fetch --quiet"] = (128, "", "network error")
    ok, err = await updater.fetch()
    assert ok is False
    assert "network error" in err


@pytest.mark.asyncio
async def test_pull_success(fake_git):
    fake_git.responses["pull --ff-only origin main"] = (0, "Updating abc..def\nFast-forward", "")
    ok, out = await updater.pull("main")
    assert ok is True
    assert "Fast-forward" in out


@pytest.mark.asyncio
async def test_pull_failure(fake_git):
    fake_git.responses["pull --ff-only origin main"] = (1, "", "diverged")
    ok, out = await updater.pull("main")
    assert ok is False
    assert "diverged" in out
