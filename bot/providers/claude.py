import asyncio
import json
import logging
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

from bot.config import Config
from bot.providers.base import CLIProvider, ProviderResponse, ProviderSession

logger = logging.getLogger(__name__)

_ENV = os.environ.copy()

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"


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


def _read_tail_lines(path: Path, n: int = 10) -> list[str]:
    """Read last N lines of a file efficiently."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk_size = min(size, n * 2000)
            f.seek(size - chunk_size)
            data = f.read().decode("utf-8", errors="replace")
            return data.strip().split("\n")[-n:]
    except OSError:
        return []


def _get_session_slug(session_id: str) -> str:
    """Try to find session slug from project JSONL files."""
    if not PROJECTS_DIR.exists():
        return ""
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        jsonl_path = project_dir / f"{session_id}.jsonl"
        if jsonl_path.exists():
            for line in reversed(_read_tail_lines(jsonl_path, 20)):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    slug = data.get("slug", "")
                    if slug:
                        return slug
                except json.JSONDecodeError:
                    continue
    return ""


class ClaudeProvider(CLIProvider):
    """Claude Code CLI provider."""

    name = "claude"

    def __init__(self, config: Config):
        self.config = config

    def _build_command(self, prompt: str, session_id: str | None = None) -> str:
        """Build shell command for Claude CLI."""
        parts = [self.config.claude_path]
        if session_id:
            parts.extend(["--resume", session_id])
        parts.extend(["-p", json.dumps(prompt)])
        parts.extend(["--dangerously-skip-permissions"])
        parts.extend(self.config.claude_flags)
        parts.extend(["--output-format", "json"])
        return " ".join(parts)

    async def run(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None = None,
    ) -> ProviderResponse:
        """Run Claude Code CLI and return parsed response."""
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
                logger.warning("Claude timed out after %ds", timeout_seconds)
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

            if raw_stdout:
                parsed = self._parse_response(raw_stdout, session_id, duration)
                if parsed.session_id or parsed.text or parsed.error:
                    return parsed

            if process.returncode != 0:
                logger.error("Claude exited %d: %s", process.returncode, raw_stderr)
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
                error=f"Claude CLI not found at '{self.config.claude_path}'",
            )
        except Exception as e:
            logger.exception("Unexpected error running Claude")
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
        """Parse JSON response from Claude CLI."""
        try:
            data = json.loads(raw)
            sid = data.get("session_id", fallback_sid or "")
            result_text = data.get("result", "")
            cost = data.get("total_cost_usd") or data.get("cost_usd") or data.get("cost")
            is_error = data.get("is_error", False)

            return ProviderResponse(
                session_id=sid,
                text=result_text if not is_error else "",
                cost=float(cost) if cost is not None else None,
                duration_seconds=duration,
                error=result_text if is_error else None,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse Claude JSON: %s", e)
            return ProviderResponse(
                session_id=fallback_sid or "",
                text=raw,
                cost=None,
                duration_seconds=duration,
                error=None,
            )

    def list_sessions(self) -> list[ProviderSession]:
        """Scan ~/.claude/sessions/ for running Claude sessions."""
        if not SESSIONS_DIR.exists():
            return []

        sessions = []
        for session_file in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                pid = data.get("pid", 0)
                session_id = data.get("sessionId", "")
                cwd = data.get("cwd", "")
                started_ms = data.get("startedAt", 0)

                if not session_id:
                    continue

                started_at = datetime.fromtimestamp(
                    started_ms / 1000, tz=timezone.utc
                ) if started_ms else datetime.now(tz=timezone.utc)

                alive = _is_process_alive(pid) if pid else False
                slug = _get_session_slug(session_id)

                sessions.append(ProviderSession(
                    session_id=session_id,
                    pid=pid,
                    cwd=cwd,
                    started_at=started_at,
                    is_alive=alive,
                    slug=slug,
                    provider="claude",
                ))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read session file %s: %s", session_file, e)

        return sessions

    def find_session(self, query: str) -> ProviderSession | None:
        """Find session by ID prefix or slug."""
        q = query.lower()
        for s in self.list_sessions():
            if s.session_id.lower().startswith(q) or s.slug.lower() == q:
                return s
        return None

    def get_session_jsonl_path(self, session_id: str) -> str | None:
        """Find JSONL file for a session across all projects."""
        if not PROJECTS_DIR.exists():
            return None
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            jsonl = project_dir / f"{session_id}.jsonl"
            if jsonl.exists():
                return str(jsonl)
        return None

    def extract_end_turn_text(self, line: str) -> str | None:
        """Parse JSONL line for Claude end_turn message."""
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return None

        if entry.get("type") != "assistant":
            return None
        msg = entry.get("message", {})
        if msg.get("stop_reason") != "end_turn" or msg.get("role") != "assistant":
            return None

        content = msg.get("content", [])
        text_parts = [c["text"] for c in content if c.get("type") == "text"]
        return "".join(text_parts).strip() if text_parts else None


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
