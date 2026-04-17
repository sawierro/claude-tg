import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Config
from bot.providers._env import build_subprocess_env
from bot.providers.base import (
    CLIProvider,
    ProviderResponse,
    ProviderSession,
    is_process_alive,
    run_subprocess,
)
from bot.providers.claude import (
    _find_wsl_exe,
    _get_wsl_distros,
    _get_wsl_home,
    _resolve_cli_exec,
    _resolve_wsl_cli,
    _wsl_path_to_windows,
)

logger = logging.getLogger(__name__)

CODEX_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SESSIONS_DIR = CODEX_DIR / "sessions"

# Additional paths where Codex CLI may store data
_CODEX_ALT_DIRS = [
    Path.home() / ".codex",
    Path.home() / ".local" / "share" / "codex",
]


_is_process_alive = is_process_alive  # backward compat


def _sqlite_connect(db_path: Path) -> sqlite3.Connection:
    """Open SQLite DB, copying to temp dir for UNC paths (\\\\wsl...).

    Copies .sqlite + WAL files (-shm, -wal) to preserve recent data.
    """
    path_str = str(db_path)
    if path_str.startswith("\\\\"):
        tmp_dir = tempfile.mkdtemp(prefix="codex_db_")
        try:
            dst = Path(tmp_dir) / db_path.name
            shutil.copy2(path_str, str(dst))
            for suffix in ("-shm", "-wal"):
                wal_src = Path(path_str + suffix)
                if wal_src.exists():
                    shutil.copy2(str(wal_src), str(dst) + suffix)
            conn = sqlite3.connect(str(dst))
            conn._tmp_dir = tmp_dir  # type: ignore[attr-defined]
            return conn
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
    return sqlite3.connect(path_str)


def _sqlite_close(conn: sqlite3.Connection) -> None:
    """Close SQLite connection and clean up temp dir if any."""
    conn.close()
    tmp_dir = getattr(conn, "_tmp_dir", None)
    if tmp_dir:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass


class CodexProvider(CLIProvider):
    """OpenAI Codex CLI provider."""

    name = "codex"

    def __init__(self, config: Config):
        self.config = config
        self._codex_path = config.codex_path if hasattr(config, "codex_path") else "codex"

    def _build_args(self, prompt: str, session_id: str | None = None) -> list[str]:
        """Build argument list for Codex CLI (no shell interpretation)."""
        args = list(_resolve_cli_exec(self._codex_path))
        args.append("exec")
        if session_id:
            args.extend(["resume", session_id])
        args.append(prompt)
        args.extend(self.config.codex_flags)
        args.append("--json")
        return args

    def _build_command(self, prompt: str, session_id: str | None = None) -> str:
        """Build shell command string (for logging/tests only)."""
        parts = [self._codex_path, "exec"]
        if session_id:
            parts.extend(["resume", session_id])
        parts.append(json.dumps(prompt))
        parts.extend(self.config.codex_flags)
        parts.append("--json")
        return " ".join(parts)

    async def run(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None = None,
        wsl_distro: str | None = None,
    ) -> ProviderResponse:
        """Run Codex CLI and return parsed response."""
        if wsl_distro:
            return await self._run_wsl(prompt, work_dir, session_id, wsl_distro)

        args = self._build_args(prompt, session_id)
        logger.info("Running codex (cwd=%s, resume=%s)", work_dir, bool(session_id))
        return await run_subprocess(
            args,
            cwd=work_dir,
            env=build_subprocess_env(),
            timeout_seconds=self.config.subprocess_timeout_minutes * 60,
            parse=self._parse_response,
            display_name="Codex",
            session_id=session_id,
            not_found_message=f"Codex CLI not found at '{self._codex_path}'",
        )

    async def _run_wsl(
        self,
        prompt: str,
        work_dir: str,
        session_id: str | None,
        wsl_distro: str,
    ) -> ProviderResponse:
        """Run Codex CLI inside a WSL distribution."""
        codex_bin = _resolve_wsl_cli(wsl_distro, "codex")

        import shlex
        parts = [shlex.quote(codex_bin), "exec"]
        if session_id:
            parts.extend(["resume", shlex.quote(session_id)])
        parts.append(shlex.quote(prompt))
        parts.extend(shlex.quote(flag) for flag in self.config.codex_flags)
        parts.append("--json")
        inner_cmd = " ".join(parts)

        args = [
            _find_wsl_exe(), "-d", wsl_distro,
            "--cd", work_dir,
            "--", "bash", "-l", "-c", inner_cmd,
        ]
        logger.info("Running codex WSL [%s] (cwd=%s, resume=%s)",
                    wsl_distro, work_dir, bool(session_id))
        return await run_subprocess(
            args,
            cwd=None,
            env=build_subprocess_env(),
            timeout_seconds=self.config.subprocess_timeout_minutes * 60,
            parse=self._parse_response,
            display_name="Codex(WSL)",
            session_id=session_id,
            not_found_message="wsl.exe not found — is WSL installed?",
        )

    def _parse_response(
        self, raw: str, fallback_sid: str | None, duration: float
    ) -> ProviderResponse:
        """Parse JSONL stream from Codex --json output. Extract final message."""
        session_id = fallback_sid or ""
        final_text = ""
        error = None
        tokens_in: int | None = None
        tokens_out: int | None = None

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

            # Extract session ID — multiple possible fields
            if event_type == "thread.started":
                session_id = event.get("sessionId", event.get("thread_id", session_id))

            # Extract final text from the last assistant message
            if event_type == "item.completed":
                item = event.get("item", {})
                if item.get("role") == "assistant":
                    content = item.get("content", [])
                    for c in content:
                        if c.get("type") == "text":
                            final_text = c.get("text", "")

            # New format: agent_message
            if event_type == "agent_message":
                text = event.get("text", "")
                if text:
                    final_text = text

            # Capture errors
            if event_type == "error":
                error = event.get("message", str(event))

            if event_type == "turn.failed":
                error = event.get("error", {}).get("message", "Turn failed")

            # Usage metrics may appear on turn.completed or as a dedicated event
            usage = event.get("usage")
            if isinstance(usage, dict):
                t_in = usage.get("input_tokens") or usage.get("prompt_tokens")
                t_out = usage.get("output_tokens") or usage.get("completion_tokens")
                if t_in is not None:
                    tokens_in = int(t_in)
                if t_out is not None:
                    tokens_out = int(t_out)

        # Fallback: if no structured events, use raw stdout as text
        if not final_text and not error:
            final_text = raw

        return ProviderResponse(
            session_id=session_id,
            text=final_text,
            cost=None,
            duration_seconds=duration,
            error=error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def list_sessions(self) -> list[ProviderSession]:
        """List active Codex sessions (native + WSL)."""
        sessions = self._list_native_sessions()
        sessions.extend(self._list_wsl_sessions())
        return sessions

    def _list_native_sessions(self) -> list[ProviderSession]:
        """List Codex sessions from native ~/.codex/."""
        logger.debug("Codex: scanning native %s (exists=%s)", CODEX_DIR, CODEX_DIR.exists())

        # Try new format first (state_*.sqlite → threads table)
        sessions = self._read_threads_from_dir(CODEX_DIR)
        if sessions:
            return sessions

        # Try legacy format (sessions.db / index.db → sessions table)
        sessions = self._read_legacy_db(CODEX_DIR)
        if sessions:
            return sessions

        # Fallback: scan session files
        return self._list_sessions_from_files()

    def _read_legacy_db(
        self, codex_dir: Path, wsl_distro: str = "",
    ) -> list[ProviderSession]:
        """Read sessions from legacy sessions.db / index.db format."""
        db_path = codex_dir / "sessions.db"
        if not db_path.exists():
            db_path = codex_dir / "index.db"
        if not db_path.exists():
            return []

        sessions = []
        try:
            conn = _sqlite_connect(db_path)
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

                started_at = datetime.now(tz=UTC)
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
                    wsl_distro=wsl_distro,
                ))
            _sqlite_close(conn)
        except Exception as e:
            logger.warning("Failed to read legacy Codex DB %s: %s", db_path, e)

        return sessions

    @staticmethod
    def _find_state_db(codex_dir: Path) -> Path | None:
        """Find the Codex state SQLite database (state_*.sqlite)."""
        # Try glob first
        try:
            candidates = sorted(codex_dir.glob("state*.sqlite"), reverse=True)
            if candidates:
                return candidates[0]
        except OSError:
            pass

        # Fallback: manual listing (glob may fail on UNC paths)
        try:
            for f in codex_dir.iterdir():
                name = f.name
                if name.startswith("state") and name.endswith(".sqlite") and "-" not in name:
                    return f
        except OSError:
            pass

        return None

    def diagnose(self) -> list[str]:
        """Return diagnostic lines for /debug."""
        lines = []

        # Native
        lines.append(f"Native dir: {CODEX_DIR} (exists={CODEX_DIR.exists()})")
        db = self._find_state_db(CODEX_DIR)
        lines.append(f"Native state DB: {db}")
        if CODEX_DIR.exists():
            try:
                files = [f.name for f in CODEX_DIR.iterdir() if f.name.startswith("state") or f.name.endswith(".db")]
                lines.append(f"DB files: {files}")
            except OSError as e:
                lines.append(f"iterdir error: {e}")

        # WSL
        from bot.providers.claude import _get_wsl_distros, _get_wsl_home, _wsl_path_to_windows
        distros = _get_wsl_distros()
        lines.append(f"WSL distros: {distros}")
        for distro in distros:
            home = _get_wsl_home(distro)
            lines.append(f"  [{distro}] home={home}")
            if not home:
                continue
            for sub in (".codex", ".local/share/codex"):
                d = _wsl_path_to_windows(distro, f"{home}/{sub}")
                exists = False
                try:
                    exists = d.exists()
                except OSError:
                    pass
                if not exists:
                    continue
                lines.append(f"  [{distro}] dir={d}")
                try:
                    all_files = [f.name for f in d.iterdir()]
                    lines.append(f"  [{distro}] files={all_files[:20]}")
                except OSError as e:
                    lines.append(f"  [{distro}] iterdir error: {e}")
                db = self._find_state_db(d)
                lines.append(f"  [{distro}] state DB={db}")
                if db:
                    try:
                        conn = _sqlite_connect(db)
                        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                        lines.append(f"  [{distro}] tables={[t[0] for t in tables]}")
                        try:
                            count = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
                            active = conn.execute("SELECT COUNT(*) FROM threads WHERE archived=0").fetchone()[0]
                            lines.append(f"  [{distro}] threads: {count} total, {active} active")
                        except Exception as e:
                            lines.append(f"  [{distro}] threads query: {e}")
                        _sqlite_close(conn)
                    except Exception as e:
                        lines.append(f"  [{distro}] DB open error: {e}")

        return lines

    def _read_threads_from_dir(
        self, codex_dir: Path, wsl_distro: str = "",
    ) -> list[ProviderSession]:
        """Read Codex threads from a state_*.sqlite database."""
        if not codex_dir.exists():
            return []

        db_path = self._find_state_db(codex_dir)
        if not db_path:
            logger.debug("Codex: no state*.sqlite in %s", codex_dir)
            return []

        sessions = []
        logger.debug("Codex: reading %s", db_path)
        try:
            conn = _sqlite_connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT id, cwd, title, created_at, updated_at, archived "
                "FROM threads WHERE archived=0 ORDER BY updated_at DESC LIMIT 20"
            )
            now_ts = int(time.time())
            for row in cursor:
                r = dict(row)
                sid = r.get("id", "")
                if not sid:
                    continue

                cwd = r.get("cwd", "")
                title = r.get("title", "")
                created_ts = r.get("created_at", 0)
                updated_ts = r.get("updated_at", 0)

                # Timestamps are Unix epoch seconds
                started_at = datetime.fromtimestamp(
                    created_ts, tz=UTC
                ) if created_ts else datetime.now(tz=UTC)

                # Consider "alive" if updated in the last 10 minutes
                alive = (now_ts - updated_ts) < 600 if updated_ts else False

                sessions.append(ProviderSession(
                    session_id=sid,
                    pid=0,
                    cwd=cwd,
                    started_at=started_at,
                    is_alive=alive,
                    slug=title or sid[:8],
                    provider="codex",
                    wsl_distro=wsl_distro,
                ))
            _sqlite_close(conn)
            logger.debug("Codex: found %d threads in %s", len(sessions), db_path)
        except Exception as e:
            logger.warning("Failed to read Codex state DB %s: %s", db_path, e)

        return sessions

    def _list_wsl_sessions(self) -> list[ProviderSession]:
        """Scan WSL distributions for Codex sessions."""
        sessions = []
        distros = _get_wsl_distros()
        logger.debug("Codex WSL: distros=%s", distros)
        for distro in distros:
            home = _get_wsl_home(distro)
            if not home:
                logger.debug("Codex WSL: no home for %s", distro)
                continue

            # Check multiple possible Codex data locations
            codex_dirs = [
                _wsl_path_to_windows(distro, f"{home}/.codex"),
                _wsl_path_to_windows(distro, f"{home}/.local/share/codex"),
            ]

            codex_dir = None
            for candidate in codex_dirs:
                try:
                    if candidate.exists():
                        codex_dir = candidate
                        break
                except OSError:
                    continue

            if not codex_dir:
                logger.debug("Codex WSL [%s]: no codex dir found at %s", distro,
                             [str(d) for d in codex_dirs])
                continue

            logger.debug("Codex WSL [%s]: found %s", distro, codex_dir)

            # Try new format (state_*.sqlite → threads), then legacy, then files
            found = self._read_threads_from_dir(codex_dir, wsl_distro=distro)
            if not found:
                found = self._read_legacy_db(codex_dir, wsl_distro=distro)
            sessions.extend(found)

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
                    zst_file.stat().st_mtime, tz=UTC
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
        """Find session history file (native + WSL). Codex uses .jsonl.zst."""
        # Native paths
        if SESSIONS_DIR.exists():
            for zst_file in SESSIONS_DIR.rglob(f"*{session_id}*.jsonl.zst"):
                return str(zst_file)
            for jsonl_file in SESSIONS_DIR.rglob(f"*{session_id}*.jsonl"):
                return str(jsonl_file)

        # WSL paths
        for distro in _get_wsl_distros():
            home = _get_wsl_home(distro)
            if not home:
                continue
            for sub in (".codex/sessions", ".local/share/codex/sessions"):
                wsl_sessions = _wsl_path_to_windows(distro, f"{home}/{sub}")
                try:
                    if not wsl_sessions.exists():
                        continue
                except OSError:
                    continue
                for zst_file in wsl_sessions.rglob(f"*{session_id}*.jsonl.zst"):
                    return str(zst_file)
                for jsonl_file in wsl_sessions.rglob(f"*{session_id}*.jsonl"):
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
