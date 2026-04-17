import platform
import sys

import pytest

from bot.providers.claude import _resolve_npm_shim


NPM_SHIM_TEMPLATE = """@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\\node.exe" (
  SET "_prog=%dp0%\\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\{pkg}\\cli.js" %*
"""


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only shim parsing")
def test_resolves_npm_claude_shim(tmp_path, monkeypatch):
    cmd = tmp_path / "claude.cmd"
    cmd.write_text(NPM_SHIM_TEMPLATE.format(pkg="@anthropic-ai\\claude-code"), encoding="utf-8")

    # Avoid hitting %PATH% — pass absolute path
    result = _resolve_npm_shim(str(cmd))
    assert result is not None
    assert len(result) == 2
    node_exe, js_path = result
    # Node: either resolved local node.exe or falls back to "node"
    assert node_exe in ("node",) or node_exe.lower().endswith("node.exe")
    assert js_path.endswith("cli.js")
    assert "claude-code" in js_path


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only shim parsing")
def test_resolves_codex_shim(tmp_path):
    cmd = tmp_path / "codex.cmd"
    cmd.write_text(NPM_SHIM_TEMPLATE.format(pkg="@openai\\codex"), encoding="utf-8")

    result = _resolve_npm_shim(str(cmd))
    assert result is not None
    _, js_path = result
    assert js_path.endswith("cli.js")
    assert "codex" in js_path


def test_non_cmd_returns_none(tmp_path):
    # Plain executable / non-existent path should return None
    result = _resolve_npm_shim(str(tmp_path / "does-not-exist"))
    assert result is None


def test_non_shim_cmd_returns_none(tmp_path):
    cmd = tmp_path / "other.cmd"
    cmd.write_text("@echo off\nwhoami\n", encoding="utf-8")
    result = _resolve_npm_shim(str(cmd))
    assert result is None
