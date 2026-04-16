import asyncio
import logging
import sys
import signal

from telegram.ext import Application

import os

from bot.config import load_config
from bot.db import init_db, cleanup_stale_sessions, reset_running_sessions
from bot.session_manager import SessionManager
from bot.telegram_handler import setup_handlers
from bot.providers.claude import ClaudeProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def post_init(app: Application) -> None:
    """Run after bot initialization."""
    config = app.bot_data["config"]
    if not config.allowed_chat_ids:
        logger.info("Registration mode: waiting for first /start to auto-register chat_id")
    else:
        logger.info("Bot ready. Allowed chat IDs: %s", config.allowed_chat_ids)


async def post_shutdown(app: Application) -> None:
    """Clean up on shutdown."""
    conn = app.bot_data.get("db_conn")
    if conn:
        await conn.close()
        logger.info("Database connection closed")


def main() -> None:
    """Entry point."""
    try:
        config = load_config()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        print(f"\nError: {e}")
        print("Run the setup script first: scripts/setup.sh (Linux/Mac) or scripts\\setup.bat (Windows)")
        sys.exit(1)

    async def run():
        conn = await init_db()
        reset_count = await reset_running_sessions(conn)
        if reset_count:
            logger.info("Reset %d stale running sessions", reset_count)
        await cleanup_stale_sessions(conn, config.session_timeout_hours)

        session_mgr = SessionManager(config, conn)

        # Register all available providers
        session_mgr.register_provider(ClaudeProvider(config))

        try:
            from bot.providers.codex import CodexProvider
            session_mgr.register_provider(CodexProvider(config))
        except ImportError:
            logger.debug("Codex provider not available")

        app = (
            Application.builder()
            .token(config.telegram_token)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )

        app.bot_data["db_conn"] = conn
        setup_handlers(app, session_mgr, config)

        logger.info("Starting Claude-TG bot...")

        async with app:
            await app.start()
            await app.updater.start_polling()

            logger.info("Bot is running. Press Ctrl+C to stop.")

            # Wait until stopped
            stop_event = asyncio.Event()

            def signal_handler():
                stop_event.set()

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, signal_handler)
                except NotImplementedError:
                    # Windows doesn't support add_signal_handler
                    pass

            try:
                await stop_event.wait()
            except KeyboardInterrupt:
                pass

            logger.info("Shutting down...")
            await app.updater.stop()
            await app.stop()

        # conn closed by post_shutdown callback
        logger.info("Bot stopped.")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")


if __name__ == "__main__":
    main()
