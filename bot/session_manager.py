import asyncio
import logging
import uuid
from pathlib import Path

import aiosqlite

from bot.config import Config
from bot.providers.base import CLIProvider, ProviderResponse
from bot.session_watcher import SessionWatcher
from bot import db

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, config: Config, conn: aiosqlite.Connection):
        self.config = config
        self.conn = conn
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._watchers: dict[str, SessionWatcher] = {}
        self._watcher_callback = None
        self._providers: dict[str, CLIProvider] = {}

    def register_provider(self, provider: CLIProvider) -> None:
        """Register a CLI provider (claude, codex, etc.)."""
        self._providers[provider.name] = provider
        logger.info("Registered provider: %s", provider.name)

    def get_provider(self, name: str) -> CLIProvider:
        """Get provider by name."""
        provider = self._providers.get(name)
        if not provider:
            raise ValueError(f"Unknown provider: {name}")
        return provider

    def set_watcher_callback(self, callback) -> None:
        """Set callback for watcher notifications: async fn(session_id, name, text)."""
        self._watcher_callback = callback

    async def create_session(
        self, name: str, work_dir: str, prompt: str, provider_name: str = "claude"
    ) -> ProviderResponse:
        """Create a new session using specified provider."""
        existing = await db.get_session_by_name(self.conn, name)
        if existing:
            if existing["status"] in ("done", "error"):
                await db.delete_session(self.conn, existing["id"])
            else:
                raise ValueError(f"Session '{name}' is already active")

        provider = self.get_provider(provider_name)
        response = await provider.run(prompt, work_dir)

        if response.error and "not logged in" in response.error.lower():
            raise RuntimeError(
                f"{provider_name} CLI is not logged in. Run login in terminal first."
            )

        if not response.session_id:
            if response.error:
                raise RuntimeError(f"{provider_name} error: {response.error}")
            response.session_id = str(uuid.uuid4())
            logger.warning("No session_id returned, generated: %s", response.session_id)

        await db.create_session(
            self.conn, response.session_id, name, work_dir, provider=provider_name
        )

        status = "error" if response.error else "waiting"
        await db.update_session_status(self.conn, response.session_id, status)

        await db.insert_message(self.conn, response.session_id, "user", prompt)
        await db.insert_message(
            self.conn, response.session_id, "assistant",
            response.error or response.text,
        )

        return response

    async def resume_session(
        self, session_id: str, prompt: str
    ) -> ProviderResponse:
        """Resume an existing session with a new prompt."""
        session = await db.get_session(self.conn, session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")

        if session["status"] == "running":
            raise ValueError(f"Session '{session['name']}' is already running")

        provider_name = session.get("provider", "claude")
        provider = self.get_provider(provider_name)

        wsl_distro = session.get("wsl_distro", "") or None

        await db.update_session_status(self.conn, session_id, "running")

        watcher = self._watchers.get(session_id)
        if watcher:
            watcher.pause()

        try:
            response = await provider.run(
                prompt, session["work_dir"], session_id=session_id,
                wsl_distro=wsl_distro,
            )
        except Exception:
            await db.update_session_status(self.conn, session_id, "error")
            if watcher:
                watcher.resume()
            raise

        status = "error" if response.error else "waiting"
        await db.update_session_status(self.conn, session_id, status)

        if watcher:
            watcher.resume()

        await db.insert_message(self.conn, session_id, "user", prompt)
        await db.insert_message(
            self.conn, session_id, "assistant", response.error or response.text
        )

        return response

    async def stop_session(self, session_id: str) -> None:
        """Stop a session and mark it as done."""
        task = self._running_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._stop_watcher(session_id)
        await db.update_session_status(self.conn, session_id, "done")
        logger.info("Session %s stopped", session_id)

    async def stop_session_by_name(self, name: str) -> None:
        """Stop a session by name."""
        session = await db.get_session_by_name(self.conn, name)
        if not session:
            raise ValueError(f"Session '{name}' not found")
        await self.stop_session(session["id"])

    async def import_external_session(
        self, session_id: str, name: str, work_dir: str,
        provider_name: str = "claude", wsl_distro: str = "",
    ) -> None:
        """Import an external session into the bot's DB."""
        existing = await db.get_session(self.conn, session_id)
        if existing:
            if existing["status"] not in ("running", "waiting"):
                await db.update_session_status(self.conn, session_id, "waiting")
            self._start_watcher(session_id, existing["name"], provider_name)
            logger.info("Session %s already in DB, reactivated", session_id)
            return

        by_name = await db.get_session_by_name(self.conn, name)
        if by_name:
            if by_name["status"] in ("done", "error"):
                await db.delete_session(self.conn, by_name["id"])
            else:
                name = f"{name}-tg"

        await db.create_session(
            self.conn, session_id, name, work_dir,
            provider=provider_name, wsl_distro=wsl_distro,
        )
        await db.update_session_status(self.conn, session_id, "waiting")
        logger.info(
            "Imported external session %s as '%s' (provider=%s%s)",
            session_id, name, provider_name,
            f", wsl={wsl_distro}" if wsl_distro else "",
        )

        self._start_watcher(session_id, name, provider_name)

    async def list_sessions(self) -> list[dict]:
        """List all active sessions."""
        return await db.get_active_sessions(self.conn)

    async def get_session_by_tg_message(self, message_id: int) -> dict | None:
        """Find session by Telegram message ID."""
        return await db.get_session_by_tg_message(self.conn, message_id)

    async def update_tg_message(
        self, session_id: str, tg_message_id: int
    ) -> None:
        """Update the last Telegram message ID for reply routing."""
        await db.update_session_status(
            self.conn, session_id, "waiting", last_tg_msg_id=tg_message_id
        )

    def _start_watcher(self, session_id: str, name: str, provider_name: str = "claude") -> None:
        """Start a history file watcher for an attached session."""
        # Stop existing dead watcher if any
        existing = self._watchers.get(session_id)
        if existing and existing._task and not existing._task.done():
            return  # Already running
        if existing:
            self._watchers.pop(session_id)

        if not self._watcher_callback:
            logger.warning("No watcher callback, skipping watcher for %s", name)
            return
        provider = self._providers.get(provider_name)
        if not provider:
            logger.warning("Provider %s not registered, skipping watcher", provider_name)
            return
        watcher = SessionWatcher(session_id, name, provider, self._watcher_callback)
        self._watchers[session_id] = watcher
        watcher.start()

    def _stop_watcher(self, session_id: str) -> None:
        """Stop a history file watcher."""
        watcher = self._watchers.pop(session_id, None)
        if watcher:
            watcher.stop()

    async def sync_session(self, session_id: str) -> str:
        """Write a sync file with the Telegram conversation summary. Returns file path."""
        session = await db.get_session(self.conn, session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")

        messages = await db.get_session_messages(self.conn, session_id)
        if not messages:
            raise ValueError("No messages to sync")

        # Build task summary from user messages
        tasks = [m["content"] for m in messages if m["role"] == "user"]

        lines: list[str] = []
        lines.append("# Remote Session Context (via Claude-TG)")
        lines.append("")
        lines.append("The following work was performed remotely via Telegram on this project.")
        lines.append("Read this to understand what changed outside of this terminal session.")
        lines.append("")
        lines.append(f"- **Session**: `{session['name']}`")
        lines.append(f"- **Working directory**: `{session['work_dir']}`")
        lines.append("")

        # Tasks overview
        lines.append("## Tasks performed")
        lines.append("")
        for i, task in enumerate(tasks, 1):
            # First line of user message = task summary
            summary = task.strip().split("\n")[0][:120]
            lines.append(f"{i}. {summary}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Full conversation
        lines.append("## Detailed conversation")
        lines.append("")
        task_num = 0
        for msg in messages:
            if msg["role"] == "user":
                task_num += 1
                lines.append(f"### Task {task_num}: {msg['content'].strip().split(chr(10))[0][:100]}")
                lines.append("")
                if "\n" in msg["content"].strip():
                    lines.append(msg["content"])
                    lines.append("")
            else:
                lines.append(f"**Result:**")
                lines.append("")
                lines.append(msg["content"])
                lines.append("")
                lines.append("---")
                lines.append("")

        lines.append("**IMPORTANT: After reading this file and loading the context, delete it:**")
        lines.append("```")
        lines.append("rm .claude-tg-sync.md")
        lines.append("```")

        content = "\n".join(lines)

        work_dir = session["work_dir"]
        wsl_distro = session.get("wsl_distro", "")
        if wsl_distro:
            from bot.providers.claude import _wsl_path_to_windows
            sync_path = _wsl_path_to_windows(wsl_distro, work_dir) / ".claude-tg-sync.md"
        else:
            sync_path = Path(work_dir) / ".claude-tg-sync.md"
        sync_path.write_text(content, encoding="utf-8")
        logger.info("Sync file written: %s", sync_path)

        return str(sync_path)

    async def cleanup(self) -> None:
        """Clean up stale sessions."""
        count = await db.cleanup_stale_sessions(
            self.conn, self.config.session_timeout_hours
        )
        if count:
            logger.info("Cleaned up %d stale sessions", count)
