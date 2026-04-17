"""Path-traversal and sensitive-file coverage for bot.telegram_handler._resolve_work_path.

These tests guard the boundary between Telegram `/get` / upload commands and
the filesystem. They exercise symlinks, `..`, the extended sensitive blocklist,
suffix filter, and sensitive-directory filter.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from bot.telegram_handler import _resolve_work_path


def _session(work_dir: str) -> dict:
    return {"work_dir": work_dir, "wsl_distro": ""}


def test_resolves_file_inside_work_dir(tmp_path):
    (tmp_path / "README.md").write_text("ok")
    resolved = _resolve_work_path(_session(str(tmp_path)), "README.md")
    assert resolved == Path(os.path.realpath(tmp_path / "README.md"))


def test_blocks_dotdot_escape(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope")
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    with pytest.raises(ValueError, match="Выход"):
        _resolve_work_path(_session(str(work_dir)), "../secret.txt")


def test_blocks_absolute_path_outside(tmp_path):
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    with pytest.raises(ValueError, match="Выход"):
        _resolve_work_path(_session(str(work_dir)), "/etc/passwd")


def test_blocks_sensitive_filename(tmp_path):
    (tmp_path / ".env").write_text("KEY=1")
    with pytest.raises(ValueError, match="запрещ"):
        _resolve_work_path(_session(str(tmp_path)), ".env")


def test_blocks_sensitive_suffix(tmp_path):
    (tmp_path / "server.pem").write_text("cert")
    with pytest.raises(ValueError, match="запрещ"):
        _resolve_work_path(_session(str(tmp_path)), "server.pem")


def test_blocks_ssh_directory(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("x")
    with pytest.raises(ValueError, match="\\.ssh"):
        _resolve_work_path(_session(str(tmp_path)), ".ssh/authorized_keys")


def test_blocks_git_directory(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("x")
    with pytest.raises(ValueError, match="\\.git"):
        _resolve_work_path(_session(str(tmp_path)), ".git/config")


def test_blocks_aws_credentials_dir(tmp_path):
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("x")
    with pytest.raises(ValueError):
        _resolve_work_path(_session(str(tmp_path)), ".aws/credentials")


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Symlink creation on Windows requires admin or developer mode",
)
def test_symlink_pointing_outside_is_blocked(tmp_path):
    """A symlink inside work_dir pointing outside must fail the boundary check."""
    target = tmp_path / "outside.txt"
    target.write_text("leak")
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    (work_dir / "link").symlink_to(target)
    with pytest.raises(ValueError, match="Выход"):
        _resolve_work_path(_session(str(work_dir)), "link")


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Symlink creation on Windows requires admin or developer mode",
)
def test_symlink_to_sensitive_blocked_by_name(tmp_path):
    """Symlink resolving to a sensitive-suffix file inside work_dir is also blocked."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    real = work_dir / "key.pem"
    real.write_text("x")
    (work_dir / "safe_link").symlink_to(real)
    with pytest.raises(ValueError, match="\\.pem"):
        _resolve_work_path(_session(str(work_dir)), "safe_link")
