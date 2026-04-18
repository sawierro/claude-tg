import asyncio
import functools
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import limit_detector
from bot import prompts as prompts_module
from bot import updater as updater_module
from bot.config import Config
from bot.message_formatter import (
    escape_markdown_v2,
    format_error,
    format_notification,
    split_message,
)
from bot.rate_limiter import RateLimiter
from bot.session_manager import SessionManager

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
            plain = text.replace("\\", "")[:4000] + "\n...(обрезано)"
            return await message.reply_text(plain, **kwargs)
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
            plain = text.replace("\\", "")[:4000] + "\n...(обрезано)"
            return await bot.send_message(chat_id, plain, **kwargs)
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
        elif user["role"] == "denied":
            # Single terse reply — no spammy notifications but no silent ignore either
            await update.message.reply_text(
                "Доступ к боту запрещён администратором\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
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
    """Decorator: only allow owner chat IDs. Non-owners go through access-request flow."""

    @functools.wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        config: Config = context.bot_data["config"]
        chat_id = update.effective_chat.id

        if _is_owner(chat_id, config):
            limiter: RateLimiter | None = context.bot_data.get("rate_limiter")
            if limiter is not None and not limiter.check(chat_id):
                logger.warning("Rate limit hit for owner chat_id=%d", chat_id)
                if update.message:
                    await update.message.reply_text(
                        f"\u26d4 Лимит {config.rate_limit_per_minute} сообщений/мин\\. "
                        f"Подождите и попробуйте снова\\.",
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                return
            return await func(update, context, *args, **kwargs)

        # Non-owner — handle access request (creates/updates bot_users row)
        await _handle_access_request(update, context, config)

    return wrapper


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — welcome screen with inline shortcuts."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔌 Подключиться", callback_data="help:connect"),
            InlineKeyboardButton("📋 Сессии", callback_data="help:sessions"),
        ],
        [
            InlineKeyboardButton("📝 Шаблоны", callback_data="help:prompts"),
            InlineKeyboardButton("📊 Usage", callback_data="help:usage"),
        ],
        [
            InlineKeyboardButton("❓ Полный /help", callback_data="help:full"),
        ],
    ])
    await update.message.reply_text(
        "*Claude\\-TG* \\— удалённое управление Claude Code / Codex\\.\n\n"
        "Запустите `claude` или `codex` в терминале и нажмите "
        "*🔌 Подключиться* ниже\\.\n\n"
        "Полный список команд: /help",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — grouped command reference."""
    text = (
        "*Claude\\-TG* \\— удалённое управление Claude Code / Codex\\.\n\n"

        "*🚀 Начало работы*\n"
        "`/connect` \\— подключиться к активному терминалу\n"
        "`/new <name> [path] [prompt]` \\— создать новую сессию\n"
        "`/sessions` \\— список сессий \\(🔁 \\= auto\\-continue, 🐧 \\= WSL\\)\n\n"

        "*💬 Работа с сессией*\n"
        "Просто отправь сообщение \\— уйдёт в активную сессию\\. "
        "*Ответь \\(reply\\)* на ответ бота, чтобы адресовать именно ту сессию\\.\n"
        "`/stop <name>` \\— отключить сессию от бота\n"
        "`/cancel` \\— убить все работающие CLI\\-процессы\n"
        "`/sync` \\— записать сводку в `\\.claude\\-tg\\-sync\\.md`\n\n"

        "*📁 Файлы*\n"
        "`/get <file>` \\— скачать файл из work\\_dir\n"
        "Отправь файл боту \\— сохранится в work\\_dir активной сессии\n\n"

        "*📝 Шаблоны промптов*\n"
        "`/prompts` \\— список шаблонов \\(кнопки\\)\n"
        "`/prompt <имя>` \\— отправить шаблон в сессию\n"
        "`/prompt_del <имя>` \\— удалить шаблон\n"
        "Отправь `\\.md` / `\\.txt` с подписью `\\#prompt` \\— сохранить как шаблон\n\n"

        "*📊 Статус и лимиты*\n"
        "`/ping [name]` \\— статус сессии \\(работает / зависла / ошибка\\)\n"
        "`/usage [name]` \\— расход токенов за 5ч / 24ч / всё время\n"
        "`/pending` \\— очередь сообщений, ждущих сброса лимита\n"
        "`/autocontinue [on|off] [name]` \\— auto\\-продолжение terminal\\-сессии\n\n"

        "*🔐 Управление доступом*\n"
        "`/viewers` \\— запросы и наблюдатели\n"
        "`/approve <id>` / `/deny <id>` \\— обработать запрос\n"
        "`/share <session> <id>` / `/unshare <session> <id>` \\— доступ к сессии\n\n"

        "*🛠 Диагностика*\n"
        "`/botstatus` \\— аптайм, активные процессы, сводка\n"
        "`/debug` \\— состояние провайдеров, WSL\\-дистрибутивов, путей\n"
        "`/update` \\— проверить обновления бота"
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
        await session_mgr.update_tg_message(
            response.session_id, last_msg.message_id,
            tg_chat_id=update.effective_chat.id,
        )

    # Delete the "processing" message
    try:
        await processing_msg.delete()
    except Exception:
        pass


@authorized
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sessions — show bot sessions + terminal sessions."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    # Bot-managed sessions (from DB) + terminal sessions (from providers, in parallel).
    db_sessions, all_terminal = await asyncio.gather(
        session_mgr.list_sessions(),
        session_mgr.list_terminal_sessions(),
    )
    db_ids = {s["id"] for s in db_sessions}
    terminal_sessions = [s for s in all_terminal if s.session_id not in db_ids]

    lines = []

    if db_sessions:
        lines.append("*Подключённые сессии:*\n")
        for i, s in enumerate(db_sessions, 1):
            emoji = {"running": "\u23f3", "waiting": "\U0001f535", "done": "\U0001f7e2", "error": "\U0001f534"}.get(s["status"], "\u2753")
            name = escape_markdown_v2(s["name"])
            provider = escape_markdown_v2(s.get("provider", "claude"))
            wsl = " \U0001f427" if s.get("wsl_distro") else ""
            ac = " \U0001f501" if s.get("auto_continue") else ""
            lines.append(f"{i}\\. {emoji}{wsl}{ac} `{name}` \\({provider}\\)")

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


def _format_elapsed(seconds: float) -> str:
    """Format a duration in a compact human-readable form."""
    if seconds < 60:
        return f"{int(seconds)} сек"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин {int(seconds % 60)} сек"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours} ч {minutes} мин"


def _fmt_num(n: int) -> str:
    """Format integer with thin-space thousand separators."""
    return f"{n:,}".replace(",", "\u2009")


@authorized
async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage [name] — show token usage for a session (and overall)."""
    from bot import db as db_module

    session_mgr: SessionManager = context.bot_data["session_mgr"]
    conn = session_mgr.conn

    session = None
    if context.args:
        name = context.args[0]
        session = await db_module.get_session_by_name(conn, name)
        if not session:
            session = await db_module.get_session(conn, name)
        if not session:
            await _safe_reply(update.message, format_error(f"Сессия '{name}' не найдена"))
            return
    else:
        remembered_id = context.user_data.get("active_session_id")
        if remembered_id:
            session = await db_module.get_session(conn, remembered_id)
        if not session:
            active = await session_mgr.list_sessions()
            waiting = [s for s in active if s["status"] in ("running", "waiting")]
            if len(waiting) == 1:
                session = waiting[0]

    lines = []
    if session:
        name_esc = escape_markdown_v2(session["name"])
        provider_esc = escape_markdown_v2(session.get("provider", "claude"))
        lines.append(f"*Сессия:* `{name_esc}` \\({provider_esc}\\)")

        for label, since in (("5 ч", "-5 hours"), ("24 ч", "-24 hours"), ("всё время", None)):
            t_in, t_out, n = await db_module.get_token_usage(conn, session["id"], since)
            total = t_in + t_out
            lines.append(
                f"  *{escape_markdown_v2(label)}*: "
                f"{_fmt_num(total)} токенов "
                f"\\(in `{_fmt_num(t_in)}` / out `{_fmt_num(t_out)}`, {n} запросов\\)"
            )
        lines.append("")

    lines.append("*По всем сессиям:*")
    for label, since in (("5 ч", "-5 hours"), ("24 ч", "-24 hours"), ("всё время", None)):
        t_in, t_out, n = await db_module.get_token_usage(conn, None, since)
        total = t_in + t_out
        lines.append(
            f"  *{escape_markdown_v2(label)}*: "
            f"{_fmt_num(total)} токенов "
            f"\\(in `{_fmt_num(t_in)}` / out `{_fmt_num(t_out)}`, {n} запросов\\)"
        )

    lines.append("")
    lines.append(escape_markdown_v2(
        "Лимит сбрасывается каждые 5 часов (Claude rolling window). "
        "Реальный остаток с сервера недоступен — это локальный счётчик."
    ))

    await _safe_reply(update.message, "\n".join(lines))


async def _resolve_session_for_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name_arg: str | None
):
    """Find a session by explicit name/sid or fall back to the active one."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    conn = session_mgr.conn

    if name_arg:
        session = await db_module.get_session_by_name(conn, name_arg)
        if not session:
            session = await db_module.get_session(conn, name_arg)
        return session

    remembered_id = context.user_data.get("active_session_id")
    if remembered_id:
        session = await db_module.get_session(conn, remembered_id)
        if session:
            return session

    active = await session_mgr.list_sessions()
    waiting = [s for s in active if s["status"] in ("running", "waiting")]
    if len(waiting) == 1:
        return waiting[0]
    return None


@authorized
async def cmd_autocontinue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /autocontinue [on|off] [name] — toggle per-session auto-continue."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    conn = session_mgr.conn

    args = context.args or []
    action: str | None = None
    name_arg: str | None = None

    if args:
        if args[0].lower() in ("on", "off"):
            action = args[0].lower()
            name_arg = args[1] if len(args) > 1 else None
        else:
            name_arg = args[0]

    session = await _resolve_session_for_command(update, context, name_arg)
    if not session:
        await _safe_reply(
            update.message,
            "Укажите сессию: `/autocontinue [on|off] <name>` "
            "или выберите через `/connect`",
        )
        return

    name_esc = escape_markdown_v2(session["name"])

    if action is None:
        state = "\U0001f501 включено" if session.get("auto_continue") else "\u2b55 выключено"
        await _safe_reply(
            update.message,
            f"*Auto\\-continue* для `{name_esc}`: {escape_markdown_v2(state)}\n\n"
            f"Управление: `/autocontinue on {escape_markdown_v2(session['name'])}` / "
            f"`/autocontinue off {escape_markdown_v2(session['name'])}`",
        )
        return

    enabled = action == "on"
    ok = await db_module.set_auto_continue(conn, session["id"], enabled)
    if not ok:
        await _safe_reply(update.message, format_error("Не удалось обновить настройку"))
        return

    if enabled:
        await _safe_reply(
            update.message,
            f"\U0001f501 Auto\\-continue *включено* для `{name_esc}`\\. "
            f"При лимите бот автоматически продолжит сессию после сброса\\.",
        )
    else:
        await _safe_reply(
            update.message,
            f"\u2b55 Auto\\-continue *выключено* для `{name_esc}`\\.",
        )


@authorized
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pending — list queued prompts waiting for limit reset."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    conn = session_mgr.conn

    items = await db_module.list_pending_prompts(conn)
    if not items:
        await _safe_reply(update.message, "Нет отложенных сообщений\\.")
        return

    lines = ["*Отложенные сообщения:*\n"]
    for p in items:
        session = await db_module.get_session(conn, p["session_id"])
        name = session["name"] if session else p["session_id"][:8]
        name_esc = escape_markdown_v2(name)
        mode_emoji = "\U0001f504" if p["mode"] == "auto" else "\U0001f514"
        preview = (p["prompt"][:60] + "...") if len(p["prompt"]) > 60 else p["prompt"]
        lines.append(
            f"`#{p['id']}` {mode_emoji} `{name_esc}` "
            f"\u2192 `{escape_markdown_v2(p['retry_at'])} UTC`\n"
            f"    _{escape_markdown_v2(preview)}_"
        )

    await _safe_reply(update.message, "\n".join(lines))


@authorized
async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /update — check GitHub for bot updates and offer to pull."""
    if not await updater_module.is_git_repo():
        await _safe_reply(update.message, format_error("Не git\\-репозиторий"))
        return

    processing = await update.message.reply_text(
        "\u23f3 Проверяю обновления\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        ok, err = await updater_module.fetch()
    except RuntimeError as e:
        await processing.edit_text(
            format_error(str(e)), parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    if not ok:
        await processing.edit_text(
            format_error(f"git fetch: {err}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    branch = await updater_module.current_branch() or "main"
    cur = await updater_module.current_commit()
    commits = await updater_module.pending_commits(branch)

    if not commits:
        await processing.edit_text(
            f"\u2705 Уже последняя версия \\(`{escape_markdown_v2(cur)}` на `{escape_markdown_v2(branch)}`\\)",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    dirty = await updater_module.is_working_tree_dirty()

    lines = [
        f"\U0001f4e5 *Доступно обновлений:* {len(commits)}",
        f"Текущий коммит: `{escape_markdown_v2(cur)}` \\(`{escape_markdown_v2(branch)}`\\)",
        "",
        "*Новые коммиты:*",
    ]
    for c in commits[:15]:
        lines.append(f"\\- `{escape_markdown_v2(c)}`")
    if len(commits) > 15:
        lines.append(f"\\.\\.\\.и ещё {len(commits) - 15}")

    if dirty:
        lines.append("")
        lines.append("\U0001f7e1 В рабочей копии есть локальные изменения \\- pull отменён\\.")
        await processing.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("\u2b07 Обновить (git pull)", callback_data=f"upd:pull:{branch}")]]
    )
    await processing.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )


async def _handle_update_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /update confirmation callback."""
    parts = query.data.split(":", 2)
    branch = parts[2] if len(parts) >= 3 else "main"

    await query.edit_message_text(
        "\u23f3 Выполняю git pull\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        ok, out = await updater_module.pull(branch)
    except RuntimeError as e:
        await query.edit_message_text(
            format_error(str(e)), parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    if not ok:
        await query.edit_message_text(
            format_error(f"git pull: {out}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    new_commit = await updater_module.current_commit()
    text = (
        f"\u2705 Обновлено до `{escape_markdown_v2(new_commit)}`\n\n"
        f"```\n{escape_markdown_v2(out[:500])}\n```\n"
        f"\U0001f504 Перезапустите бота, чтобы применить изменения\\."
    )
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@authorized
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ping [name] — check if a session is alive or hung."""
    from bot import db as db_module

    session_mgr: SessionManager = context.bot_data["session_mgr"]
    config: Config = context.bot_data["config"]
    conn = session_mgr.conn

    session = None
    if context.args:
        name_or_sid = context.args[0]
        session = await db_module.get_session_by_name(conn, name_or_sid)
        if not session:
            session = await db_module.get_session(conn, name_or_sid)
    else:
        remembered_id = context.user_data.get("active_session_id")
        if remembered_id:
            session = await db_module.get_session(conn, remembered_id)
        if not session:
            active = await session_mgr.list_sessions()
            waiting = [s for s in active if s["status"] in ("running", "waiting")]
            if len(waiting) == 1:
                session = waiting[0]

    if not session:
        await _safe_reply(
            update.message,
            "Укажите имя сессии: `/ping <name>` или выберите через `/connect`",
        )
        return

    status = session["status"]
    updated_iso = session["updated_at"]
    try:
        updated = datetime.fromisoformat(updated_iso.replace(" ", "T")).replace(
            tzinfo=UTC
        )
        elapsed = (datetime.now(UTC) - updated).total_seconds()
    except (ValueError, AttributeError):
        elapsed = 0

    last_msg_iso = await db_module.get_last_message_time(conn, session["id"])
    last_msg_ago = None
    if last_msg_iso:
        try:
            lm = datetime.fromisoformat(last_msg_iso.replace(" ", "T")).replace(
                tzinfo=UTC
            )
            last_msg_ago = (datetime.now(UTC) - lm).total_seconds()
        except (ValueError, AttributeError):
            pass

    timeout_s = config.subprocess_timeout_minutes * 60

    if status == "running":
        if timeout_s > 0 and elapsed > timeout_s:
            emoji = "\U0001f534"  # red
            health = f"вероятно зависла \\(> {config.subprocess_timeout_minutes} мин\\)"
        elif timeout_s > 0 and elapsed > timeout_s / 2:
            emoji = "\U0001f7e1"  # yellow
            health = f"работает долго \\({_format_elapsed(elapsed)}\\)"
        else:
            emoji = "\u23f3"  # hourglass
            health = f"работает \\({_format_elapsed(elapsed)}\\)"
    elif status == "waiting":
        emoji = "\U0001f7e2"  # green
        if last_msg_ago is not None:
            health = f"ждёт сообщение \\(ответ был {_format_elapsed(last_msg_ago)} назад\\)"
        else:
            health = "ждёт сообщение"
    elif status == "done":
        emoji = "\u2705"
        health = "завершена"
    elif status == "error":
        emoji = "\U0001f534"
        health = "ошибка"
    else:
        emoji = "\u2753"
        health = escape_markdown_v2(status)

    name_esc = escape_markdown_v2(session["name"])
    provider_esc = escape_markdown_v2(session.get("provider", "claude"))
    wsl = session.get("wsl_distro") or ""
    wsl_line = f"\n\U0001f427 WSL: `{escape_markdown_v2(wsl)}`" if wsl else ""

    text = (
        f"{emoji} `{name_esc}` \\({provider_esc}\\){wsl_line}\n"
        f"Статус: {health}"
    )

    await _safe_reply(update.message, text)


@authorized
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — kill all in-flight CLI subprocesses."""
    from bot.providers import _tracking

    session_mgr: SessionManager = context.bot_data["session_mgr"]

    active_procs = _tracking.active_count()
    if active_procs == 0:
        await update.message.reply_text(
            "Нет активных процессов для отмены\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    killed = await _tracking.kill_all()

    # Also mark any running sessions in DB as 'done' so UI reflects reality
    sessions = await session_mgr.list_sessions()
    for s in sessions:
        if s["status"] == "running":
            await session_mgr.stop_session(s["id"])

    await update.message.reply_text(
        f"\U0001f7e2 Убито процессов: {killed}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@authorized
async def cmd_prompts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /prompts — list saved prompt templates as inline keyboard."""
    config: Config = context.bot_data["config"]

    try:
        names = prompts_module.list_prompts(config.prompts_dir)
    except OSError as e:
        await _safe_reply(update.message, format_error(f"Не удалось прочитать каталог: {e}"))
        return

    if not names:
        await _safe_reply(
            update.message,
            "Нет сохранённых шаблонов\\.\n\n"
            "Отправьте `\\.md` или `\\.txt` файл с подписью `\\#prompt`\\, "
            "чтобы сохранить его как шаблон\\.",
        )
        return

    keyboard = []
    for i, name in enumerate(names):
        keyboard.append([InlineKeyboardButton(name, callback_data=f"pr:{i}")])

    context.user_data["prompt_list"] = names
    await update.message.reply_text(
        "*Шаблоны промптов:*\nНажмите для отправки в активную сессию\\.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@authorized
async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /prompt <name> — send a prompt template to the active session."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    config: Config = context.bot_data["config"]

    if not context.args:
        await _safe_reply(update.message, "Использование: `/prompt <имя>`")
        return

    name = " ".join(context.args)
    try:
        content = prompts_module.read_prompt(config.prompts_dir, name)
    except ValueError as e:
        await _safe_reply(update.message, format_error(str(e)))
        return

    session = await _find_active_session(update, context, session_mgr)
    if not session:
        return

    await _resume_and_reply(update, context, session, content)


@authorized
async def cmd_prompt_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /prompt_del <name> — delete a prompt template."""
    config: Config = context.bot_data["config"]

    if not context.args:
        await _safe_reply(update.message, "Использование: `/prompt_del <имя>`")
        return

    name = " ".join(context.args)
    try:
        prompts_module.delete_prompt(config.prompts_dir, name)
    except ValueError as e:
        await _safe_reply(update.message, format_error(str(e)))
        return

    await _safe_reply(update.message, f"\U0001f5d1 Шаблон `{escape_markdown_v2(name)}` удалён")


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

    all_sessions = await session_mgr.list_terminal_sessions()

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
    text = update.message.text

    if not text:
        return

    session = None

    # If replying to a bot message, find the session (scoped by chat_id)
    if update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id
        session = await session_mgr.get_session_by_tg_message(
            reply_msg_id, chat_id=update.effective_chat.id
        )

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
    elif data.startswith("pr:"):
        await _handle_prompt_callback(update, query, context)
    elif data.startswith("upd:"):
        await _handle_update_callback(query, context)
    elif data.startswith("lim:"):
        await _handle_limit_callback(query, context)
    elif data.startswith("resm_cancel:"):
        await _handle_resume_cancel_callback(query, context)
    elif data.startswith("resm:"):
        await _handle_resume_pending_callback(query, context)
    elif data.startswith("help:"):
        await _handle_help_shortcut(query, context)


async def _handle_help_shortcut(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start inline shortcuts — point user to the right command."""
    action = query.data.split(":", 1)[1]
    hints = {
        "connect": "Отправьте `/connect` — появится список активных terminal\\-сессий с кнопками\\.",
        "sessions": "Отправьте `/sessions` — покажу все сессии \\(🔁 auto\\-continue, 🐧 WSL\\)\\.",
        "prompts": (
            "*Шаблоны промптов*\n"
            "`/prompts` — список с кнопками\n"
            "`/prompt <имя>` — отправить шаблон\n"
            "Пришлите `\\.md`/`\\.txt` с подписью `\\#prompt` — сохранится как шаблон\\."
        ),
        "usage": "Отправьте `/usage` — покажу расход токенов за 5ч / 24ч / всё время\\.",
        "full": None,  # handled below
    }
    if action == "full":
        # Fake a /help invocation: edit the message with the grouped reference
        from bot.message_formatter import escape_markdown_v2  # noqa: F401 (kept for clarity)
        # Build same text as cmd_help; dedup by calling cmd_help's body inline
        await query.edit_message_text(
            "Вводите /help — полный список команд\\.\n"
            "Или выберите действие кнопкой ниже\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    msg = hints.get(action, "Неизвестный раздел\\.")
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


async def _handle_prompt_callback(
    update: Update, query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle prompt template selection — send content to active session."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    config: Config = context.bot_data["config"]

    try:
        idx = int(query.data.split(":", 1)[1])
    except ValueError:
        await query.edit_message_text("Неверный выбор\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    names = context.user_data.get("prompt_list") or []
    if idx < 0 or idx >= len(names):
        # List may be stale — refresh and retry
        names = prompts_module.list_prompts(config.prompts_dir)
        if idx < 0 or idx >= len(names):
            await query.edit_message_text(
                "Шаблон не найден \\(список устарел\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

    name = names[idx]
    try:
        content = prompts_module.read_prompt(config.prompts_dir, name)
    except ValueError as e:
        await query.edit_message_text(
            format_error(str(e)), parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    # Find active session (same logic as handle_message)
    session = None
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
            context.user_data["pending_prompt"] = content
            await query.edit_message_text(
                "Выберите сессию:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        else:
            await query.edit_message_text(
                "Нет активных сессий\\. `/connect` или `/new`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

    await query.edit_message_text(
        f"\u23f3 Отправляю шаблон `{escape_markdown_v2(name)}` в "
        f"*{escape_markdown_v2(session['name'])}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await _resume_and_reply(update, context, session, content, edit_message=query.message)


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

    # Detect usage/rate limit and offer to queue the prompt for later
    if response.error and limit_detector.is_limit_error(response.error):
        await _offer_pending_resume(update, context, session, prompt, response.error)
        return

    status = "error" if response.error else "waiting"
    notification = format_notification(
        session["name"], session["work_dir"], response, status
    )

    chunks = split_message(notification, config.max_message_length)
    chat_id = update.effective_chat.id

    # If the response is very long, send the full text as an attachment plus
    # a short header — beats stitching together 10+ message chunks.
    if not response.error and response.text and len(response.text) > 12000:
        await _send_response_as_document(context, chat_id, session, response)
        return

    last_msg = None
    for chunk in chunks:
        last_msg = await _safe_send(context.bot, chat_id, chunk)

    # Update session with last message ID (scoped by chat)
    if last_msg:
        await session_mgr.update_tg_message(
            session["id"], last_msg.message_id, tg_chat_id=chat_id,
        )


async def _send_response_as_document(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    session: dict,
    response,
) -> None:
    """Send an over-12k-char Claude response as a file attachment with a short header."""
    from io import BytesIO

    session_mgr: SessionManager = context.bot_data["session_mgr"]
    preview = response.text[:500]
    header = (
        f"\U0001f4e6 *Длинный ответ от* `{escape_markdown_v2(session['name'])}` "
        f"\\({len(response.text)} символов\\) отправлен файлом\\.\n\n"
        f"*Превью:*\n{escape_markdown_v2(preview)}\\.\\.\\."
    )
    last_msg = await _safe_send(context.bot, chat_id, header)
    buf = BytesIO(response.text.encode("utf-8"))
    buf.name = f"{session['name']}-response.md"
    try:
        await context.bot.send_document(
            chat_id=chat_id,
            document=buf,
            filename=buf.name,
        )
    except Exception:
        logger.exception("Failed to send long response as document")
        # Fall back to chunked send
        for chunk in split_message(response.text, 4000):
            last_msg = await _safe_send(context.bot, chat_id, chunk)
    if last_msg:
        await session_mgr.update_tg_message(
            session["id"], last_msg.message_id, tg_chat_id=chat_id,
        )


async def _offer_pending_resume(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict,
    prompt: str,
    error: str,
) -> None:
    """On limit error, create a pending_prompt entry and let the user pick the mode."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    chat_id = update.effective_chat.id

    retry_at = limit_detector.parse_reset_time(error)
    retry_at_iso = retry_at.strftime("%Y-%m-%d %H:%M:%S")

    # Default mode is "auto" — fire-and-forget: resume automatically at reset.
    pending_id = await db_module.create_pending_prompt(
        session_mgr.conn, session["id"], chat_id, prompt, retry_at_iso, "auto",
    )

    wait_seconds = max(0, (retry_at - datetime.now(UTC)).total_seconds())
    wait_str = _format_elapsed(wait_seconds)
    name_esc = escape_markdown_v2(session["name"])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "\U0001f514 Только напомнить",
            callback_data=f"lim:manual:{pending_id}",
        )],
        [InlineKeyboardButton(
            "\u274c Отменить",
            callback_data=f"lim:cancel:{pending_id}",
        )],
    ])

    await update.message.reply_text(
        (
            f"\U0001f7e1 *Лимит достигнут для* `{name_esc}`\n"
            f"Сброс через: {escape_markdown_v2(wait_str)} "
            f"\\(`{escape_markdown_v2(retry_at_iso)} UTC`\\)\n\n"
            f"\U0001f504 *Автовозобновление включено* \\- сообщение отправится само после сброса\\.\n"
            f"Не нравится? Выберите вариант ниже\\."
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )


async def _handle_limit_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle limit-mode selection callback: lim:<mode>:<pending_id>."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _, mode, pid_str = parts
    try:
        pending_id = int(pid_str)
    except ValueError:
        return

    pending = await db_module.get_pending_prompt(session_mgr.conn, pending_id)
    if not pending:
        await query.edit_message_text(
            "Запись отложенного сообщения не найдена\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if mode == "cancel":
        await db_module.delete_pending_prompt(session_mgr.conn, pending_id)
        await query.edit_message_text(
            "\u274c Отложенное сообщение удалено\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if mode not in ("auto", "manual"):
        return

    await session_mgr.conn.execute(
        "UPDATE pending_prompts SET mode=? WHERE id=?", (mode, pending_id)
    )
    await session_mgr.conn.commit()

    label = "Auto-resume" if mode == "auto" else "Напоминание"
    await query.edit_message_text(
        f"\u2705 Режим: *{escape_markdown_v2(label)}*\\. "
        f"Сработает при сбросе лимита \\(`{escape_markdown_v2(pending['retry_at'])} UTC`\\)",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_resume_pending_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Resume now' click on a manual-mode pending notification."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        return
    try:
        pending_id = int(parts[1])
    except ValueError:
        return

    pending = await db_module.get_pending_prompt(session_mgr.conn, pending_id)
    if not pending:
        await query.edit_message_text(
            "Запись не найдена \\(возможно, уже обработана\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session = await db_module.get_session(session_mgr.conn, pending["session_id"])
    if not session:
        await db_module.delete_pending_prompt(session_mgr.conn, pending_id)
        await query.edit_message_text(
            "Сессия удалена\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    await query.edit_message_text(
        f"\u23f3 Возобновляю `{escape_markdown_v2(session['name'])}`\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        await session_mgr.resume_session(pending["session_id"], pending["prompt"])
    except Exception as e:
        logger.exception("Manual resume failed")
        await query.edit_message_text(
            format_error(f"Не удалось возобновить: {e}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    finally:
        await db_module.delete_pending_prompt(session_mgr.conn, pending_id)

    await query.edit_message_text(
        f"\u2705 `{escape_markdown_v2(session['name'])}` возобновлена\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_resume_cancel_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Cancel' click on a manual-mode pending notification."""
    from bot import db as db_module
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    parts = query.data.split(":", 1)
    try:
        pending_id = int(parts[1])
    except (ValueError, IndexError):
        return

    await db_module.delete_pending_prompt(session_mgr.conn, pending_id)
    await query.edit_message_text(
        "\u274c Отложенное сообщение отменено\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


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


_SENSITIVE_NAMES = {
    # Bot & project secrets
    "config.json", ".env", "claude_tg.db",
    ".env.local", ".env.production", ".env.development", ".env.staging",
    "secrets.json", "credentials.json", "credentials",
    # SSH / PGP / cloud
    "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa", "known_hosts",
    ".pgpass", ".bashrc", ".zshrc", ".bash_history", ".zsh_history",
    ".npmrc", ".pypirc", ".netrc", ".gitconfig",
    # CMS/framework common leaks
    "wp-config.php", "local.xml", "parameters.yml",
}
_SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer",
    ".gpg", ".kdbx", ".kdb", ".keychain", ".asc",
}
_SENSITIVE_DIRS = {
    ".git", ".ssh", ".aws", ".gnupg", ".gpg", ".gcloud",
    ".azure", ".kube", ".docker", ".vscode", ".idea",
}
MAX_FILE_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


def _resolve_work_path(session: dict, rel_path: str) -> Path:
    """Resolve a relative path against session's work_dir, handling WSL.

    Raises ValueError on:
      - path traversal (escape via `..` or absolute path or out-of-tree symlink)
      - sensitive file names / suffixes / directory components
    Uses os.path.realpath which follows symlinks — any symlink pointing outside
    the work dir will fail the boundary check.
    """
    work_dir = session["work_dir"]
    wsl_distro = session.get("wsl_distro", "")

    if wsl_distro:
        from bot.providers.claude import _wsl_path_to_windows
        base = _wsl_path_to_windows(wsl_distro, work_dir)
    else:
        base = Path(work_dir)

    # realpath to follow symlinks (so any escape via symlink is neutralised)
    resolved = Path(os.path.realpath(base / rel_path))
    base_resolved = Path(os.path.realpath(base))

    # Boundary check: os.sep suffix prevents /project_evil matching /project
    if resolved != base_resolved and not str(resolved).startswith(str(base_resolved) + os.sep):
        raise ValueError("Выход за пределы рабочей директории")

    name_lower = resolved.name.lower()
    if name_lower in _SENSITIVE_NAMES:
        raise ValueError(f"Доступ к {resolved.name} запрещён")
    if resolved.suffix.lower() in _SENSITIVE_SUFFIXES:
        raise ValueError(f"Доступ к {resolved.suffix} файлам запрещён")

    # Reject anything inside sensitive subdirectories
    try:
        rel_parts = resolved.relative_to(base_resolved).parts
    except ValueError:
        rel_parts = ()
    for part in rel_parts[:-1]:  # skip leaf itself
        if part.lower() in _SENSITIVE_DIRS:
            raise ValueError(f"Каталог {part} запрещён")

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
        file_size = file_path.stat().st_size
    except OSError as e:
        await _safe_reply(update.message, format_error(f"stat: {e}"))
        return
    if file_size > MAX_FILE_DOWNLOAD_BYTES:
        await _safe_reply(
            update.message,
            format_error(
                f"Файл слишком большой: {file_size // (1024*1024)} МБ "
                f"\\(лимит {MAX_FILE_DOWNLOAD_BYTES // (1024*1024)} МБ\\)"
            ),
        )
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
    """Handle file uploads — save to active session's work_dir, or to prompts/ if caption is #prompt."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]
    config: Config = context.bot_data["config"]

    doc = update.message.document
    if not doc:
        return

    caption = (update.message.caption or "").strip().lower()
    if caption.startswith("#prompt"):
        await _save_as_prompt(update, context, doc, config)
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


async def _save_as_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, doc, config: Config
) -> None:
    """Save uploaded document as a prompt template."""
    filename = doc.file_name or "prompt.md"

    if doc.file_size and doc.file_size > prompts_module.MAX_FILE_SIZE:
        await _safe_reply(
            update.message,
            format_error(f"Файл больше {prompts_module.MAX_FILE_SIZE // 1024} КБ"),
        )
        return

    try:
        name = prompts_module.validate_filename(filename)
    except ValueError as e:
        await _safe_reply(update.message, format_error(str(e)))
        return

    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(name).suffix) as tf:
        tmp_path = tf.name

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(tmp_path)
        content = Path(tmp_path).read_bytes()
    except Exception as e:
        logger.exception("Failed to download prompt file")
        await _safe_reply(update.message, format_error(f"Ошибка загрузки: {e}"))
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    try:
        prompts_module.save_prompt(config.prompts_dir, name, content)
    except ValueError as e:
        await _safe_reply(update.message, format_error(str(e)))
        return

    await _safe_reply(
        update.message,
        f"\u2705 Шаблон `{escape_markdown_v2(name)}` сохранён\\.\n"
        f"Отправить: `/prompt {escape_markdown_v2(name)}` или `/prompts`",
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


_BOT_START_TIME = datetime.now(UTC)


@authorized
async def cmd_botstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /botstatus — health check: uptime, active processes, session counts."""
    from bot import db as db_module
    from bot.providers import _tracking

    session_mgr: SessionManager = context.bot_data["session_mgr"]
    conn = session_mgr.conn

    uptime_s = (datetime.now(UTC) - _BOT_START_TIME).total_seconds()
    active_procs = _tracking.active_count()
    sessions = await db_module.get_all_sessions(conn)
    by_status: dict[str, int] = {}
    for s in sessions:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    pending = await db_module.list_pending_prompts(conn)
    owners = ", ".join(str(x) for x in context.bot_data["config"].allowed_chat_ids)

    lines = [
        "*Bot status*",
        "Версия: *0\\.2\\.0*",
        f"Uptime: {_format_elapsed(uptime_s)}",
        f"Активных CLI\\-процессов: {active_procs}",
        f"Сессий в БД: {len(sessions)} " + ", ".join(
            f"{escape_markdown_v2(k)}\\={v}" for k, v in sorted(by_status.items())
        ),
        f"Отложенных промптов: {len(pending)}",
        f"Owners: `{escape_markdown_v2(owners)}`",
    ]
    await _safe_reply(update.message, "\n".join(lines))


@authorized
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /debug — show provider diagnostics."""
    session_mgr: SessionManager = context.bot_data["session_mgr"]

    lines = ["*Диагностика провайдеров:*\n"]

    for name, provider in session_mgr._providers.items():
        lines.append(f"*{escape_markdown_v2(name)}:*")
        try:
            sessions = await asyncio.to_thread(provider.list_sessions)
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
            lines.append("  *Детали:*")
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
    app.bot_data["rate_limiter"] = RateLimiter(config.rate_limit_per_minute)

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

    async def on_terminal_limit(session_id: str, raw_text: str) -> None:
        """Watcher detected a limit in a terminal session — queue auto-continuation if opted in."""
        from bot import db as db_module

        conn = app.bot_data.get("db_conn")
        if not conn:
            return
        session = await db_module.get_session(conn, session_id)
        if not session or not session.get("auto_continue"):
            return
        existing = await db_module.get_pending_by_session(conn, session_id)
        if existing:
            return

        owner_ids = config.allowed_chat_ids
        if not owner_ids:
            return

        retry_at = limit_detector.parse_reset_time(raw_text)
        retry_at_iso = retry_at.strftime("%Y-%m-%d %H:%M:%S")

        pending_id = await db_module.create_pending_prompt(
            conn, session_id, owner_ids[0],
            config.auto_continue_prompt, retry_at_iso, "auto",
        )

        name_esc = escape_markdown_v2(session["name"])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "\u274c Отменить автопродолжение",
                callback_data=f"resm_cancel:{pending_id}",
            ),
        ]])
        text = (
            f"\U0001f504 *Лимит в терминальной сессии* `{name_esc}`\n"
            f"Автопродолжение после сброса в `{escape_markdown_v2(retry_at_iso)} UTC`"
        )
        for chat_id in owner_ids:
            try:
                await app.bot.send_message(
                    chat_id, text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("Failed to send auto-continue notification")

    session_mgr.set_limit_callback(on_terminal_limit)

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
    app.add_handler(CommandHandler("botstatus", cmd_botstatus))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("autocontinue", cmd_autocontinue))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("prompts", cmd_prompts))
    app.add_handler(CommandHandler("prompt", cmd_prompt))
    app.add_handler(CommandHandler("prompt_del", cmd_prompt_del))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Catch-all for unsupported types (stickers, GIFs, voice, video, etc.)
    app.add_handler(MessageHandler(~filters.COMMAND, handle_unsupported))

    logger.info("Telegram handlers registered")
