import asyncio
import logging
import os
import platform
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ProviderResponse:
    """Unified response from any CLI provider."""
    session_id: str
    text: str
    cost: float | None
    duration_seconds: float
    error: str | None
    tokens_in: int | None = None
    tokens_out: int | None = None


@dataclass
class ProviderSession:
    """An external session discovered from a terminal."""
    session_id: str
    pid: int
    cwd: str
    started_at: datetime
    is_alive: bool
    slug: str
    provider: str  # "claude" or "codex"
    wsl_distro: str = ""  # WSL distribution name, empty for native Windows/Linux


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is running (cross-platform)."""
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


async def kill_process(process: asyncio.subprocess.Process) -> None:
    """Gracefully terminate, then force kill after 5 seconds."""
    try:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
    except ProcessLookupError:
        pass


class CLIProvider(ABC):
    """Abstract interface for CLI agent providers (Claude, Codex, etc.)."""

    name: str = ""

    @abstractmethod
    async def run(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None = None,
        wsl_distro: str | None = None,
    ) -> ProviderResponse:
        """Run the CLI agent with a prompt. If session_id given, resume that session."""
        ...

    @abstractmethod
    def list_sessions(self) -> list[ProviderSession]:
        """List sessions running in terminals (synchronous, reads files)."""
        ...

    @abstractmethod
    def find_session(self, query: str) -> ProviderSession | None:
        """Find a session by ID prefix or slug."""
        ...

    @abstractmethod
    def get_session_jsonl_path(self, session_id: str) -> str | None:
        """Return path to session's history file for watching."""
        ...

    @abstractmethod
    def extract_end_turn_text(self, line: str) -> str | None:
        """Parse a JSONL line. Return text if it's a completed assistant response."""
        ...


async def run_subprocess(
    argv: list[str],
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout_seconds: int,
    parse: "callable",
    display_name: str,
    session_id: str | None,
    not_found_message: str,
) -> ProviderResponse:
    """Shared subprocess runner used by Claude / Codex providers.

    Centralises process spawning, timeout handling, tracking, and error
    wrapping so each provider only supplies argv, cwd, and a parser.
    """
    from bot.providers import _tracking  # local to break import cycle

    start_time = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        try:
            stdout, stderr = await _tracking.communicate_tracked(process, timeout_seconds)
        except TimeoutError:
            logger.warning("%s timed out after %ds", display_name, timeout_seconds)
            await kill_process(process)
            return ProviderResponse(
                session_id=session_id or "",
                text="",
                cost=None,
                duration_seconds=time.monotonic() - start_time,
                error=f"Timeout after {timeout_seconds // 60} minutes",
            )

        duration = time.monotonic() - start_time
        raw_stdout = stdout.decode("utf-8", errors="replace").strip()
        raw_stderr = stderr.decode("utf-8", errors="replace").strip()

        if raw_stdout:
            parsed = parse(raw_stdout, session_id, duration)
            if parsed.session_id or parsed.text or parsed.error:
                return parsed

        if process.returncode != 0:
            logger.error("%s exited %d: %s", display_name, process.returncode, raw_stderr)
            return ProviderResponse(
                session_id=session_id or "",
                text=raw_stderr or raw_stdout,
                cost=None,
                duration_seconds=duration,
                error=f"Exit code {process.returncode}: {(raw_stderr or raw_stdout)[:500]}",
            )

        return parse(raw_stdout, session_id, duration)

    except FileNotFoundError:
        return ProviderResponse(
            session_id=session_id or "",
            text="",
            cost=None,
            duration_seconds=time.monotonic() - start_time,
            error=not_found_message,
        )
    except Exception as e:
        logger.exception("Unexpected error running %s", display_name)
        return ProviderResponse(
            session_id=session_id or "",
            text="",
            cost=None,
            duration_seconds=time.monotonic() - start_time,
            error=str(e),
        )
