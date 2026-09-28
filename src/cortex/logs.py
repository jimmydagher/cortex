"""The one logging front door: plain-text lines to <paths.logs>/cortex.log (rotated) and stdout.

Every line goes through a bounded queue to one background writer, so a slow disk never
blocks a request. Three kinds of line share one format:

  2026-09-26T15:50:43-05:00 INFO    req=4f2a91c07b3e [laptop] commit: HX0002 → NEOCORTEX/WRITING.md
  2026-09-26T15:50:43-05:00 INFO    req=4f2a91c07b3e (access) POST /mcp 200 14ms
  2026-09-26T15:50:43-05:00 WARNING req=- (uvicorn.error) message from a library logger

`[who] action: detail` lines are activity: they also feed the GUI's Activity tab.
"""
from __future__ import annotations

import contextlib
import logging
import queue
import re
import sys
import threading
from collections import deque
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import QueueHandler, RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig
from .errors import describe

REQUEST_ID: ContextVar[str] = ContextVar("cortex_request_id", default="-")
ACTIVITY_LOGGER = "cortex.activity"
ACCESS_TAG = "access"
LIBRARY_LOGGERS = ("", "uvicorn", "mcp")  # "" is the root logger
LINE_FORMAT = "%(asctime)s %(levelname)-7s req=%(request_id)s %(display)s"
LOG_LINE = re.compile(
    r"^(?P<ts>\S+) (?P<level>[A-Z]+) +req=(?P<request_id>\S+) \[(?P<who>[^\]]*)\] (?P<action>[^:]*?)(?:: (?P<detail>.*))?$"
)
WORKER_POLL_SECONDS = 0.5
# A log record, a flush marker the writer sets, or None to stop the writer.
QueueItem = logging.LogRecord | threading.Event | None


def local_iso(timestamp: float | None = None) -> str:
    """An ISO 8601 timestamp with the container's UTC offset, to the second.

    Args:
        timestamp: seconds since the epoch; now when omitted.

    Returns:
        e.g. `2026-09-26T15:50:43-05:00`.
    """
    moment = datetime.fromtimestamp(timestamp) if timestamp is not None else datetime.now()
    return moment.astimezone().isoformat(timespec="seconds")


def _record(name: str, level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 0, message, (), None)


def _one_line(text: str) -> str:
    return " | ".join(part.strip() for part in text.splitlines() if part.strip())


class _Formatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802 - logging's API name
        return local_iso(record.created)


class _Context(logging.Filter):
    """Runs in the caller's thread: stamps the request id and the display text on each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        if record.name == ACTIVITY_LOGGER:
            record.display = record.getMessage()
        else:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message = f"{message} {describe(record.exc_info[1])}"
            record.display = f"({record.name}) {_one_line(message)}"
        return True


class _BoundedQueueHandler(QueueHandler):
    """Never blocks the caller: a full queue drops the line and counts it."""

    def __init__(self, target: queue.Queue[QueueItem], owner: Logger) -> None:
        super().__init__(target)  # the queue also carries flush markers and the stop signal
        self._owner = owner

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        record = super().prepare(record)
        record.exc_info = None  # already folded into `display` as one line
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._owner.count_dropped()


class Logger:
    """Cortex's central logger. One instance per process; pass it to whatever needs to log."""

    def __init__(self, config: LoggingConfig, logs_dir: Path) -> None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = logs_dir / config.file
        self.events: deque[dict[str, str]] = deque(maxlen=config.recent_events)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._drain_seconds = config.drain_seconds
        self._load_recent()

        formatter = _Formatter(LINE_FORMAT)
        self._file = RotatingFileHandler(self.path, maxBytes=config.max_bytes, backupCount=config.backups, encoding="utf-8", delay=True)
        self._stdout = logging.StreamHandler(sys.stdout)
        for handler in (self._file, self._stdout):
            handler.setFormatter(formatter)

        self._queue: queue.Queue[QueueItem] = queue.Queue(maxsize=config.queue_size)
        self._front = _BoundedQueueHandler(self._queue, self)
        self._front.addFilter(_Context())
        self._level = logging.getLevelName(config.level)
        # A standalone logger (not logging.getLogger) so each instance writes only to its own file.
        self._activity = logging.Logger(ACTIVITY_LOGGER, level=self._level)
        self._activity.addHandler(self._front)
        self._attached: list[logging.Logger] = []
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._drain, name="cortex-log", daemon=True)
        self._worker.start()

    # ---------- writing ----------

    def is_enabled(self, level: int) -> bool:
        """Cheap check before building an expensive message.

        Args:
            level: a logging level such as logging.DEBUG.

        Returns:
            True when a line at this level would be written.
        """
        return self._activity.isEnabledFor(level)

    def event(self, who: str, action: str, detail: str = "", level: int = logging.INFO) -> None:
        """Log an activity line (`[who] action: detail`), also shown in the GUI.

        Args:
            who: the actor: a client's key label, `gui`, `server`...
            action: a short verb phrase without a colon.
            detail: free text; flattened to one line and capped at 300 characters.
            level: the logging level.
        """
        if not self.is_enabled(level):
            return
        who = re.sub(r"[\[\]\s]+", "-", who).strip("-") or "?"
        detail = " ".join(detail.split())[:300]
        self._activity.log(level, f"[{who}] {action}" + (f": {detail}" if detail else ""))
        self.events.append({
            "ts": local_iso(), "level": logging.getLevelName(level), "request_id": REQUEST_ID.get(),
            "who": who, "action": action, "detail": detail,
        })

    def access(self, method: str, path: str, status: int, duration_ms: float) -> None:
        """Log the one access line for a finished request (never bodies, cookies or headers).

        Args:
            method: HTTP method.
            path: request path without the query string.
            status: response status code.
            duration_ms: time from request start to response end.
        """
        if self.is_enabled(logging.INFO):
            self._front.handle(_record(ACCESS_TAG, logging.INFO, f"{method} {path} {status} {duration_ms:.0f}ms"))

    def error(self, error: BaseException, context: dict[str, str]) -> None:
        """Global-error-handler reporter: one ERROR line with the call chain, redacted.

        Args:
            error: the unhandled exception.
            context: where it happened (request id, thread...).
        """
        where = " ".join(f"{key}={value}" for key, value in context.items() if key != "request_id")
        token = REQUEST_ID.set(context["request_id"]) if "request_id" in context else None
        try:
            self._activity.log(logging.ERROR, f"[error] unhandled{(' ' + where) if where else ''}: {describe(error)}")
        finally:
            if token is not None:
                REQUEST_ID.reset(token)

    def warn(self, message: str) -> None:
        """Log a WARNING from Cortex itself.

        Args:
            message: what's wrong; the run continues.
        """
        self.event("cortex", "warning", message, logging.WARNING)

    # ---------- library loggers ----------

    def attach_library_loggers(self) -> None:
        """Route the root, uvicorn and MCP SDK loggers through this front door (one logging path)."""
        for name in LIBRARY_LOGGERS:
            library = logging.getLogger(name)
            library.addHandler(self._front)
            if name == "":
                library.setLevel(self._level)
            self._attached.append(library)

    def _detach(self) -> None:
        for library in self._attached:
            library.removeHandler(self._front)
        self._attached.clear()

    # ---------- background writer ----------

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=WORKER_POLL_SECONDS)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if item is None:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            lost = self._take_dropped()
            if lost:
                self._write(_record(ACTIVITY_LOGGER, logging.WARNING, f"[cortex] dropped {lost} log lines: the log queue was full"))
            self._write(item)

    def _write(self, record: logging.LogRecord) -> None:
        if not hasattr(record, "display"):
            _Context().filter(record)
        for handler in (self._file, self._stdout):
            handler.handle(record)

    def count_dropped(self) -> None:
        """Record one line lost to a full queue (called by the queue handler)."""
        with self._dropped_lock:
            self._dropped += 1

    def _take_dropped(self) -> int:
        with self._dropped_lock:
            lost, self._dropped = self._dropped, 0
        return lost

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until everything queued so far is written, at most `timeout` seconds.

        Args:
            timeout: the most seconds to wait; logging.drain_seconds when omitted.

        Returns:
            True when the writer caught up in time.
        """
        deadline = self._drain_seconds if timeout is None else timeout
        done = threading.Event()
        try:
            self._queue.put(done, timeout=deadline)
        except queue.Full:
            return False
        return done.wait(deadline)

    def close(self) -> None:
        """Drain within logging.drain_seconds, then close the file.

        Anything logged after this can only reach stdout: the file is closed.
        """
        self._detach()
        self._stop.set()
        with contextlib.suppress(queue.Full):  # a full queue at shutdown: the timed join below still bounds the wait
            self._queue.put(None, timeout=self._drain_seconds)
        self._worker.join(self._drain_seconds)
        if not self._worker.is_alive():
            self._file.close()
        self._activity.handlers = [self._stdout]
        self._stdout.addFilter(_Context())

    # ---------- GUI feed ----------

    def _load_recent(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines[-(self.events.maxlen or 0):]:
            match = LOG_LINE.match(line)
            if match:
                self.events.append({**match.groupdict(), "detail": match["detail"] or ""})

    def recent(self, limit: int) -> list[dict[str, str]]:
        """Newest activity events first, for the GUI.

        Args:
            limit: how many events to return at most.

        Returns:
            Event dicts with ts, level, request_id, who, action, detail.
        """
        return list(self.events)[-limit:][::-1]
