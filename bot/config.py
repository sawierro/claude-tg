import json
import os
import logging
import platform
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"


@dataclass
class Config:
    telegram_token: str
    allowed_chat_ids: list[int] = field(default_factory=list)
    default_work_dir: str = "."
    claude_path: str = "claude.cmd" if platform.system() == "Windows" else "claude"
    claude_flags: list[str] = field(default_factory=list)
    max_message_length: int = 4000
    session_timeout_hours: int = 24
    subprocess_timeout_minutes: int = 0  # 0 = no timeout
    prompts_dir: str = "prompts"
    auto_continue_prompt: str = "Продолжи с того места, где остановился."

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        """Save current config to JSON file (token excluded — use .env)."""
        data = {
            "allowed_chat_ids": self.allowed_chat_ids,
            "default_work_dir": self.default_work_dir,
            "claude_path": self.claude_path,
            "claude_flags": self.claude_flags,
            "max_message_length": self.max_message_length,
            "session_timeout_hours": self.session_timeout_hours,
            "subprocess_timeout_minutes": self.subprocess_timeout_minutes,
            "prompts_dir": self.prompts_dir,
            "auto_continue_prompt": self.auto_continue_prompt,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        logger.info("Config saved to %s", path)


def load_config(path: str | None = None) -> Config:
    """Load config from JSON file and environment variables."""
    load_dotenv()

    config_path = path or DEFAULT_CONFIG_PATH
    data = {}

    if Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Config loaded from %s", config_path)
    else:
        logger.warning("Config file %s not found, using defaults + env vars", config_path)

    # Env vars override config file
    token = os.environ.get("TELEGRAM_TOKEN", data.get("telegram_token", ""))
    if not token:
        raise ValueError(
            "TELEGRAM_TOKEN is required. Set it in .env file or config.json."
        )

    return Config(
        telegram_token=token,
        allowed_chat_ids=data.get("allowed_chat_ids", []),
        default_work_dir=data.get("default_work_dir", "."),
        claude_path=data.get("claude_path", "claude"),
        claude_flags=data.get("claude_flags", []),
        max_message_length=data.get("max_message_length", 4000),
        session_timeout_hours=data.get("session_timeout_hours", 24),
        subprocess_timeout_minutes=data.get("subprocess_timeout_minutes", 0),
        prompts_dir=data.get("prompts_dir", "prompts"),
        auto_continue_prompt=data.get(
            "auto_continue_prompt",
            "Продолжи с того места, где остановился.",
        ),
    )
