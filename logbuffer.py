"""
A small in-memory ring buffer of recently logged lines.

This exists so the optional Remote web dashboard can expose a live view of the
application's logs (gated behind the `web_server_show_logs` setting) without
depending on the desktop GUI's Output tab, and without re-reading the log file
from disk on every request. It's attached to the "TwitchDrops" logger once, in
main.py, right next to the other handlers (console/file), so it's populated
from the very first log line regardless of whether the GUI or the web
dashboard end up using it.
"""
from __future__ import annotations

import logging
from collections import deque


class LogBuffer:
    def __init__(self, maxlen: int = 500) -> None:
        self._lines: "deque[str]" = deque(maxlen=maxlen)

    def append(self, line: str) -> None:
        self._lines.append(line)

    def get_lines(self, limit: int | None = None) -> list[str]:
        lines = list(self._lines)
        if limit is not None and limit > 0:
            lines = lines[-limit:]
        return lines


# Single shared instance for the whole process.
buffer = LogBuffer()

# Lines exactly as shown in the desktop app's Output box (timestamp included); this is what
# the Remote dashboard's Logs tab shows.
console = LogBuffer(1000)


class BufferHandler(logging.Handler):
    """A logging.Handler that appends formatted records to the shared LogBuffer."""

    def __init__(self, target: LogBuffer | None = None) -> None:
        super().__init__()
        self._buffer = target if target is not None else buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
        except Exception:
            self.handleError(record)
