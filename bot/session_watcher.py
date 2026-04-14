import asyncio
import logging
from pathlib import Path

from bot.providers.base import CLIProvider

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 3


class SessionWatcher:
    """Watches a session's history file and calls a callback on new responses."""

    def __init__(
        self,
        session_id: str,
        session_name: str,
        provider: CLIProvider,
        callback,
    ):
        self.session_id = session_id
        self.session_name = session_name
        self._provider = provider
        self._callback = callback
        self._task: asyncio.Task | None = None
        self._paused = asyncio.Event()
        self._paused.set()
        self._skip_to_end = False

    def start(self) -> None:
        """Start watching in background."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("Watcher started for session %s (%s)", self.session_name, self.session_id[:8])

    def stop(self) -> None:
        """Stop watching."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Watcher stopped for session %s", self.session_name)

    def pause(self) -> None:
        """Pause watching (e.g. while bot is sending via --resume)."""
        self._paused.clear()

    def resume(self) -> None:
        """Resume watching after pause. Skip anything written during pause."""
        self._skip_to_end = True
        self._paused.set()

    async def _watch_loop(self) -> None:
        """Main watch loop — tail the history file for new responses."""
        jsonl_str = self._provider.get_session_jsonl_path(self.session_id)
        if not jsonl_str:
            logger.warning("History file not found for session %s", self.session_id[:8])
            return

        jsonl_path = Path(jsonl_str)

        try:
            file_pos = jsonl_path.stat().st_size
        except OSError:
            file_pos = 0

        logger.info("Watching %s from position %d", jsonl_path.name, file_pos)

        try:
            while True:
                await self._paused.wait()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

                try:
                    current_size = jsonl_path.stat().st_size
                except OSError:
                    continue

                if self._skip_to_end:
                    self._skip_to_end = False
                    file_pos = current_size
                    continue

                if current_size <= file_pos:
                    continue

                try:
                    with open(jsonl_path, "r", encoding="utf-8") as f:
                        f.seek(file_pos)
                        new_data = f.read()
                        file_pos = f.tell()
                except OSError as e:
                    logger.warning("Error reading history file: %s", e)
                    continue

                for line in new_data.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue

                    text = self._provider.extract_end_turn_text(line)
                    if text:
                        logger.info(
                            "Watcher detected response in %s: %s",
                            self.session_name, text[:80]
                        )
                        try:
                            await self._callback(
                                self.session_id, self.session_name, text
                            )
                        except Exception:
                            logger.exception("Watcher callback failed")

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Watcher loop crashed for %s", self.session_name)
