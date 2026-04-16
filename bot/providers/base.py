import asyncio
import os
import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ProviderResponse:
    """Unified response from any CLI provider."""
    session_id: str
    text: str
    cost: float | None
    duration_seconds: float
    error: str | None


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
        except asyncio.TimeoutError:
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
