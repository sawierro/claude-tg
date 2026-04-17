"""Thin wrapper — preserves old imports. Real logic in providers/claude.py."""
from bot.config import Config
from bot.providers.base import ProviderSession
from bot.providers.claude import ClaudeProvider

# Backwards-compatible alias
ExternalSession = ProviderSession


def list_external_sessions() -> list[ProviderSession]:
    """List Claude sessions from terminals."""
    config = Config(telegram_token="")
    provider = ClaudeProvider(config)
    return provider.list_sessions()


def find_session_by_query(query: str) -> ProviderSession | None:
    """Find Claude session by ID prefix or slug."""
    config = Config(telegram_token="")
    provider = ClaudeProvider(config)
    return provider.find_session(query)
