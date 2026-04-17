import asyncio
import functools
import json
import logging
import os
import platform
import re
import shutil as _shutil
import subprocess as _subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Config
from bot.providers.base import CLIProvider, ProviderResponse, ProviderSession, is_process_alive, kill_process

logger = logging.getLogger(__name__)

_ENV = None  # inherit current environment (not a frozen copy)

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def _resolve_npm_shim(cmd_path: str) -> list[str] | None:
    """If cmd_path is an npm-style .cmd shim on Windows, return [node, js_path].

    Windows .cmd wrappers invoke node via cmd.exe, which truncates arguments
    at embedded newlines when parsing `%1`/`%*`. Resolving the shim to a
    direct `node cli.js` invocation bypasses cmd.exe and preserves multi-line
    prompts.
    """
    if platform.system() != "Windows":
        return None
    resolved_str = _shutil.which(cmd_path) or cmd_path
    resolved = Path(resolved_str)
    if not resolved.exists() or resolved.suffix.lower() != ".cmd":
        return None
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Typical npm shim tail:
    #   "%_prog%"  "%dp0%\path\to\cli.js" %*
    m = re.search(
        r'"(%_prog%|[^"]*?node\.exe)"\s+"([^"]+\.js)"',
        content,
        re.IGNORECASE,
    )
    if not m:
        return None
    dp0 = str(resolved.parent) + "\\"
    node_spec = m.group(1).replace("%dp0%", dp0)
    js_path = os.path.normpath(m.group(2).replace("%dp0%", dp0))
    if node_spec == "%_prog%":
        candidate = resolved.parent / "node.exe"
        node_exe = str(candidate) if candidate.exists() else "node"
    else:
        node_exe = os.path.normpath(node_spec)
    return [node_exe, js_path]


@functools.lru_cache(maxsize=8)
def _resolve_cli_exec(cli_path: str) -> tuple[str, ...]:
    """Return argv prefix for a CLI. On Windows, bypass .cmd npm shims."""
    shim = _resolve_npm_shim(cli_path)
    if shim:
        logger.info("Resolved npm shim %s -> %s", cli_path, shim)
        return tuple(shim)
    return (cli_path,)

# ---------------------------------------------------------------------------
# WSL helpers (Windows only)
# ---------------------------------------------------------------------------

_wsl_unc_prefix_cache: dict[str, str] = {}


def _find_wsl_exe() -> str:
    """Find wsl.exe reliably — shutil.which, then System32 fallbacks."""
    import shutil as _shutil
    found = _shutil.which("wsl")
    if found:
        return found
    # Fallback: common Windows paths (PATH may be incomplete)
    for candidate in (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe",
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Sysnative" / "wsl.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return "wsl"  # hope for the best


def _resolve_wsl_cli(distro: str, cli_name: str) -> str:
    """Find a CLI tool inside WSL — handles nvm/npm-installed binaries."""
    try:
        result = _subprocess.run(
            [_find_wsl_exe(), "-d", distro, "--", "bash", "-lc", f"command -v {cli_name}"],
            capture_output=True, text=True, timeout=10,
        )
        path = result.stdout.strip()
        if result.returncode == 0 and path:
            return path
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        pass
    return cli_name


def _get_wsl_distros() -> list[str]:
    """Return installed WSL distribution names."""
    if platform.system() != "Windows":
        return []
    wsl = _find_wsl_exe()
    try:
        result = _subprocess.run(
            [wsl, "-l", "-q"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        # wsl.exe outputs UTF-16LE on Windows
        try:
            text = result.stdout.decode("utf-16-le")
        except UnicodeDecodeError:
            text = result.stdout.decode("utf-8", errors="replace")
        distros = [
            line.strip().strip("\x00")
            for line in text.strip().split("\n")
            if line.strip().strip("\x00")
        ]
        return distros
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        return []


@functools.lru_cache(maxsize=16)
def _get_wsl_home(distro: str) -> str | None:
    """Return the default user's home directory inside a WSL distro."""
    try:
        result = _subprocess.run(
            [_find_wsl_exe(), "-d", distro, "--", "printenv", "HOME"],
            capture_output=True, text=True, timeout=10,
        )
        home = result.stdout.strip()
        if result.returncode == 0 and home:
            return home
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        pass
    return None


def _wsl_path_to_windows(distro: str, linux_path: str) -> Path:
    """Convert a Linux path inside WSL to a Windows-accessible UNC path."""
    rel = linux_path.lstrip("/")

    if distro in _wsl_unc_prefix_cache:
        return Path(_wsl_unc_prefix_cache[distro]) / rel

    for prefix in (f"\\\\wsl.localhost\\{distro}", f"\\\\wsl$\\{distro}"):
        try:
            if Path(prefix).exists():
                _wsl_unc_prefix_cache[distro] = prefix
                return Path(prefix) / rel
        except OSError:
            continue

    fallback = f"\\\\wsl.localhost\\{distro}"
    _wsl_unc_prefix_cache[distro] = fallback
    return Path(fallback) / rel


_is_process_alive = is_process_alive  # backward compat


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
        args.extend(["--dangerously-skip-permissions"])
        args.extend(self.config.claude_flags)
        args.extend(["--output-format", "json"])
        return args

    def _build_command(self, prompt: str, session_id: str | None = None) -> str:
        """Build shell command string (for logging/tests only)."""
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
        wsl_distro: str | None = None,
    ) -> ProviderResponse:
        """Run Claude Code CLI and return parsed response."""
        if wsl_distro:
            return await self._run_wsl(prompt, work_dir, session_id, wsl_distro)

        args = self._build_args(prompt, session_id)
        logger.info("Running claude (cwd=%s, resume=%s)", work_dir, bool(session_id))
        start_time = time.monotonic()

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=_ENV,
            )

            timeout_seconds = self.config.subprocess_timeout_minutes * 60
            try:
                if timeout_seconds > 0:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout_seconds
                    )
                else:
                    stdout, stderr = await process.communicate()
            except TimeoutError:
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

    async def _run_wsl(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None,
        wsl_distro: str,
    ) -> ProviderResponse:
        """Run Claude Code CLI inside a WSL distribution."""
        # Resolve actual claude path inside WSL (handles nvm/npm installs)
        claude_bin = _resolve_wsl_cli(wsl_distro, "claude")

        # Build inner command for Linux shell
        parts = [claude_bin]
        if session_id:
            parts.extend(["--resume", session_id])
        # Escape single quotes for bash single-quoted string
        escaped_prompt = prompt.replace("'", "'\\''")
        parts.extend(["-p", f"'{escaped_prompt}'"])
        parts.extend(["--dangerously-skip-permissions"])
        parts.extend(self.config.claude_flags)
        parts.extend(["--output-format", "json"])
        inner_cmd = " ".join(parts)

        # Use create_subprocess_exec to avoid Windows cmd.exe quoting issues
        wsl = _find_wsl_exe()
        args = [
            wsl, "-d", wsl_distro,
            "--cd", work_dir,
            "--", "bash", "-l", "-c", inner_cmd,
        ]
        logger.info("Running claude WSL [%s] (cwd=%s, resume=%s)", wsl_distro, work_dir, bool(session_id))
        start_time = time.monotonic()

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timeout_seconds = self.config.subprocess_timeout_minutes * 60
            try:
                if timeout_seconds > 0:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout_seconds
                    )
                else:
                    stdout, stderr = await process.communicate()
            except TimeoutError:
                logger.warning("Claude (WSL) timed out after %ds", timeout_seconds)
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
                logger.error("Claude (WSL) exited %d: %s", process.returncode, raw_stderr)
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
                error="wsl.exe not found — is WSL installed?",
            )
        except Exception as e:
            logger.exception("Unexpected error running Claude in WSL")
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


_kill_process = kill_process  # backward compat
