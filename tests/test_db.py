import pytest
import pytest_asyncio
import aiosqlite
from bot.db import (
    init_db,
    create_session,
    update_session_status,
    get_session,
    get_session_by_name,
    get_session_by_tg_message,
    get_active_sessions,
    delete_session,
    insert_message,
    cleanup_stale_sessions,
    get_token_usage,
    get_last_message_time,
)


@pytest_asyncio.fixture
async def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    connection = await init_db(db_path)
    yield connection
    await connection.close()


@pytest.mark.asyncio
async def test_create_and_get_session(conn):
    await create_session(conn, "sid-1", "test-session", "/tmp/project")
    session = await get_session(conn, "sid-1")
    assert session is not None
    assert session["name"] == "test-session"
    assert session["status"] == "running"
    assert session["work_dir"] == "/tmp/project"


@pytest.mark.asyncio
async def test_get_session_by_name(conn):
    await create_session(conn, "sid-2", "named-session", "/tmp")
    session = await get_session_by_name(conn, "named-session")
    assert session is not None
    assert session["id"] == "sid-2"


@pytest.mark.asyncio
async def test_update_status(conn):
    await create_session(conn, "sid-3", "status-test", "/tmp")
    await update_session_status(conn, "sid-3", "waiting", last_tg_msg_id=42)
    session = await get_session(conn, "sid-3")
    assert session["status"] == "waiting"
    assert session["last_tg_msg_id"] == 42


@pytest.mark.asyncio
async def test_get_by_tg_message(conn):
    await create_session(conn, "sid-4", "tg-test", "/tmp")
    await update_session_status(conn, "sid-4", "waiting", last_tg_msg_id=100)
    session = await get_session_by_tg_message(conn, 100)
    assert session is not None
    assert session["id"] == "sid-4"


@pytest.mark.asyncio
async def test_active_sessions(conn):
    await create_session(conn, "sid-5", "active-1", "/tmp")
    await create_session(conn, "sid-6", "active-2", "/tmp")
    await update_session_status(conn, "sid-6", "done")
    active = await get_active_sessions(conn)
    assert len(active) == 1
    assert active[0]["name"] == "active-1"


@pytest.mark.asyncio
async def test_delete_session(conn):
    await create_session(conn, "sid-7", "delete-me", "/tmp")
    await insert_message(conn, "sid-7", "user", "hello")
    await delete_session(conn, "sid-7")
    assert await get_session(conn, "sid-7") is None


@pytest.mark.asyncio
async def test_insert_message(conn):
    await create_session(conn, "sid-8", "msg-test", "/tmp")
    await insert_message(conn, "sid-8", "user", "test prompt", tg_message_id=55)
    await insert_message(conn, "sid-8", "assistant", "test response")

    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM messages WHERE session_id='sid-8' ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 2
    assert dict(rows[0])["role"] == "user"
    assert dict(rows[1])["role"] == "assistant"


@pytest.mark.asyncio
async def test_unique_session_name(conn):
    await create_session(conn, "sid-9", "unique-name", "/tmp")
    with pytest.raises(Exception):
        await create_session(conn, "sid-10", "unique-name", "/tmp")


@pytest.mark.asyncio
async def test_token_usage_aggregation(conn):
    await create_session(conn, "sid-t1", "tok-a", "/tmp")
    await create_session(conn, "sid-t2", "tok-b", "/tmp")
    await insert_message(conn, "sid-t1", "user", "q1")
    await insert_message(conn, "sid-t1", "assistant", "r1", tokens_in=100, tokens_out=50)
    await insert_message(conn, "sid-t1", "assistant", "r2", tokens_in=200, tokens_out=75)
    await insert_message(conn, "sid-t2", "assistant", "r3", tokens_in=10, tokens_out=5)

    # Per-session (all-time)
    t_in, t_out, n = await get_token_usage(conn, "sid-t1", None)
    assert t_in == 300
    assert t_out == 125
    assert n == 2

    # Across all sessions
    t_in, t_out, n = await get_token_usage(conn, None, None)
    assert t_in == 310
    assert t_out == 130
    assert n == 3


@pytest.mark.asyncio
async def test_get_last_message_time(conn):
    await create_session(conn, "sid-lm", "last-msg", "/tmp")
    assert await get_last_message_time(conn, "sid-lm") is None
    await insert_message(conn, "sid-lm", "user", "hi")
    ts = await get_last_message_time(conn, "sid-lm")
    assert ts is not None


@pytest.mark.asyncio
async def test_auto_continue_default_and_toggle(conn):
    from bot.db import set_auto_continue
    await create_session(conn, "sid-ac", "ac-test", "/tmp")

    s = await get_session(conn, "sid-ac")
    assert s["auto_continue"] == 0

    ok = await set_auto_continue(conn, "sid-ac", True)
    assert ok
    s = await get_session(conn, "sid-ac")
    assert s["auto_continue"] == 1

    ok = await set_auto_continue(conn, "sid-ac", False)
    assert ok
    s = await get_session(conn, "sid-ac")
    assert s["auto_continue"] == 0

    # Non-existent session returns False
    ok = await set_auto_continue(conn, "no-such-sid", True)
    assert not ok


@pytest.mark.asyncio
async def test_get_pending_by_session(conn):
    from bot.db import create_pending_prompt, get_pending_by_session
    await create_session(conn, "sid-pb", "pending-by", "/tmp")

    assert await get_pending_by_session(conn, "sid-pb") is None

    pid = await create_pending_prompt(conn, "sid-pb", 42, "hi", "2026-04-16 12:00:00", "auto")
    row = await get_pending_by_session(conn, "sid-pb")
    assert row is not None
    assert row["id"] == pid
    assert row["mode"] == "auto"
