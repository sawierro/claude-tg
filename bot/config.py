import json
import logging
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_AUTO_CONTINUE_PROMPT = "Продолжи с того места, где остановился."


@dataclass
class Config:
    telegram_token: str
    allowed_chat_ids: list[int] = field(default_factory=list)
    default_work_dir: str = "."
    claude_path: str = "claude.cmd" if platform.system() == "Windows" else "claude"
    claude_flags: list[str] = field(default_factory=list)
    codex_path: str = "codex.cmd" if platform.system() == "Windows" else "codex"
    codex_flags: list[str] = field(default_factory=list)
    max_message_length: int = 4000
    session_timeout_hours: int = 24
    subprocess_timeout_minutes: int = 30
    prompts_dir: str = "prompts"
    auto_continue_prompt: str = DEFAULT_AUTO_CONTINUE_PROMPT
    rate_limit_per_minute: int = 30
    concurrent_sessions: int = 3

    def validate(self) -> None:
        """Validate config values. Raises ValueError on invalid input."""
        if self.subprocess_timeout_minutes < 0:
            raise ValueError("subprocess_timeout_minutes must be >= 0")
        if self.max_message_length <= 0 or self.max_message_length > 4096:
            raise ValueError("max_message_length must be in 1..4096")
        if self.session_timeout_hours <= 0:
            raise ValueError("session_timeout_hours must be > 0")
        if self.rate_limit_per_minute <= 0:
            raise ValueError("rate_limit_per_minute must be > 0")
        if self.concurrent_sessions <= 0:
            raise ValueError("concurrent_sessions must be > 0")
        if not isinstance(self.allowed_chat_ids, list) or not all(
            isinstance(x, int) for x in self.allowed_chat_ids
        ):
            raise ValueError("allowed_chat_ids must be a list of integers")

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        """Save current config to JSON file (token NEVER written)."""
        data = {
            "allowed_chat_ids": self.allowed_chat_ids,
            "default_work_dir": self.default_work_dir,
            "claude_path": self.claude_path,
            "claude_flags": self.claude_flags,
            "codex_path": self.codex_path,
            "codex_flags": self.codex_flags,
            "max_message_length": self.max_message_length,
            "session_timeout_hours": self.session_timeout_hours,
            "subprocess_timeout_minutes": self.subprocess_timeout_minutes,
            "prompts_dir": self.prompts_dir,
            "auto_continue_prompt": self.auto_continue_prompt,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "concurrent_sessions": self.concurrent_sessions,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        _restrict_permissions(path)
        logger.info("Config saved to %s", path)


def _restrict_permissions(path: str) -> None:
    """Set file permissions to 0600 on POSIX. No-op on Windows."""
    if platform.system() == "Windows":
        return
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning("Could not chmod %s to 0600: %s", path, e)


def _parse_owner_ids(raw: str | None, data: dict) -> list[int]:
    """Parse owner chat IDs from env (comma-separated) or config.json list."""
    if raw:
        ids = []
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                ids.append(int(piece))
            except ValueError as e:
                raise ValueError(f"Invalid chat id in OWNER_CHAT_ID: {piece!r}") from e
        return ids
    raw_list = data.get("allowed_chat_ids", [])
    if not isinstance(raw_list, list):
        raise ValueError("allowed_chat_ids in config.json must be a list")
    return [int(x) for x in raw_list]


def load_config(path: str | None = None) -> Config:
    """Load config from JSON file + environment variables.

    Token is read ONLY from TELEGRAM_TOKEN env var. If found inside config.json,
    we log a warning and ignore it (it must not live in the JSON file).
    Owner chat IDs can be supplied via OWNER_CHAT_ID env (comma-separated list)
    or allowed_chat_ids in config.json. If neither is set, the bot refuses to
    start — legacy "first message auto-registers" behaviour is removed.
    """
    load_dotenv()

    config_path = path or DEFAULT_CONFIG_PATH
    data: dict = {}

    if Path(config_path).exists():
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Config loaded from %s", config_path)
    else:
        logger.warning("Config file %s not found, using defaults + env vars", config_path)

    if "telegram_token" in data and data["telegram_token"]:
        logger.warning(
            "telegram_token found in %s — IGNORING. "
            "Move it to .env (TELEGRAM_TOKEN=...) and remove from JSON.",
            config_path,
        )

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "TELEGRAM_TOKEN is required — set it in .env, not in config.json."
        )

    allowed_chat_ids = _parse_owner_ids(os.environ.get("OWNER_CHAT_ID"), data)
    if not allowed_chat_ids:
        raise ValueError(
            "No owner configured. Set OWNER_CHAT_ID=<your_chat_id> in .env "
            "(or allowed_chat_ids in config.json). The bot no longer "
            "auto-registers the first caller."
        )

    cfg = Config(
        telegram_token=token,
        allowed_chat_ids=allowed_chat_ids,
        default_work_dir=data.get("default_work_dir", "."),
        claude_path=data.get(
            "claude_path",
            "claude.cmd" if platform.system() == "Windows" else "claude",
        ),
        claude_flags=list(data.get("claude_flags", [])),
        codex_path=data.get(
            "codex_path",
            "codex.cmd" if platform.system() == "Windows" else "codex",
        ),
        codex_flags=list(data.get("codex_flags", [])),
        max_message_length=data.get("max_message_length", 4000),
        session_timeout_hours=data.get("session_timeout_hours", 24),
        subprocess_timeout_minutes=data.get("subprocess_timeout_minutes", 30),
        prompts_dir=data.get("prompts_dir", "prompts"),
        auto_continue_prompt=data.get("auto_continue_prompt", DEFAULT_AUTO_CONTINUE_PROMPT),
        rate_limit_per_minute=data.get("rate_limit_per_minute", 30),
        concurrent_sessions=data.get("concurrent_sessions", 3),
    )
    cfg.validate()
    return cfg
