import json
import logging
import shlex
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Config
from bot.providers._env import build_subprocess_env
from bot.providers._shim import resolve_cli_exec, resolve_npm_shim
from bot.providers._wsl import (
    find_wsl_exe,
    get_wsl_distros,
    get_wsl_home,
    resolve_wsl_cli,
    wsl_path_to_windows,
)
from bot.providers.base import (
    CLIProvider,
    ProviderResponse,
    ProviderSession,
    is_process_alive,
    run_subprocess,
)

logger = logging.getLogger(__name__)

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Backwards-compat aliases — tests import these names, plus codex.py expects them.
_find_wsl_exe = find_wsl_exe
_resolve_wsl_cli = resolve_wsl_cli
_get_wsl_distros = get_wsl_distros
_get_wsl_home = get_wsl_home
_wsl_path_to_windows = wsl_path_to_windows
_resolve_npm_shim = resolve_npm_shim
_resolve_cli_exec = resolve_cli_exec
_is_process_alive = is_process_alive


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

    def _build_args(self, prompt: str, session_id: str | None = None) -> list[str]:
        """Build argument list for Claude CLI (no shell interpretation)."""
        args = list(_resolve_cli_exec(self.config.claude_path))
        if session_id:
            args.extend(["--resume", session_id])
        args.extend(["-p", prompt])
        args.extend(self.config.claude_flags)
        args.extend(["--output-format", "json"])
        return args

    def _build_command(self, prompt: str, session_id: str | None = None) -> str:
        """Build shell command string (for logging/tests only)."""
        parts = [self.config.claude_path]
        if session_id:
            parts.extend(["--resume", session_id])
        parts.extend(["-p", json.dumps(prompt)])
        parts.extend(self.config.claude_flags)
        parts.extend(["--output-format", "json"])
        return " ".join(parts)

    async def run(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None = None,
        wsl_distro: str | None = None,
    ) -> ProviderResponse:
        """Run Claude Code CLI and return parsed response."""
        if wsl_distro:
            return await self._run_wsl(prompt, work_dir, session_id, wsl_distro)

        args = self._build_args(prompt, session_id)
        logger.info("Running claude (cwd=%s, resume=%s)", work_dir, bool(session_id))
        return await run_subprocess(
            args,
            cwd=work_dir,
            env=build_subprocess_env(),
            timeout_seconds=self.config.subprocess_timeout_minutes * 60,
            parse=self._parse_response,
            display_name="Claude",
            session_id=session_id,
            not_found_message=f"Claude CLI not found at '{self.config.claude_path}'",
        )

    async def _run_wsl(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None,
        wsl_distro: str,
    ) -> ProviderResponse:
        """Run Claude Code CLI inside a WSL distribution."""
        claude_bin = resolve_wsl_cli(wsl_distro, "claude")

        parts = [shlex.quote(claude_bin)]
        if session_id:
            parts.extend(["--resume", shlex.quote(session_id)])
        parts.extend(["-p", shlex.quote(prompt)])
        parts.extend(shlex.quote(flag) for flag in self.config.claude_flags)
        parts.extend(["--output-format", "json"])
        inner_cmd = " ".join(parts)

        args = [
            find_wsl_exe(), "-d", wsl_distro,
            "--cd", work_dir,
            "--", "bash", "-l", "-c", inner_cmd,
        ]
        logger.info("Running claude WSL [%s] (cwd=%s, resume=%s)",
                    wsl_distro, work_dir, bool(session_id))
        return await run_subprocess(
            args,
            cwd=None,
            env=build_subprocess_env(),
            timeout_seconds=self.config.subprocess_timeout_minutes * 60,
            parse=self._parse_response,
            display_name="Claude(WSL)",
            session_id=session_id,
            not_found_message="wsl.exe not found — is WSL installed?",
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

            usage = data.get("usage") or {}
            t_in = usage.get("input_tokens")
            t_out = usage.get("output_tokens")
            # Include cached input tokens in total_in for a realistic cost picture
            cache_read = usage.get("cache_read_input_tokens") or 0
            cache_creation = usage.get("cache_creation_input_tokens") or 0
            if t_in is not None:
                t_in = int(t_in) + int(cache_read) + int(cache_creation)

            return ProviderResponse(
                session_id=sid,
                text=result_text if not is_error else "",
                cost=float(cost) if cost is not None else None,
                duration_seconds=duration,
                error=result_text if is_error else None,
                tokens_in=int(t_in) if t_in is not None else None,
                tokens_out=int(t_out) if t_out is not None else None,
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
        """Scan ~/.claude/sessions/ for running Claude sessions (native + WSL)."""
        sessions = self._list_native_sessions()
        sessions.extend(self._list_wsl_sessions())
        return sessions

    def _list_native_sessions(self) -> list[ProviderSession]:
        """Scan native (Windows/Linux) ~/.claude/sessions/."""
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
                    started_ms / 1000, tz=UTC
                ) if started_ms else datetime.now(tz=UTC)

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

    def _list_wsl_sessions(self) -> list[ProviderSession]:
        """Scan WSL distributions for Claude sessions."""
        sessions = []
        for distro in _get_wsl_distros():
            home = _get_wsl_home(distro)
            if not home:
                continue

            sessions_dir = _wsl_path_to_windows(distro, f"{home}/.claude/sessions")
            try:
                if not sessions_dir.exists():
                    continue
            except OSError:
                continue

            for session_file in sessions_dir.glob("*.json"):
                try:
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                    pid = data.get("pid", 0)
                    session_id = data.get("sessionId", "")
                    cwd = data.get("cwd", "")
                    started_ms = data.get("startedAt", 0)

                    if not session_id:
                        continue

                    started_at = datetime.fromtimestamp(
                        started_ms / 1000, tz=UTC
                    ) if started_ms else datetime.now(tz=UTC)

                    # Check process via /proc (may not work for all setups)
                    alive = True
                    if pid:
                        try:
                            alive = _wsl_path_to_windows(
                                distro, f"/proc/{pid}"
                            ).exists()
                        except OSError:
                            alive = True

                    slug = self._get_wsl_slug(distro, session_id, home)

                    sessions.append(ProviderSession(
                        session_id=session_id,
                        pid=pid,
                        cwd=cwd,
                        started_at=started_at,
                        is_alive=alive,
                        slug=slug,
                        provider="claude",
                        wsl_distro=distro,
                    ))
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(
                        "Failed to read WSL session file %s: %s", session_file, e
                    )

        return sessions

    def _get_wsl_slug(self, distro: str, session_id: str, home: str) -> str:
        """Find session slug from WSL project JSONL files."""
        projects_dir = _wsl_path_to_windows(distro, f"{home}/.claude/projects")
        try:
            if not projects_dir.exists():
                return ""
        except OSError:
            return ""
        for project_dir in projects_dir.iterdir():
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

    def find_session(self, query: str) -> ProviderSession | None:
        """Find session by ID prefix or slug."""
        q = query.lower()
        for s in self.list_sessions():
            if s.session_id.lower().startswith(q) or s.slug.lower() == q:
                return s
        return None

    def get_session_jsonl_path(self, session_id: str) -> str | None:
        """Find JSONL file for a session across all projects (native + WSL)."""
        # Check native paths
        if PROJECTS_DIR.exists():
            for project_dir in PROJECTS_DIR.iterdir():
                if not project_dir.is_dir():
                    continue
                jsonl = project_dir / f"{session_id}.jsonl"
                if jsonl.exists():
                    return str(jsonl)

        # Check WSL paths (UNC paths are readable from Windows)
        for distro in _get_wsl_distros():
            home = _get_wsl_home(distro)
            if not home:
                continue
            projects_dir = _wsl_path_to_windows(distro, f"{home}/.claude/projects")
            try:
                if not projects_dir.exists():
                    continue
            except OSError:
                continue
            for project_dir in projects_dir.iterdir():
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
