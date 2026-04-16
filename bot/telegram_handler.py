import json
import logging
import functools
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from bot.config import Config
from bot.session_manager import SessionManager
from bot.message_formatter import (
    format_notification,
    format_session_list,
    format_error,
    escape_markdown_v2,
    split_message,
)
from bot.external_sessions import list_external_sessions, find_session_by_query

logger = logging.getLogger(__name__)


async def _safe_reply(message, text: str, **kwargs):
    """Send message with MarkdownV2, fallback to plain text on parse error."""
    try:
        return await message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, **kwargs
        )
    except Exception as e:
        err = str(e).lower()
        if "can't parse entities" in err:
            logger.warning("MarkdownV2 parse failed, falling back to plain text")
            plain = text.replace("\\", "")
            return await message.reply_text(plain, **kwargs)
        if "message is too long" in err:
            return await message.reply_text(text[:4000] + "\n\\.\\.\\.\\(обрезано\\)", parse_mode=ParseMode.MARKDOWN_V2, **kwargs)
        raise


async def _safe_send(bot, chat_id: int, text: str, **kwargs):
    """Send message with MarkdownV2, fallback to plain text on parse error."""
    try:
        return await bot.send_message(
            chat_id, text, parse_mode=ParseMode.MARKDOWN_V2, **kwargs
        )
    except Exception as e:
        err = str(e).lower()
        if "can't parse entities" in err:
            logger.warning("MarkdownV2 parse failed, falling back to plain text")
            plain = text.replace("\\", "")
            return await bot.send_message(chat_id, plain, **kwargs)
        if "message is too long" in err:
            return await bot.send_message(chat_id, text[:4000] + "\n...(обрезано)", **kwargs)
        raise


def _is_owner(chat_id: int, config: Config) -> bool:
    """Check if chat_id is the bot owner."""
    return chat_id in config.allowed_chat_ids


async def _handle_access_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, config: Config
) -> None:
    """Handle message from an unknown user — create access request."""
    from bot import db as db_module

    chat_id = update.effective_chat.id
    conn = context.bot_data.get("db_conn")
    if not conn:
        return

    user = await db_module.get_bot_user(conn, chat_id)

    if user:
        if user["role"] == "viewer":
            await update.message.reply_text(
                "Вы подключены как наблюдатель \\(только чтение\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        elif user["role"] == "pending":
            await update.message.reply_text(
                "Ваш запрос на доступ на рассмотрении\\. Ожидайте\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        # denied — silently ignore
        return

    # New user — create request and notify owner
    tg_user = update.effective_user
    username = tg_user.username or ""
    full_name = tg_user.full_name or ""

    await db_module.create_bot_user(conn, chat_id, username, full_name, "pending")
    logger.info("Access request from %s (@%s, chat_id=%d)", full_name, username, chat_id)

    await update.message.reply_text(
        "Запрос на доступ отправлен владельцу\\. Ожидайте подтверждения\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Notify owner(s)
    esc_name = escape_markdown_v2(full_name)
    esc_user = escape_markdown_v2(f"@{username}") if username else "без username"
    for owner_id in config.allowed_chat_ids:
        await _safe_send(
            context.bot, owner_id,
            f"\U0001f514 *Запрос на доступ*\n\n"
            f"\U0001f464 {esc_name} \\({esc_user}\\)\n"
            f"ID: `{chat_id}`\n\n"
            f"`/approve {chat_id}` \\- одобрить\n"
            f"`/deny {chat_id}` \\- отклонить",
        )


def authorized(func):
    """Decorator: only allow owner chat IDs. Others get access request flow."""

    @functools.wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        config: Config = context.bot_data["config"]
        chat_id = update.effective_chat.id

        # Registration mode: empty whitelist → auto-register first user
        if not config.allowed_chat_ids:
            config.allowed_chat_ids.append(chat_id)
            config.save()
            logger.info("Auto-registered chat_id %d", chat_id)
            await update.message.reply_text(
                f"Регистрация завершена\\! Ваш chat\\_id: `{chat_id}`\n"
                f"Отправьте /help для списка команд\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        if _is_owner(chat_id, config):
            return await func(update, context, *args, **kwargs)

        # Non-owner — handle access request
        await _handle_access_request(update, context, config)

    return wrapper


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "*Claude\\-TG Bot*\n\n"
        "Удалённое управление сессиями Claude Code\\.\n\n"
        "Команды: /help",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    text = (
        "*Команды:*\n\n"
        "`/connect` \\- Подключиться к терминалу\n"
        "`/new <name> [path] [prompt]` \\- Новая сессия\n"
        "`/sessions` \\- Активные сессии бота\n"
        "`/get <file>` \\- Скачать файл из проекта\n"
        "`/stop <name>` \\- Отключить сессию\n"
        "`/sync` \\- Записать сводку в файл для терминала\n"
        "`/cancel` \\- Прервать текущую работу\n\n"
        "*Файлы:*\n"
        "\\- `/get README\\.md` \\- скачать файл\n"
        "\\- Отправьте файл \\- сохранится в work\\_dir\n\n"
        "*Доступ:*\n"
        "`/viewers` \\- запросы и наблюдатели\n"
        "`/approve <id>` \\- одобрить запрос\n"
        "`/deny <id>` \\- отклонить запрос\n"
        "`/share <session> <id>` \\- дать доступ к сессии\n"
        "`/unshare <session> <id>` \\- отозвать доступ"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@authorized
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new <name> [path] [prompt] command."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    config: Config = context.bot_data["config"]

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: `/new <name> [path] [prompt]`\n"
            "Пример: `/new myproject . Исправь баг в auth\\.py`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    name = args[0]

    # Parse: /new <name> [prompt...]
    # If second arg is an existing directory, use it as work_dir
    # Otherwise everything after name is the prompt
    if len(args) >= 2:
        candidate_path = Path(args[1]).resolve()
        if candidate_path.is_dir():
            work_dir = str(candidate_path)
            prompt = " ".join(args[2:]) if len(args) >= 3 else f"You are working on project '{name}'."
        else:
            work_dir = config.default_work_dir
            prompt = " ".join(args[1:])
    else:
        work_dir = config.default_work_dir
        prompt = f"You are working on project '{name}'."

    # Resolve work_dir
    work_dir_path = Path(work_dir).resolve()
    if not work_dir_path.is_dir():
        await update.message.reply_text(
            format_error(f"Directory not found: {work_dir}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Send "processing" message
    processing_msg = await update.message.reply_text(
        f"\u23f3 Запуск сессии *{escape_markdown_v2(name)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        response = await session_mgr.create_session(
            name, str(work_dir_path), prompt
        )
    except ValueError as e:
        await processing_msg.edit_text(
            format_error(str(e)), parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    except Exception as e:
        logger.exception("Failed to create session")
        await processing_msg.edit_text(
            format_error(f"Failed to create session: {e}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Format and send result
    status = "error" if response.error else "waiting"
    notification = format_notification(
        name, str(work_dir_path), response, status
    )

    chunks = split_message(notification, config.max_message_length)
    last_msg = None
    for chunk in chunks:
        last_msg = await _safe_reply(update.message, chunk)

    # Update session with the last message ID for reply routing
    if last_msg and response.session_id:
        await session_mgr.update_tg_message(response.session_id, last_msg.message_id)

    # Delete the "processing" message
    try:
        await processing_msg.delete()
    except Exception:
        pass


@authorized
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sessions — show bot sessions + terminal sessions."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    # Bot-managed sessions (from DB)
    db_sessions = await session_mgr.list_sessions()

    # Terminal sessions (from providers)
    terminal_sessions = []
    db_ids = {s["id"] for s in db_sessions}
    for provider in session_mgr._providers.values():
        for s in provider.list_sessions():
            if s.session_id not in db_ids:
                terminal_sessions.append(s)

    lines = []

    if db_sessions:
        lines.append("*Подключённые сессии:*\n")
        for i, s in enumerate(db_sessions, 1):
            emoji = {"running": "\u23f3", "waiting": "\U0001f535", "done": "\U0001f7e2", "error": "\U0001f534"}.get(s["status"], "\u2753")
            name = escape_markdown_v2(s["name"])
            provider = escape_markdown_v2(s.get("provider", "claude"))
            wsl = " \U0001f427" if s.get("wsl_distro") else ""
            lines.append(f"{i}\\. {emoji}{wsl} `{name}` \\({provider}\\)")

    if terminal_sessions:
        if lines:
            lines.append("")
        lines.append("*В терминалах \\(не подключены\\):*\n")
        for s in terminal_sessions:
            icon = {"claude": "\U0001f7e3", "codex": "\U0001f7e2"}.get(s.provider, "\u26aa")
            wsl = f" \U0001f427{escape_markdown_v2(s.wsl_distro)}" if s.wsl_distro else ""
            label = escape_markdown_v2(s.slug or s.session_id[:8])
            lines.append(f"\u26ab{icon}{wsl} `{label}` \\- `/connect`")

    if not lines:
        lines.append("Нет сессий\\. Запустите `claude`/`codex` и `/connect`")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


@authorized
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop <name> command."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    if not context.args:
        await update.message.reply_text(
            "Использование: `/stop <name>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    name = context.args[0]
    try:
        await session_mgr.stop_session_by_name(name)
        await update.message.reply_text(
            f"\U0001f7e2 Сессия `{escape_markdown_v2(name)}` остановлена\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except ValueError as e:
        await update.message.reply_text(
            format_error(str(e)), parse_mode=ParseMode.MARKDOWN_V2
        )


@authorized
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — kill all running sessions."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    sessions = await session_mgr.list_sessions()
    running = [s for s in sessions if s["status"] == "running"]

    if not running:
        await update.message.reply_text("Нет активных процессов для отмены\\.",
                                         parse_mode=ParseMode.MARKDOWN_V2)
        return

    for s in running:
        await session_mgr.stop_session(s["id"])

    names = ", ".join(s["name"] for s in running)
    await update.message.reply_text(
        f"Остановлено: `{escape_markdown_v2(names)}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@authorized
async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sync — write conversation summary to the session's work_dir."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    active = await session_mgr.list_sessions()
    waiting = [s for s in active if s["status"] == "waiting"]

    if not waiting:
        await update.message.reply_text(
            "Нет активных сессий для синхронизации\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if len(waiting) > 1:
        keyboard = [
            [InlineKeyboardButton(s["name"], callback_data=f"sync:{s['id']}")]
            for s in waiting
        ]
        await update.message.reply_text(
            "Выберите сессию для синхронизации:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session = waiting[0]
    await _do_sync(update.message, session_mgr, session)


async def _do_sync(message, session_mgr: SessionManager, session: dict) -> None:
    """Execute sync and send confirmation."""
    try:
        file_path = await session_mgr.sync_session(session["id"])
    except ValueError as e:
        await _safe_reply(message, format_error(str(e)))
        return
    except Exception as e:
        logger.exception("Sync failed")
        await _safe_reply(message, format_error(f"Sync failed: {e}"))
        return

    esc_name = escape_markdown_v2(session["name"])
    esc_path = escape_markdown_v2(file_path)
    await _safe_reply(
        message,
        f"\u2705 Синхронизация `{esc_name}` завершена\\.\n\n"
        f"Файл: `{esc_path}`\n\n"
        f"В терминале Claude прочитает его автоматически "
        f"или выполните:\n`cat \\.claude\\-tg\\-sync\\.md`",
    )


@authorized
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /connect — show terminal sessions from all providers as inline buttons."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    all_sessions = []
    for provider in session_mgr._providers.values():
        all_sessions.extend(provider.list_sessions())

    if not all_sessions:
        await update.message.reply_text(
            "Нет активных сессий в терминалах\\.\n"
            "Запустите `claude` или `codex` в терминале\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    PROVIDER_ICON = {"claude": "\U0001f7e3", "codex": "\U0001f7e2"}  # 🟣 / 🟢

    keyboard = []
    for s in all_sessions:
        icon = PROVIDER_ICON.get(s.provider, "\u26aa")
        wsl_tag = f" \U0001f427{s.wsl_distro}" if s.wsl_distro else ""  # 🐧
        label = s.slug or s.session_id[:8]
        short_cwd = Path(s.cwd).name if s.cwd else "?"
        btn_text = f"{icon}{wsl_tag} {label} | {short_cwd}"
        # Telegram limits callback_data to 64 bytes.
        # Use short session_id prefix — find_session() matches by prefix.
        sid_short = s.session_id[:12]
        cb_data = f"a:{s.provider}:{sid_short}"
        keyboard.append(
            [InlineKeyboardButton(btn_text, callback_data=cb_data)]
        )

    await update.message.reply_text(
        "*Выберите сессию для подключения:*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text messages to the appropriate session."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    config: Config = context.bot_data["config"]
    text = update.message.text

    if not text:
        return

    session = None

    # If replying to a bot message, find the session
    if update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id
        session = await session_mgr.get_session_by_tg_message(reply_msg_id)

    # If not a reply, try remembered session, then active sessions
    if not session:
        # Check if user has a remembered active session
        remembered_id = context.user_data.get("active_session_id")
        if remembered_id:
            from bot import db as db_module
            remembered = await db_module.get_session(session_mgr.conn, remembered_id)
            if remembered and remembered["status"] in ("running", "waiting"):
                session = remembered

    if not session:
        active = await session_mgr.list_sessions()
        waiting = [s for s in active if s["status"] == "waiting"]

        if len(waiting) == 1:
            session = waiting[0]
        elif len(waiting) > 1:
            keyboard = [
                [InlineKeyboardButton(s["name"], callback_data=f"resume:{s['id']}")]
                for s in waiting
            ]
            await update.message.reply_text(
                "Выберите сессию:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            context.user_data["pending_prompt"] = text
            return
        else:
            await update.message.reply_text(
                "Нет активных сессий\\. `/connect` или `/new`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

    # Resume the session
    await _resume_and_reply(update, context, session, text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    config: Config = context.bot_data["config"]

    if not _is_owner(query.from_user.id, config):
        await query.answer("Доступ запрещён", show_alert=True)
        return

    await query.answer()

    data = query.data
    if data.startswith("a:") or data.startswith("attach:"):
        await _handle_attach_callback(query, context)
    elif data.startswith("resume:"):
        await _handle_resume_callback(update, query, context)
    elif data.startswith("sync:"):
        await _handle_sync_callback(query, context)


async def _handle_sync_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle sync session selection callback."""
    session_id = query.data.split(":", 1)[1]
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    from bot import db as db_module

    session = await db_module.get_session(session_mgr.conn, session_id)
    if not session:
        await query.edit_message_text(
            "Сессия не найдена\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    await query.edit_message_text(
        f"\u23f3 Синхронизация *{escape_markdown_v2(session['name'])}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await _do_sync(query.message, session_mgr, session)


async def _handle_attach_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle attach button press — connect to a terminal session."""
    # Formats:
    #   a:provider:sid_prefix        (new short format)
    #   attach:provider:session_id   (legacy)
    raw = query.data
    parts = raw.split(":", 2)
    if len(parts) == 3:
        provider_name, session_id = parts[1], parts[2]
    else:
        provider_name, session_id = "claude", parts[-1]

    session_mgr: SessionManager = context.bot_data["session_mgr"]

    try:
        provider = session_mgr.get_provider(provider_name)
    except ValueError:
        await query.edit_message_text(
            format_error(f"Провайдер '{provider_name}' не подключен\\."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    ext = provider.find_session(session_id)
    if not ext:
        await query.edit_message_text(
            format_error("Сессия не найдена\\. Возможно, терминал закрыт\\."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    name = ext.slug or ext.session_id[:8]
    wsl_distro = ext.wsl_distro

    try:
        await session_mgr.import_external_session(
            ext.session_id, name, ext.cwd,
            provider_name=provider_name, wsl_distro=wsl_distro,
        )
    except ValueError as e:
        await query.edit_message_text(
            format_error(str(e)), parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    ICON = {"claude": "\U0001f7e3", "codex": "\U0001f7e2"}
    icon = ICON.get(provider_name, "\U0001f517")
    wsl_tag = f" \U0001f427 WSL/{escape_markdown_v2(wsl_distro)}" if wsl_distro else ""

    # Remember this session for future messages
    context.user_data["active_session_id"] = ext.session_id

    await query.edit_message_text(
        f"{icon}{wsl_tag} Подключено: `{escape_markdown_v2(name)}`\n"
        f"\U0001f4c1 `{escape_markdown_v2(ext.cwd)}`\n\n"
        f"Отправьте сообщение для продолжения\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_resume_callback(
    update: Update, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle session selection for message routing."""
    session_id = query.data.split(":", 1)[1]
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    from bot import db as db_module

    session = await db_module.get_session(session_mgr.conn, session_id)
    if not session:
        await query.edit_message_text("Сессия не найдена\\.",
                                       parse_mode=ParseMode.MARKDOWN_V2)
        return

    prompt = context.user_data.get("pending_prompt", "")
    if not prompt:
        await query.edit_message_text("Нет сообщения для отправки\\.",
                                       parse_mode=ParseMode.MARKDOWN_V2)
        return

    await query.edit_message_text(
        f"\u23f3 Отправляю в *{escape_markdown_v2(session['name'])}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    await _resume_and_reply(update, context, session, prompt, edit_message=query.message)


async def _resume_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict,
    prompt: str,
    edit_message=None,
) -> None:
    """Resume a session and send the response to Telegram."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    config: Config = context.bot_data["config"]

    # Send or edit processing message
    if edit_message:
        processing_msg = edit_message
        await processing_msg.edit_text(
            f"\u23f3 Обработка в *{escape_markdown_v2(session['name'])}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        processing_msg = await update.message.reply_text(
            f"\u23f3 Обработка в *{escape_markdown_v2(session['name'])}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    try:
        response = await session_mgr.resume_session(session["id"], prompt)
    except Exception as e:
        logger.exception("Failed to resume session")
        await processing_msg.edit_text(
            format_error(f"Resume failed: {e}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    logger.debug(
        "Response: session=%s len=%d error=%s",
        response.session_id[:8] if response.session_id else "?",
        len(response.text) if response.text else 0,
        bool(response.error),
    )

    # Delete processing message
    try:
        await processing_msg.delete()
    except Exception:
        pass

    status = "error" if response.error else "waiting"
    notification = format_notification(
        session["name"], session["work_dir"], response, status
    )

    chunks = split_message(notification, config.max_message_length)
    last_msg = None
    chat_id = update.effective_chat.id

    for chunk in chunks:
        last_msg = await _safe_send(context.bot, chat_id, chunk)

    # Update session with last message ID
    if last_msg:
        await session_mgr.update_tg_message(session["id"], last_msg.message_id)


async def _find_active_session(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_mgr: SessionManager
) -> dict | None:
    """Find the active session for file operations. Returns session dict or None."""
    # Check remembered session
    remembered_id = context.user_data.get("active_session_id")
    if remembered_id:
        from bot import db as db_module
        session = await db_module.get_session(session_mgr.conn, remembered_id)
        if session and session["status"] in ("running", "waiting"):
            return session

    active = await session_mgr.list_sessions()
    waiting = [s for s in active if s["status"] in ("running", "waiting")]

    if len(waiting) == 1:
        return waiting[0]
    elif len(waiting) > 1:
        names = ", ".join(s["name"] for s in waiting)
        await _safe_reply(
            update.message,
            f"Несколько активных сессий: `{escape_markdown_v2(names)}`\n"
            f"Используйте `/connect` чтобы выбрать\\.",
        )
        return None
    else:
        await _safe_reply(
            update.message,
            "Нет активных сессий\\. `/connect` или `/new`",
        )
        return None


_SENSITIVE_FILES = {"config.json", ".env", "claude_tg.db"}


def _resolve_work_path(session: dict, rel_path: str) -> Path:
    """Resolve a relative path against session's work_dir, handling WSL.

    Raises ValueError on path traversal or access to sensitive files.
    """
    work_dir = session["work_dir"]
    wsl_distro = session.get("wsl_distro", "")

    if wsl_distro:
        from bot.providers.claude import _wsl_path_to_windows
        base = _wsl_path_to_windows(wsl_distro, work_dir)
    else:
        base = Path(work_dir)

    resolved = (base / rel_path).resolve()
    base_resolved = base.resolve()

    # Prevent path traversal
    if not str(resolved).startswith(str(base_resolved)):
        raise ValueError("Выход за пределы рабочей директории")

    # Block sensitive files
    if resolved.name in _SENSITIVE_FILES:
        raise ValueError(f"Доступ к {resolved.name} запрещён")

    return resolved


@authorized
async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /get <path> — send a file from session's work_dir to Telegram."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    if not context.args:
        await update.message.reply_text(
            "Использование: `/get <filename>`\n"
            "Пример: `/get README\\.md`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    rel_path = " ".join(context.args)
    session = await _find_active_session(update, context, session_mgr)
    if not session:
        return

    try:
        file_path = _resolve_work_path(session, rel_path)
    except ValueError as e:
        await _safe_reply(update.message, format_error(str(e)))
        return

    if not file_path.exists():
        await _safe_reply(update.message, format_error(f"Файл не найден: {rel_path}"))
        return

    if not file_path.is_file():
        await _safe_reply(update.message, format_error(f"Не файл: {rel_path}"))
        return

    try:
        with open(file_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=file_path.name,
            )
    except Exception as e:
        logger.exception("Failed to send file")
        await _safe_reply(update.message, format_error(f"Ошибка отправки: {e}"))


@authorized
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads — save to active session's work_dir."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    doc = update.message.document
    if not doc:
        return

    session = await _find_active_session(update, context, session_mgr)
    if not session:
        return

    filename = doc.file_name or "uploaded_file"
    # Sanitize filename — strip path components
    filename = Path(filename).name

    MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
    if doc.file_size and doc.file_size > MAX_UPLOAD_SIZE:
        await _safe_reply(update.message, format_error("Файл слишком большой \\(макс 10 МБ\\)"))
        return

    try:
        target_path = _resolve_work_path(session, filename)
    except ValueError as e:
        await _safe_reply(update.message, format_error(str(e)))
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(str(target_path))
    except Exception as e:
        logger.exception("Failed to download file")
        await _safe_reply(update.message, format_error(f"Ошибка загрузки: {e}"))
        return

    esc_name = escape_markdown_v2(filename)
    esc_dir = escape_markdown_v2(session["work_dir"])
    await _safe_reply(
        update.message,
        f"\u2705 `{esc_name}` \u2192 `{esc_dir}`",
    )


# ---------------------------------------------------------------------------
# Owner-only admin commands: /approve, /deny, /share, /unshare, /viewers
# ---------------------------------------------------------------------------

@authorized
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /approve <chat_id> — approve access request."""
    from bot import db as db_module
    conn = context.bot_data["db_conn"]

    if not context.args:
        await _safe_reply(update.message, "Использование: `/approve <chat_id>`")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await _safe_reply(update.message, format_error("Неверный chat\\_id"))
        return

    user = await db_module.get_bot_user(conn, target_id)
    if not user:
        await _safe_reply(update.message, format_error("Пользователь не найден"))
        return

    await db_module.update_bot_user_role(conn, target_id, "viewer")
    esc = escape_markdown_v2(user["full_name"] or str(target_id))
    await _safe_reply(update.message, f"\u2705 {esc} одобрен как наблюдатель")

    # Notify the user
    try:
        await _safe_send(
            context.bot, target_id,
            "\u2705 Ваш запрос одобрен\\! Вы подключены как наблюдатель\\.",
        )
    except Exception:
        pass


@authorized
async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /deny <chat_id> — deny access request."""
    from bot import db as db_module
    conn = context.bot_data["db_conn"]

    if not context.args:
        await _safe_reply(update.message, "Использование: `/deny <chat_id>`")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await _safe_reply(update.message, format_error("Неверный chat\\_id"))
        return

    await db_module.update_bot_user_role(conn, target_id, "denied")
    await _safe_reply(update.message, f"\u274c Доступ для `{target_id}` отклонён")


@authorized
async def cmd_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /share <session_name> <chat_id> — grant watcher access."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    conn = context.bot_data["db_conn"]

    if len(context.args) < 2:
        await _safe_reply(
            update.message,
            "Использование: `/share <session_name> <chat_id>`",
        )
        return

    session_name = context.args[0]
    try:
        target_id = int(context.args[1])
    except ValueError:
        await _safe_reply(update.message, format_error("Неверный chat\\_id"))
        return

    # Check user is an approved viewer
    user = await db_module.get_bot_user(conn, target_id)
    if not user or user["role"] != "viewer":
        await _safe_reply(update.message, format_error("Пользователь не одобрен как наблюдатель"))
        return

    # Find session
    session = await db_module.get_session_by_name(conn, session_name)
    if not session:
        await _safe_reply(update.message, format_error(f"Сессия '{session_name}' не найдена"))
        return

    await db_module.add_session_viewer(conn, target_id, session["id"])
    esc_name = escape_markdown_v2(session_name)
    esc_user = escape_markdown_v2(user["full_name"] or str(target_id))
    await _safe_reply(
        update.message,
        f"\U0001f441 {esc_user} подключён к сессии `{esc_name}`",
    )

    # Notify viewer
    try:
        await _safe_send(
            context.bot, target_id,
            f"\U0001f441 Вам открыт доступ к сессии `{esc_name}` \\(только чтение\\)",
        )
    except Exception:
        pass


@authorized
async def cmd_unshare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unshare <session_name> <chat_id> — revoke watcher access."""
    from bot import db as db_module
    conn = context.bot_data["db_conn"]

    if len(context.args) < 2:
        await _safe_reply(
            update.message,
            "Использование: `/unshare <session_name> <chat_id>`",
        )
        return

    session_name = context.args[0]
    try:
        target_id = int(context.args[1])
    except ValueError:
        await _safe_reply(update.message, format_error("Неверный chat\\_id"))
        return

    session = await db_module.get_session_by_name(conn, session_name)
    if not session:
        await _safe_reply(update.message, format_error(f"Сессия '{session_name}' не найдена"))
        return

    await db_module.remove_session_viewer(conn, target_id, session["id"])
    esc_name = escape_markdown_v2(session_name)
    await _safe_reply(update.message, f"\u274c Доступ к `{esc_name}` для `{target_id}` отозван")


@authorized
async def cmd_viewers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /viewers — list pending requests and active viewers."""
    from bot import db as db_module
    conn = context.bot_data["db_conn"]

    pending = await db_module.get_pending_users(conn)
    viewers = await db_module.get_viewers(conn)

    lines = []

    if pending:
        lines.append("*Ожидают одобрения:*\n")
        for u in pending:
            name = escape_markdown_v2(u["full_name"] or "?")
            uname = escape_markdown_v2(f"@{u['username']}") if u["username"] else ""
            lines.append(f"\U0001f7e1 {name} {uname} \\- `{u['chat_id']}`")
        lines.append("")

    if viewers:
        lines.append("*Наблюдатели:*\n")
        for u in viewers:
            name = escape_markdown_v2(u["full_name"] or "?")
            uname = escape_markdown_v2(f"@{u['username']}") if u["username"] else ""
            sids = await db_module.get_viewer_session_ids(conn, u["chat_id"])
            sess_count = f" \\({len(sids)} сессий\\)" if sids else ""
            lines.append(f"\U0001f7e2 {name} {uname} \\- `{u['chat_id']}`{sess_count}")

    if not lines:
        await _safe_reply(update.message, "Нет запросов и наблюдателей\\.")
        return

    await _safe_reply(update.message, "\n".join(lines))


@authorized
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /debug — show provider diagnostics."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    lines = ["*Диагностика провайдеров:*\n"]

    for name, provider in session_mgr._providers.items():
        lines.append(f"*{escape_markdown_v2(name)}:*")
        try:
            sessions = provider.list_sessions()
            lines.append(f"  Найдено сессий: {len(sessions)}")
            for s in sessions[:5]:
                wsl = f" WSL/{escape_markdown_v2(s.wsl_distro)}" if s.wsl_distro else ""
                alive = "\u2705" if s.is_alive else "\u274c"
                label = escape_markdown_v2(s.slug or s.session_id[:12])
                lines.append(f"  {alive}{wsl} `{label}`")
            if len(sessions) > 5:
                lines.append(f"  \\.\\.\\.и ещё {len(sessions) - 5}")
        except Exception as e:
            lines.append(f"  \U0001f534 Ошибка: {escape_markdown_v2(str(e))}")

        # Detailed diagnostics if provider supports it
        if hasattr(provider, "diagnose"):
            lines.append("")
            lines.append(f"  *Детали:*")
            try:
                for diag_line in provider.diagnose():
                    lines.append(f"  `{escape_markdown_v2(diag_line)}`")
            except Exception as e:
                lines.append(f"  diagnose error: {escape_markdown_v2(str(e))}")
        lines.append("")

    await _safe_reply(update.message, "\n".join(lines))


@authorized
async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unsupported message types (stickers, GIFs, voice, etc.)."""
    await _safe_reply(
        update.message,
        "Поддерживаются только текст и файлы\\. "
        "Отправьте /help для списка команд\\.",
    )


def setup_handlers(app: Application, session_mgr: SessionManager, config: Config) -> None:
    """Register all handlers with the Telegram application."""
    app.bot_data["config"] = config
    app.bot_data["session_mgr"] = session_mgr

    # Set up watcher callback — forwards terminal responses to owner + viewers
    async def on_terminal_response(session_id: str, session_name: str, text: str):
        from bot import db as db_module

        conn = app.bot_data.get("db_conn")
        owner_ids = config.allowed_chat_ids
        if not owner_ids:
            return

        esc_name = escape_markdown_v2(session_name)
        esc_text = escape_markdown_v2(text)
        notification = (
            f"\U0001f4e1 *Терминал* \\| `{esc_name}`\n\n"
            f"{esc_text}"
        )
        chunks = split_message(notification, config.max_message_length)

        # Send to owners
        for chat_id in owner_ids:
            for chunk in chunks:
                await _safe_send(app.bot, chat_id, chunk)

        # Send to session viewers
        if conn:
            viewer_ids = await db_module.get_session_viewer_ids(conn, session_id)
            for chat_id in viewer_ids:
                for chunk in chunks:
                    try:
                        await _safe_send(app.bot, chat_id, chunk)
                    except Exception:
                        logger.warning("Failed to send to viewer %d", chat_id)

    session_mgr.set_watcher_callback(on_terminal_response)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("share", cmd_share))
    app.add_handler(CommandHandler("unshare", cmd_unshare))
    app.add_handler(CommandHandler("viewers", cmd_viewers))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Catch-all for unsupported types (stickers, GIFs, voice, video, etc.)
    app.add_handler(MessageHandler(~filters.COMMAND, handle_unsupported))

    logger.info("Telegram handlers registered")
