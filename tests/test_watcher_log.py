"""Tests for the watcher rolling-message log kept on SessionManager."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.session_manager import RECENT_LOG_SIZE, RECENT_LOG_TRUNCATE, SessionManager


def _make_manager() -> SessionManager:
    cfg = MagicMock()
    conn = MagicMock()
    return SessionManager(cfg, conn)


def test_records_up_to_n_messages_per_session():
    mgr = _make_manager()
    for i in range(RECENT_LOG_SIZE + 2):
        mgr.record_watcher_message("sess-1", "alpha", f"msg {i}")

    entries = mgr.get_recent_messages("sess-1")
    assert len(entries) == RECENT_LOG_SIZE
    texts = [text for _, _, text in entries]
    # Oldest two get evicted; only the most recent RECENT_LOG_SIZE remain.
    expected = [f"msg {i}" for i in range(2, RECENT_LOG_SIZE + 2)]
    assert texts == expected


def test_truncates_long_messages():
    mgr = _make_manager()
    long_text = "x" * (RECENT_LOG_TRUNCATE + 500)
    mgr.record_watcher_message("sess-1", "alpha", long_text)

    entries = mgr.get_recent_messages("sess-1")
    assert len(entries) == 1
    _, _, stored = entries[0]
    assert len(stored) == RECENT_LOG_TRUNCATE + 1  # truncated + "…"
    assert stored.endswith("…")


def test_separate_buffers_per_session():
    mgr = _make_manager()
    mgr.record_watcher_message("sess-a", "alpha", "from a")
    mgr.record_watcher_message("sess-b", "beta", "from b1")
    mgr.record_watcher_message("sess-b", "beta", "from b2")

    all_logs = mgr.all_recent_messages()
    assert set(all_logs) == {"sess-a", "sess-b"}
    assert [t for _, _, t in all_logs["sess-a"]] == ["from a"]
    assert [t for _, _, t in all_logs["sess-b"]] == ["from b1", "from b2"]


def test_get_recent_messages_unknown_session_returns_empty():
    mgr = _make_manager()
    assert mgr.get_recent_messages("nope") == []
    assert mgr.all_recent_messages() == {}
