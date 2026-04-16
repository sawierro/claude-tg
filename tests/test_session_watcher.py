import asyncio

import pytest

from bot import session_watcher
from bot.session_watcher import SessionWatcher


class _StubProvider:
    """Minimal provider stub for watcher tests."""

    def __init__(self, jsonl_path: str):
        self._path = jsonl_path

    def get_session_jsonl_path(self, sid):
        return self._path

    def extract_end_turn_text(self, line):
        return None  # only limit detection matters for these tests


@pytest.mark.asyncio
async def test_watcher_calls_limit_callback_once(tmp_path, monkeypatch):
    # Make the watcher poll very fast and debounce short so tests finish quickly
    monkeypatch.setattr(session_watcher, "POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(session_watcher, "LIMIT_DEBOUNCE_SECONDS", 300)

    jsonl = tmp_path / "s.jsonl"
    jsonl.write_text("")  # start empty

    calls = []

    async def on_limit(sid, line):
        calls.append((sid, line))

    async def on_response(sid, name, text):
        pass

    provider = _StubProvider(str(jsonl))
    watcher = SessionWatcher(
        "sid-1", "name-1", provider, on_response, on_limit_callback=on_limit
    )
    watcher.start()
    try:
        # Append a limit line
        with open(jsonl, "a", encoding="utf-8") as f:
            f.write('{"error": "Claude usage limit reached"}\n')
        # Wait for at least one poll cycle
        await asyncio.sleep(0.3)

        # Append a second limit line within debounce window — must NOT call again
        with open(jsonl, "a", encoding="utf-8") as f:
            f.write('{"error": "rate_limit_error hit again"}\n')
        await asyncio.sleep(0.3)
    finally:
        watcher.stop()
        if watcher._task:
            try:
                await watcher._task
            except (asyncio.CancelledError, Exception):
                pass

    assert len(calls) == 1
    assert calls[0][0] == "sid-1"


@pytest.mark.asyncio
async def test_watcher_ignores_non_limit_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(session_watcher, "POLL_INTERVAL_SECONDS", 0.05)

    jsonl = tmp_path / "s.jsonl"
    jsonl.write_text("")

    calls = []

    async def on_limit(sid, line):
        calls.append(line)

    async def on_response(sid, name, text):
        pass

    provider = _StubProvider(str(jsonl))
    watcher = SessionWatcher(
        "sid-2", "name-2", provider, on_response, on_limit_callback=on_limit
    )
    watcher.start()
    try:
        with open(jsonl, "a", encoding="utf-8") as f:
            f.write('{"text": "everything is fine"}\n')
            f.write('{"type": "assistant", "content": "hello"}\n')
        await asyncio.sleep(0.3)
    finally:
        watcher.stop()
        if watcher._task:
            try:
                await watcher._task
            except (asyncio.CancelledError, Exception):
                pass

    assert calls == []
