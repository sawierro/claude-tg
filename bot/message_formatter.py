import re
import logging

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

        # Ensure code blocks are closed
        open_blocks = chunk.count("```")
        if open_blocks % 2 == 1:
            chunk += "\n```"
            in_code_block = True
        elif in_code_block:
            chunk = "```\n" + chunk
            in_code_block = chunk.count("```") % 2 == 1

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
