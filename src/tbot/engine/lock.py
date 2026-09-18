"""Single-instance lock.

Two engines on one account is not a redundancy story. Both would size positions
off the same equity, both would open trades against the same limits, and each
would reconcile the other's fills as unexplained venue positions. It is an easy
mistake to make — launchd already running the bot, then a manual
``scripts/run.py`` to check something.

Uses ``flock`` on a file held open for the process lifetime. The kernel releases
it automatically when the process dies, however it dies, so a crash cannot leave
a stale lock that blocks the restart launchd is about to perform.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

from tbot.core.logging import get_logger

log = get_logger("engine.lock")


class AlreadyRunning(RuntimeError):
    """Raised when another engine holds the lock."""


class SingleInstanceLock:
    """Advisory file lock scoped to one runtime directory."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            holder = handle.read().strip() or "unknown"
            handle.close()
            raise AlreadyRunning(
                f"Another engine is already running (pid {holder}).\n"
                f"Two engines on one account would double every position and each would "
                f"reconcile the other's fills as unexplained.\n"
                f"Stop the other one first:\n"
                f"  launchctl unload -w ~/Library/LaunchAgents/com.tbot.bot.plist\n"
                f"  # or: kill {holder}"
            ) from None

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        log.info("instance_lock_acquired", pid=os.getpid(), path=str(self.path))

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
