"""Single-process lock.

The MVP needs exactly one worker. Two mechanisms from the approved design are
combined, because each alone is insufficient:

* an ``flock`` advisory lock on a lock file — released automatically by the OS
  when the process dies (including ``SIGKILL``), so a crash never leaves a stale
  lock that blocks restarts;
* the recorded PID — so an operator can see **which** process holds the lock
  rather than only that it is held.

This is deliberately not a distributed lease: one VM, one worker.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path

from .util import utcnow_iso


class LockBusyError(Exception):
    """Another live process already holds the worker lock."""

    def __init__(self, path: Path, holder: dict[str, object] | None) -> None:
        self.path = path
        self.holder = holder or {}
        detail = ""
        if self.holder:
            pid = self.holder.get("pid")
            started = self.holder.get("started_at")
            cmd = self.holder.get("command")
            detail = f" held by pid={pid} started_at={started}" + (f" ({cmd})" if cmd else "")
        super().__init__(f"worker lock {path} is already held{detail}")


class WorkerLock:
    """Context manager holding an exclusive lock for the lifetime of the worker."""

    def __init__(self, path: Path | str, *, command: str | None = None) -> None:
        self.path = Path(path)
        self.command = command
        self._fd: int | None = None
        self.acquired = False

    def acquire(self) -> "WorkerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise
            holder = self._read_holder()
            os.close(fd)
            raise LockBusyError(self.path, holder) from None

        os.ftruncate(fd, 0)
        payload = {
            "pid": os.getpid(),
            "started_at": utcnow_iso(),
            "command": self.command or "agent-dispatch worker",
        }
        os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
        os.fsync(fd)

        self._fd = fd
        self.acquired = True
        return self

    def _read_holder(self) -> dict[str, object] | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        return data if isinstance(data, dict) else None

    def holder(self) -> dict[str, object] | None:
        return self._read_holder()

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best effort on release
            pass
        finally:
            os.close(self._fd)
            self._fd = None
            self.acquired = False

    def __enter__(self) -> "WorkerLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
