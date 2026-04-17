import os

import pytest

from bot.providers._env import build_subprocess_env


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start from an empty environment for deterministic assertions."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)


def test_telegram_token_is_stripped(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_subprocess_env()
    assert "TELEGRAM_TOKEN" not in env
    assert env.get("PATH") == "/usr/bin"


def test_bot_token_aliases_stripped(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret2")
    env = build_subprocess_env()
    assert "BOT_TOKEN" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env


def test_arbitrary_secrets_dropped(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DATABASE_URL", "x")
    env = build_subprocess_env()
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "GITHUB_TOKEN" not in env
    assert "DATABASE_URL" not in env


def test_whitelisted_vars_pass_through(monkeypatch):
    monkeypatch.setenv("PATH", "/p")
    monkeypatch.setenv("HOME", "/h")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    env = build_subprocess_env()
    assert env["PATH"] == "/p"
    assert env["HOME"] == "/h"
    assert env["LANG"] == "en_US.UTF-8"


def test_anthropic_openai_prefixes_pass(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "b")
    monkeypatch.setenv("CLAUDE_CODE_HOME", "/c")
    env = build_subprocess_env()
    assert env["ANTHROPIC_API_KEY"] == "a"
    assert env["OPENAI_API_KEY"] == "b"
    assert env["CLAUDE_CODE_HOME"] == "/c"


def test_extra_overrides_defaults(monkeypatch):
    monkeypatch.setenv("PATH", "/original")
    env = build_subprocess_env({"PATH": "/custom"})
    assert env["PATH"] == "/custom"


def test_extra_cannot_reintroduce_blocked(monkeypatch):
    env = build_subprocess_env({"TELEGRAM_TOKEN": "sneaky"})
    assert "TELEGRAM_TOKEN" not in env
