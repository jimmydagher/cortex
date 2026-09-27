"""The two things Cortex can be asked to do: serve, or validate its configuration.

Each command takes the already-loaded config and central logger and returns an ExitCode;
the entry point in __main__ only wires these together.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from enum import StrEnum

import uvicorn

from .config import Config, summary
from .errors import ErrorHandler, ExitCode, SecretError, StartupError
from .logs import Logger
from .secrets import Secrets
from .web import create_app

SERVER_ACTOR = "server"


class Command(StrEnum):
    """Sub-commands of `cortex`."""

    SERVE = "serve"
    VALIDATE_CONFIG = "validate-config"


def _writable(path: os.PathLike[str], label: str) -> list[str]:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as error:
        return [f"{label} {path}: can't create it ({error.strerror})"]
    return [] if os.access(path, os.W_OK) else [f"{label} {path}: not writable by this user"]


def validate_config(config: Config, logger: Logger, errors: ErrorHandler) -> ExitCode:
    """Check configuration, secrets and folders without doing real work (a deploy pre-flight).

    Args:
        config: the validated config (loading it already checked the schema).
        logger: the central logger.
        errors: the global error handler (unused; every command takes it).

    Returns:
        SUCCEEDED when everything checks out, STARTUP_FAILURE listing every problem otherwise.
    """
    secret_store = Secrets(config.secrets)
    problems = secret_store.check([config.secrets.admin_password, config.secrets.session_key])
    problems += _writable(config.paths.brain, "paths.brain")
    problems += _writable(config.paths.state, "paths.state")
    problems += _writable(config.paths.logs, "paths.logs")
    for line in summary(config):
        print(line)
    if problems:
        for problem in problems:
            print(f"PROBLEM {problem}")
        return ExitCode.STARTUP_FAILURE
    print("OK configuration, secrets and folders are ready")
    return ExitCode.SUCCEEDED


def serve(config: Config, logger: Logger, errors: ErrorHandler) -> ExitCode:
    """Run the web server until the platform stops it.

    Args:
        config: the validated config.
        logger: the central logger; library loggers are attached to it here.
        errors: the global error handler.

    Returns:
        INTERRUPTED after a graceful stop, STARTUP_FAILURE when the server never started.
    """
    secret_store = Secrets(config.secrets, warn=logger.warn)
    try:
        app = create_app(config, logger, secret_store, errors)
    except (SecretError, StartupError) as error:
        for problem in error.problems:
            logger.event(SERVER_ACTOR, "startup failed", problem, level=logging.ERROR)
        return ExitCode.STARTUP_FAILURE
    logger.attach_library_loggers()
    for line in summary(config):
        logger.event(SERVER_ACTOR, "start", line)
    server = uvicorn.Server(uvicorn.Config(
        app,
        host=config.server.host,
        port=config.server.port,
        proxy_headers=True,
        forwarded_allow_ips=config.server.forwarded_allow_ips,
        log_config=None,  # uvicorn logs through Cortex's logger (attached above), not its own handlers
        access_log=False,  # RequestContext writes the one access line per request
    ))
    server.run()
    logger.event(SERVER_ACTOR, "stop", "graceful stop" if server.started else "the server didn't start")
    return ExitCode.INTERRUPTED if server.started else ExitCode.STARTUP_FAILURE


COMMANDS: dict[Command, Callable[[Config, Logger, ErrorHandler], ExitCode]] = {
    Command.SERVE: serve,
    Command.VALIDATE_CONFIG: validate_config,
}
