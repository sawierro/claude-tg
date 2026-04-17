"""Tests for the auto-resume background worker.

We fake a SessionManager and a telegram Application, then drive the worker's
private `_tick` to verify:
  - auto-mode pending prompts get resumed and the row is deleted
  - manual-mode pending prompts trigger a notification (row stays)
  - vanishing sessions clean up gracefully
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from bot import db
from bot import resume_worker as rw
from bot.providers.base import ProviderResponse


@pytest_asyncio.fixture
async def conn(tmp_path):
    connection = await db.init_db(str(tmp_path / "rw.db"))
    yield connection
    await connection.close()


@pytest.fixture
def fake_app():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))


@pytest.fixture
def fake_session_mgr(conn):
    mgr = SimpleNamespace(conn=conn, resume_session=AsyncMock())
    return mgr


@pytest.mark.asyncio
async def test_auto_resume_processes_due_and_deletes_row(
    conn, fake_app, fake_session_mgr
):
    await db.create_session(conn, "sid-1", "auto-test", "/tmp")
    # retry_at in the past — should be due immediately
    pid = await db.create_pending_prompt(
        conn, "sid-1", 101, "продолжи", "1970-01-01 00:00:00", "auto"
    )
    fake_session_mgr.resume_session.return_value = ProviderResponse(
        session_id="sid-1",
        text="continued",
        cost=None,
        duration_seconds=0.1,
        error=None,
    )

    await rw._tick(fake_app, fake_session_mgr)

    fake_session_mgr.resume_session.assert_awaited_once_with("sid-1", "продолжи")
    # Row deleted
    assert await db.get_pending_prompt(conn, pid) is None
    # Owner was notified (at least once — resume start + result)
    assert fake_app.bot.send_message.await_count >= 1


@pytest.mark.asyncio
async def test_auto_resume_handles_deleted_session(
    conn, fake_app, fake_session_mgr
):
    """If the session is deleted between queueing and retry_at, worker tidies up."""
    await db.create_session(conn, "gone-sid", "gone", "/tmp")
    pid = await db.create_pending_prompt(
        conn, "gone-sid", 42, "hi", "1970-01-01 00:00:00", "auto"
    )
    # The pending_prompts FK has ON DELETE CASCADE, so deleting the session
    # removes the pending row too. To actually exercise the "session gone but
    # pending survives" branch we bypass CASCADE by re-inserting manually.
    await db.delete_session(conn, "gone-sid")
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute(
        "INSERT INTO pending_prompts(id, session_id, chat_id, prompt, retry_at, mode) "
        "VALUES(?, 'gone-sid', 42, 'hi', '1970-01-01 00:00:00', 'auto')",
        (pid,),
    )
    await conn.commit()
    await conn.execute("PRAGMA foreign_keys=ON")

    await rw._tick(fake_app, fake_session_mgr)

    # Worker deletes the stale row and does not attempt a resume
    assert await db.get_pending_prompt(conn, pid) is None
    fake_session_mgr.resume_session.assert_not_called()


@pytest.mark.asyncio
async def test_manual_mode_notifies_without_resuming(
    conn, fake_app, fake_session_mgr
):
    await db.create_session(conn, "sid-m", "manual-test", "/tmp")
    pid = await db.create_pending_prompt(
        conn, "sid-m", 42, "что делать?", "1970-01-01 00:00:00", "manual"
    )

    await rw._tick(fake_app, fake_session_mgr)

    fake_session_mgr.resume_session.assert_not_called()
    fake_app.bot.send_message.assert_awaited()  # notification sent
    # Row is NOT deleted — user must click "Resume" button to act
    assert await db.get_pending_prompt(conn, pid) is not None


@pytest.mark.asyncio
async def test_auto_resume_swallows_exception_but_cleans_up(
    conn, fake_app, fake_session_mgr
):
    await db.create_session(conn, "sid-x", "boom-test", "/tmp")
    pid = await db.create_pending_prompt(
        conn, "sid-x", 5, "die", "1970-01-01 00:00:00", "auto"
    )
    fake_session_mgr.resume_session.side_effect = RuntimeError("simulated")

    await rw._tick(fake_app, fake_session_mgr)

    # Row deleted even on failure; next tick won't retry forever
    assert await db.get_pending_prompt(conn, pid) is None
    # User was still notified
    assert fake_app.bot.send_message.await_count >= 1
