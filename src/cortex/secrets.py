"""The one secrets accessor: Docker secrets files, with an env-var fallback only where config allows.

Deployed, Cortex reads each secret from a file named after it in secrets.dir (Docker
mounts compose secrets at /run/secrets/<name>). The environment fallback exists for
local development and is off in every deployed override, so a missing secret there
fails at startup instead of silently using something else.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from .config import SecretsConfig
from .errors import SecretError


def env_name(secret: str) -> str:
    """The local-development environment variable for a secret.

    Args:
        secret: the secret's name, e.g. `cortex-admin-pwd`.

    Returns:
        The variable name, e.g. `CORTEX_ADMIN_PWD`.
    """
    return secret.upper().replace("-", "_")


class Secrets:
    """Reads secrets by name; values stay in memory only for the life of the process."""

    def __init__(self, config: SecretsConfig, environ: Mapping[str, str] = os.environ,
                 warn: Callable[[str], None] | None = None) -> None:
        self._config = config
        self._environ = environ
        self._warn = warn

    def get(self, name: str) -> str:
        """Return a secret's value.

        Args:
            name: the secret's name (config holds names, never values).

        Returns:
            The secret value, stripped of surrounding whitespace.

        Raises:
            SecretError: naming the secret and where it was looked for, never a value.
        """
        value = self.optional(name)
        if value:
            return value
        where = f"{self._config.dir / name}" + (f" or ${env_name(name)}" if self._config.allow_env_fallback else "")
        raise SecretError([f"secret {name} is missing: put it in {where}"])

    def optional(self, name: str) -> str | None:
        """Return a secret's value, or None when it isn't set (for secrets that switch a feature on).

        Args:
            name: the secret's name.

        Returns:
            The secret value, stripped of surrounding whitespace, or None when absent or empty.

        Raises:
            SecretError: the file exists but can't be read.
        """
        path = self._config.dir / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            value = ""
        except OSError as error:
            raise SecretError([f"secret {name}: can't read {path} ({error.strerror})"]) from error
        if value:
            return value
        if self._config.allow_env_fallback:
            value = self._environ.get(env_name(name), "").strip()
            if value:
                if self._warn:
                    self._warn(f"secret {name} read from ${env_name(name)} (local-development fallback)")
                return value
        return None

    def check(self, names: list[str]) -> list[str]:
        """Report every secret that can't be read, without returning any value.

        Args:
            names: secret names to check.

        Returns:
            One problem line per unreadable secret; empty when all are readable.
        """
        problems = []
        for name in names:
            try:
                self.get(name)
            except SecretError as error:
                problems += error.problems
        return problems
