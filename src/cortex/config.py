"""Configuration: config/default.yaml deep-merged with override/<env>.yaml, validated once.

The only environment variables read anywhere in Cortex for settings are the wiring in
Wiring below: they locate the config before it can load. Every other setting comes
from the YAML files, is validated against the schema here before any work starts, and
a missing key is an error, never a silent fallback.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError


class Wiring(StrEnum):
    """Environment variables that must exist before config can load. Keep this list short."""

    ENV = "CORTEX_ENV"
    CONFIG_DIR = "CORTEX_CONFIG_DIR"
    OVERRIDE_DIR = "CORTEX_OVERRIDE_DIR"


DEFAULT_FILE = "default.yaml"
# VERSION sits at the repo root, two folders above this package (src/cortex/): the same
# layout the Dockerfile copies into /app.
VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"
NOT_CONFIGURED = "<not configured>"
SET_AT_RUNTIME = "<set at runtime>"
REPO_PATTERN = r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$"
SECRET_NAME_PATTERN = r"^[a-z0-9]+-[a-z0-9]+-[a-z0-9]+$"
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
PowerSetting = Literal["on", "off"]


class _Section(BaseModel):
    """Every section rejects unknown keys and has no defaults: values come from YAML."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(_Section):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    public_url: str
    allowed_hosts: list[str] = Field(min_length=1)
    secure_cookies: bool
    forwarded_allow_ips: str = Field(min_length=1)


class PathsConfig(_Section):
    brain: Path
    state: Path
    logs: Path


class LoggingConfig(_Section):
    level: LogLevel
    file: str = Field(min_length=1)
    max_bytes: int = Field(ge=10_000)
    backups: int = Field(ge=0, le=100)
    queue_size: int = Field(ge=100)
    drain_seconds: float = Field(gt=0, le=8)
    recent_events: int = Field(ge=10, le=10_000)


class GuiConfig(_Section):
    session_days: int = Field(ge=1, le=365)
    login_window_seconds: int = Field(ge=1)
    login_max_failures: int = Field(ge=1)
    trail_limit: int = Field(ge=1, le=500)
    activity_limit: int = Field(ge=1, le=1000)


class McpConfig(_Section):
    list_limit: int = Field(ge=10, le=10_000)


class VaultConfig(_Section):
    max_note_bytes: int = Field(ge=1_000)
    scan_ttl_seconds: float = Field(ge=0)


class LayoutConfig(_Section):
    synapse: str = Field(min_length=1)
    engram: str = Field(min_length=1)
    hippocampus_guide: str = Field(min_length=1)
    memory: str = Field(min_length=1)
    personal: str = Field(min_length=1)
    personal_map: str = Field(min_length=1)


class AuditConfig(_Section):
    script: str
    timeout_seconds: float = Field(gt=0, le=600)


class SetupConfig(_Section):
    template_repo: str = Field(pattern=REPO_PATTERN)
    download_timeout_seconds: float = Field(gt=0, le=600)
    max_download_bytes: int = Field(ge=1_000_000)


class SecretsConfig(_Section):
    dir: Path
    allow_env_fallback: bool
    admin_password: str = Field(pattern=SECRET_NAME_PATTERN)
    session_key: str = Field(pattern=SECRET_NAME_PATTERN)


class StateConfig(_Section):
    initial_power: PowerSetting
    last_used_flush_seconds: float = Field(ge=0)


class RuntimeConfig(_Section):
    """Derived at load time; overrides anything the files say."""

    env: str
    version: str
    files: list[str]


class Config(_Section):
    server: ServerConfig
    paths: PathsConfig
    logging: LoggingConfig
    gui: GuiConfig
    mcp: McpConfig
    vault: VaultConfig
    layout: LayoutConfig
    audit: AuditConfig
    setup: SetupConfig
    secrets: SecretsConfig
    state: StateConfig
    runtime: RuntimeConfig


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge override into base: nested mappings merge, everything else is replaced.

    Args:
        base: the lower-priority mapping (the default file).
        override: the higher-priority mapping (an environment override).

    Returns:
        A new merged dictionary; neither input is modified.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path, problems: list[str]) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{path}: file not found")
        return {}
    except (OSError, yaml.YAMLError) as error:
        problems.append(f"{path}: can't read it ({' '.join(str(error).split())})")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        problems.append(f"{path}: must be a YAML mapping")
        return {}
    return data


def _placeholders(data: Mapping[str, Any], marker: str, prefix: str = "") -> list[str]:
    found = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            found += _placeholders(value, marker, dotted + ".")
        elif value == marker:
            found.append(dotted)
    return found


def _read_version(problems: list[str]) -> str:
    """The release version from VERSION, the single source the release chain bumps."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as error:
        problems.append(f"{VERSION_FILE}: can't read the version ({error.strerror})")
        return ""


def load(environ: Mapping[str, str], extra: Mapping[str, Any] | None = None) -> Config:
    """Load, merge and validate the configuration.

    Args:
        environ: the process environment (only the Wiring variables are read).
        extra: an optional last layer merged on top, used by tests for temp paths.

    Returns:
        The validated, frozen Config.

    Raises:
        ConfigError: listing every problem found, not just the first.
    """
    problems: list[str] = []
    wiring = {name: environ.get(name.value, "").strip() for name in Wiring}
    for name, value in wiring.items():
        if not value:
            problems.append(f"environment variable {name.value} is not set")
    if problems:
        raise ConfigError(problems)

    env = wiring[Wiring.ENV]
    default_path = Path(wiring[Wiring.CONFIG_DIR]) / DEFAULT_FILE
    override_path = Path(wiring[Wiring.OVERRIDE_DIR]) / f"{env}.yaml"
    merged = deep_merge(_read_yaml(default_path, problems), _read_yaml(override_path, problems))
    if extra:
        merged = deep_merge(merged, extra)

    for key in _placeholders(merged, NOT_CONFIGURED):
        problems.append(f"{key} is not configured: set it in {override_path}")
    merged["runtime"] = {"env": env, "version": _read_version(problems), "files": [str(default_path), str(override_path)]}

    try:
        config = Config.model_validate(merged)
    except ValidationError as error:
        for detail in error.errors():
            where = ".".join(str(part) for part in detail["loc"])
            if detail.get("input") != NOT_CONFIGURED:
                problems.append(f"{where}: {detail['msg']}")
    if problems:
        raise ConfigError(problems)
    return config


def summary(config: Config) -> list[str]:
    """Human-readable lines describing the resolved config (no secret values exist in it).

    Args:
        config: a validated config.

    Returns:
        One line per section, for validate-config and the startup log.
    """
    return [
        f"environment {config.runtime.env} · version {config.runtime.version} · files {', '.join(config.runtime.files)}",
        f"listen {config.server.host}:{config.server.port} · allowed hosts {', '.join(config.server.allowed_hosts)} · secure cookies {config.server.secure_cookies}",
        f"brain {config.paths.brain} · state {config.paths.state} · logs {config.paths.logs}",
        f"log level {config.logging.level} · secrets from {config.secrets.dir}" + (" (env fallback on)" if config.secrets.allow_env_fallback else ""),
    ]
