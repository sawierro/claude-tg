# Security Model — Claude-TG

This document describes the threat model and security guarantees of
Claude-TG. Read it before exposing the bot to anyone other than yourself.

## What the bot actually is

Claude-TG forwards messages from Telegram into a local Claude Code / Codex
CLI process running on your machine. **The CLI can read, write, and execute
any code with your user's permissions**, and by default it is run with
`--dangerously-skip-permissions` (Claude) / `--yolo` (Codex). That means:

> Anyone who can send a message to the bot can execute arbitrary code and
> read arbitrary files as the user running the bot.

Treat bot access as equivalent to SSH access.

## Owner & access model

- **Owners** have full control: create sessions, send prompts, transfer
  files. Owners are configured via the `OWNER_CHAT_ID` env var
  (comma-separated list). Without at least one owner, the bot refuses to
  start.
- **Viewers** (approved via `/approve`) get read-only access: they receive
  watcher notifications for sessions the owner has `/share`d with them.
  They cannot send prompts, download files, or modify sessions.
- **Everyone else** gets a single "access request pending" message and is
  otherwise ignored. Denied users get one explicit "access denied"
  response and are silenced.

Auto-registration of the first caller was **removed** in v0.2.0. Earlier
versions accepted the first `/start` message as the owner — this let
anyone who saw the bot's username before you did hijack it. If you are
upgrading, delete any leftover `allowed_chat_ids` from `config.json` and
set `OWNER_CHAT_ID` in `.env`.

## Secrets handling

- The **Telegram bot token** is read only from the `TELEGRAM_TOKEN`
  environment variable (loaded from `.env`). If a `telegram_token` key is
  present in `config.json` it is ignored with a warning.
- `config.json` is written with `0600` permissions on POSIX.
- `.env`, `config.json`, `claude_tg.db`, and `smoke_test.env` are in
  `.gitignore` and have never been committed to the repository.

## Subprocess sandboxing

Each Claude / Codex invocation runs with a **minimised environment**
(`bot/providers/_env.py`). Only the following env vars pass through:

- Process basics: `PATH`, `HOME`, `USER`, `LANG`, Windows system dirs
- Node tooling: `NODE_PATH`, `NVM_DIR`, etc.
- Provider auth prefixes: `CLAUDE_*`, `ANTHROPIC_*`, `OPENAI_*`, `CODEX_*`

Everything else — `TELEGRAM_TOKEN`, `AWS_*`, `GITHUB_TOKEN`, shell rc
loot, etc. — is stripped before the child starts. A prompt-injection
payload that tricks Claude into `echo $GITHUB_TOKEN` will see nothing.

`TELEGRAM_TOKEN` / `BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` are **always**
removed, even if somehow reintroduced.

## File access (`/get`, uploads)

Every file path the bot touches goes through
`_resolve_work_path(session, rel_path)`:

1. Path is resolved via `os.path.realpath` (follows symlinks).
2. Resolved path must be inside the session's `work_dir`. Paths escaping
   via `..` or symlinks pointing outside are rejected.
3. Sensitive filenames are blocked: `.env`, SSH keys, `id_rsa`,
   `.bashrc`, `.npmrc`, `wp-config.php`, etc.
4. Sensitive **suffixes** are blocked: `.pem`, `.key`, `.p12`, `.pfx`,
   `.crt`, `.gpg`, `.kdbx`, `.asc`.
5. Sensitive **directory components** are blocked anywhere in the path:
   `.git`, `.ssh`, `.aws`, `.gnupg`, `.docker`, `.kube`, `.vscode`.
6. Downloads capped at 25 MB; uploads at 10 MB; prompt-template uploads
   at 1 MB.

This is a blocklist plus boundary check — not a whitelist. Prompts that
ask Claude itself to `cat /etc/passwd` via the Bash tool are **not**
caught here, only direct `/get` access is. If you run the bot against
work dirs you don't control, assume the worst.

## Rate limiting

`bot/rate_limiter.py` applies a sliding-window per-owner rate limit
(default 30 msg/min, configurable via `rate_limit_per_minute`) at the
`@authorized` decorator level. A concurrency guard primitive exists for
future capping of in-flight provider calls.

## Process lifecycle

Active CLI subprocesses are tracked in `bot/providers/_tracking.py`.

- `/cancel` kills every in-flight `claude` / `codex` child process.
- On shutdown (SIGINT / SIGTERM), the bot kills everything in the
  tracker before exiting. No orphan processes remain.

## Data flow & persistence

- SQLite database (`claude_tg.db`, WAL mode) stores sessions, messages,
  viewer ACLs, and the pending-prompt queue.
- `resume_worker` restores pending prompts from disk after restart — the
  queue survives crashes.
- WAL file is checkpointed hourly by `bot/maintenance.py`.

## Updates (`/update`)

The bot can self-update via `git pull` from `origin/main`. If the remote
is compromised, the bot will pull malware on the next `/update`. This is
a feature for a personal bot only — if you're running the bot for anyone
else, disable the `/update` command or remove the handler.

## Known residual risks (by design)

- **Prompt injection → RCE.** With `--dangerously-skip-permissions`
  enabled, any document/prompt Claude reads can execute arbitrary code.
  This is intrinsic to the CLI's design; the bot does not mitigate it.
- **Work-dir-scoped file access only.** Claude itself can still read
  `~/.ssh/id_rsa` via its own Bash tool, since that's outside the bot's
  `/get` path check.
- **`/update` without signature verification.** The update pipeline
  assumes the GitHub remote is trusted.
- **No `--dangerously-skip-permissions` per-session toggle.** The flag
  lives in `claude_flags` (`config.json`). Removing it makes Claude
  prompt for every edit, which blocks unattended use.

## Reporting

Found a security issue? Open a GitHub issue tagged `security`, or email
the repository owner directly.
