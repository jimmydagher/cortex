"""Typed errors, their HTTP status and exit outcomes, and the one global error handler.

Everything Cortex raises on purpose is a CortexError subtype, so a handler can tell bad
input from a missing note from a broken setup. HTTP status is decided here, in one
table, never ad hoc in a handler. Anything not anticipated reaches ErrorHandler.
"""
from __future__ import annotations

import re
import sys
import threading
import traceback
from collections.abc import Callable
from enum import IntEnum, StrEnum
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    """Machine-readable error codes, the same for the GUI API, the /mcp gate and logs."""

    INVALID_INPUT = "invalid_input"
    INVALID_HOST = "invalid_host"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    NOT_CONFIGURED = "not_configured"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_FAILED = "upstream_failed"
    STARTUP_FAILED = "startup_failed"
    INTERNAL = "internal_error"


HTTP_STATUS: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.INVALID_INPUT: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.INVALID_HOST: HTTPStatus.BAD_REQUEST,
    ErrorCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.METHOD_NOT_ALLOWED: HTTPStatus.METHOD_NOT_ALLOWED,
    ErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.NOT_CONFIGURED: HTTPStatus.CONFLICT,
    ErrorCode.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.UPSTREAM_FAILED: HTTPStatus.BAD_GATEWAY,
    ErrorCode.STARTUP_FAILED: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.INTERNAL: HTTPStatus.INTERNAL_SERVER_ERROR,
}

GENERIC_MESSAGE = "Something went wrong on the server. Quote the request id when reporting it."


class ExitCode(IntEnum):
    """Process outcomes; documented in docs/cheat-sheet.md."""

    SUCCEEDED = 0
    FAILED = 1
    INVALID_INPUT = 2
    STARTUP_FAILURE = 3
    INTERRUPTED = 130


class CortexError(Exception):
    """Root of every error Cortex raises deliberately. The message is safe to show a client."""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    @property
    def status(self) -> HTTPStatus:
        """HTTP status for this error, from the one mapping table."""
        return HTTP_STATUS[self.code]


class InvalidInputError(CortexError):
    """The caller sent something malformed or out of range."""

    code = ErrorCode.INVALID_INPUT


class InvalidHostError(CortexError):
    """The request's Host header isn't in server.allowed_hosts."""

    code = ErrorCode.INVALID_HOST


class UnauthorizedError(CortexError):
    """No valid credential (API key or GUI session)."""

    code = ErrorCode.UNAUTHORIZED


class ForbiddenError(CortexError):
    """Authenticated, but the action isn't allowed (e.g. a protected note)."""

    code = ErrorCode.FORBIDDEN


class NotFoundError(CortexError):
    """A note, SYNAPSE entry, key or route that doesn't exist."""

    code = ErrorCode.NOT_FOUND


class ConflictError(CortexError):
    """The request clashes with current state (already approved, target exists)."""

    code = ErrorCode.CONFLICT


class NotConfiguredError(CortexError):
    """The brain isn't set up yet, so the capability can't run."""

    code = ErrorCode.NOT_CONFIGURED


class RateLimitedError(CortexError):
    """Too many attempts in the current window."""

    code = ErrorCode.RATE_LIMITED


class UpstreamError(CortexError):
    """An outside system (GitHub) failed; the detail is logged, not returned."""

    code = ErrorCode.UPSTREAM_FAILED


class StartupError(CortexError):
    """Cortex can't start: every problem found is listed at once."""

    code = ErrorCode.STARTUP_FAILED

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class ConfigError(StartupError):
    """The merged configuration failed validation."""


class SecretError(StartupError):
    """A required secret couldn't be read."""


# ---------- redaction and one-line formatting ----------

_REDACTIONS = [
    (re.compile(r"(?i)(bearer\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"ctx_[A-Za-z0-9_-]{16,}"), "ctx_[redacted]"),
    (re.compile(r"(?i)((?:password|secret|token|key)\s*[=:]\s*)\S+"), r"\1[redacted]"),
]


def redact(text: str) -> str:
    """Mask bearer tokens, Cortex API keys and `name = value` secrets in text.

    Args:
        text: any text that may be written to a log or handed to a reporter.

    Returns:
        The text with credential-looking values replaced by `[redacted]`.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def describe(error: BaseException) -> str:
    """One line for an error: type, message and the call chain, innermost frame first.

    Args:
        error: the exception to describe.

    Returns:
        `Type: message @ file.py:12 in function <- file.py:40 in caller`, redacted.
    """
    frames = traceback.extract_tb(error.__traceback__)[::-1][:8]
    chain = " <- ".join(f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}" for frame in frames)
    message = " ".join(str(error).split())
    return redact(f"{type(error).__name__}: {message}" + (f" @ {chain}" if chain else ""))


# ---------- the global error handler ----------

Reporter = Callable[[BaseException, dict[str, str]], None]


def _stderr_reporter(error: BaseException, context: dict[str, str]) -> None:
    where = " ".join(f"{key}={value}" for key, value in context.items())
    print(f"ERROR unhandled {where}: {describe(error)}", file=sys.stderr, flush=True)


class ErrorHandler:
    """The single place every unhandled error ends up.

    Registered before configuration or logging exist, so early failures go to stderr;
    use_logger() swaps that for the central logger once it's up. Reporters run in
    order, logging first; a project adds one with add_reporter(), never by editing
    this class. Nothing in here may raise: a failing step degrades to silence.
    """

    def __init__(self) -> None:
        self._reporters: list[Reporter] = [_stderr_reporter]
        self._lock = threading.Lock()

    def use_logger(self, reporter: Reporter) -> None:
        """Make the central logger the first reporter, replacing stderr.

        Args:
            reporter: a callable that logs an error with its context.
        """
        with self._lock:
            self._reporters[0] = reporter

    def add_reporter(self, reporter: Reporter) -> None:
        """Append a reporter (a pager, a queue...) that runs after logging.

        Args:
            reporter: a callable taking the error and its context.
        """
        with self._lock:
            self._reporters.append(reporter)

    def handle(self, error: BaseException, context: dict[str, str] | None = None) -> None:
        """Report an unhandled error through every reporter; never raises.

        Args:
            error: the exception that escaped.
            context: extra fields for the report, such as the request id.
        """
        try:
            with self._lock:
                reporters = list(self._reporters)
        except Exception:  # noqa: BLE001 - the handler must never raise
            return
        for reporter in reporters:
            try:
                reporter(error, dict(context or {}))
            except Exception:  # noqa: BLE001 - a failing reporter never stops the others
                continue

    def install(self) -> None:
        """Wire the handler into Python's uncaught-error hooks (main thread and workers)."""

        def excepthook(kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
            self.handle(error.with_traceback(trace), {"where": "main"})

        def thread_hook(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is not None:
                self.handle(args.exc_value, {"where": f"thread {args.thread.name if args.thread else '?'}"})

        sys.excepthook = excepthook
        threading.excepthook = thread_hook

    def asyncio_hook(self, loop: Any, context: dict[str, Any]) -> None:
        """asyncio's exception handler: errors from tasks nobody awaited.

        Args:
            loop: the event loop (unused, part of asyncio's signature).
            context: asyncio's error context with `message` and maybe `exception`.
        """
        error = context.get("exception") or RuntimeError(str(context.get("message", "asyncio error")))
        self.handle(error, {"where": "asyncio"})
