"""Thin wrapper — preserves old imports. Real logic in providers/claude.py."""
from dataclasses import dataclass

from bot.config import Config


# Backwards-compatible alias
@dataclass
class ClaudeResponse:
    session_id: str
    text: str
    cost: float | None
    duration_seconds: float
    error: str | None


def _build_command(prompt: str, config: Config, session_id: str | None = None) -> str:
    """Kept for test compatibility."""
    from bot.providers.claude import ClaudeProvider
    provider = ClaudeProvider(config)
    return provider._build_command(prompt, session_id)


def _parse_response(raw: str, fallback_session_id: str | None, duration: float) -> ClaudeResponse:
    """Kept for test compatibility."""
    from bot.providers.claude import ClaudeProvider
    config = Config(telegram_token="")
    provider = ClaudeProvider(config)
    r = provider._parse_response(raw, fallback_session_id, duration)
    return ClaudeResponse(
        session_id=r.session_id,
        text=r.text,
        cost=r.cost,
        duration_seconds=r.duration_seconds,
        error=r.error,
    )


async def run_claude(
    prompt: str, work_dir: str, config: Config, session_id: str | None = None
) -> ClaudeResponse:
    """Kept for backward compatibility."""
    from bot.providers.claude import ClaudeProvider
    provider = ClaudeProvider(config)
    r = await provider.run(prompt, work_dir, session_id)
    return ClaudeResponse(
        session_id=r.session_id,
        text=r.text,
        cost=r.cost,
        duration_seconds=r.duration_seconds,
        error=r.error,
    )
