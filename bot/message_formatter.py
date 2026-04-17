import logging
import re

from bot.claude_runner import ClaudeResponse

logger = logging.getLogger(__name__)

STATUS_EMOJI = {
    "running": "\u23f3",   # ⏳
    "waiting": "\U0001f535", # 🔵
    "done": "\U0001f7e2",   # 🟢
    "error": "\U0001f534",  # 🔴
}

# Characters that must be escaped in Telegram MarkdownV2
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"
_ESCAPE_RE = re.compile(r"([" + re.escape(_ESCAPE_CHARS) + r"])")


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2, preserving code blocks."""
    parts = []
    segments = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)

    for i, segment in enumerate(segments):
        if i % 2 == 1:
            # Inside code block — don't escape
            parts.append(segment)
        else:
            parts.append(_ESCAPE_RE.sub(r"\\\1", segment))

    return "".join(parts)


def split_message(text: str, max_length: int = 4000) -> list[str]:
    """Split long text into chunks respecting code blocks and word boundaries."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    remaining = text
    in_code_block = False

    while remaining:
        if len(remaining) <= max_length:
            chunk = remaining
            remaining = ""
        else:
            # Find a good split point
            chunk = remaining[:max_length]
            split_at = max_length

            # Try to split at paragraph boundary
            last_para = chunk.rfind("\n\n")
            if last_para > max_length // 4:
                split_at = last_para + 1
            else:
                # Try line boundary
                last_line = chunk.rfind("\n")
                if last_line > max_length // 4:
                    split_at = last_line + 1
                else:
                    # Try word boundary
                    last_space = chunk.rfind(" ")
                    if last_space > max_length // 4:
                        split_at = last_space + 1

            chunk = remaining[:split_at]
            remaining = remaining[split_at:]

        # Track code block state across chunks
        # Count only standalone ``` (not inside inline code)
        triple_count = chunk.count("```")
        if in_code_block:
            # Previous chunk left a code block open — reopen it
            chunk = "```\n" + chunk
            triple_count += 1

        if triple_count % 2 == 1:
            # Odd number of ``` means a block is left open — close it
            chunk += "\n```"
            in_code_block = True
        else:
            in_code_block = False

        chunks.append(chunk.strip())

    return [c for c in chunks if c]


def format_notification(
    session_name: str,
    work_dir: str,
    response: ClaudeResponse,
    status: str = "waiting",
) -> str:
    """Format a Claude response as a Telegram notification."""
    emoji = STATUS_EMOJI.get(status, "\u2753")
    duration = f"{response.duration_seconds:.0f}"

    header = (
        f"{emoji} *Сессия:* `{escape_markdown_v2(session_name)}`\n"
        f"\U0001f4c1 `{escape_markdown_v2(work_dir)}`\n"
        f"\u23f1 {escape_markdown_v2(duration)} сек\n"
    )

    if response.error:
        body = f"\n\U0001f534 *Ошибка:*\n{escape_markdown_v2(response.error)}"
        _, hint = classify_error(response.error)
        if hint:
            body += f"\n\n\U0001f4a1 {escape_markdown_v2(hint)}"
    else:
        body = (
            f"\n\u2500\u2500\u2500 Результат \u2500\u2500\u2500\n"
            f"{escape_markdown_v2(response.text)}\n"
            f"\u2500\u2500\u2500 Конец \u2500\u2500\u2500"
        )

    footer = escape_markdown_v2(
        "\n\n↩️ Ответьте на это сообщение чтобы продолжить"
    )

    return header + body + footer


def format_session_list(sessions: list[dict]) -> str:
    """Format list of sessions for Telegram."""
    if not sessions:
        return "Нет активных сессий\\."

    lines = ["*Активные сессии:*\n"]
    for i, s in enumerate(sessions, 1):
        emoji = STATUS_EMOJI.get(s["status"], "\u2753")
        name = escape_markdown_v2(s["name"])
        lines.append(f"{i}\\. {emoji} `{name}` \\- {escape_markdown_v2(s['status'])}")

    return "\n".join(lines)


def format_error(error: str) -> str:
    """Format an error message for Telegram."""
    return f"\U0001f534 *Ошибка:* {escape_markdown_v2(error)}"


# ---------------------------------------------------------------------------
# Error classification — turn opaque provider errors into actionable hints.
# ---------------------------------------------------------------------------

_ERROR_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"not logged in|api key|authentication", re.IGNORECASE),
        "auth",
        "CLI не авторизован. Откройте терминал и выполните `claude` (или `codex`) чтобы войти.",
    ),
    (
        re.compile(r"rate limit|usage limit|too many requests|quota exceeded", re.IGNORECASE),
        "limit",
        "Достигнут лимит провайдера. Бот поставит сообщение в очередь автоматически — смотрите /pending.",
    ),
    (
        re.compile(r"timeout|timed out", re.IGNORECASE),
        "timeout",
        "Таймаут CLI. Увеличьте `subprocess_timeout_minutes` в config.json или разбейте задачу.",
    ),
    (
        re.compile(r"not found|no such file|cannot access", re.IGNORECASE),
        "filesystem",
        "Файл или каталог не найден. Проверьте путь и права доступа.",
    ),
    (
        re.compile(r"network|connection|ECONNREFUSED|getaddrinfo", re.IGNORECASE),
        "network",
        "Проблема с сетью. Проверьте интернет и попробуйте снова.",
    ),
    (
        re.compile(r"permission denied|EACCES", re.IGNORECASE),
        "permission",
        "Нет прав доступа. Проверьте, что бот запущен под правильным пользователем.",
    ),
    (
        re.compile(r"JSONDecodeError|unexpected token|invalid json", re.IGNORECASE),
        "parse",
        "CLI вернул неожиданный формат. Возможна несовместимая версия claude/codex — обновите CLI.",
    ),
]


def classify_error(error: str) -> tuple[str, str | None]:
    """Return (category, suggested_action) for a raw provider error string.

    Category is one of: auth, limit, timeout, filesystem, network, permission,
    parse, unknown. The action is a short user-facing hint, or None if no rule
    matched.
    """
    if not error:
        return "unknown", None
    for pattern, category, hint in _ERROR_RULES:
        if pattern.search(error):
            return category, hint
    return "unknown", None


def format_error_with_hint(error: str) -> str:
    """Format an error plus a classified hint when possible."""
    category, hint = classify_error(error)
    base = f"\U0001f534 *Ошибка:* {escape_markdown_v2(error)}"
    if hint:
        return base + f"\n\n\U0001f4a1 {escape_markdown_v2(hint)}"
    return base
