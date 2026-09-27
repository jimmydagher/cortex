# Cortex

MCP server and web GUI that serve a Markdown second brain (routed by `CORTEX.md`) from a container. Design and intent: `docs/design.md`.

## SDSI profile

- Language: Python 3.14 (pip + venv; no pyproject.toml)
- Project type: web (Starlette routes serving the GUI and a JSON API, plus the MCP endpoint at `/mcp`)
- Deploy target: container on the owner's NAS (docker compose), image built once by CI and pulled from GHCR

## Deliberate deviations from SDSI

- **Single-process state.** The GUI login lockout (`web.Sessions.failures`) and API keys' last-used times live in process memory. Cortex runs as one process on one NAS; revisit only if it's ever scaled out (web companion: stateless handlers).
- **The SDK's DNS-rebinding check is off.** `web.HostCheck` validates the Host header once for every route, `/mcp` included, against `server.allowed_hosts`; the MCP SDK's own copy would be a second, disagreeing list.
- **Health probes aren't access-logged.** `/healthz` is hit every 30 s by Docker; logging it would drown the access lines.
- **Frontmatter tags are read without a YAML parser** (`vault.frontmatter_tags`): only `tags:` is needed and vault notes often carry loosely written YAML.

## Concurrency model

One asyncio event loop (uvicorn/Starlette). Blocking work never runs on it: GUI handlers call Brain through `run_in_threadpool`, and the MCP SDK runs the (sync) tools on worker threads. Two background threads: the log writer (`logs.Logger`, bounded queue, drained within `logging.drain_seconds` on stop) and the setup job (`brain.Brain._setup_in_background`, one at a time). Shared state has one owner each: `State` (its lock), `Hippocampus` (its lock), `Vault` index (its lock).

## Conventions

- `brain.Brain` is the service layer. MCP tools and GUI handlers only parse, call one Brain method and shape the reply.
- Errors: raise a `CortexError` subtype; status comes from `errors.HTTP_STATUS`. Never return an error dict by hand; never catch `Exception` outside `errors`/`web.UnexpectedErrors`/the MCP tool wrapper.
- Logs: plain text through `logs.Logger` only (`event`, `access`, `warn`); files in `paths.logs`, never JSON, never in config or data folders.
- `favicon.ico` is generated from the shapes in `scripts/python/make_favicon.py` (kept in step with `favicon.svg`); never edit the `.ico`.
- `scripts/python/release.py` and `scripts/git/*` are copied unchanged from the SDSI plugin; update them by recopying.
- Local checks before calling anything done: `python scripts/python/check.py` (ruff, mypy strict, pytest; the tools' settings live in that script).
- No TOML or extra config files: app settings only in `config/*.yaml`, packages only in `requirements*.in` → `requirements*.txt` (regenerate with `scripts/python/lock.py`). Cortex isn't installed as a package: `src/` goes on `PYTHONPATH` and it runs as `python -m cortex`.

## What changed → what else must be updated

| Change | Also update |
| --- | --- |
| A config key | `config/default.yaml` (value or placeholder) → `config.py` schema → README's config table; overrides that must state it |
| A secret | `config/default.yaml` (`secrets:` name) → `docker-compose.yml` secrets → `docs/setup-nas.md` → README |
| An MCP tool or prompt | `mcp_tools.py` → README's tool table → `docs/cheat-sheet.md` |
| A GUI API route | `web.py` → `app.js` → request-level test in `tests/test_app.py` |
| A command or exit code | `commands.py` / `errors.ExitCode` → `docs/cheat-sheet.md` → `.vscode/launch.json` |
| A dependency | `requirements.in` or `requirements-dev.in` → `python scripts/python/lock.py` → commit both `.txt` files |
| A volume or env wiring | `Dockerfile` → `docker-compose.yml` → `.env.example` → `docs/setup-nas.md` |
| Anything user- or operator-visible | a bullet in `CHANGELOG.md` › 🚧 Unreleased |
