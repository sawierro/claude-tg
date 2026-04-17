from bot.claude_runner import ClaudeResponse
from bot.message_formatter import (
    escape_markdown_v2,
    format_error,
    format_notification,
    format_session_list,
    split_message,
)


def test_escape_special_chars():
    assert escape_markdown_v2("hello_world") == r"hello\_world"
    assert escape_markdown_v2("2+2=4") == r"2\+2\=4"
    assert escape_markdown_v2("test.py") == r"test\.py"


def test_escape_preserves_code_blocks():
    text = "hello `code_here` world"
    result = escape_markdown_v2(text)
    assert "`code_here`" in result
    assert r"hello" in result


def test_escape_preserves_triple_backticks():
    text = "before ```python\nfoo_bar = 1\n``` after"
    result = escape_markdown_v2(text)
    assert "foo_bar = 1" in result
    assert r"before" in result


def test_split_short_message():
    assert split_message("short", 100) == ["short"]


def test_split_long_message():
    text = "line\n" * 1000
    chunks = split_message(text, 200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 210  # slight overhead from closing code blocks


def test_split_respects_paragraphs():
    text = "para one\n\npara two\n\npara three"
    chunks = split_message(text, 20)
    assert len(chunks) >= 2


def test_format_notification_success():
    response = ClaudeResponse(
        session_id="sid",
        text="Done!",
        cost=0.005,
        duration_seconds=12.5,
        error=None,
    )
    result = format_notification("my-session", "/tmp/project", response, "waiting")
    assert "my\\-session" in result or "my-session" in result
    assert "12" in result  # duration


def test_format_notification_error():
    response = ClaudeResponse(
        session_id="sid",
        text="",
        cost=None,
        duration_seconds=5.0,
        error="Something failed",
    )
    result = format_notification("err-session", "/tmp", response, "error")
    assert "Ошибка" in result


def test_format_session_list_empty():
    result = format_session_list([])
    assert "Нет активных" in result


def test_format_session_list_with_sessions():
    sessions = [
        {"name": "session-1", "status": "waiting"},
        {"name": "session-2", "status": "running"},
    ]
    result = format_session_list(sessions)
    assert "session\\-1" in result or "session-1" in result
    assert "session\\-2" in result or "session-2" in result


def test_format_error():
    result = format_error("test error")
    assert "Ошибка" in result
    assert "test error" in result or "test\\ error" in result
