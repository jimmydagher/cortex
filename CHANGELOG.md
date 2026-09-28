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

## 🆕VERSION 0.2.3 📅 2026-09-28

### Added or New Features
- Graph options, like Obsidian's graph settings: an **Options** panel on the Brain tab with filters (search, show orphans, local graph around the selected note with a depth), display settings (node size, link thickness and opacity, arrows, labels, the zoom at which labels appear, dimming on hover) and forces (center, repel, link force, link distance), plus Reset. Legend entries now show or hide their color group. Settings are remembered in the browser.

### Removed
(none)

### Changed
- Deploying works like flammeau and life-dashboard: `deploy-nas.ps1` builds the image on the NAS from your working copy, with no GitHub Actions image and no GHCR pull. The image tag comes from the `VERSION` file (TODO #10). `-Version` now rolls back to an image an earlier deploy built on the NAS. Operator: remove `CORTEX_VERSION` from `.env.nas`; it's no longer read.
- Graph nodes are smaller by default. The **Labels** checkbox moved into the Options panel as "Always show labels".
- The sign-in screen asks for "your passcode" instead of "Admin password", since guests sign in there too, and shows the running version.

### Bug/Issues/Fixes
- The note reader no longer shows a stray "null" next to the tags of notes that aren't protected.

## 🟩VERSION 0.2.2 📅 2026-09-27

### Added or New Features
- Optional read-only guest account for the GUI. A guest signs in with their own password and can browse the graph, notes, SYNAPSE and activity, but can't change anything or see API keys, settings or setup. Operator: `deploy-nas.ps1 -SetGuestPassword` creates it (the `cortex-guest-pwd` secret, which must differ from the admin password); `-RemoveGuest` deletes it.

### Removed
(none)

### Changed
(none)

### Bug/Issues/Fixes
- `deploy-nas.ps1 -ResetAdminPassword` / `-RotateSessionKey` now restart Cortex so the new secret takes effect; before, a running container kept the old one until its next restart.

## 🟨VERSION 0.2.1 📅 2026-09-27

### Added or New Features
(none)

### Removed
(none)

### Changed
- `deploy-nas.ps1` says when the version isn't on GHCR yet (CI still running) instead of stopping at Docker's "manifest unknown".

### Bug/Issues/Fixes
(none)

## 🟧VERSION 0.2.0 📅 2026-09-27

### Added or New Features
- Personal layer: `cortex_load` also returns `CEREBELLUM/MAP.md` and the sections it lists under `Always`, and warns about map links that don't resolve.
- `cortex_check` for "check the brain": runs the brain's own audit script (`audit.script`), or built-in dead-link, dead-heading-link and SYNAPSE ID checks when there is none (TODO #7).
- `cortex_rename_heading` renames a heading and updates every `[[note#Heading]]` link to it; `synapse_commit` takes `renames` for protected notes, applied in the same commit.
- Dead heading links (`[[note#Heading]]` to a heading that doesn't exist) show in the GUI's dead-link count and list, matching the brain's audit.
- claude.ai custom connectors: the `/mcp` gate also accepts the API key in an `x-auth-token` or `x-api-key` request header (claude.ai reserves `Authorization` for its own sign-in), and the Connect tab shows the claude.ai settings.
- The GUI's top bar shows the running version (from `VERSION`) next to the Cortex name.
- `scripts/ps1/deploy-nas.ps1`: deploys to the NAS from your PC through the `synology` Docker context (as flammeau does): creates the folders, uploads `config/override/nas.local.yaml`, writes missing secrets over the Docker connection (asks once for the admin password), runs the pre-flight, starts Cortex and checks `/healthz`.
- `scripts/ps1/run-local.ps1`: runs Cortex on your PC from `.venv`, installing requirements when they change; `-BrainPath` serves any brain folder, `-CheckOnly` stops after `validate-config`.

### Removed
(none)

### Changed
- The default protected list adds the personal layer (`CEREBELLUM/`) and the HIPPOCAMPUS guide. Operator: an already set-up Cortex keeps its list; add both in the GUI's Settings.
- A missing SYNAPSE or ENGRAM is created from the templates in the brain's `HIPPOCAMPUS/HIPPOCAMPUS.md`; the built-in templates are only the fallback.
- Tool descriptions cover personal SYNAPSE destinations (`CEREBELLUM/FILE#Section`), "remember for me", adding the MAP row in the same commit, and "reload the brain".
- `docker-compose.yml` mounts the NAS secrets folder read-only at `/run/secrets` instead of using compose `secrets:` (whose file paths a remote Docker context resolves on the PC), and reads its wiring from `.env.nas` (was `.env`; the example is now `.env.nas.example`). Operator: copy `.env.nas.example` to `.env.nas` on your PC.
- New config keys with defaults in `config/default.yaml`: `layout.hippocampus_guide`, `layout.personal`, `layout.personal_map`, `audit.script`, `audit.timeout_seconds`; nothing to set.

### Bug/Issues/Fixes
(none)

## 🟥VERSION 0.1.0 📅 2026-09-26

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

