"""Request-level tests through Starlette's test client: the /mcp gate and tools over real
JSON-RPC, the GUI API, error shapes, request ids and host checking."""
from __future__ import annotations

import json
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import ADMIN_PASSWORD, make_config
from starlette.testclient import TestClient

from cortex import bootstrap
from cortex.config import Config
from cortex.errors import ErrorHandler, SecretError
from cortex.logs import LOG_LINE, Logger
from cortex.secrets import Secrets
from cortex.web import create_app

CSRF = {"X-Cortex-CSRF": "1"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def build(config: Config, logger: Logger, errors: ErrorHandler, configure: bool = True) -> TestClient:
    app = create_app(config, logger, Secrets(config.secrets), errors)
    if configure:
        app.state.brain.configure("CORTEX.md")
    return TestClient(app)


@pytest.fixture
def client(config: Config, logger: Logger, errors: ErrorHandler) -> Iterator[TestClient]:
    with build(config, logger, errors) as test_client:
        yield test_client


def login(client: TestClient) -> None:
    assert client.post("/api/login", json={"password": ADMIN_PASSWORD}).status_code == 200


def new_key(client: TestClient, label: str = "test") -> str:
    login(client)
    return str(client.post("/api/keys", json={"label": label}, headers=CSRF).json()["key"])


def rpc(client: TestClient, key: str | None, method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> httpx2.Response:
    headers = {**MCP_HEADERS, **({"Authorization": f"Bearer {key}"} if key else {})}
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    return client.post("/mcp", headers=headers, content=json.dumps(body))


def call(client: TestClient, key: str, tool: str, **arguments: Any) -> tuple[bool, str]:
    init = rpc(client, key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    assert init.status_code == 200, init.text
    response = rpc(client, key, "tools/call", {"name": tool, "arguments": arguments}, request_id=2)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return bool(result.get("isError", False)), "".join(part.get("text", "") for part in result["content"])


def error_of(response: httpx2.Response) -> dict[str, str]:
    body: dict[str, dict[str, str]] = response.json()
    return body["error"]


# ---------- MCP gate and tools ----------


def test_mcp_requires_valid_key(client: TestClient) -> None:
    response = rpc(client, None, "tools/list")
    assert response.status_code == 401 and error_of(response)["code"] == "unauthorized"
    assert rpc(client, "ctx_wrong", "tools/list").status_code == 401


def test_revoked_key_is_refused(client: TestClient) -> None:
    key = new_key(client)
    key_id = client.get("/api/keys").json()["keys"][0]["id"]
    assert client.delete(f"/api/keys/{key_id}", headers=CSRF).status_code == 200
    assert rpc(client, key, "tools/list").status_code == 401


def test_tools_listed(client: TestClient) -> None:
    key = new_key(client)
    rpc(client, key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    names = {tool["name"] for tool in rpc(client, key, "tools/list", request_id=2).json()["result"]["tools"]}
    assert {"cortex_load", "cortex_read", "synapse_queue", "synapse_commit", "cortex_power"} <= names


def test_load_read_and_synapse_flow(client: TestClient, brain_dir: Path) -> None:
    key = new_key(client, "laptop")
    failed, text = call(client, key, "cortex_load")
    assert not failed and "--- CORTEX.md ---" in text and "SYNAPSE: 0 pending" in text

    failed, text = call(client, key, "cortex_read", note="CORTEX#Brain Upkeep")
    assert not failed and text.splitlines()[2] == "## Brain Upkeep"
    failed, text = call(client, key, "cortex_read", note="[[MEMORY/CODING\\|CODING]]")
    assert not failed and text.startswith("MEMORY/CODING.md · tags: memory/technical · protected")

    failed, text = call(client, key, "synapse_queue", target="MEMORY/WRITING › Avoid", change="Cut hedging", why="recurs")
    assert text == "Queued HX0002 → MEMORY/WRITING › Avoid"

    # the GUI approves; the next load tells the AI to write it
    assert client.post("/api/synapse/HX0002/approve", headers=CSRF).status_code == 200
    failed, text = call(client, key, "cortex_load")
    assert "waiting to be written" in text and "HX0002" in text

    failed, text = call(client, key, "synapse_commit", id="HX0002", summary="hedging joins Avoid",
                        changes=[{"note": "MEMORY/WRITING.md", "edits": [{"old_text": "- Filler.", "new_text": "- Filler and hedging."}]}])
    assert not failed, text
    assert "- Filler and hedging." in (brain_dir / "MEMORY/WRITING.md").read_text(encoding="utf-8")
    assert "- [x] HX0002" in (brain_dir / "HIPPOCAMPUS/ENGRAM.md").read_text(encoding="utf-8")
    assert any(event["who"] == "laptop" and event["action"] == "commit" for event in client.get("/api/activity").json()["events"])


def test_failed_commit_writes_nothing(client: TestClient, brain_dir: Path) -> None:
    key = new_key(client)
    call(client, key, "synapse_queue", target="MEMORY/WRITING › Avoid", change="x", why="y")
    before = (brain_dir / "MEMORY/WRITING.md").read_text(encoding="utf-8")
    failed, text = call(client, key, "synapse_commit", id="HX0002", summary="s", changes=[
        {"note": "MEMORY/WRITING.md", "edits": [{"old_text": "- Filler.", "new_text": "- Changed."}]},
        {"note": "MEMORY/CODING.md", "edits": [{"old_text": "does not exist", "new_text": "x"}]},
    ])
    assert failed and "not found" in text
    assert (brain_dir / "MEMORY/WRITING.md").read_text(encoding="utf-8") == before
    assert "HX0002" in (brain_dir / "HIPPOCAMPUS/SYNAPSE.md").read_text(encoding="utf-8")


def test_protected_notes_refuse_direct_writes(client: TestClient, brain_dir: Path) -> None:
    key = new_key(client)
    failed, text = call(client, key, "cortex_write", note="MEMORY/CODING.md", content="overwritten")
    assert failed and "protected" in text
    failed, text = call(client, key, "cortex_write", note="HIPPOCAMPUS/SYNAPSE.md", content="x")
    assert failed and "protected" in text
    failed, text = call(client, key, "cortex_write", note="PROJECTS/Acme/ACME.md", edits=[{"old_text": "Starting.", "new_text": "Shipped v1."}])
    assert not failed and "Shipped v1." in (brain_dir / "PROJECTS/Acme/ACME.md").read_text(encoding="utf-8")


def test_power_off_blocks_tools_until_on(client: TestClient) -> None:
    key = new_key(client)
    assert call(client, key, "cortex_power", state="off")[1].startswith("Brain: off")
    failed, text = call(client, key, "cortex_load")
    assert failed and "brain is off" in text
    assert client.get("/api/status").json()["power"] == "off"
    call(client, key, "cortex_power", state="on")
    assert not call(client, key, "cortex_load")[0]


def test_refused_path_reaches_model(client: TestClient) -> None:
    key = new_key(client)
    failed, text = call(client, key, "cortex_read", note="../../etc/passwd")
    assert failed and ("no note matches" in text or "refused" in text)


def test_unexpected_tool_error_is_reported_with_request_id_only(client: TestClient, errors: ErrorHandler,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression (F-019): a crash inside a tool must reach the global handler and leak no internals.
    key = new_key(client)
    reported: list[BaseException] = []
    errors.add_reporter(lambda error, _context: reported.append(error))
    brain = client.app.state.brain  # type: ignore[attr-defined]
    monkeypatch.setattr(brain, "search", lambda *_args: (_ for _ in ()).throw(RuntimeError("secret internals /srv/x")))
    failed, text = call(client, key, "cortex_search", query="x")
    assert failed and "internal error (request" in text and "secret internals" not in text
    assert [str(error) for error in reported] == ["secret internals /srv/x"]


# ---------- GUI API ----------


def test_gui_requires_login_and_csrf(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 401 and error_of(response)["code"] == "unauthorized"
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    login(client)
    assert client.get("/api/status").json()["configured"] is True
    assert client.post("/api/power", json={"state": "off"}).status_code == 403
    assert client.post("/api/power", json={"state": "off"}, headers=CSRF).json()["power"] == "off"


def test_login_lockout(client: TestClient) -> None:
    for _ in range(5):
        client.post("/api/login", json={"password": "nope"})
    response = client.post("/api/login", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 429 and error_of(response)["code"] == "rate_limited"


def test_malformed_json_is_invalid_input_not_wrong_password(client: TestClient) -> None:
    # Regression (F-022): a broken body used to be read as empty and answered "wrong password".
    response = client.post("/api/login", content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and error_of(response)["code"] == "invalid_input"


def test_body_validation_lists_every_problem(client: TestClient) -> None:
    login(client)
    response = client.post("/api/settings", json={"cortex_path": "", "protected": "not a list", "extra": 1}, headers=CSRF)
    message = error_of(response)["message"]
    assert response.status_code == 422 and "cortex_path" in message and "protected" in message and "extra" in message


def test_error_shape_status_map_and_request_id(client: TestClient) -> None:
    login(client)
    missing = client.get("/api/note", params={"path": "NOPE"}, headers={"X-Request-ID": "req-abc-12345"})
    assert missing.status_code == 404
    assert error_of(missing) == {"code": "not_found", "message": "no such note", "request_id": "req-abc-12345"}
    assert missing.headers["x-request-id"] == "req-abc-12345"
    assert client.post("/api/synapse/HX9999/approve", headers=CSRF).status_code == 404
    unknown_route = client.get("/api/nope")
    assert unknown_route.status_code == 404 and error_of(unknown_route)["code"] == "not_found"
    wrong_method = client.put("/api/status")
    assert wrong_method.status_code == 405 and error_of(wrong_method)["code"] == "method_not_allowed"


def test_conflict_maps_to_409(client: TestClient) -> None:
    key = new_key(client)
    call(client, key, "synapse_queue", target="X", change="c", why="w")
    assert client.post("/api/synapse/HX0002/approve", headers=CSRF).status_code == 200
    again = client.post("/api/synapse/HX0002/approve", headers=CSRF)
    assert again.status_code == 409 and error_of(again)["code"] == "conflict"


def test_unexpected_error_is_generic_500_and_logged(client: TestClient, logger: Logger, errors: ErrorHandler,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression (F-018/F-019): an unanticipated error used to come back as a 400 carrying its message.
    login(client)
    errors.use_logger(logger.error)
    brain = client.app.state.brain  # type: ignore[attr-defined]
    monkeypatch.setattr(brain.vault, "graph", lambda: (_ for _ in ()).throw(RuntimeError("secret internals /srv/x")))
    response = client.get("/api/graph")
    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error" and "secret internals" not in response.text
    assert logger.flush()
    assert "unhandled where=GET /api/graph: RuntimeError: secret internals" in logger.path.read_text(encoding="utf-8")


def test_unknown_host_is_refused(client: TestClient) -> None:
    response = client.get("/api/session", headers={"Host": "evil.example"})
    assert response.status_code == 400 and error_of(response)["code"] == "invalid_host"
    assert client.get("/healthz", headers={"Host": "127.0.0.1:8765"}).text == "ok"


def test_note_rendering_escapes_html_and_links_notes(client: TestClient, brain_dir: Path) -> None:
    (brain_dir / "EVIL.md").write_text("<script>alert(1)</script>\n\n[[MEMORY/CODING|CODING]] [x](javascript:alert(1))", encoding="utf-8")
    login(client)
    html = client.get("/api/note", params={"path": "EVIL"}).json()["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="#note=MEMORY/CODING.md"' in html
    assert 'href="javascript' not in html  # left as plain text


def test_keys_are_hashed_and_security_headers_set(client: TestClient, config: Config) -> None:
    key = new_key(client)
    listing = client.get("/api/keys").json()["keys"]
    assert "hash" not in listing[0] and key not in json.dumps(listing)
    state = (config.paths.state / "state.json").read_text(encoding="utf-8")
    assert key not in state and "session_secret" not in state
    page = client.get("/")
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


def test_favicon_ico_is_served(client: TestClient) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code == 200 and response.headers["content-type"] == "image/x-icon"
    assert response.content[:4] == b"\x00\x00\x01\x00"  # ICO header


def test_unconfigured_brain_fails_instead_of_empty_success(config: Config, logger: Logger, errors: ErrorHandler) -> None:
    # Regression (F-021): SYNAPSE used to return empty lists before setup.
    with build(config, logger, errors, configure=False) as client:
        login(client)
        response = client.get("/api/synapse")
        assert response.status_code == 409 and error_of(response)["code"] == "not_configured"


def test_missing_secret_fails_startup(tmp_path: Path, brain_dir: Path, logger: Logger, errors: ErrorHandler) -> None:
    config = make_config(tmp_path, brain_dir)
    (config.secrets.dir / "cortex-session-key").unlink()
    with pytest.raises(SecretError, match="cortex-session-key is missing"):
        create_app(config, logger, Secrets(config.secrets), errors)


# ---------- setup ----------


def test_setup_blank_brain(tmp_path: Path, brain_dir: Path, logger: Logger, errors: ErrorHandler) -> None:
    empty = tmp_path / "empty-brain"
    empty.mkdir()
    config = make_config(tmp_path, empty)
    with build(config, logger, errors, configure=False) as client:
        login(client)
        assert client.get("/api/status").json()["configured"] is False
        bad = client.post("/api/setup", json={"mode": "existing", "cortex_path": "nope.md"}, headers=CSRF)
        assert bad.status_code == 404 and "no CORTEX file" in error_of(bad)["message"]
        assert client.get("/api/status").json()["cortex_path"] == ""
        job = client.post("/api/setup", json={"mode": "blank"}, headers=CSRF)
        assert job.status_code == 200 and job.json()["status"] == "succeeded"
        status = client.get("/api/status").json()
        assert status["configured"] and status["cortex_path"] == "CORTEX.md"
        assert status["protected"] == ["CORTEX.md", "MEMORY/"]
        graph = client.get("/api/graph").json()
        assert {node["id"] for node in graph["nodes"]} == {"CORTEX.md", "MEMORY/GENERAL.md", "HIPPOCAMPUS/SYNAPSE.md", "HIPPOCAMPUS/ENGRAM.md"}
        assert graph["dead"] == []


def test_template_setup_runs_in_background(tmp_path: Path, brain_dir: Path, logger: Logger, errors: ErrorHandler,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression (F-024): the GitHub download used to block every request for up to a minute.
    empty = tmp_path / "empty-brain"
    empty.mkdir()
    config = make_config(tmp_path, empty)

    def fake_download(root: Path, _config: object) -> str:
        time.sleep(0.2)
        shutil.copytree(brain_dir, root, dirs_exist_ok=True)
        return "CORTEX.md"

    monkeypatch.setattr(bootstrap, "download_template", fake_download)
    with build(config, logger, errors, configure=False) as client:
        login(client)
        started = client.post("/api/setup", json={"mode": "template"}, headers=CSRF)
        assert started.status_code == 202 and started.json()["status"] == "running"
        assert client.get("/api/session").status_code == 200  # the server keeps answering meanwhile
        for _ in range(50):
            job = client.get("/api/setup/job").json()
            if job["status"] != "running":
                break
            time.sleep(0.05)
        assert job["status"] == "succeeded" and job["configured"] is True


def test_brain_in_subfolder(tmp_path: Path, brain_dir: Path, logger: Logger, errors: ErrorHandler) -> None:
    outer = tmp_path / "outer"
    shutil.copytree(brain_dir, outer / "claude-brain")
    (outer / "claude-brain/.obsidian").mkdir()
    (outer / "claude-brain/.obsidian/graph.json").write_text('{"colorGroups":[{"query":"tag:#memory/core","color":{"a":1,"rgb":2783446}}]}')
    config = make_config(tmp_path, outer)
    app = create_app(config, logger, Secrets(config.secrets), errors)
    brain = app.state.brain
    brain.configure("claude-brain/CORTEX.md")
    assert brain.hippocampus.synapse_path == "claude-brain/HIPPOCAMPUS/SYNAPSE.md"
    assert brain.state.get("protected") == ["claude-brain/CORTEX.md", "claude-brain/MEMORY/"]
    assert brain.is_protected("claude-brain/MEMORY/CODING.md") and not brain.is_protected("claude-brain/PROJECTS/Acme/ACME.md")
    graph = brain.vault.graph()
    assert {"source": "claude-brain/CORTEX.md", "target": "claude-brain/MEMORY/CODING.md"} in graph["links"]
    assert next(node for node in graph["nodes"] if node["id"] == "claude-brain/CORTEX.md")["color"] == "#2a78d6"


# ---------- logging ----------


def test_activity_log_is_plain_text_with_request_ids(client: TestClient, logger: Logger, config: Config) -> None:
    client.post("/api/login", json={"password": "nope"})
    key = new_key(client, "nas laptop")
    call(client, key, "cortex_read", note="MEMORY/CODING")
    assert logger.flush()
    lines = logger.path.read_text(encoding="utf-8").splitlines()
    assert lines and not any(line.lstrip().startswith("{") for line in lines)
    assert any(" WARNING req=" in line and "[gui] login failed: " in line for line in lines)
    read_line = next(line for line in lines if "[nas-laptop] read: MEMORY/CODING.md" in line)
    match = LOG_LINE.match(read_line)
    assert match and match["request_id"] != "-"
    assert any(f"req={match['request_id']} (access) POST /mcp 200" in line for line in lines)
    assert logger.path.parent == config.paths.logs and not (config.paths.state / "cortex.log").exists()
    reloaded = Logger(config.logging, config.paths.logs)
    try:
        assert reloaded.recent(1)[0]["who"] == "nas-laptop"
    finally:
        reloaded.close()
