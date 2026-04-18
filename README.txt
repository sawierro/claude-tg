CLAUDE-TG

Telegram bot for remote control of Claude Code / OpenAI Codex CLI
sessions from your phone.

This plain-text file is a minimal pointer — the canonical docs are in
README.md (markdown rendering with diagrams) and SECURITY.md (threat
model). Please read them before running the bot in any shared
environment.

====================================================================

IMPORTANT: SECURITY

The bot runs `claude` / `codex` with full filesystem access under your
user account. By default it ships with
`--dangerously-skip-permissions`/`--yolo`, which means anyone who can
send a message to the bot can execute arbitrary code and read
arbitrary files as you. Treat bot access as equivalent to SSH.

Before deploying:

- Set OWNER_CHAT_ID in .env (bot refuses to start without an owner —
  auto-registration of the first caller was removed in 0.2.0 to
  prevent hijacking).
- Read SECURITY.md.

====================================================================

QUICK START

  git clone https://github.com/sawierro/claude-tg.git
  cd claude-tg
  cp .env.example .env          # edit TELEGRAM_TOKEN + OWNER_CHAT_ID
  ./setup.sh                    # or setup.bat on Windows
  ./start.sh                    # or start.bat

Then open your bot in Telegram and send /help.

====================================================================

DOCS

  README.md         — full guide with diagrams and command reference
  SECURITY.md       — threat model, access / sandboxing / file rules
  CHANGELOG.md      — release notes
