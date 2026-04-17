"""npm `.cmd` shim resolution — bypass cmd.exe for multi-line argv.

Windows .cmd wrappers invoke node via cmd.exe, which truncates arguments at
embedded newlines when parsing `%1`/`%*`. This module finds the underlying
`node cli.js` invocation so we can launch it directly via
asyncio.create_subprocess_exec, preserving newlines end-to-end.
"""
from __future__ import annotations

import functools
import logging
import os
import platform
import re
import shutil as _shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_npm_shim(cmd_path: str) -> list[str] | None:
    """If cmd_path is an npm-style .cmd shim on Windows, return [node, js_path]."""
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
def resolve_cli_exec(cli_path: str) -> tuple[str, ...]:
    """Return argv prefix for a CLI. On Windows, bypass .cmd npm shims."""
    shim = resolve_npm_shim(cli_path)
    if shim:
        logger.info("Resolved npm shim %s -> %s", cli_path, shim)
        return tuple(shim)
    return (cli_path,)
