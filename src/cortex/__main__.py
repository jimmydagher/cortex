"""Entry point: `cortex serve` or `cortex validate-config`. Wires things together and exits.

Order matters: the global error handler is registered before config or logging exist,
so even a failure while loading config is reported (to stderr) with a distinct exit code.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import config as config_loader
from .commands import COMMANDS, Command
from .errors import ConfigError, ErrorHandler, ExitCode
from .logs import Logger


def main(argv: list[str] | None = None) -> int:
    """Run a Cortex command.

    Args:
        argv: arguments after the program name; sys.argv[1:] when omitted.

    Returns:
        The ExitCode as an int (documented in docs/cheat-sheet.md).
    """
    errors = ErrorHandler()
    errors.install()
    parser = argparse.ArgumentParser(prog="cortex", description="MCP server and web GUI for a Markdown second brain.")
    parser.add_argument("command", nargs="?", choices=[command.value for command in Command])
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_usage(sys.stderr)
        print("cortex: a command is required (serve or validate-config)", file=sys.stderr)
        return ExitCode.INVALID_INPUT

    try:
        config = config_loader.load(os.environ)
    except ConfigError as error:
        for problem in error.problems:
            print(f"CONFIG PROBLEM {problem}", file=sys.stderr)
        return ExitCode.STARTUP_FAILURE

    logger = Logger(config.logging, config.paths.logs)
    errors.use_logger(logger.error)
    try:
        return COMMANDS[Command(arguments.command)](config, logger, errors)
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main())
