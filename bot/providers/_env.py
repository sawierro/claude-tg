"""Build a minimal, sanitised environment for CLI subprocesses.

Rationale
---------
Inheriting the entire bot process environment leaks secrets (TELEGRAM_TOKEN,
AWS_*, GITHUB_TOKEN, OPENAI_API_KEY, etc.) into Claude/Codex child processes.
With `--dangerously-skip-permissions` / `--yolo` flags enabled, a prompt-
injection payload can trivially read env vars and exfiltrate them via tool
calls. We therefore whitelist a small set of vars strictly needed for the CLI
to run (PATH, HOME, locale, Anthropic/OpenAI auth) and drop everything else.
"""
from __future__ import annotations

import os

_ALWAYS_INCLUDE = {
    # Process basics
    "PATH", "PATHEXT", "HOME", "USER", "USERNAME",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    # Windows
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA",
    "COMSPEC", "USERPROFILE", "HOMEPATH", "HOMEDRIVE",
    # POSIX
    "SHELL", "LOGNAME", "PWD",
    # Node tooling (npm, nvm)
    "NODE_PATH", "NODE_OPTIONS", "NVM_DIR", "NVM_BIN",
    # Terminal
    "TERM", "COLORTERM",
}

# Prefixes whose env vars we pass through (Anthropic/OpenAI auth lives here)
_PASS_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "OPENAI_", "CODEX_")

# Explicitly block even if they match a prefix/whitelist (defensive)
_ALWAYS_BLOCK = {
    "TELEGRAM_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "BOT_TOKEN",
}


def build_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a minimal env dict safe to pass to a Claude/Codex subprocess."""
    current = os.environ
    env: dict[str, str] = {}
    for key, value in current.items():
        if key in _ALWAYS_BLOCK:
            continue
        upper = key.upper()
        if upper in _ALWAYS_INCLUDE or any(upper.startswith(p) for p in _PASS_PREFIXES):
            env[key] = value
    if extra:
        env.update(extra)
    # Final sweep: guarantee blocked keys are never present
    for bad in _ALWAYS_BLOCK:
        env.pop(bad, None)
    return env
