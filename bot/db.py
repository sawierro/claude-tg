import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = str(Path(__file__).resolve().parent.parent / "claude_tg.db")

# ---------------------------------------------------------------------------
# Migrations — versioned, idempotent, tracked in schema_version table.
# Add a new tuple to MIGRATIONS when changing the schema; NEVER edit an
# applied migration in place.
# ---------------------------------------------------------------------------

MIGRATION_1_INITIAL = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    work_dir        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('running','waiting','done','error')),
    provider        TEXT NOT NULL DEFAULT 'claude',
    wsl_distro      TEXT NOT NULL DEFAULT '',
    last_tg_msg_id  INTEGER,
    last_tg_chat_id INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_sessions_tg_route ON sessions(last_tg_chat_id, last_tg_msg_id);

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

# Columns added after initial release — kept as separate migrations so existing
# databases upgrade cleanly. Each one is idempotent via a schema_version check.
MIGRATION_2_LEGACY_COLUMNS = [
    ("sessions", "provider", "TEXT NOT NULL DEFAULT 'claude'"),
    ("sessions", "wsl_distro", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "auto_continue", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "last_tg_chat_id", "INTEGER"),
    ("messages", "tokens_in", "INTEGER"),
    ("messages", "tokens_out", "INTEGER"),
]

MIGRATIONS: list[tuple[int, str]] = [
    (1, "initial_schema"),
    (2, "legacy_columns_backfill"),
]


async def _apply_migration(conn: aiosqlite.Connection, version: int) -> None:
    """Apply a single migration by version number."""
    if version == 1:
        await conn.executescript(MIGRATION_1_INITIAL)
    elif version == 2:
        for table, col, decl in MIGRATION_2_LEGACY_COLUMNS:
            try:
                async with conn.execute(f"SELECT {col} FROM {table} LIMIT 1"):
                    pass
            except Exception:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                logger.info("Migration 2: added %s.%s", table, col)
    else:
        raise ValueError(f"Unknown migration version: {version}")


async def _current_version(conn: aiosqlite.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    async with conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def init_db(db_path: str = DB_PATH) -> aiosqlite.Connection:
    """Initialize database: WAL mode, versioned migrations, foreign keys."""
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA synchronous=NORMAL")

    # Always ensure schema_version exists (bootstrap)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    await conn.commit()

    # Backwards compat: if we see an old DB with tables but no version rows,
    # stamp it as version 1 before layering further migrations on top.
    if await _current_version(conn) == 0:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ) as cur:
            pre_existing = await cur.fetchone()
        if pre_existing:
            await conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES(1)")
            await conn.commit()
            logger.info("Detected legacy schema — stamped as version 1")

    applied = await _current_version(conn)
    for version, name in MIGRATIONS:
        if version > applied:
            logger.info("Applying migration %d (%s)", version, name)
            async with tx(conn):
                await _apply_migration(conn, version)
                await conn.execute(
                    "INSERT INTO schema_version(version) VALUES(?)", (version,)
                )

    logger.info("Database initialized at %s (schema v%d)", db_path, await _current_version(conn))
    return conn


# ---------------------------------------------------------------------------
# Transaction helper — batches multiple writes into one fsync.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def tx(conn: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
    """Async transaction context: wraps writes in BEGIN/COMMIT or ROLLBACK."""
    await conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        await conn.execute("ROLLBACK")
        raise
    else:
        await conn.execute("COMMIT")


async def wal_checkpoint(conn: aiosqlite.Connection) -> None:
    """Truncate the WAL file — call periodically to keep it from growing."""
    try:
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        logger.warning("wal_checkpoint failed: %s", e)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

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
        "INSERT INTO sessions (id, name, work_dir, status, provider, wsl_distro) "
        "VALUES (?, ?, ?, 'running', ?, ?)",
        (session_id, name, work_dir, provider, wsl_distro),
    )
    await conn.commit()


async def update_session_status(
    conn: aiosqlite.Connection,
    session_id: str,
    status: str,
    last_tg_msg_id: int | None = None,
    last_tg_chat_id: int | None = None,
) -> None:
    """Update session status and optionally reply-routing fields."""
    if last_tg_msg_id is not None:
        if last_tg_chat_id is not None:
            await conn.execute(
                "UPDATE sessions SET status=?, last_tg_msg_id=?, last_tg_chat_id=?, "
                "updated_at=datetime('now') WHERE id=?",
                (status, last_tg_msg_id, last_tg_chat_id, session_id),
            )
        else:
            await conn.execute(
                "UPDATE sessions SET status=?, last_tg_msg_id=?, "
                "updated_at=datetime('now') WHERE id=?",
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
    conn: aiosqlite.Connection,
    tg_message_id: int,
    tg_chat_id: int | None = None,
) -> dict | None:
    """Find session by Telegram message ID; scoped by chat_id when supplied.

    Scoping by chat_id prevents cross-user collisions: Telegram message ids
    are only unique per-chat, so a stale row for chat A could match a fresh
    message in chat B without this filter.
    """
    if tg_chat_id is not None:
        async with conn.execute(
            "SELECT * FROM sessions WHERE last_tg_msg_id=? AND last_tg_chat_id=?",
            (tg_message_id, tg_chat_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
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


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

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
        "INSERT INTO messages (session_id, role, content, tg_message_id, tokens_in, tokens_out) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, role, content, tg_message_id, tokens_in, tokens_out),
    )
    await conn.commit()


async def get_token_usage(
    conn: aiosqlite.Connection,
    session_id: str | None = None,
    since: str | None = None,
) -> tuple[int, int, int]:
    """Return (sum_in, sum_out, message_count) for usage aggregation."""
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
    """Mark stale running/waiting sessions as error."""
    # Use parameter substitution with explicit modifier — avoids string concat.
    modifier = f"-{int(timeout_hours)} hours"
    cursor = await conn.execute(
        "UPDATE sessions SET status='error', updated_at=datetime('now') "
        "WHERE status IN ('running', 'waiting') "
        "AND updated_at < datetime('now', ?)",
        (modifier,),
    )
    await conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# bot_users — access requests & viewer management
# ---------------------------------------------------------------------------

async def get_bot_user(conn: aiosqlite.Connection, chat_id: int) -> dict | None:
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
    await conn.execute(
        "INSERT OR IGNORE INTO bot_users (chat_id, username, full_name, role) VALUES (?, ?, ?, ?)",
        (chat_id, username, full_name, role),
    )
    await conn.commit()


async def update_bot_user_role(
    conn: aiosqlite.Connection, chat_id: int, role: str
) -> bool:
    cursor = await conn.execute(
        "UPDATE bot_users SET role=? WHERE chat_id=?", (role, chat_id)
    )
    await conn.commit()
    return cursor.rowcount > 0


async def get_pending_users(conn: aiosqlite.Connection) -> list[dict]:
    async with conn.execute(
        "SELECT * FROM bot_users WHERE role='pending' ORDER BY created_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_viewers(conn: aiosqlite.Connection) -> list[dict]:
    async with conn.execute(
        "SELECT * FROM bot_users WHERE role='viewer' ORDER BY created_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# session_viewers
# ---------------------------------------------------------------------------

async def add_session_viewer(
    conn: aiosqlite.Connection, chat_id: int, session_id: str
) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO session_viewers (chat_id, session_id) VALUES (?, ?)",
        (chat_id, session_id),
    )
    await conn.commit()


async def remove_session_viewer(
    conn: aiosqlite.Connection, chat_id: int, session_id: str
) -> None:
    await conn.execute(
        "DELETE FROM session_viewers WHERE chat_id=? AND session_id=?",
        (chat_id, session_id),
    )
    await conn.commit()


async def get_session_viewer_ids(
    conn: aiosqlite.Connection, session_id: str
) -> list[int]:
    async with conn.execute(
        "SELECT chat_id FROM session_viewers WHERE session_id=?", (session_id,)
    ) as cur:
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def get_viewer_session_ids(
    conn: aiosqlite.Connection, chat_id: int
) -> list[str]:
    async with conn.execute(
        "SELECT session_id FROM session_viewers WHERE chat_id=?", (chat_id,)
    ) as cur:
        rows = await cur.fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# pending_prompts
# ---------------------------------------------------------------------------

async def create_pending_prompt(
    conn: aiosqlite.Connection,
    session_id: str,
    chat_id: int,
    prompt: str,
    retry_at: str,
    mode: str,
) -> int:
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
    async with conn.execute(
        "SELECT * FROM pending_prompts WHERE id=?", (pending_id,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_due_pending_prompts(conn: aiosqlite.Connection) -> list[dict]:
    async with conn.execute(
        "SELECT * FROM pending_prompts WHERE retry_at <= datetime('now') "
        "ORDER BY retry_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def delete_pending_prompt(
    conn: aiosqlite.Connection, pending_id: int
) -> None:
    await conn.execute("DELETE FROM pending_prompts WHERE id=?", (pending_id,))
    await conn.commit()


async def list_pending_prompts(conn: aiosqlite.Connection) -> list[dict]:
    async with conn.execute(
        "SELECT * FROM pending_prompts ORDER BY retry_at ASC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_pending_by_session(
    conn: aiosqlite.Connection, session_id: str
) -> dict | None:
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
    cursor = await conn.execute(
        "UPDATE sessions SET auto_continue=? WHERE id=?",
        (1 if enabled else 0, session_id),
    )
    await conn.commit()
    return cursor.rowcount > 0
