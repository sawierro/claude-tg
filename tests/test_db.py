import aiosqlite
import pytest
import pytest_asyncio

from bot.db import (
    _current_version,
    create_session,
    delete_session,
    get_active_sessions,
    get_last_message_time,
    get_session,
    get_session_by_name,
    get_session_by_tg_message,
    get_token_usage,
    init_db,
    insert_message,
    tx,
    update_session_status,
    wal_checkpoint,
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


@pytest.mark.asyncio
async def test_schema_version_tracked(conn):
    """init_db must stamp schema_version table with at least one row."""
    v = await _current_version(conn)
    assert v >= 1


@pytest.mark.asyncio
async def test_init_db_idempotent(tmp_path):
    """Running init_db twice on the same DB must not duplicate migrations."""
    db_path = str(tmp_path / "mig.db")
    c1 = await init_db(db_path)
    v1 = await _current_version(c1)
    await c1.close()
    c2 = await init_db(db_path)
    v2 = await _current_version(c2)
    async with c2.execute("SELECT COUNT(*) FROM schema_version") as cur:
        count = (await cur.fetchone())[0]
    await c2.close()
    assert v1 == v2
    assert count == v2  # one row per migration, no duplicates


@pytest.mark.asyncio
async def test_tx_rolls_back_on_error(conn):
    """A transaction helper must roll back raw SQL writes on exception."""
    try:
        async with tx(conn):
            await conn.execute(
                "INSERT INTO sessions(id,name,work_dir,status) "
                "VALUES('rb','rollback','/tmp','waiting')"
            )
            raise ValueError("boom")
    except ValueError:
        pass
    assert await get_session(conn, "rb") is None


@pytest.mark.asyncio
async def test_tx_commits_on_success(conn):
    """A transaction helper must commit when the block succeeds."""
    async with tx(conn):
        await conn.execute(
            "INSERT INTO sessions(id,name,work_dir,status) "
            "VALUES('ok','commit-ok','/tmp','waiting')"
        )
    assert await get_session(conn, "ok") is not None


@pytest.mark.asyncio
async def test_wal_checkpoint_runs(conn):
    """wal_checkpoint must not raise on an open connection."""
    await wal_checkpoint(conn)  # just verify no exception


@pytest.mark.asyncio
async def test_tg_message_routing_scoped_by_chat(conn):
    """Two sessions sharing the same tg_message_id must be disambiguated by chat_id."""
    await create_session(conn, "sa", "a", "/tmp")
    await create_session(conn, "sb", "b", "/tmp")
    await update_session_status(conn, "sa", "waiting", last_tg_msg_id=100, last_tg_chat_id=111)
    await update_session_status(conn, "sb", "waiting", last_tg_msg_id=100, last_tg_chat_id=222)

    s_a = await get_session_by_tg_message(conn, 100, tg_chat_id=111)
    s_b = await get_session_by_tg_message(conn, 100, tg_chat_id=222)
    assert s_a and s_a["id"] == "sa"
    assert s_b and s_b["id"] == "sb"
