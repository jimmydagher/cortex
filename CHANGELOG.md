# Changelog

All notable changes, newest first. See `VERSION` for the current release.

## 🚧 Unreleased

### Added or New Features
(none)

### Removed
(none)

### Changed
(none)

### Bug/Issues/Fixes
(none)

## 🆕VERSION 0.1.0 📅 2026-09-26

### Added or New Features
- Cortex: an MCP server at `/mcp` (per-client API keys, 10 tools, 2 prompts) and a web GUI (graph view, note reader, SYNAPSE review, keys, activity, settings, first-run setup) serving a Markdown brain routed by `CORTEX.md`.
- Configuration in `config/default.yaml` with per-environment overrides (`local`, `test`, and a `nas` template), validated at startup with every problem reported at once. Operator: copy `config/override/nas.yaml` to the `/config` volume as `nas.yaml` and set `server.allowed_hosts` and `server.secure_cookies` (F-010).
- `cortex validate-config`: checks config, secrets and folders without serving, for use as a deploy pre-flight (F-011).
- Secrets as Docker secret files `cortex-admin-pwd` and `cortex-session-key`; the environment fallback works only in local development. Operator: create both files before starting (docs/setup-nas.md) (F-014, F-015).
- Host checking: the GUI and `/mcp` answer only to `server.allowed_hosts` (F-012).
- Request ids: every response carries `X-Request-ID`, every log line carries `req=<id>`, and each request writes one access line (F-016).
- One error shape for the GUI API and `/mcp`, `{"error": {"code", "message", "request_id"}}`, with typed errors mapped to HTTP status in one table (F-018, F-020).
- A global error handler: unexpected failures are logged with their request id and answered with a generic 500 (F-019).
- Distinct exit codes: 0 succeeded, 2 invalid input, 3 startup failure, 130 interrupted (F-023).
- The claude-brain template download runs as a background job; the GUI polls it instead of waiting on one long request (F-024).
- `/favicon.ico`, generated from the SVG icon's shapes by `scripts/python/make_favicon.py` (F-013).
- CI (GitHub Actions): lint, type-check, tests, dependency vulnerability scan, changelog check and a Docker build on every pull request; on `main`, publishes `ghcr.io/jimmydagher/cortex:<VERSION>` once per version (F-027).
- Browser tests of sign-in, the graph, approval and keys (`pytest -m e2e`), and an integration test against the real template on GitHub (`pytest -m integration`) (F-026, F-028).
- Docs: `docs/design.md` (intent and decisions), `docs/setup-nas.md`, `docs/deployment.md`, `docs/cheat-sheet.md` and `docs/index.md`; `CLAUDE.md` with the SDSI profile, deliberate deviations and the concurrency model (F-001, F-003, F-030).
- The SDSI release chain (`VERSION`, this changelog, `TODO.md`, `scripts/git` hooks, `scripts/python/release.py`) and a VS Code launch configuration for local run and debug (F-032, F-033).

### Removed
(none)

### Changed
- Runs on Python 3.14, with the base image pinned by digest (F-029).
- Packages are installed with pip from `requirements.txt` (exact versions, generated from `requirements.in` by `scripts/python/lock.py`); local checks run with `python scripts/python/check.py`: ruff, mypy (strict), pytest (F-002) (TODO #4).
- The NAS pulls the image CI built for a version (`CORTEX_VERSION` in `.env`) instead of building it (F-034).
- Runtime state (API key hashes, power switch, setup paths) lives in `/data/cortex/state.json`, logs in `/logs`, and `/config` holds only the environment's override.
- MCP tools and GUI handlers share one service layer (`Brain`); GUI request bodies are validated at the boundary (F-004, F-006).
- Closed value sets are enums with a name→handler registry for GUI actions and setup modes (F-005).
- Full names, docstrings on every public function, one path normalizer and one code-fence scanner (F-007, F-008, F-009).
- Before setup, SYNAPSE requests fail with 409 `not_configured` instead of returning empty lists (F-021).
- Unreadable or oversized notes are logged as warnings, and a malformed JSON body gets a 422 instead of "wrong password" (F-022).
- uvicorn's and the MCP SDK's logs go through the same plain-text log as Cortex's own (F-017).
- The log queue is bounded; lines dropped by a full queue are counted and reported (F-025).
- Every command block in the docs says where it runs (F-031).

### Bug/Issues/Fixes
(none)

