"""Process tracking for CLI subprocesses — enables kill-on-shutdown and /cancel."""
from __future__ import annotations

import asyncio
import logging

from bot.providers.base import kill_process

logger = logging.getLogger(__name__)

_active: set[asyncio.subprocess.Process] = set()


def register(process: asyncio.subprocess.Process) -> None:
    _active.add(process)


def unregister(process: asyncio.subprocess.Process) -> None:
    _active.discard(process)


async def communicate_tracked(
    process: asyncio.subprocess.Process, timeout_seconds: int
) -> tuple[bytes, bytes]:
    """Run process.communicate() while tracking the process for shutdown/kill."""
    register(process)
    try:
        if timeout_seconds > 0:
            return await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        return await process.communicate()
    finally:
        unregister(process)


async def kill_all() -> int:
    """Terminate all tracked processes. Returns how many were killed."""
    procs = list(_active)
    count = 0
    for p in procs:
        if p.returncode is None:
            try:
                await kill_process(p)
                count += 1
            except Exception:
                logger.exception("Failed to kill process pid=%s", p.pid)
    _active.clear()
    return count


def active_count() -> int:
    """Return number of currently tracked processes."""
    return sum(1 for p in _active if p.returncode is None)
