"""Config loading, validation and the command-line outcomes (exit codes)."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import REPO, TEST_ENVIRON, make_config

from cortex import config as config_loader
from cortex.__main__ import main
from cortex.commands import validate_config
from cortex.errors import ConfigError, ErrorHandler, ExitCode
from cortex.logs import Logger


def write_override(folder: Path, env: str, text: str) -> dict[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{env}.yaml").write_text(text, encoding="utf-8")
    return {"CORTEX_ENV": env, "CORTEX_CONFIG_DIR": str(REPO / "config"), "CORTEX_OVERRIDE_DIR": str(folder)}


def test_test_override_loads_and_derives_runtime(config: config_loader.Config) -> None:
    assert config.runtime.env == "test"
    assert config.runtime.version == (REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert config.server.allowed_hosts == ["testserver", "cortex.local"]


def test_missing_wiring_is_reported_all_at_once() -> None:
    with pytest.raises(ConfigError) as caught:
        config_loader.load({})
    assert len(caught.value.problems) == 3


def test_every_problem_is_reported_in_one_pass(tmp_path: Path) -> None:
    # Regression: validation must not stop at the first error (sdsi:config).
    environ = write_override(tmp_path, "broken", "server:\n  port: 99999\n  bogus: 1\nlogging:\n  level: LOUD\n")
    with pytest.raises(ConfigError) as caught:
        config_loader.load(environ)
    text = " ".join(caught.value.problems)
    for expected in ("server.allowed_hosts is not configured", "server.secure_cookies is not configured",
                     "server.port", "server.bogus", "logging.level"):
        assert expected in text


def test_nas_template_must_be_filled_in() -> None:
    environ = {**TEST_ENVIRON, "CORTEX_ENV": "nas"}
    with pytest.raises(ConfigError, match="server.allowed_hosts is not configured: set it in"):
        config_loader.load(environ)


def test_missing_override_file_is_an_error(tmp_path: Path) -> None:
    environ = {**TEST_ENVIRON, "CORTEX_OVERRIDE_DIR": str(tmp_path)}
    with pytest.raises(ConfigError, match="test.yaml: file not found"):
        config_loader.load(environ)


def test_runtime_values_cannot_be_overridden(tmp_path: Path, brain_dir: Path) -> None:
    config = make_config(tmp_path, brain_dir, runtime={"env": "prod", "version": "9.9.9", "files": []})
    assert config.runtime.env == "test" and config.runtime.version != "9.9.9"


def test_validate_config_reports_ok_and_problems(tmp_path: Path, brain_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path, brain_dir)
    logger = Logger(config.logging, config.paths.logs)
    try:
        assert validate_config(config, logger, ErrorHandler()) == ExitCode.SUCCEEDED
        assert "OK configuration, secrets and folders are ready" in capsys.readouterr().out
        (config.secrets.dir / "cortex-admin-pwd").unlink()
        (config.secrets.dir / "cortex-session-key").unlink()
        assert validate_config(config, logger, ErrorHandler()) == ExitCode.STARTUP_FAILURE
        output = capsys.readouterr().out
        assert output.count("PROBLEM secret") == 2 and "test-password" not in output
    finally:
        logger.close()


def test_validate_config_checks_the_optional_guest_password(tmp_path: Path, brain_dir: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path, brain_dir)
    logger = Logger(config.logging, config.paths.logs)
    try:
        assert validate_config(config, logger, ErrorHandler()) == ExitCode.SUCCEEDED
        assert "guest account off" in capsys.readouterr().out
        (config.secrets.dir / "cortex-guest-pwd").write_text("test-password", encoding="utf-8")  # same as the admin's
        assert validate_config(config, logger, ErrorHandler()) == ExitCode.STARTUP_FAILURE
        assert "PROBLEM secret cortex-guest-pwd must differ from cortex-admin-pwd" in capsys.readouterr().out
        (config.secrets.dir / "cortex-guest-pwd").write_text("another-password", encoding="utf-8")
        assert validate_config(config, logger, ErrorHandler()) == ExitCode.SUCCEEDED
        assert "guest account on" in capsys.readouterr().out
    finally:
        logger.close()


def test_no_command_is_invalid_input(capsys: pytest.CaptureFixture[str]) -> None:
    # Regression: a run with no instruction must fail, not do nothing and report success.
    assert main([]) == ExitCode.INVALID_INPUT
    assert "a command is required" in capsys.readouterr().err


def test_bad_config_is_a_startup_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("CORTEX_ENV", "CORTEX_CONFIG_DIR", "CORTEX_OVERRIDE_DIR"):
        monkeypatch.delenv(name, raising=False)
    assert main(["validate-config"]) == ExitCode.STARTUP_FAILURE
    assert "CONFIG PROBLEM environment variable CORTEX_ENV is not set" in capsys.readouterr().err
