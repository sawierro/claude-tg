import asyncio
import json
import logging
import os
import platform
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from bot.config import Config
from bot.providers.base import CLIProvider, ProviderResponse, ProviderSession

logger = logging.getLogger(__name__)

_ENV = os.environ.copy()

CODEX_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SESSIONS_DIR = CODEX_DIR / "sessions"


def _is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is running."""
    if platform.system() == "Windows":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class CodexProvider(CLIProvider):
    """OpenAI Codex CLI provider."""

    name = "codex"

    def __init__(self, config: Config):
        self.config = config
        self._codex_path = config.codex_path if hasattr(config, "codex_path") else "codex"

    def _build_command(self, prompt: str, session_id: str | None = None) -> str:
        """Build shell command for Codex CLI."""
        parts = [self._codex_path, "exec"]
        if session_id:
            parts.extend(["resume", session_id])
        parts.append(json.dumps(prompt))
        parts.extend(["--yolo", "--json"])
        return " ".join(parts)

    async def run(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None = None,
    ) -> ProviderResponse:
        """Run Codex CLI and return parsed response."""
        cmd = self._build_command(prompt, session_id)
        logger.info("Running: %s (cwd=%s)", cmd, work_dir)
        start_time = time.monotonic()

        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=_ENV,
            )

            timeout_seconds = self.config.subprocess_timeout_minutes * 60
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.warning("Codex timed out after %ds", timeout_seconds)
                await _kill_process(process)
                return ProviderResponse(
                    session_id=session_id or "",
                    text="",
                    cost=None,
                    duration_seconds=time.monotonic() - start_time,
                    error=f"Timeout after {self.config.subprocess_timeout_minutes} minutes",
                )

            duration = time.monotonic() - start_time
            raw_stdout = stdout.decode("utf-8", errors="replace").strip()
            raw_stderr = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode != 0 and not raw_stdout:
                return ProviderResponse(
                    session_id=session_id or "",
                    text=raw_stderr or raw_stdout,
                    cost=None,
                    duration_seconds=duration,
                    error=f"Exit code {process.returncode}: {(raw_stderr or raw_stdout)[:500]}",
                )

            return self._parse_response(raw_stdout, session_id, duration)

        except FileNotFoundError:
            return ProviderResponse(
                session_id=session_id or "",
                text="",
                cost=None,
                duration_seconds=time.monotonic() - start_time,
                error=f"Codex CLI not found at '{self._codex_path}'",
            )
        except Exception as e:
            logger.exception("Unexpected error running Codex")
            return ProviderResponse(
                session_id=session_id or "",
                text="",
                cost=None,
                duration_seconds=time.monotonic() - start_time,
                error=str(e),
            )

    def _parse_response(
        self, raw: str, fallback_sid: str | None, duration: float
    ) -> ProviderResponse:
        """Parse JSONL stream from Codex --json output. Extract final message."""
        session_id = fallback_sid or ""
        final_text = ""
        error = None

        # Codex outputs newline-delimited JSON events
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            # Extract session ID from thread.started
            if event_type == "thread.started":
                session_id = event.get("sessionId", session_id)

            # Extract final text from the last assistant message
            if event_type == "item.completed":
                item = event.get("item", {})
                if item.get("role") == "assistant":
                    content = item.get("content", [])
                    for c in content:
                        if c.get("type") == "text":
                            final_text = c.get("text", "")

            # Capture errors
            if event_type == "error":
                error = event.get("message", str(event))

            if event_type == "turn.failed":
                error = event.get("error", {}).get("message", "Turn failed")

        # Fallback: if no structured events, use raw stdout as text
        if not final_text and not error:
            final_text = raw

        return ProviderResponse(
            session_id=session_id,
            text=final_text,
            cost=None,
            duration_seconds=duration,
            error=error,
        )

    def list_sessions(self) -> list[ProviderSession]:
        """List active Codex sessions from ~/.codex/ SQLite index."""
        sessions = []

        # Try SQLite index first
        db_path = CODEX_DIR / "sessions.db"
        if not db_path.exists():
            db_path = CODEX_DIR / "index.db"
        if not db_path.exists():
            # Fallback: scan session files
            return self._list_sessions_from_files()

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 20"
            )
            for row in cursor:
                row_dict = dict(row)
                sid = row_dict.get("session_id", row_dict.get("id", ""))
                cwd = row_dict.get("cwd", row_dict.get("working_directory", ""))
                pid = row_dict.get("pid", 0)
                name = row_dict.get("name", row_dict.get("title", ""))
                created = row_dict.get("created_at", "")

                if not sid:
                    continue

                started_at = datetime.now(tz=timezone.utc)
                if created:
                    try:
                        started_at = datetime.fromisoformat(str(created))
                    except (ValueError, TypeError):
                        pass

                alive = _is_process_alive(pid) if pid else False

                sessions.append(ProviderSession(
                    session_id=sid,
                    pid=pid,
                    cwd=cwd,
                    started_at=started_at,
                    is_alive=alive,
                    slug=name or sid[:8],
                    provider="codex",
                ))
            conn.close()
        except Exception as e:
            logger.warning("Failed to read Codex sessions DB: %s", e)
            return self._list_sessions_from_files()

        return sessions

    def _list_sessions_from_files(self) -> list[ProviderSession]:
        """Fallback: scan session files if SQLite index not available."""
        if not SESSIONS_DIR.exists():
            return []

        sessions = []
        # Codex stores sessions as YYYY/MM/DD/rollout-*.jsonl.zst
        for zst_file in SESSIONS_DIR.rglob("*.jsonl.zst"):
            # Extract session ID from filename (rollout-...-<uuid>.jsonl.zst)
            name = zst_file.stem.replace(".jsonl", "")
            parts = name.split("-")
            if len(parts) >= 5:
                # UUID is last 5 parts joined by -
                sid = "-".join(parts[-5:])
            else:
                sid = name

            sessions.append(ProviderSession(
                session_id=sid,
                pid=0,
                cwd="",
                started_at=datetime.fromtimestamp(
                    zst_file.stat().st_mtime, tz=timezone.utc
                ),
                is_alive=False,
                slug=sid[:8],
                provider="codex",
            ))

        sessions.sort(key=lambda s: s.started_at, reverse=True)
        return sessions[:20]

    def find_session(self, query: str) -> ProviderSession | None:
        """Find session by ID prefix or slug."""
        q = query.lower()
        for s in self.list_sessions():
            if s.session_id.lower().startswith(q) or s.slug.lower() == q:
                return s
        return None

    def get_session_jsonl_path(self, session_id: str) -> str | None:
        """Find session history file. Codex uses .jsonl.zst (compressed)."""
        if not SESSIONS_DIR.exists():
            return None
        for zst_file in SESSIONS_DIR.rglob(f"*{session_id}*.jsonl.zst"):
            return str(zst_file)
        # Also check for uncompressed
        for jsonl_file in SESSIONS_DIR.rglob(f"*{session_id}*.jsonl"):
            return str(jsonl_file)
        return None

    def extract_end_turn_text(self, line: str) -> str | None:
        """Parse a JSONL line for Codex end-of-turn message."""
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return None

        event_type = entry.get("type", "")

        # item.completed with assistant role = final response
        if event_type == "item.completed":
            item = entry.get("item", {})
            if item.get("role") == "assistant":
                content = item.get("content", [])
                text_parts = [c["text"] for c in content if c.get("type") == "text"]
                if text_parts:
                    return "".join(text_parts).strip()

        # turn.completed might also signal end of turn
        if event_type == "turn.completed":
            result = entry.get("result", {})
            text = result.get("text", "")
            if text:
                return text.strip()

        return None


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    """Gracefully terminate, then force kill after 5 seconds."""
    try:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    except ProcessLookupError:
        pass
