import json

import pytest

from bot.config import Config, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove real env influence from tests."""
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    # Prevent dotenv from loading the real .env file
    monkeypatch.setattr("bot.config.load_dotenv", lambda: None)


@pytest.fixture
def config_file(tmp_path):
    config_data = {
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


def test_load_config_from_file(config_file, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "env-token")
    config = load_config(config_file)
    assert config.telegram_token == "env-token"
    assert config.allowed_chat_ids == [111, 222]
    assert config.default_work_dir == "/tmp/test"


def test_owner_chat_id_env_overrides(config_file, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "env-token")
    monkeypatch.setenv("OWNER_CHAT_ID", "555,666")
    config = load_config(config_file)
    assert config.allowed_chat_ids == [555, 666]


def test_missing_token_raises(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"allowed_chat_ids": [1]}))
    with pytest.raises(ValueError, match="TELEGRAM_TOKEN"):
        load_config(str(path))


def test_token_in_json_is_ignored(tmp_path, monkeypatch, caplog):
    """A token leaked into config.json must be ignored with a warning, never trusted."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"telegram_token": "LEAKED", "allowed_chat_ids": [1]}))
    monkeypatch.setenv("TELEGRAM_TOKEN", "correct-env-token")
    config = load_config(str(path))
    assert config.telegram_token == "correct-env-token"
    assert "IGNORING" in caplog.text


def test_missing_owner_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    with pytest.raises(ValueError, match="OWNER_CHAT_ID"):
        load_config(str(tmp_path / "nonexistent.json"))


def test_config_defaults():
    config = Config(telegram_token="t", allowed_chat_ids=[1])
    assert config.max_message_length == 4000
    assert config.session_timeout_hours == 24
    # Security criteria 2.1: default timeout must be non-zero
    assert config.subprocess_timeout_minutes == 30
    assert config.claude_flags == []
    assert config.rate_limit_per_minute > 0
    assert config.concurrent_sessions > 0


def test_config_save_excludes_token(tmp_path):
    config = Config(telegram_token="save-test", allowed_chat_ids=[999])
    path = str(tmp_path / "saved.json")
    config.save(path)

    with open(path) as f:
        data = json.load(f)
    assert "telegram_token" not in data  # token must not be saved to file
    assert data["allowed_chat_ids"] == [999]


def test_validate_rejects_negative_timeout():
    cfg = Config(telegram_token="t", allowed_chat_ids=[1], subprocess_timeout_minutes=-1)
    with pytest.raises(ValueError):
        cfg.validate()


def test_validate_rejects_oversized_message_length():
    cfg = Config(telegram_token="t", allowed_chat_ids=[1], max_message_length=9000)
    with pytest.raises(ValueError):
        cfg.validate()


def test_invalid_owner_id_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("OWNER_CHAT_ID", "abc,def")
    with pytest.raises(ValueError, match="Invalid chat id"):
        load_config(str(tmp_path / "nope.json"))
