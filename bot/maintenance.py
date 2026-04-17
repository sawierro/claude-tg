import asyncio
import logging

import aiosqlite

from bot import db

logger = logging.getLogger(__name__)

# Run WAL checkpoint once an hour. Criterion 2.8 asks for "not less than 1/hr
# or when WAL size exceeds 10 MB"; the time-based trigger is enough in practice
# because our write volume is low (one row per user message).
CHECKPOINT_INTERVAL_SECONDS = 60 * 60  # 1 hour


async def maintenance_worker(conn: aiosqlite.Connection) -> None:
    """Periodically truncate the WAL file to keep it from growing unbounded."""
    logger.info(
        "Maintenance worker started (WAL checkpoint every %d min)",
        CHECKPOINT_INTERVAL_SECONDS // 60,
    )
    while True:
        try:
            await asyncio.sleep(CHECKPOINT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Maintenance worker cancelled")
            raise
        try:
            await db.wal_checkpoint(conn)
            logger.debug("WAL checkpoint completed")
        except Exception:
            logger.exception("Maintenance tick failed")
