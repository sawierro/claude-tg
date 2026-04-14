import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from bot.claude_runner import run_claude, _parse_response, _build_command, ClaudeResponse
from bot.config import Config


@pytest.fixture
def config():
    return Config(
        telegram_token="test",
        claude_path="claude",
        claude_flags=[],
        subprocess_timeout_minutes=1,
    )


def test_parse_valid_json():
    raw = json.dumps({
        "session_id": "abc-123",
        "result": "Hello world",
        "total_cost_usd": 0.003,
        "is_error": False,
    })
    resp = _parse_response(raw, None, 5.0)
    assert resp.session_id == "abc-123"
    assert resp.text == "Hello world"
    assert resp.cost == 0.003
    assert resp.error is None


def test_parse_error_response():
    raw = json.dumps({
        "session_id": "abc-123",
        "result": "Something went wrong",
        "is_error": True,
    })
    resp = _parse_response(raw, None, 3.0)
    assert resp.error == "Something went wrong"
    assert resp.text == ""


def test_parse_invalid_json():
    resp = _parse_response("not json at all", "fallback-id", 2.0)
    assert resp.session_id == "fallback-id"
    assert resp.text == "not json at all"
    assert resp.error is None


def test_parse_missing_fields():
    raw = json.dumps({"session_id": "sid"})
    resp = _parse_response(raw, None, 1.0)
    assert resp.session_id == "sid"
    assert resp.text == ""
    assert resp.cost is None


def test_build_command_new_session(config):
    cmd = _build_command("hello world", config)
    assert "claude" in cmd
    assert "-p" in cmd
    assert "--output-format" in cmd
    assert "--output-format" in cmd
    assert "--resume" not in cmd


def test_build_command_resume(config):
    cmd = _build_command("continue", config, session_id="old-sid")
    assert "--resume" in cmd
    assert "old-sid" in cmd
    assert "-p" in cmd


@pytest.mark.asyncio
async def test_run_claude_success(config):
    mock_process = AsyncMock()
    mock_process.communicate = AsyncMock(return_value=(
        json.dumps({"session_id": "new-sid", "result": "ok", "total_cost_usd": 0.01}).encode(),
        b"",
    ))
    mock_process.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_process):
        resp = await run_claude("hello", ".", config)
        assert resp.session_id == "new-sid"
        assert resp.text == "ok"
        assert resp.error is None


@pytest.mark.asyncio
async def test_run_claude_nonzero_exit(config):
    mock_process = AsyncMock()
    mock_process.communicate = AsyncMock(return_value=(b"", b"some error"))
    mock_process.returncode = 1

    with patch("asyncio.create_subprocess_shell", return_value=mock_process):
        resp = await run_claude("hello", ".", config)
        assert resp.error is not None
