import json
import os
import pytest
from pathlib import Path
from bot.config import load_config, Config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove real .env influence from tests."""
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    # Prevent dotenv from loading the real .env file
    monkeypatch.setattr("bot.config.load_dotenv", lambda: None)


@pytest.fixture
def config_file(tmp_path):
    config_data = {
        "telegram_token": "test-token-123",
        "allowed_chat_ids": [111, 222],
        "default_work_dir": "/tmp/test",
        "claude_path": "claude",
        "claude_flags": [],
        "max_message_length": 4000,
        "session_timeout_hours": 24,
        "subprocess_timeout_minutes": 30,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    return str(path)


def test_load_config_from_file(config_file):
    config = load_config(config_file)
    assert config.telegram_token == "test-token-123"
    assert config.allowed_chat_ids == [111, 222]
    assert config.default_work_dir == "/tmp/test"


def test_env_overrides_config(config_file, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "env-token")
    config = load_config(config_file)
    assert config.telegram_token == "env-token"


def test_missing_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"telegram_token": ""}))
    with pytest.raises(ValueError, match="TELEGRAM_TOKEN"):
        load_config(str(path))


def test_missing_file_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "env-only-token")
    config = load_config(str(tmp_path / "nonexistent.json"))
    assert config.telegram_token == "env-only-token"
    assert config.allowed_chat_ids == []


def test_config_defaults():
    config = Config(telegram_token="t")
    assert config.max_message_length == 4000
    assert config.session_timeout_hours == 24
    assert config.subprocess_timeout_minutes == 0
    assert config.claude_flags == []


def test_config_save(tmp_path):
    config = Config(telegram_token="save-test", allowed_chat_ids=[999])
    path = str(tmp_path / "saved.json")
    config.save(path)

    with open(path) as f:
        data = json.load(f)
    assert "telegram_token" not in data  # token must not be saved to file
    assert data["allowed_chat_ids"] == [999]
