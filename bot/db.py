import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parent.parent / "claude_tg.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    work_dir        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('running','waiting','done','error')),
    provider        TEXT NOT NULL DEFAULT 'claude',
    wsl_distro      TEXT NOT NULL DEFAULT '',
    last_tg_msg_id  INTEGER,
    auto_continue   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content         TEXT NOT NULL,
    tg_message_id   INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_tg_msg ON sessions(last_tg_msg_id);

CREATE TABLE IF NOT EXISTS bot_users (
    chat_id     INTEGER PRIMARY KEY,
    username    TEXT DEFAULT '',
    full_name   TEXT DEFAULT '',
    role        TEXT NOT NULL CHECK(role IN ('pending','viewer','denied')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_viewers (
    chat_id     INTEGER NOT NULL,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    PRIMARY KEY (chat_id, session_id)
);

CREATE TABLE IF NOT EXISTS pending_prompts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    chat_id         INTEGER NOT NULL,
    prompt          TEXT NOT NULL,
    retry_at        TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK(mode IN ('auto','manual')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pending_retry ON pending_prompts(retry_at);
"""


async def init_db(db_path: str = DB_PATH) -> aiosqlite.Connection:
    """Initialize database, create tables, enable WAL mode."""
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()

    # Migration: add provider column if missing (upgrade from v1)
    try:
        await conn.execute("SELECT provider FROM sessions LIMIT 1")
    except Exception:
        await conn.execute("ALTER TABLE sessions ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude'")
        await conn.commit()
        logger.info("Migrated: added provider column to sessions")

    # Migration: add wsl_distro column if missing
    try:
        await conn.execute("SELECT wsl_distro FROM sessions LIMIT 1")
    except Exception:
        await conn.execute("ALTER TABLE sessions ADD COLUMN wsl_distro TEXT NOT NULL DEFAULT ''")
        await conn.commit()
        logger.info("Migrated: added wsl_distro column to sessions")

    # Migration: add tokens_in/tokens_out columns if missing
    try:
        await conn.execute("SELECT tokens_in FROM messages LIMIT 1")
    except Exception:
        await conn.execute("ALTER TABLE messages ADD COLUMN tokens_in INTEGER")
        await conn.execute("ALTER TABLE messages ADD COLUMN tokens_out INTEGER")
        await conn.commit()
        logger.info("Migrated: added tokens_in/tokens_out columns to messages")

    # Migration: add auto_continue column if missing
    try:
        await conn.execute("SELECT auto_continue FROM sessions LIMIT 1")
    except Exception:
        await conn.execute("ALTER TABLE sessions ADD COLUMN auto_continue INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
        logger.info("Migrated: added auto_continue column to sessions")

    logger.info("Database initialized at %s", db_path)
    return conn


async def create_session(
    conn: aiosqlite.Connection,
    session_id: str,
    name: str,
    work_dir: str,
    provider: str = "claude",
    wsl_distro: str = "",
) -> None:
    """Insert a new session."""
    await conn.execute(
        "INSERT INTO sessions (id, name, work_dir, status, provider, wsl_distro) VALUES (?, ?, ?, 'running', ?, ?)",
        (session_id, name, work_dir, provider, wsl_distro),
    )
    await conn.commit()


async def update_session_status(
    conn: aiosqlite.Connection,
    session_id: str,
    status: str,
    last_tg_msg_id: int | None = None,
) -> None:
    """Update session status and optionally the last telegram message id."""
    if last_tg_msg_id is not None:
        await conn.execute(
            "UPDATE sessions SET status=?, last_tg_msg_id=?, updated_at=datetime('now') WHERE id=?",
            (status, last_tg_msg_id, session_id),
        )
    else:
        await conn.execute(
            "UPDATE sessions SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, session_id),
        )
    await conn.commit()


async def get_session(conn: aiosqlite.Connection, session_id: str) -> dict | None:
    """Get session by Claude session ID."""
    async with conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_session_by_name(conn: aiosqlite.Connection, name: str) -> dict | None:
    """Get session by user-friendly name."""
    async with conn.execute("SELECT * FROM sessions WHERE name=?", (name,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_session_by_tg_message(
    conn: aiosqlite.Connection, tg_message_id: int
) -> dict | None:
    """Get session by the last Telegram message ID (for reply routing)."""
    async with conn.execute(
        "SELECT * FROM sessions WHERE last_tg_msg_id=?", (tg_message_id,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_active_sessions(conn: aiosqlite.Connection) -> list[dict]:
    """Get all sessions with status running or waiting."""
    async with conn.execute(
        "SELECT * FROM sessions WHERE status IN ('running', 'waiting') ORDER BY updated_at DESC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_all_sessions(conn: aiosqlite.Connection) -> list[dict]:
    """Get all sessions ordered by last update."""
    async with conn.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def delete_session(conn: aiosqlite.Connection, session_id: str) -> None:
    """Delete a session and its messages (CASCADE)."""
    await conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    await conn.commit()


async def insert_message(
    conn: aiosqlite.Connection,
    session_id: str,
    role: str,
    content: str,
    tg_message_id: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    """Insert a message into the history."""
    await conn.execute(
        "INSERT INTO messages (session_id, role, content, tg_message_id, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, role, content, tg_message_id, tokens_in, tokens_out),
    )
    await conn.commit()


async def get_token_usage(
    conn: aiosqlite.Connection,
    session_id: str | None = None,
    since: str | None = None,
) -> tuple[int, int, int]:
    """Return (sum_in, sum_out, message_count) for usage aggregation.

    If session_id is None, aggregates across all sessions.
    If since is None, aggregates all-time. Otherwise since is SQLite datetime string
    (e.g. '-5 hours', '-24 hours').
    """
    where = ["role='assistant'"]
    params: list = []
    if session_id is not None:
        where.append("session_id=?")
        params.append(session_id)
    if since is not None:
        where.append("created_at >= datetime('now', ?)")
        params.append(since)

    sql = (
        "SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), COUNT(*) "
        "FROM messages WHERE " + " AND ".join(where)
    )
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
        return (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)


async def get_session_messages(conn: aiosqlite.Connection, session_id: str) -> list[dict]:
    """Return all messages for a session ordered by created_at."""
    async with conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_last_message_time(
    conn: aiosqlite.Connection, session_id: str
) -> str | None:
    """Return ISO timestamp of the most recent message for a session, or None."""
    async with conn.execute(
        "SELECT MAX(created_at) FROM messages WHERE session_id=?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def reset_running_sessions(conn: aiosqlite.Connection) -> int:
    """Reset all 'running' sessions to 'waiting' on bot startup."""
    cursor = await conn.execute(
        "UPDATE sessions SET status='waiting', updated_at=datetime('now') WHERE status='running'"
    )
    await conn.commit()
    return cursor.rowcount


async def cleanup_stale_sessions(
    conn: aiosqlite.Connection, timeout_hours: int
) -> int:
    """Mark stale running/waiting sessions as error. Returns count of cleaned sessions."""
    cursor = await conn.execute(
        """UPDATE sessions SET status='error', updated_at=datetime('now')
           WHERE status IN ('running', 'waiting')
           AND updated_at < datetime('now', ? || ' hours')""",
        (f"-{timeout_hours}",),
    )
    await conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# bot_users — access requests & viewer management
# ---------------------------------------------------------------------------

async def get_bot_user(conn: aiosqlite.Connection, chat_id: int) -> dict | None:
    """Get a bot user by chat_id."""
    async with conn.execute("SELECT * FROM bot_users WHERE chat_id=?", (chat_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_bot_user(
    conn: aiosqlite.Connection,
    chat_id: int,
    username: str,
    full_name: str,
    role: str = "pending",
) -> None:
    """Create a new bot user (access request)."""
    await conn.execute(
        "INSERT OR IGNORE INTO bot_users (chat_id, username, full_name, role) VALUES (?, ?, ?, ?)",
        (chat_id, username, full_name, role),
    )
    await conn.commit()


async def update_bot_user_role(
    conn: aiosqlite.Connection, chat_id: int, role: str
) -> bool:
    """Update user role. Returns True if user existed."""
    cursor = await conn.execute(
        "UPDATE bot_users SET role=? WHERE chat_id=?", (role, chat_id)
    )
    await conn.commit()
    return cursor.rowcount > 0


async def get_pending_users(conn: aiosqlite.Connection) -> list[dict]:
    """Get all users with pending access requests."""
    async with conn.execute(
        "SELECT * FROM bot_users WHERE role='pending' ORDER BY created_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_viewers(conn: aiosqlite.Connection) -> list[dict]:
    """Get all approved viewers."""
    async with conn.execute(
        "SELECT * FROM bot_users WHERE role='viewer' ORDER BY created_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# session_viewers — per-session read-only access
# ---------------------------------------------------------------------------

async def add_session_viewer(
    conn: aiosqlite.Connection, chat_id: int, session_id: str
) -> None:
    """Grant read-only watcher access to a session."""
    await conn.execute(
        "INSERT OR IGNORE INTO session_viewers (chat_id, session_id) VALUES (?, ?)",
        (chat_id, session_id),
    )
    await conn.commit()


async def remove_session_viewer(
    conn: aiosqlite.Connection, chat_id: int, session_id: str
) -> None:
    """Revoke watcher access to a session."""
    await conn.execute(
        "DELETE FROM session_viewers WHERE chat_id=? AND session_id=?",
        (chat_id, session_id),
    )
    await conn.commit()


async def get_session_viewer_ids(
    conn: aiosqlite.Connection, session_id: str
) -> list[int]:
    """Get chat_ids of all viewers for a session."""
    async with conn.execute(
        "SELECT chat_id FROM session_viewers WHERE session_id=?", (session_id,)
    ) as cur:
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def get_viewer_session_ids(
    conn: aiosqlite.Connection, chat_id: int
) -> list[str]:
    """Get session_ids a viewer has access to."""
    async with conn.execute(
        "SELECT session_id FROM session_viewers WHERE chat_id=?", (chat_id,)
    ) as cur:
        rows = await cur.fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# pending_prompts — queue for auto-resume after limit reset
# ---------------------------------------------------------------------------

async def create_pending_prompt(
    conn: aiosqlite.Connection,
    session_id: str,
    chat_id: int,
    prompt: str,
    retry_at: str,
    mode: str,
) -> int:
    """Insert a pending prompt. retry_at is ISO datetime. Returns row id."""
    cursor = await conn.execute(
        "INSERT INTO pending_prompts (session_id, chat_id, prompt, retry_at, mode) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, chat_id, prompt, retry_at, mode),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_pending_prompt(
    conn: aiosqlite.Connection, pending_id: int
) -> dict | None:
    """Get a pending prompt by id."""
    async with conn.execute(
        "SELECT * FROM pending_prompts WHERE id=?", (pending_id,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_due_pending_prompts(conn: aiosqlite.Connection) -> list[dict]:
    """Return pending prompts whose retry_at has passed."""
    async with conn.execute(
        "SELECT * FROM pending_prompts WHERE retry_at <= datetime('now') "
        "ORDER BY retry_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def delete_pending_prompt(
    conn: aiosqlite.Connection, pending_id: int
) -> None:
    """Delete a pending prompt entry."""
    await conn.execute("DELETE FROM pending_prompts WHERE id=?", (pending_id,))
    await conn.commit()


async def list_pending_prompts(conn: aiosqlite.Connection) -> list[dict]:
    """List all pending prompts ordered by retry_at."""
    async with conn.execute(
        "SELECT * FROM pending_prompts ORDER BY retry_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_pending_by_session(
    conn: aiosqlite.Connection, session_id: str
) -> dict | None:
    """Return the most recent pending prompt for a session, or None."""
    async with conn.execute(
        "SELECT * FROM pending_prompts WHERE session_id=? "
        "ORDER BY retry_at DESC LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_auto_continue(
    conn: aiosqlite.Connection, session_id: str, enabled: bool
) -> bool:
    """Toggle auto_continue for a session. Returns True if session existed."""
    cursor = await conn.execute(
        "UPDATE sessions SET auto_continue=? WHERE id=?",
        (1 if enabled else 0, session_id),
    )
    await conn.commit()
    return cursor.rowcount > 0
