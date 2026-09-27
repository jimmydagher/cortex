"""End-to-end browser tests of the critical GUI flows: `pytest -m e2e` (not in the default run).

Needs a browser once: `python -m playwright install chromium`. The fixture starts Cortex on a
free local port inside the test process and stops it afterwards.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from conftest import ADMIN_PASSWORD, make_config

from cortex.errors import ErrorHandler
from cortex.logs import Logger
from cortex.secrets import Secrets
from cortex.web import create_app

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402 - only after the skip check

STARTUP_SECONDS = 10


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def base_url(tmp_path: Path, brain_dir: Path) -> Iterator[str]:
    port = free_port()
    config = make_config(tmp_path, brain_dir, server={"allowed_hosts": ["127.0.0.1"]})
    logger = Logger(config.logging, config.paths.logs)
    app = create_app(config, logger, Secrets(config.secrets), ErrorHandler())
    app.state.brain.configure("CORTEX.md")
    app.state.brain.queue_entry("test", "MEMORY/WRITING › Avoid", "Cut hedging", "recurs", "correction")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_SECONDS
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(STARTUP_SECONDS)
        logger.close()


def sign_in(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.fill("#login-password", ADMIN_PASSWORD)
    page.click("#login-form button[type=submit]")
    expect(page.locator("#view-app")).to_be_visible()


@pytest.mark.e2e
def test_sign_in_and_open_a_note_from_search(page: Page, base_url: str) -> None:
    sign_in(page, base_url)
    expect(page.locator("#stats")).to_contain_text("notes")
    page.fill("#graph-search", "MEMORY/CODING.md")
    page.press("#graph-search", "Enter")
    expect(page.locator("#note-path")).to_have_text("MEMORY/CODING.md")
    expect(page.locator("#note-tags")).to_contain_text("protected")


@pytest.mark.e2e
def test_approve_a_synapse_entry(page: Page, base_url: str) -> None:
    sign_in(page, base_url)
    page.click("button[data-tab=synapse]")
    expect(page.locator("#list-pending")).to_contain_text("Cut hedging")
    page.click("#list-pending button.primary")
    expect(page.locator("#list-approved")).to_contain_text("HX0002")


@pytest.mark.e2e
def test_create_and_revoke_a_key(page: Page, base_url: str) -> None:
    sign_in(page, base_url)
    page.click("button[data-tab=connect]")
    page.fill("#key-label", "e2e-client")
    page.click("#key-form button[type=submit]")
    expect(page.locator("#key-raw")).to_contain_text("ctx_")
    expect(page.locator("#snip-cli")).to_contain_text("ctx_")
    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#keys-table button.danger")
    expect(page.locator("#keys-table")).to_contain_text("No keys yet")
