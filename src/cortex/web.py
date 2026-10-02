"""The HTTP app: bearer-gated /mcp for clients, password-gated GUI and JSON API for the human
(an admin who can change things, and an optional read-only guest).

Handlers are thin: parse and validate the body (Pydantic models below), call one Brain
method in the thread pool (Brain does file and network I/O), shape the response.
Errors have one shape everywhere:

  {"error": {"code": "not_found", "message": "...", "request_id": "4f2a91c07b3e"}}

Middleware, outermost first: request id + access line, host check, security headers,
unexpected-error catcher. Typed errors map to status through errors.HTTP_STATUS.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .bootstrap import SetupMode
from .brain import Brain
from .config import Config
from .errors import (
    GENERIC_MESSAGE,
    HTTP_STATUS,
    CortexError,
    ErrorCode,
    ErrorHandler,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    SecretError,
    UnauthorizedError,
)
from .logs import REQUEST_ID, Logger
from .mcp_tools import CLIENT_HEADER, REQUEST_ID_HEADER, build_mcp
from .secrets import Secrets
from .state import LABEL_MAX, Power
from .synapse import Entry
from .vault import WIKILINK, link_target, scan_lines, split_frontmatter

STATIC = Path(__file__).parent / "static"
COOKIE = "cortex_session"
CSRF_HEADER = "x-cortex-csrf"
REQUEST_ID_RESPONSE_HEADER = "x-request-id"
INCOMING_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
MCP_PATH = "/mcp"
# Besides Authorization: the standard API-key headers claude.ai's connector dialog offers.
KEY_HEADERS = (b"x-auth-token", b"x-api-key")
HEALTH_PATH = "/healthz"
GUI_ACTOR = "gui"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
TASK_MARKER = re.compile(r"\[([ xX])\](?: |$)")
HTTP_ERROR_CODES = {HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED}


# ---------- error shape ----------


def error_response(code: ErrorCode, message: str, headers: dict[str, str] | None = None,
                   status: int | None = None) -> JSONResponse:
    """The one error response shape.

    Args:
        code: the machine-readable code; decides the status unless `status` is given.
        message: safe to show a client.
        headers: extra response headers.
        status: an explicit status (only for framework errors such as 405).

    Returns:
        A JSON response with code, message and the request id.
    """
    body = {"error": {"code": code.value, "message": message, "request_id": REQUEST_ID.get()}}
    return JSONResponse(body, status_code=status or HTTP_STATUS[code], headers=headers)


async def _send_error(scope: Scope, receive: Receive, send: Send, code: ErrorCode, message: str,
                      headers: dict[str, str] | None = None) -> None:
    await error_response(code, message, headers)(scope, receive, send)


# ---------- middleware ----------


class RequestContext:
    """Gives every request an id (echoed in X-Request-ID) and writes one access line when it ends."""

    def __init__(self, app: ASGIApp, logger: Logger) -> None:
        self.app, self.logger = app, logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if INCOMING_REQUEST_ID.match(incoming) else secrets.token_hex(6)
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        status = HTTPStatus.INTERNAL_SERVER_ERROR.value

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (REQUEST_ID_RESPONSE_HEADER.encode(), request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            if scope["path"] != HEALTH_PATH:  # container health probes every 30 s would drown the log
                self.logger.access(scope["method"], scope["path"], status, (time.perf_counter() - started) * 1000)
            REQUEST_ID.reset(token)


class HostCheck:
    """Answers only to server.allowed_hosts, for the GUI and /mcp alike (DNS-rebinding defense)."""

    def __init__(self, app: ASGIApp, allowed: list[str]) -> None:
        self.app = app
        self.allowed = {host.lower() for host in allowed}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        host = dict(scope["headers"]).get(b"host", b"").decode("latin-1").lower()
        name = host[1:].split("]", 1)[0] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if name not in self.allowed and host not in self.allowed:
            await _send_error(scope, receive, send, ErrorCode.INVALID_HOST, "this host name isn't in server.allowed_hosts")
            return
        await self.app(scope, receive, send)


class SecurityHeaders:
    """CSP and friends on every GUI response (not on /mcp, which serves no pages)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(MCP_PATH):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []),
                                      (b"content-security-policy", CSP.encode()),
                                      (b"x-content-type-options", b"nosniff"),
                                      (b"referrer-policy", b"no-referrer"),
                                      (b"x-frame-options", b"DENY"),
                                      (b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class UnexpectedErrors:
    """Anything not anticipated: reported to the global handler, answered with a generic 500."""

    def __init__(self, app: ASGIApp, errors: ErrorHandler) -> None:
        self.app, self.errors = app, errors

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as error:  # noqa: BLE001 - the web layer's hook into the global handler
            self.errors.handle(error, {"where": f"{scope['method']} {scope['path']}", "request_id": REQUEST_ID.get()})
            if not started:
                await _send_error(scope, receive, send, ErrorCode.INTERNAL, GENERIC_MESSAGE)


def presented_keys(headers: dict[bytes, bytes]) -> list[str]:
    """Every credential a client sent: `Authorization: Bearer <key>`, and the key in `x-auth-token` or
    `x-api-key` (raw or as `Bearer <key>`), which claude.ai's request headers use when its own
    sign-in keeps `Authorization` for an OAuth token.

    Args:
        headers: the request's headers, lowercase names.

    Returns:
        The candidate keys, in that order, without empty ones.
    """
    candidates = []
    scheme, _, token = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
    if scheme.lower() == "bearer":
        candidates.append(token.strip())
    for name in KEY_HEADERS:
        value = headers.get(name, b"").decode("latin-1").strip()
        candidates.append(value[len("bearer "):].strip() if value.lower().startswith("bearer ") else value)
    return [candidate for candidate in candidates if candidate]


class BearerGate:
    """Only requests with a valid API key reach the MCP transport.

    The key's label and the request id are forwarded in headers the gate controls, so
    tools can attribute activity without trusting anything the client sent.
    """

    def __init__(self, app: ASGIApp, brain: Brain) -> None:
        self.app, self.brain = app, brain
        self.controlled = {CLIENT_HEADER, REQUEST_ID_HEADER}

    def _check(self, candidates: list[str]) -> dict[str, str] | None:
        return next((record for key in candidates if (record := self.brain.state.check_key(key))), None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(name, value) for name, value in scope["headers"] if name.decode("latin-1").lower() not in self.controlled]
        record = await run_in_threadpool(self._check, presented_keys(dict(headers)))
        if not record:
            await _send_error(scope, receive, send, ErrorCode.UNAUTHORIZED,
                              "send your Cortex API key as Authorization: Bearer <key> or in an x-auth-token header",
                              {"WWW-Authenticate": 'Bearer realm="cortex"'})
            return
        headers += [(CLIENT_HEADER.encode(), record["label"].encode("utf-8", "replace")),
                    (REQUEST_ID_HEADER.encode(), REQUEST_ID.get().encode())]
        await self.app({**scope, "headers": headers}, receive, send)


# ---------- GUI sessions ----------


class Role(StrEnum):
    """Who a GUI session belongs to."""

    ADMIN = "admin"
    GUEST = "guest"


def guest_password(config: Config, secret_store: Secrets) -> str | None:
    """The optional guest password; its secret file existing is what turns the guest account on.

    Args:
        config: validated configuration (secret names).
        secret_store: the secrets accessor.

    Returns:
        The guest password, or None when there's no guest account.

    Raises:
        SecretError: the admin password is missing, or the guest password equals it
            (a login couldn't tell the two apart).
    """
    guest = secret_store.optional(config.secrets.guest_password)
    if guest is not None and hmac.compare_digest(guest.encode(), secret_store.get(config.secrets.admin_password).encode()):
        raise SecretError([f"secret {config.secrets.guest_password} must differ from {config.secrets.admin_password}"])
    return guest


class Sessions:
    """Stateless signed cookies, one signing key per role, and a login lockout.

    Each role's key is derived from its own password, so changing one password signs out
    only that role. The lockout lives in process memory: Cortex runs as one process on
    one NAS (recorded as a deliberate deviation in CLAUDE.md).
    """

    def __init__(self, session_key: str, admin_password: str, guest: str | None, config: Config) -> None:
        self.passwords = {Role.ADMIN: admin_password} | ({Role.GUEST: guest} if guest else {})
        self.keys = {role: hashlib.sha256(f"{session_key}:{password}".encode() if role == Role.ADMIN
                                          else f"{session_key}:{role}:{password}".encode()).digest()
                     for role, password in self.passwords.items()}
        self.lifetime = config.gui.session_days * 86400
        self.window = config.gui.login_window_seconds
        self.max_failures = config.gui.login_max_failures
        self.failures: dict[str, list[float]] = {}

    def issue(self, role: Role) -> str:
        """A new signed session cookie value for a role."""
        payload = f"{int(time.time())}.{secrets.token_urlsafe(12)}"
        return payload + "." + hmac.new(self.keys[role], payload.encode(), "sha256").hexdigest()

    def role(self, cookie: str | None) -> Role | None:
        """Whose session a cookie is, if it's ours and not expired.

        Args:
            cookie: the cookie value, if any.

        Returns:
            The session's role, or None for a missing, forged or expired cookie.
        """
        if not cookie or cookie.count(".") != 2:
            return None
        payload, signature = cookie.rsplit(".", 1)
        issued = payload.split(".", 1)[0]
        if not issued.isdigit() or time.time() - int(issued) >= self.lifetime:
            return None
        return next((role for role, key in self.keys.items()
                     if hmac.compare_digest(signature, hmac.new(key, payload.encode(), "sha256").hexdigest())), None)

    def check_password(self, address: str, password: str) -> Role:
        """Check a login attempt.

        Args:
            address: the client's IP, for the lockout.
            password: what was typed.

        Returns:
            The role whose password it is.

        Raises:
            RateLimitedError: too many failures from this address in the window.
            UnauthorizedError: wrong password.
        """
        recent = [moment for moment in self.failures.get(address, []) if time.time() - moment < self.window]
        self.failures[address] = recent
        if len(recent) >= self.max_failures:
            raise RateLimitedError("too many attempts; wait a few minutes")
        # Every password is compared, so the time taken doesn't tell whether a guest account exists.
        matches = [role for role, expected in self.passwords.items() if hmac.compare_digest(password.encode(), expected.encode())]
        if not matches:
            recent.append(time.time())
            raise UnauthorizedError("wrong password")
        self.failures.pop(address, None)
        return matches[0]


# ---------- request bodies ----------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginBody(_Body):
    password: str = Field(min_length=1, max_length=1024)


class PowerBody(_Body):
    state: Power


class KeyBody(_Body):
    label: str = Field(min_length=1, max_length=LABEL_MAX)


class RejectBody(_Body):
    reason: str = Field(min_length=1, max_length=300)


class SettingsBody(_Body):
    cortex_path: str = Field(min_length=1)
    synapse_path: str | None = None
    engram_path: str | None = None
    protected: list[str]


class SetupBody(_Body):
    mode: SetupMode
    cortex_path: str = ""


async def parse[Body: BaseModel](request: Request, model: type[Body]) -> Body:
    """Validate a JSON body once, at the boundary.

    Args:
        request: the incoming request.
        model: the body's Pydantic model.

    Returns:
        The validated body.

    Raises:
        InvalidInputError: malformed JSON or a field that fails validation (all problems listed).
    """
    try:
        return model.model_validate_json(await request.body() or b"{}")
    except ValidationError as error:
        problems = [f"{'.'.join(str(part) for part in detail['loc']) or 'body'}: {detail['msg']}" for detail in error.errors()]
        raise InvalidInputError("; ".join(problems)) from error


class SynapseAction(StrEnum):
    """What the GUI can do to a SYNAPSE entry."""

    APPROVE = "approve"
    UNAPPROVE = "unapprove"
    REJECT = "reject"


SYNAPSE_ACTIONS: dict[SynapseAction, Callable[[Brain, str, RejectBody | None], Entry]] = {
    SynapseAction.APPROVE: lambda brain, entry_id, _body: brain.set_approved(GUI_ACTOR, entry_id, True),
    SynapseAction.UNAPPROVE: lambda brain, entry_id, _body: brain.set_approved(GUI_ACTOR, entry_id, False),
    SynapseAction.REJECT: lambda brain, entry_id, body: brain.reject_entry(GUI_ACTOR, entry_id, body.reason if body else ""),
}
ACTION_BODIES: dict[SynapseAction, type[RejectBody]] = {SynapseAction.REJECT: RejectBody}


# ---------- note rendering ----------


def render_note(brain: Brain, relative: str, text: str) -> str:
    """Markdown to HTML with raw HTML disabled; wikilinks become in-app links.

    Args:
        brain: for resolving wikilinks.
        relative: the note's path, for relative links.
        text: the note.

    Returns:
        Safe HTML for the GUI's reader.
    """
    _, body = split_frontmatter(text)
    lines = []
    for line, in_code in scan_lines(body):
        if not in_code:
            parts = re.split(r"(`[^`\n]*`)", line)
            for index in range(0, len(parts), 2):
                parts[index] = WIKILINK.sub(lambda match: _wikilink(brain, relative, match.group(1)), parts[index])
            line = "".join(parts)
        lines.append(line)
    return str(MARKDOWN.render("\n".join(lines)))


def _task_lists(state: StateCore) -> None:
    """Turn `- [ ] item` / `- [x] item` into read-only checkboxes (a markdown-it core rule)."""
    tokens = state.tokens
    for index in range(2, len(tokens)):
        inline, item = tokens[index], tokens[index - 2]
        if inline.type != "inline" or tokens[index - 1].type != "paragraph_open" or item.type != "list_item_open":
            continue
        first = inline.children[0] if inline.children else None
        match = TASK_MARKER.match(first.content) if first is not None and first.type == "text" else None
        if first is None or match is None:
            continue
        first.content = first.content[match.end() :]
        box = Token("html_inline", "", 0)
        box.content = f'<input type="checkbox" disabled{" checked" if match.group(1) != " " else ""}>'
        inline.children = [box, *(inline.children or [])]
        item.attrJoin("class", "task")
        for parent in reversed(tokens[: index - 2]):
            if parent.type in ("bullet_list_open", "ordered_list_open") and parent.level == item.level - 1:
                if "tasks" not in str(parent.attrGet("class") or ""):
                    parent.attrJoin("class", "tasks")
                break


MARKDOWN.core.ruler.push("task_lists", _task_lists)


def _wikilink(brain: Brain, source: str, raw: str) -> str:
    target = link_target(raw)
    label = raw.split("|", 1)[1] if "|" in raw else raw.rstrip("\\")
    label = re.sub(r"([\[\]\\])", r"\\\1", label.replace("\\", "").strip() or target)
    resolved = brain.vault.resolve(target, source) if target else source
    href = f"#note={quote(resolved)}" if resolved else f"#missing={quote(target)}"
    return f"[{label}]({href})"


# ---------- app ----------


def create_app(config: Config, logger: Logger, secret_store: Secrets, errors: ErrorHandler) -> Starlette:
    """Build the web app.

    Args:
        config: validated configuration.
        logger: the central logger.
        secret_store: the secrets accessor (admin password, session key, optional guest password).
        errors: the global error handler.

    Returns:
        The Starlette app serving the GUI, its API and /mcp.

    Raises:
        SecretError: a required secret is missing, or the guest password equals the admin's.
    """
    admin_password = secret_store.get(config.secrets.admin_password)
    session_key = secret_store.get(config.secrets.session_key)
    guest = guest_password(config, secret_store)
    brain = Brain(config, logger, errors.handle)
    sessions = Sessions(session_key, admin_password, guest, config)
    mcp = build_mcp(brain, errors)
    # Host checking happens once for every route in HostCheck, so the SDK's own copy stays off.
    mcp.streamable_http_app(stateless_http=True, json_response=True,
                            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    session_manager = mcp.session_manager

    def api(handler: Callable[[Request], Awaitable[Any]], write: bool = False,
            admin: bool = False) -> Callable[[Request], Awaitable[Response]]:
        # Writes, and reads marked admin, are refused to the read-only guest.
        async def endpoint(request: Request) -> Response:
            role = sessions.role(request.cookies.get(COOKIE))
            if role is None:
                raise UnauthorizedError("login required")
            if write and request.headers.get(CSRF_HEADER) != "1":
                return error_response(ErrorCode.FORBIDDEN, "missing CSRF header")
            if (write or admin) and role != Role.ADMIN:
                raise ForbiddenError("the guest account is read-only")
            result = await handler(request)
            return result if isinstance(result, Response) else JSONResponse(result)

        return endpoint

    # ----- session -----

    async def login(request: Request) -> Response:
        body = await parse(request, LoginBody)
        address = request.client.host if request.client else "?"
        try:
            role = sessions.check_password(address, body.password)
        except (UnauthorizedError, RateLimitedError):
            logger.event(GUI_ACTOR, "login failed", address, level=logging.WARNING)
            raise
        response = JSONResponse({"ok": True, "role": role.value})
        response.set_cookie(COOKIE, sessions.issue(role), max_age=sessions.lifetime, httponly=True, samesite="strict",
                            secure=config.server.secure_cookies)
        logger.event(GUI_ACTOR, "login", f"{address} as {role.value}")
        return response

    async def logout(request: Request) -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    async def session(request: Request) -> Response:
        role = sessions.role(request.cookies.get(COOKIE))
        return JSONResponse({"authenticated": role is not None, "role": role.value if role else None,
                             "version": config.runtime.version})  # shown on the sign-in screen

    # ----- reads -----

    async def status(request: Request) -> dict[str, Any]:
        result = await run_in_threadpool(brain.status)
        result["public_url"] = result["public_url"] or str(request.base_url).rstrip("/")
        return result

    async def graph(request: Request) -> dict[str, Any]:
        return await run_in_threadpool(brain.graph)

    async def note(request: Request) -> dict[str, Any]:
        view = await run_in_threadpool(brain.note_view, request.query_params.get("path", ""))
        view["html"] = await run_in_threadpool(render_note, brain, view["path"], view.pop("text"))
        return view

    async def synapse(request: Request) -> dict[str, Any]:
        return await run_in_threadpool(brain.synapse_view)

    async def activity(request: Request) -> dict[str, Any]:
        return {"events": logger.recent(config.gui.activity_limit)}

    # ----- writes -----

    async def synapse_action(request: Request) -> dict[str, Any]:
        try:
            action = SynapseAction(request.path_params["action"])
        except ValueError as error:
            raise NotFoundError(f"unknown action {request.path_params['action']}") from error
        body = await parse(request, ACTION_BODIES[action]) if action in ACTION_BODIES else None
        entry = await run_in_threadpool(SYNAPSE_ACTIONS[action], brain, request.path_params["id"], body)
        return {"ok": True, "entry": entry.as_dict()}

    async def power(request: Request) -> dict[str, Any]:
        body = await parse(request, PowerBody)
        await run_in_threadpool(brain.set_power, GUI_ACTOR, body.state)
        return await run_in_threadpool(brain.status)

    async def list_keys(request: Request) -> dict[str, Any]:
        return {"keys": brain.state.public_keys()}

    async def create_key(request: Request) -> dict[str, Any]:
        body = await parse(request, KeyBody)
        record, raw = await run_in_threadpool(brain.create_key, GUI_ACTOR, body.label)
        return {"key": raw, "record": record}

    async def revoke_key(request: Request) -> dict[str, Any]:
        await run_in_threadpool(brain.revoke_key, GUI_ACTOR, request.path_params["id"])
        return {"ok": True}

    async def settings(request: Request) -> dict[str, Any]:
        body = await parse(request, SettingsBody)
        return await run_in_threadpool(brain.apply_settings, GUI_ACTOR, body.cortex_path, body.synapse_path or None,
                                       body.engram_path or None, body.protected)

    async def setup_scan(request: Request) -> dict[str, Any]:
        return await run_in_threadpool(brain.setup_scan)

    async def setup(request: Request) -> Response:
        body = await parse(request, SetupBody)
        job = await run_in_threadpool(brain.run_setup, GUI_ACTOR, body.mode, body.cortex_path)
        return JSONResponse(job, status_code=HTTPStatus.ACCEPTED if job["status"] == "running" else HTTPStatus.OK)

    async def setup_job(request: Request) -> dict[str, Any]:
        return brain.setup_job()

    # ----- pages -----

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    async def favicon(request: Request) -> Response:
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")

    async def healthz(request: Request) -> Response:
        return Response("ok", media_type="text/plain")

    # ----- errors -----

    async def cortex_error(request: Request, error: Exception) -> Response:
        typed = cast(CortexError, error)  # registered for CortexError only
        return error_response(typed.code, typed.message)

    async def http_error(request: Request, error: Exception) -> Response:
        http = cast(HTTPException, error)  # registered for HTTPException only
        code = HTTP_ERROR_CODES.get(HTTPStatus(http.status_code), ErrorCode.INTERNAL)
        return error_response(code, str(http.detail), status=http.status_code)

    routes = [
        Route(MCP_PATH, endpoint=BearerGate(session_manager.handle_request, brain)),
        Route(HEALTH_PATH, healthz),
        Route("/", index),
        Route("/favicon.ico", favicon),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/session", session),
        Route("/api/status", api(status)),
        Route("/api/graph", api(graph)),
        Route("/api/note", api(note)),
        Route("/api/synapse", api(synapse)),
        Route("/api/synapse/{id}/{action}", api(synapse_action, write=True), methods=["POST"]),
        Route("/api/activity", api(activity)),
        Route("/api/power", api(power, write=True), methods=["POST"]),
        Route("/api/keys", api(list_keys, admin=True)),
        Route("/api/keys", api(create_key, write=True), methods=["POST"]),
        Route("/api/keys/{id}", api(revoke_key, write=True), methods=["DELETE"]),
        Route("/api/settings", api(settings, write=True), methods=["POST"]),
        Route("/api/setup", api(setup_scan, admin=True)),
        Route("/api/setup", api(setup, write=True), methods=["POST"]),
        Route("/api/setup/job", api(setup_job, admin=True)),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        asyncio.get_running_loop().set_exception_handler(errors.asyncio_hook)
        async with session_manager.run():
            yield

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(RequestContext, logger=logger),
            Middleware(HostCheck, allowed=config.server.allowed_hosts),
            Middleware(SecurityHeaders),
            Middleware(UnexpectedErrors, errors=errors),
        ],
        exception_handlers={CortexError: cortex_error, HTTPException: http_error},
        lifespan=lifespan,
    )
    app.state.brain = brain
    return app
