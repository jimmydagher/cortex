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


def raw_rpc(client: TestClient, headers: dict[str, str]) -> httpx2.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return client.post("/mcp", headers={**MCP_HEADERS, **headers}, content=json.dumps(body))


def test_key_in_claude_ai_request_headers_is_accepted(client: TestClient) -> None:
    # claude.ai's connector dialog reserves Authorization for its own sign-in; the key comes in x-auth-token or x-api-key.
    key = new_key(client, "claude-ai")
    assert raw_rpc(client, {"x-auth-token": key}).status_code == 200
    assert raw_rpc(client, {"X-API-Key": f"Bearer {key}"}).status_code == 200
    # Its own OAuth token in Authorization doesn't hide a valid key in x-auth-token.
    assert raw_rpc(client, {"Authorization": "Bearer oauth-token-from-claude", "x-auth-token": key}).status_code == 200
    refused = raw_rpc(client, {"x-auth-token": "ctx_wrong"})
    assert refused.status_code == 401 and "x-auth-token" in error_of(refused)["message"]


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
    failed, text = call(client, key, "cortex_read", note="[[NEOCORTEX/CODING\\|CODING]]")
    assert not failed and text.startswith("NEOCORTEX/CODING.md · tags: memory/technical · protected")

    failed, text = call(client, key, "synapse_queue", target="NEOCORTEX/WRITING › Avoid", change="Cut hedging", why="recurs")
    assert text == "Queued HX0002 → NEOCORTEX/WRITING › Avoid"

    # the GUI approves; the next load tells the AI to write it
    assert client.post("/api/synapse/HX0002/approve", headers=CSRF).status_code == 200
    failed, text = call(client, key, "cortex_load")
    assert "waiting to be written" in text and "HX0002" in text

    failed, text = call(client, key, "synapse_commit", id="HX0002", summary="hedging joins Avoid",
                        changes=[{"note": "NEOCORTEX/WRITING.md", "edits": [{"old_text": "- Filler.", "new_text": "- Filler and hedging."}]}])
    assert not failed, text
    assert "- Filler and hedging." in (brain_dir / "NEOCORTEX/WRITING.md").read_text(encoding="utf-8")
    assert "- [x] HX0002" in (brain_dir / "HIPPOCAMPUS/ENGRAM.md").read_text(encoding="utf-8")
    assert any(event["who"] == "laptop" and event["action"] == "commit" for event in client.get("/api/activity").json()["events"])


def test_failed_commit_writes_nothing(client: TestClient, brain_dir: Path) -> None:
    key = new_key(client)
    call(client, key, "synapse_queue", target="NEOCORTEX/WRITING › Avoid", change="x", why="y")
    before = (brain_dir / "NEOCORTEX/WRITING.md").read_text(encoding="utf-8")
    failed, text = call(client, key, "synapse_commit", id="HX0002", summary="s", changes=[
        {"note": "NEOCORTEX/WRITING.md", "edits": [{"old_text": "- Filler.", "new_text": "- Changed."}]},
        {"note": "NEOCORTEX/CODING.md", "edits": [{"old_text": "does not exist", "new_text": "x"}]},
    ])
    assert failed and "not found" in text
    assert (brain_dir / "NEOCORTEX/WRITING.md").read_text(encoding="utf-8") == before
    assert "HX0002" in (brain_dir / "HIPPOCAMPUS/SYNAPSE.md").read_text(encoding="utf-8")


def test_protected_notes_refuse_direct_writes(client: TestClient, brain_dir: Path) -> None:
    key = new_key(client)
    failed, text = call(client, key, "cortex_write", note="NEOCORTEX/CODING.md", content="overwritten")
    assert failed and "protected" in text
    failed, text = call(client, key, "cortex_write", note="HIPPOCAMPUS/SYNAPSE.md", content="x")
    assert failed and "protected" in text
    failed, text = call(client, key, "cortex_write", note="PREFRONTAL/PROJECTS/Acme/ACME.md", edits=[{"old_text": "Starting.", "new_text": "Shipped v1."}])
    assert not failed and "Shipped v1." in (brain_dir / "PREFRONTAL/PROJECTS/Acme/ACME.md").read_text(encoding="utf-8")


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
    status = client.get("/api/status").json()
    assert status["configured"] is True
    # The top bar shows the running release, read from VERSION.
    assert status["version"] == (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
    assert client.post("/api/power", json={"state": "off"}).status_code == 403
    assert client.post("/api/power", json={"state": "off"}, headers=CSRF).json()["power"] == "off"


GUEST_PASSWORD = "guest-password"


def test_guest_can_read_but_not_change(config: Config, logger: Logger, errors: ErrorHandler) -> None:
    (config.secrets.dir / "cortex-guest-pwd").write_text(GUEST_PASSWORD, encoding="utf-8")
    with build(config, logger, errors) as client:
        login_response = client.post("/api/login", json={"password": GUEST_PASSWORD})
        assert login_response.status_code == 200 and login_response.json()["role"] == "guest"
        assert client.get("/api/session").json() == {"authenticated": True, "role": "guest", "version": config.runtime.version}
        for path in ("/api/status", "/api/graph", "/api/note?path=CORTEX.md", "/api/synapse", "/api/activity"):
            assert client.get(path).status_code == 200, path
        for path in ("/api/keys", "/api/setup", "/api/setup/job"):
            response = client.get(path)
            assert response.status_code == 403 and error_of(response)["code"] == "forbidden", path
        writes = [("POST", "/api/power", {"state": "off"}), ("POST", "/api/keys", {"label": "x"}),
                  ("DELETE", "/api/keys/abc", None), ("POST", "/api/synapse/HX0001/approve", None),
                  ("POST", "/api/settings", {"cortex_path": "CORTEX.md", "protected": []}),
                  ("POST", "/api/setup", {"mode": "blank"})]
        for method, path, body in writes:
            response = client.request(method, path, json=body, headers=CSRF)
            assert response.status_code == 403 and "read-only" in error_of(response)["message"], path
        assert client.get("/api/status").json()["power"] == "on"
        # The admin still signs in with their own password and can change things.
        login(client)
        assert client.get("/api/session").json()["role"] == "admin"
        assert client.get("/api/keys").status_code == 200


def test_guest_cookie_is_not_an_admin_cookie(config: Config, logger: Logger, errors: ErrorHandler) -> None:
    (config.secrets.dir / "cortex-guest-pwd").write_text(GUEST_PASSWORD, encoding="utf-8")
    with build(config, logger, errors) as client:
        client.post("/api/login", json={"password": GUEST_PASSWORD})
        guest_cookie = client.cookies["cortex_session"]
    # Without the guest secret, the guest's cookie no longer opens anything.
    (config.secrets.dir / "cortex-guest-pwd").unlink()
    with build(config, logger, errors) as client:
        client.cookies.set("cortex_session", guest_cookie)
        assert client.get("/api/status").status_code == 401
        assert client.post("/api/login", json={"password": GUEST_PASSWORD}).status_code == 401


def test_guest_password_must_differ_from_admin(config: Config, logger: Logger, errors: ErrorHandler) -> None:
    (config.secrets.dir / "cortex-guest-pwd").write_text(ADMIN_PASSWORD, encoding="utf-8")
    with pytest.raises(SecretError, match="cortex-guest-pwd must differ"):
        create_app(config, logger, Secrets(config.secrets), errors)


def test_no_guest_account_by_default(client: TestClient, config: Config) -> None:
    # The sign-in screen shows the running version, so the session answers with it before login too.
    assert client.get("/api/session").json() == {"authenticated": False, "role": None, "version": config.runtime.version}
    login(client)
    assert client.get("/api/session").json() == {"authenticated": True, "role": "admin", "version": config.runtime.version}


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
    (brain_dir / "EVIL.md").write_text("<script>alert(1)</script>\n\n[[NEOCORTEX/CODING|CODING]] [x](javascript:alert(1))", encoding="utf-8")
    login(client)
    html = client.get("/api/note", params={"path": "EVIL"}).json()["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="#note=NEOCORTEX/CODING.md"' in html
    assert 'href="javascript' not in html  # left as plain text


def test_note_rendering_shows_task_lists_as_checkboxes(client: TestClient, brain_dir: Path) -> None:
    (brain_dir / "TASKS.md").write_text("- [ ] todo\n- [x] done\n- plain\n\n`- [ ] code`\n", encoding="utf-8")
    login(client)
    html = client.get("/api/note", params={"path": "TASKS"}).json()["html"]
    assert '<ul class="tasks">' in html
    assert '<li class="task"><input type="checkbox" disabled>todo</li>' in html
    assert '<li class="task"><input type="checkbox" disabled checked>done</li>' in html
    assert "<li>plain</li>" in html
    assert "<code>- [ ] code</code>" in html


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
        assert status["protected"] == ["CORTEX.md", "NEOCORTEX/", "PREFRONTAL/", "HIPPOCAMPUS/HIPPOCAMPUS.md",
                                       "!PREFRONTAL/PROJECTS/"]
        graph = client.get("/api/graph").json()
        assert {node["id"] for node in graph["nodes"]} == {"CORTEX.md", "NEOCORTEX/NEOCORTEX.md", "NEOCORTEX/GENERAL.md",
                                                           "HIPPOCAMPUS/SYNAPSE.md", "HIPPOCAMPUS/ENGRAM.md"}
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
    assert brain.state.get("protected") == ["claude-brain/CORTEX.md", "claude-brain/NEOCORTEX/", "claude-brain/PREFRONTAL/",
                                            "claude-brain/HIPPOCAMPUS/HIPPOCAMPUS.md", "!claude-brain/PREFRONTAL/PROJECTS/"]
    assert brain.is_protected("claude-brain/NEOCORTEX/CODING.md") and not brain.is_protected("claude-brain/PREFRONTAL/PROJECTS/Acme/ACME.md")
    assert brain.is_protected("claude-brain/PREFRONTAL/STYLE.md")
    graph = brain.vault.graph()
    assert {"source": "claude-brain/NEOCORTEX/NEOCORTEX.md", "target": "claude-brain/NEOCORTEX/CODING.md"} in graph["links"]
    assert next(node for node in graph["nodes"] if node["id"] == "claude-brain/CORTEX.md")["color"] == "#2a78d6"


# ---------- logging ----------


def test_activity_log_is_plain_text_with_request_ids(client: TestClient, logger: Logger, config: Config) -> None:
    client.post("/api/login", json={"password": "nope"})
    key = new_key(client, "nas laptop")
    call(client, key, "cortex_read", note="NEOCORTEX/CODING")
    assert logger.flush()
    lines = logger.path.read_text(encoding="utf-8").splitlines()
    assert lines and not any(line.lstrip().startswith("{") for line in lines)
    assert any(" WARNING req=" in line and "[gui] login failed: " in line for line in lines)
    read_line = next(line for line in lines if "[nas-laptop] read: NEOCORTEX/CODING.md" in line)
    match = LOG_LINE.match(read_line)
    assert match and match["request_id"] != "-"
    assert any(f"req={match['request_id']} (access) POST /mcp 200" in line for line in lines)
    assert logger.path.parent == config.paths.logs and not (config.paths.state / "cortex.log").exists()
    reloaded = Logger(config.logging, config.paths.logs)
    try:
        assert reloaded.recent(1)[0]["who"] == "nas-laptop"
    finally:
        reloaded.close()


# ---------- the live brain's layout: personal layer, guide, heading links, audit ----------


def test_load_includes_personal_index_and_always_sections(client: TestClient) -> None:
    # The personal layer's ## headings are its index: Always sections load now, area sections per task.
    key = new_key(client)
    failed, text = call(client, key, "cortex_load")
    assert not failed and "Personal layer:" in text
    assert "  - CODING: PREFRONTAL/STYLE.md#CODING" in text and "  - WRITING: PREFRONTAL/STYLE.md#WRITING" in text
    assert "--- PREFRONTAL/PERSONA.md#Always (Always) ---" in text and "Direct, no small talk." in text
    assert "Log files are plain text" not in text  # the CODING section loads only for coding tasks
    assert "Another-dev test first." not in text and "PREFRONTAL/PREFRONTAL.md#" not in text  # the guide isn't indexed
    failed, text = call(client, key, "cortex_read", note="PREFRONTAL/STYLE.md#CODING")  # a listed path reads as given
    assert not failed and "Log files are plain text" in text and "Short sentences." not in text


def test_load_returns_cortex_then_router_then_personal_layer(client: TestClient) -> None:
    key = new_key(client)
    failed, text = call(client, key, "cortex_load")
    assert not failed
    positions = [text.index(f"--- {path} ---") for path in ("CORTEX.md", "NEOCORTEX/NEOCORTEX.md",
                                                             "PREFRONTAL/PERSONA.md#Always (Always)")]
    assert positions == sorted(positions)
    assert "| Code | [[NEOCORTEX/CODING" in text


def test_load_without_router_says_so(config: Config, brain_dir: Path, logger: Logger, errors: ErrorHandler) -> None:
    (brain_dir / "NEOCORTEX/NEOCORTEX.md").unlink()
    with build(config, logger, errors) as client:
        key = new_key(client)
        failed, text = call(client, key, "cortex_load")
        assert not failed and "--- CORTEX.md ---" in text
        assert "No router at NEOCORTEX/NEOCORTEX.md" in text


def test_personal_layer_and_guide_are_protected(client: TestClient) -> None:
    # Regression: the personal layer is human-owned and changes only through an approved commit.
    key = new_key(client)
    for note in ("PREFRONTAL/STYLE.md", "PREFRONTAL/NEW.md", "HIPPOCAMPUS/HIPPOCAMPUS.md"):
        failed, text = call(client, key, "cortex_write", note=note, content="x")
        assert failed and "protected" in text, note


def test_projects_are_writable_inside_the_protected_personal_layer(client: TestClient, brain_dir: Path) -> None:
    # PREFRONTAL is protected, but project hubs are working memory: `!PREFRONTAL/PROJECTS/` exempts them.
    key = new_key(client)
    for note in ("PREFRONTAL/STYLE.md", "PREFRONTAL/PREFRONTAL.md", "NEOCORTEX/NEOCORTEX.md"):
        failed, text = call(client, key, "cortex_write", note=note, content="x")
        assert failed and "protected" in text, note
    failed, text = call(client, key, "cortex_write", note="PREFRONTAL/PROJECTS/Acme/NEW.md",
                        content="---\ntags:\n  - project/acme\n---\nNew.\n")
    assert not failed, text
    assert (brain_dir / "PREFRONTAL/PROJECTS/Acme/NEW.md").is_file()


def test_exemption_wins_but_never_over_the_hippocampus(config: Config, logger: Logger, errors: ErrorHandler) -> None:
    app = create_app(config, logger, Secrets(config.secrets), errors)
    brain = app.state.brain
    brain.configure("CORTEX.md", protected=["PREFRONTAL/", "!PREFRONTAL/PROJECTS/", "!HIPPOCAMPUS/"])
    assert brain.is_protected("PREFRONTAL/STYLE.md")
    assert brain.is_protected("prefrontal/style")
    assert not brain.is_protected("PREFRONTAL/PROJECTS/Acme/ACME.md")
    assert brain.is_protected("HIPPOCAMPUS/SYNAPSE.md")


def test_missing_synapse_is_created_from_the_brains_guide(config: Config, brain_dir: Path, logger: Logger,
                                                         errors: ErrorHandler) -> None:
    # SYNAPSE/ENGRAM are in-transit files a fresh clone doesn't have; the brain's guide holds their templates.
    (brain_dir / "HIPPOCAMPUS/SYNAPSE.md").unlink()
    (brain_dir / "HIPPOCAMPUS/ENGRAM.md").unlink()
    with build(config, logger, errors):
        synapse = (brain_dir / "HIPPOCAMPUS/SYNAPSE.md").read_text(encoding="utf-8")
        assert "From the guide. Process: [[CORTEX#Brain Upkeep" in synapse and "Next ID: HX0001" in synapse
        assert "From the guide: trail" in (brain_dir / "HIPPOCAMPUS/ENGRAM.md").read_text(encoding="utf-8")


def test_commit_to_personal_layer_updates_the_index_without_a_map(client: TestClient, brain_dir: Path) -> None:
    # No hand-kept index: a new file's section and a renamed heading show up in the next load by themselves.
    key = new_key(client)
    failed, text = call(client, key, "synapse_queue", target="PREFRONTAL/ENVIRONMENT#CODING", change="Windows 11, PowerShell",
                        why="machine facts", source="preference")
    assert text == "Queued HX0002 → PREFRONTAL/ENVIRONMENT#CODING"
    failed, text = call(client, key, "synapse_commit", id="HX0002", summary="machine facts", changes=[
        {"note": "PREFRONTAL/ENVIRONMENT.md",
         "content": "---\ntags:\n  - memory/personal\n---\n> Up: [[PREFRONTAL/PREFRONTAL|PREFRONTAL]]\n\n## CODING\n- Windows 11, PowerShell.\n"},
    ], renames=[{"note": "PREFRONTAL/STYLE.md", "old_heading": "WRITING", "new_heading": "PROSE"}])
    assert not failed, text
    assert "## PROSE" in (brain_dir / "PREFRONTAL/STYLE.md").read_text(encoding="utf-8")
    failed, text = call(client, key, "cortex_load")
    assert "  - CODING: PREFRONTAL/ENVIRONMENT.md#CODING, PREFRONTAL/STYLE.md#CODING" in text
    assert "  - PROSE: PREFRONTAL/STYLE.md#PROSE" in text and "WRITING: PREFRONTAL" not in text
    assert "landed in PREFRONTAL/ENVIRONMENT#CODING" in (brain_dir / "HIPPOCAMPUS/ENGRAM.md").read_text(encoding="utf-8")


def test_rename_heading_tool(client: TestClient, brain_dir: Path) -> None:
    key = new_key(client)
    failed, text = call(client, key, "cortex_rename_heading", note="PREFRONTAL/PROJECTS/Acme/ACME.md", old_heading="Now", new_heading="Status")
    assert not failed and "updated links in PREFRONTAL/PROJECTS/Acme/NOTES.md" in text
    assert "[[ACME#Status]]" in (brain_dir / "PREFRONTAL/PROJECTS/Acme/NOTES.md").read_text(encoding="utf-8")
    failed, text = call(client, key, "cortex_rename_heading", note="PREFRONTAL/STYLE.md", old_heading="CODING", new_heading="CODE")
    assert failed and "protected" in text and "renames" in text
    assert "## CODING" in (brain_dir / "PREFRONTAL/STYLE.md").read_text(encoding="utf-8")


def test_check_runs_the_brains_own_audit_script(client: TestClient, brain_dir: Path) -> None:
    key = new_key(client)
    script = brain_dir / "scripts" / "check_brain.py"
    script.parent.mkdir()
    script.write_text("import os\nprint('0 errors, 0 warnings', 'secret-env' if 'CORTEX_ADMIN_PWD' in os.environ else 'clean-env')\n",
                      encoding="utf-8")
    failed, text = call(client, key, "cortex_check")
    assert not failed and text.startswith("Passed · scripts/check_brain.py") and "0 errors, 0 warnings clean-env" in text
    script.write_text("import sys\nprint('CORTEX.md:3: dead link [[X]]')\nsys.exit(1)\n", encoding="utf-8")
    failed, text = call(client, key, "cortex_check")
    assert text.startswith("Problems found · scripts/check_brain.py") and "dead link [[X]]" in text


def test_check_falls_back_to_built_in_checks(client: TestClient) -> None:
    key = new_key(client)
    failed, text = call(client, key, "cortex_check")
    assert text.startswith("Problems found · built-in checks")
    assert "PREFRONTAL/PROJECTS/Acme/ACME.md: dead link [[Missing Note]]" in text
    assert "PREFRONTAL/PROJECTS/Acme/ACME.md: dead heading link [[NEOCORTEX/WRITING#Nope]]" in text


def test_check_warns_when_a_layout_folder_is_unprotected(config: Config, logger: Logger, errors: ErrorHandler) -> None:
    # A protected list saved before the layout moved still names MEMORY/ and CEREBELLUM/.
    app = create_app(config, logger, Secrets(config.secrets), errors)
    brain = app.state.brain
    brain.configure("CORTEX.md", protected=["CORTEX.md", "MEMORY/", "CEREBELLUM/"])
    output = brain.audit("test").output
    for folder in ("NEOCORTEX/", "PREFRONTAL/"):
        assert f"warning: {folder} isn't protected" in output
    assert "NEOCORTEX/NEOCORTEX.md isn't protected" not in output  # covered by the folder's warning
    brain.configure("CORTEX.md", protected=["CORTEX.md", "NEOCORTEX/", "PREFRONTAL/", "!NEOCORTEX/NEOCORTEX.md"])
    assert "warning: NEOCORTEX/NEOCORTEX.md isn't protected" in brain.audit("test").output
    brain.configure("CORTEX.md")
    assert "isn't protected" not in brain.audit("test").output


def test_graph_marks_the_entry_point(client: TestClient) -> None:
    # The GUI highlights the brain's entry point; only the configured CORTEX.md is marked.
    login(client)
    nodes = client.get("/api/graph").json()["nodes"]
    assert [node["id"] for node in nodes if node["entry"]] == ["CORTEX.md"]


def test_graph_lists_dead_heading_links(client: TestClient) -> None:
    login(client)
    dead = client.get("/api/graph").json()["dead"]
    assert {"source": "PREFRONTAL/PROJECTS/Acme/ACME.md", "target": "NEOCORTEX/WRITING#Nope"} in dead
