"""Portable helpers for non-blocking subprocess pipe reads."""

from __future__ import annotations

import os
import queue
import select
import threading
import time
from typing import IO, TextIO


def _readline_timeout(stream: IO[str] | TextIO, timeout_sec: float) -> str | None:
    """Read one line with timeout.

    On POSIX, uses ``select`` on the file descriptor. On Windows, pipes are not
    selectable, so we fall back to a short blocking read via a helper thread
    when the stream has an associated reader queue; otherwise we try a brief
    blocking readline with a worker thread.
    """
    if os.name != "nt":
        try:
            ready, _, _ = select.select([stream], [], [], max(0.0, timeout_sec))
        except (ValueError, OSError, TypeError):
            return None
        if not ready:
            return None
        line = stream.readline()
        if line == "":
            return None
        return line.rstrip("\n")

    # Windows: use a one-shot reader thread.
    result: queue.Queue[str | None] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            line = stream.readline()
            result.put(None if line == "" else line.rstrip("\n"))
        except Exception:  # noqa: BLE001
            result.put(None)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    try:
        return result.get(timeout=max(0.001, timeout_sec))
    except queue.Empty:
        return None


class LineReader:
    """Background line reader safe for Windows pipes and POSIX."""

    def __init__(self, stream: IO[str] | TextIO) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self._thread.start()

    def _run(self, stream: IO[str] | TextIO) -> None:
        try:
            while not self._stop.is_set():
                line = stream.readline()
                if line == "":
                    self._queue.put(None)
                    return
                self._queue.put(line.rstrip("\n"))
        except Exception:  # noqa: BLE001
            self._queue.put(None)

    def readline(self, timeout_sec: float = 0.2) -> str | None:
        try:
            item = self._queue.get(timeout=max(0.0, timeout_sec))
        except queue.Empty:
            return None
        return item

    def close(self) -> None:
        self._stop.set()
