"""The logging front door, the global error handler and the secrets accessor."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from conftest import make_config

from cortex.config import Config
from cortex.errors import ErrorHandler, SecretError, describe, redact
from cortex.logs import Logger
from cortex.secrets import Secrets, env_name


def test_level_floor_and_messages_after_close(tmp_path: Path, brain_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path, brain_dir, logging={"level": "WARNING"})
    logger = Logger(config.logging, config.paths.logs)
    assert not logger.is_enabled(logging.INFO)
    logger.event("gui", "login", "10.0.0.1")
    logger.event("gui", "login failed", "10.0.0.2", level=logging.WARNING)
    logger.close()
    logger.event("gui", "late", "after close", level=logging.WARNING)
    text = logger.path.read_text(encoding="utf-8")
    assert "login failed: 10.0.0.2" in text and "[gui] login:" not in text and "late" not in text
    assert "[gui] late: after close" in capsys.readouterr().out  # the file is closed: stdout only


def test_full_queue_drops_and_reports_instead_of_blocking(tmp_path: Path, brain_dir: Path) -> None:
    # Regression (F-025): an unbounded queue hides a stuck writer until memory runs out.
    config = make_config(tmp_path, brain_dir, logging={"queue_size": 100})
    logger = Logger(config.logging, config.paths.logs)
    gate = threading.Event()
    original = logger._write

    def stuck_writer(record: logging.LogRecord) -> None:
        gate.wait(5)
        original(record)

    logger._write = stuck_writer  # type: ignore[method-assign]  # simulate a slow disk
    try:
        for number in range(300):
            logger.event("load", "line", str(number))
        gate.set()
        assert logger.flush(5)
    finally:
        logger._write = original  # type: ignore[method-assign]
        logger.close()
    assert "dropped" in logger.path.read_text(encoding="utf-8")


def test_library_loggers_share_the_front_door(logger: Logger) -> None:
    logger.attach_library_loggers()
    try:
        logging.getLogger("uvicorn.error").warning("port in use")
        assert logger.flush()
    finally:
        logger._detach()
    assert "(uvicorn.error) port in use" in logger.path.read_text(encoding="utf-8")


def test_error_handler_never_raises_and_reporters_all_run() -> None:
    handler = ErrorHandler()
    seen: list[str] = []

    def broken(_error: BaseException, _context: dict[str, str]) -> None:
        raise RuntimeError("reporter bug")

    handler.use_logger(broken)
    handler.add_reporter(lambda error, context: seen.append(f"{error}|{context['where']}"))
    handler.handle(ValueError("boom"), {"where": "test"})
    assert seen == ["boom|test"]


def test_describe_is_one_redacted_line() -> None:
    try:
        raise RuntimeError("token ctx_ABCDEFGHIJKLMNOPQRSTUV and\nAuthorization: Bearer abc.def")
    except RuntimeError as error:
        text = describe(error)
    assert "\n" not in text and "ctx_ABCDEF" not in text and "abc.def" not in text
    assert text.startswith("RuntimeError:") and "test_logs_errors_secrets.py:" in text
    assert redact("password = hunter2") == "password = [redacted]"


def test_secrets_come_from_files_and_env_only_when_allowed(tmp_path: Path, brain_dir: Path, config: Config) -> None:
    store = Secrets(config.secrets, environ={"CORTEX_ADMIN_PWD": "from-env"})
    assert store.get("cortex-admin-pwd") == "test-password"
    (config.secrets.dir / "cortex-admin-pwd").unlink()
    with pytest.raises(SecretError, match="cortex-admin-pwd is missing"):
        store.get("cortex-admin-pwd")  # fallback is off in the test (deployed-like) config

    local = make_config(tmp_path, brain_dir, secrets={"allow_env_fallback": True})
    (local.secrets.dir / "cortex-admin-pwd").unlink()
    warnings: list[str] = []
    assert Secrets(local.secrets, environ={env_name("cortex-admin-pwd"): "from-env"}, warn=warnings.append).get("cortex-admin-pwd") == "from-env"
    assert warnings and "from-env" not in warnings[0]
