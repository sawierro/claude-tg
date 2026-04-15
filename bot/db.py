import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = "claude_tg.db"

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
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content         TEXT NOT NULL,
    tg_message_id   INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""


async def init_db(db_path: str = DB_PATH) -> aiosqlite.Connection:
    """Initialize database, create tables, enable WAL mode."""
    conn = await aiosqlite.connect(db_path)
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
    conn.row_factory = aiosqlite.Row
    async with conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_session_by_name(conn: aiosqlite.Connection, name: str) -> dict | None:
    """Get session by user-friendly name."""
    conn.row_factory = aiosqlite.Row
    async with conn.execute("SELECT * FROM sessions WHERE name=?", (name,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_session_by_tg_message(
    conn: aiosqlite.Connection, tg_message_id: int
) -> dict | None:
    """Get session by the last Telegram message ID (for reply routing)."""
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM sessions WHERE last_tg_msg_id=?", (tg_message_id,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_active_sessions(conn: aiosqlite.Connection) -> list[dict]:
    """Get all sessions with status running or waiting."""
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM sessions WHERE status IN ('running', 'waiting') ORDER BY updated_at DESC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_all_sessions(conn: aiosqlite.Connection) -> list[dict]:
    """Get all sessions ordered by last update."""
    conn.row_factory = aiosqlite.Row
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
) -> None:
    """Insert a message into the history."""
    await conn.execute(
        "INSERT INTO messages (session_id, role, content, tg_message_id) VALUES (?, ?, ?, ?)",
        (session_id, role, content, tg_message_id),
    )
    await conn.commit()


async def get_session_messages(conn: aiosqlite.Connection, session_id: str) -> list[dict]:
    """Return all messages for a session ordered by created_at."""
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


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
