"""WSL helpers shared between Claude and Codex providers.

Extracted from providers/claude.py so providers/codex.py can import from a
common location instead of reaching into a sibling module's private API.
"""
from __future__ import annotations

import functools
import logging
import os
import platform
import shutil as _shutil
import subprocess as _subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_wsl_unc_prefix_cache: dict[str, str] = {}

# `wsl.exe -l -q` spawn costs ~200-500ms on Windows; distros list rarely
# changes, so cache it for a short TTL to keep /sessions and /connect snappy.
_WSL_DISTROS_TTL_SECONDS = 60.0
_wsl_distros_cache: tuple[float, list[str]] | None = None


def find_wsl_exe() -> str:
    """Find wsl.exe reliably — shutil.which, then System32 fallbacks."""
    found = _shutil.which("wsl")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe",
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Sysnative" / "wsl.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return "wsl"


def resolve_wsl_cli(distro: str, cli_name: str) -> str:
    """Find a CLI tool inside WSL — handles nvm/npm-installed binaries."""
    try:
        result = _subprocess.run(
            [find_wsl_exe(), "-d", distro, "--", "bash", "-lc", f"command -v {cli_name}"],
            capture_output=True, text=True, timeout=10,
        )
        path = result.stdout.strip()
        if result.returncode == 0 and path:
            return path
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        pass
    return cli_name


def get_wsl_distros() -> list[str]:
    """Return installed WSL distribution names (cached for 60s)."""
    if platform.system() != "Windows":
        return []

    global _wsl_distros_cache
    now = time.monotonic()
    if _wsl_distros_cache is not None:
        ts, cached = _wsl_distros_cache
        if now - ts < _WSL_DISTROS_TTL_SECONDS:
            return cached

    wsl = find_wsl_exe()
    try:
        result = _subprocess.run(
            [wsl, "-l", "-q"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            distros: list[str] = []
        else:
            try:
                text = result.stdout.decode("utf-16-le")
            except UnicodeDecodeError:
                text = result.stdout.decode("utf-8", errors="replace")
            distros = [
                line.strip().strip("\x00")
                for line in text.strip().split("\n")
                if line.strip().strip("\x00")
            ]
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        distros = []

    _wsl_distros_cache = (now, distros)
    return distros


def invalidate_wsl_distros_cache() -> None:
    """Drop the cached WSL distros list (call after known WSL state changes)."""
    global _wsl_distros_cache
    _wsl_distros_cache = None


@functools.lru_cache(maxsize=16)
def get_wsl_home(distro: str) -> str | None:
    """Return the default user's home directory inside a WSL distro."""
    try:
        result = _subprocess.run(
            [find_wsl_exe(), "-d", distro, "--", "printenv", "HOME"],
            capture_output=True, text=True, timeout=10,
        )
        home = result.stdout.strip()
        if result.returncode == 0 and home:
            return home
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        pass
    return None


def wsl_path_to_windows(distro: str, linux_path: str) -> Path:
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
