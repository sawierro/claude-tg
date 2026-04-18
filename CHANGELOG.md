# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] — 2026-04-18 (unreleased)

### Added

- `/lastlog [name]` — shows the last 3 messages the watcher has observed
  for each session (or for the named session). Entries live in-memory
  per session (3 × up to 2 000 chars) and reset on bot restart.

### Performance

- `/sessions` and `/connect` are noticeably faster: `get_wsl_distros()`
  now has a 60s TTL cache, and provider scans run off the event loop
  via `asyncio.to_thread` + `gather`.

## [0.2.0] — 2026-04-18 (unreleased)

Security hardening release.

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

### Refactor

- Extracted `bot/providers/_wsl.py` (WSL helpers) and
  `bot/providers/_shim.py` (npm `.cmd` resolver) so providers no longer
  reach into each other's private API. Codex drops the `from claude
  import _private_*` chain.
- Centralised subprocess spawning in `bot/providers/base.run_subprocess`
  (template-method pattern). Removed ~200 lines of duplication between
  Claude and Codex native/WSL paths.
- Dropped dead `_running_tasks` dict from `SessionManager` and the
  unused `_kill_process = kill_process` compat alias from providers.

### UX

- `/start` shows inline shortcuts (Connect / Sessions / Prompts / Usage /
  Full help).
- `/help` reorganised by scenario (Start / Work / Files / Templates /
  Status / Access / Diagnostics).
- Denied users get a single explicit "access denied" reply instead of
  silent ignore.
- Long responses (> 12 000 chars) are sent as a `.md` attachment with a
  preview header instead of a 10-message wall.
- Provider errors are classified (`auth`, `limit`, `timeout`,
  `filesystem`, `network`, `permission`, `parse`) and the notification
  includes a 💡 hint with a suggested action.
- New `/botstatus` command: uptime, active CLI subprocess count,
  per-status session breakdown, pending-prompt queue size — basic
  health check.

### Ops

- `Dockerfile` + `docker-compose.yml` (Python 3.12 + Node 20 + npm
  Claude/Codex CLIs, non-root bot user, tini as PID 1, persistent
  volume for DB + prompts).
- `deploy/claude-tg.service` systemd unit with hardening flags
  (`PrivateTmp`, `ProtectSystem=full`, `KillSignal=SIGTERM`,
  `TimeoutStopSec=30s` for clean subprocess shutdown).
- `LOG_LEVEL` / `LOG_FILE` env vars: the latter enables a
  `RotatingFileHandler` (10 MB × 5 backups).
- `.dockerignore` prevents local DBs / secrets / caches leaking into
  the build context.

### Documentation

- New `SECURITY.md` documenting threat model, owner/viewer roles, secret
  handling, rate limiting, and known residual risks.
- New `CHANGELOG.md` (this file).
- `README.md` updated: security section pointing at SECURITY.md,
  Deployment section with Docker + systemd + logging + health check,
  Architecture tree refreshed with all new modules.
- `README.txt` reduced to a concise pointer with a prominent security
  callout — no longer drifts from `README.md`.

### Testing

- `tests/test_rate_limiter.py`, `tests/test_providers_env.py`,
  `tests/test_path_traversal.py`, `tests/test_resume_worker.py`,
  `tests/test_updater.py`, plus expanded `tests/test_db.py` and
  `tests/test_message_formatter.py`.
- `.github/workflows/ci.yml`: pytest + ruff + coverage on push/PR
  (Ubuntu + Windows × Python 3.11/3.12).
- Coverage raised from **19.3% → 30.3%**. 125 tests pass.

### Known follow-ups (not blocking 0.2.0)

- Handler split: `bot/telegram_handler.py` is still a 2000-line file.
  Needs handler-level tests first to refactor safely.
- Test coverage (~30%) — blocked on the handler split.
- `/update` origin-URL allowlist: the bot currently trusts whatever
  `origin` is configured.

## [0.1.0] — initial release

- Original feature set: Telegram ↔ Claude Code / Codex CLI bridge, session
  attach/resume, watcher, /sync, viewer ACLs, prompt templates, /ping,
  /usage, /pending, /update, /autocontinue, WSL support.
