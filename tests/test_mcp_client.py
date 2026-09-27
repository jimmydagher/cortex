"""The official MCP client against the full app in-process (bearer gate and host check included)."""
from __future__ import annotations

from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from cortex.config import Config
from cortex.errors import ErrorHandler
from cortex.logs import Logger
from cortex.secrets import Secrets
from cortex.web import create_app


def test_official_client_round_trip(config: Config, logger: Logger, errors: ErrorHandler) -> None:
    # Guards compatibility with the SDK's own client: tools, prompts and attribution by key label.
    app = create_app(config, logger, Secrets(config.secrets), errors)
    brain = app.state.brain
    brain.configure("CORTEX.md")
    _, key = brain.state.create_key("sdk-client")

    async def run() -> tuple[set[str], Any, set[str]]:
        async with app.router.lifespan_context(app):
            http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://cortex.local",
                headers={"Authorization": f"Bearer {key}"},
            )
            async with Client(streamable_http_client("http://cortex.local/mcp", http_client=http)) as client:
                tools = {tool.name for tool in (await client.list_tools()).tools}
                loaded = await client.call_tool("cortex_load", {})
                prompts = {prompt.name for prompt in (await client.list_prompts()).prompts}
                return tools, loaded, prompts

    tools, loaded, prompts = anyio.run(run)
    assert "synapse_commit" in tools
    assert not loaded.is_error and "--- CORTEX.md ---" in loaded.content[0].text
    assert {"cortex", "synapse"} <= prompts
    assert any(event["who"] == "sdk-client" and event["action"] == "load" for event in logger.recent(20))
