# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-18 (unreleased)

Security hardening release aligned with `claude_tg_quality_criteria.md`.

### Security

- **BREAKING: auto-registration of the first caller removed.** Owner must be
  configured explicitly via `OWNER_CHAT_ID` env var (comma-separated list) or
  `allowed_chat_ids` in `config.json`. Without one the bot refuses to start.
- `--dangerously-skip-permissions` / `--yolo` are no longer hardcoded in the
  providers. They now live in `Config.claude_flags` / `codex_flags` and ship
  in `config.example.json` so behaviour is unchanged by default but removable.
- Telegram bot token is read only from the `TELEGRAM_TOKEN` environment
  variable. Any `telegram_token` key in `config.json` is now ignored with a
  logged warning.
- Subprocess environment is minimised via `bot/providers/_env.py`. Only
  `PATH`, `HOME`, locale, Node tooling, and `CLAUDE_*` / `ANTHROPIC_*` /
  `OPENAI_*` / `CODEX_*` prefixes pass through. `TELEGRAM_TOKEN` /
  `BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` are always stripped.
- File-access boundary (`/get`, uploads) uses `os.path.realpath` (follows
  symlinks) plus an extended sensitive-file blocklist (SSH keys, `.bashrc`,
  `.npmrc`, `.aws/*`, `.gnupg/*`, etc.), blocked suffixes (`.pem`, `.key`,
  `.p12`, `.pfx`, `.crt`, `.gpg`, `.kdbx`, `.asc`), and blocked directory
  components (`.git`, `.ssh`, `.aws`, `.gnupg`, `.docker`, `.kube`, ...).
- `/get` cap raised to 25 MB with a size check before reading.
- WSL prompt argv is now built with `shlex.quote` instead of ad-hoc string
  replace. Flags from `claude_flags` / `codex_flags` are quoted per item.
- Per-chat rate limiter (`bot/rate_limiter.py`) applied in the `@authorized`
  decorator. Default 30 msg/min, configurable via `rate_limit_per_minute`.
- Active CLI subprocesses are tracked centrally
  (`bot/providers/_tracking.py`). `/cancel` and shutdown now actually kill
  in-flight `claude` / `codex` processes; no more orphans.
- `config.json` is written with `0600` permissions on POSIX.
- Denied users get one explicit "access denied" reply — no more silent
  ignore.

### Reliability

- SQLite migrations now go through a numbered `schema_version` table. The
  old `try/except ALTER TABLE` chain was removed; a backfill pass stamps
  existing installs as version 1 before applying new migrations.
- New `bot.db.tx()` async transaction helper (BEGIN / COMMIT / ROLLBACK).
- WAL checkpoint runs hourly via `bot/maintenance.py` to prevent
  unbounded WAL growth.
- `SessionWatcher` now auto-restarts on crash with exponential backoff
  (5s → 60s, capped at 10 attempts).
- `get_session_by_tg_message()` accepts an optional `chat_id` — reply
  routing is now scoped per chat to prevent cross-user collisions.
- `subprocess_timeout_minutes` default changed from `0` to `30`. `0` still
  disables the timeout; the `Config.validate()` hook rejects negative
  values and other invalid combinations.

### Infrastructure

- Added `pyproject.toml` with pinned deps, `[tool.ruff]`, `[tool.coverage]`,
  and `[tool.pytest.ini_options]` configuration.
- Added `requirements-dev.txt` for local / CI development dependencies.
- Added GitHub Actions CI workflow (`.github/workflows/ci.yml`): pytest
  with coverage + ruff, matrix on Ubuntu + Windows × Python 3.11 / 3.12.
- `requirements.txt` dependencies now pinned by minor version.

### Fixed

- Multi-line prompts from Telegram no longer get truncated on Windows.
  `.cmd` npm shims are now resolved to their underlying `node cli.js`
  invocation at startup, bypassing `cmd.exe`'s argv truncation bug
  (fix committed in 6aee7ca before this release).
- Removed dead `SessionManager._running_tasks` field. `/cancel` is now
  honest about what it does (kill in-flight subprocesses).

### Documentation

- New `SECURITY.md` documenting threat model, owner/viewer roles, secret
  handling, rate limiting, and known residual risks.
- `README.md` updated to mention security section and drop the
  auto-registration claim.

## [0.1.0] — initial release

- Original feature set: Telegram ↔ Claude Code / Codex CLI bridge, session
  attach/resume, watcher, /sync, viewer ACLs, prompt templates, /ping,
  /usage, /pending, /update, /autocontinue, WSL support.
