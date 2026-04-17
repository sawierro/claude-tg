import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application

from bot import db as db_module
from bot.message_formatter import escape_markdown_v2
from bot.session_manager import SessionManager

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60


async def _send(app: Application, chat_id: int, text: str, **kwargs):
    """Send with MarkdownV2 fallback to plain text."""
    try:
        return await app.bot.send_message(
            chat_id, text, parse_mode=ParseMode.MARKDOWN_V2, **kwargs
        )
    except Exception as e:
        err = str(e).lower()
        if "can't parse entities" in err or "message is too long" in err:
            plain = text.replace("\\", "")[:4000]
            return await app.bot.send_message(chat_id, plain, **kwargs)
        raise


async def _process_auto(
    app: Application, session_mgr: SessionManager, pending: dict
) -> None:
    """Auto-resume a pending prompt."""
    sid = pending["session_id"]
    chat_id = pending["chat_id"]
    prompt = pending["prompt"]
    pending_id = pending["id"]

    session = await db_module.get_session(session_mgr.conn, sid)
    if not session:
        await _send(app, chat_id,
            f"\u26a0 Отложенное сообщение отменено: сессия `{escape_markdown_v2(sid[:12])}` удалена\\."
        )
        await db_module.delete_pending_prompt(session_mgr.conn, pending_id)
        return

    name_esc = escape_markdown_v2(session["name"])
    await _send(app, chat_id,
        f"\U0001f504 Автовозобновление `{name_esc}` после сброса лимита\\.\\.\\."
    )

    try:
        response = await session_mgr.resume_session(sid, prompt)
    except Exception as e:
        logger.exception("Auto-resume failed for pending %d", pending_id)
        await _send(app, chat_id,
            f"\U0001f534 Автовозобновление `{name_esc}` не удалось: "
            f"`{escape_markdown_v2(str(e))}`"
        )
        await db_module.delete_pending_prompt(session_mgr.conn, pending_id)
        return

    await db_module.delete_pending_prompt(session_mgr.conn, pending_id)

    if response.error:
        await _send(app, chat_id,
            f"\U0001f534 `{name_esc}`: {escape_markdown_v2(response.error[:500])}"
        )
    else:
        body = response.text[:1500] + ("..." if len(response.text) > 1500 else "")
        await _send(app, chat_id,
            f"\u2705 `{name_esc}`:\n{escape_markdown_v2(body)}"
        )


async def _process_manual(
    app: Application, session_mgr: SessionManager, pending: dict
) -> None:
    """Send a notification that the limit has reset, with a Resume button."""
    sid = pending["session_id"]
    chat_id = pending["chat_id"]
    prompt = pending["prompt"]
    pending_id = pending["id"]

    session = await db_module.get_session(session_mgr.conn, sid)
    if not session:
        await db_module.delete_pending_prompt(session_mgr.conn, pending_id)
        return

    name_esc = escape_markdown_v2(session["name"])
    preview = prompt[:300] + ("..." if len(prompt) > 300 else "")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f504 Возобновить", callback_data=f"resm:{pending_id}"),
        InlineKeyboardButton("\u274c Отменить", callback_data=f"resm_cancel:{pending_id}"),
    ]])

    try:
        await app.bot.send_message(
            chat_id,
            (
                f"\U0001f7e2 Лимит сброшен для `{name_esc}`\\.\n\n"
                f"*Отложенное сообщение:*\n```\n{escape_markdown_v2(preview)}\n```"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning("Failed to send manual-resume notification: %s", e)
        plain = (
            f"Лимит сброшен для {session['name']}.\n\n"
            f"Отложенное сообщение:\n{preview}\n\n"
            f"Используйте кнопку в /resume_list"
        )
        try:
            await app.bot.send_message(chat_id, plain, reply_markup=keyboard)
        except Exception:
            logger.exception("Could not deliver manual-resume notification")


async def _tick(app: Application, session_mgr: SessionManager) -> None:
    """Process all due pending prompts once."""
    try:
        due = await db_module.get_due_pending_prompts(session_mgr.conn)
    except Exception:
        logger.exception("Failed to query pending prompts")
        return

    for pending in due:
        try:
            if pending["mode"] == "auto":
                await _process_auto(app, session_mgr, pending)
            else:
                await _process_manual(app, session_mgr, pending)
        except Exception:
            logger.exception("Error processing pending prompt %d", pending.get("id"))


async def resume_worker(app: Application, session_mgr: SessionManager) -> None:
    """Background task: periodically check for pending prompts that are due."""
    logger.info("Resume worker started (check every %ds)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await _tick(app, session_mgr)
        except asyncio.CancelledError:
            logger.info("Resume worker cancelled")
            raise
        except Exception:
            logger.exception("Unhandled error in resume worker tick")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
