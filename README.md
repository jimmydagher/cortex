# Cortex

An MCP server with a web GUI that serves a Markdown second brain to Claude (or any MCP client). It runs in a container, typically on a NAS, and reads the brain from a folder outside the container (`/data/brain`), so Obsidian, git and Cortex can all work on the same files.

The brain is routed by a `CORTEX.md` file: a routing table that points the AI at the one instruction file a task needs. Structure beyond that is yours. The [claude-brain](https://github.com/jimmydagher/claude-brain) template is one way to lay it out, and the setup wizard can download it for you.

## What it does

- **Follows the network of notes.** `cortex_load` returns `CORTEX.md`; `cortex_read` opens whatever it routes to. Wikilinks resolve the way Obsidian resolves them (`[[MEMORY/CODING|CODING]]`, `[[CODING]]`, `[[CORTEX#Brain Upkeep]]`), so any folder layout works.
- **Gates memory through the hippocampus.** New lessons are queued in `SYNAPSE.md` and wait for your decision. Approve them in the GUI or tell Claude "commit to memory HX0007"; rejected ideas go to `ENGRAM.md` so they aren't proposed again. Protected notes (the routing file and memory areas) refuse direct writes, so the gate is enforced by the server, not only by the prompt.
- **Has a power switch.** "Turn off the brain" makes every tool answer "off" for every client until you turn it back on. Nothing needs uninstalling.
- **Shows you the brain.** The GUI has a graph view (colored from your `.obsidian/graph.json`), a note reader, the SYNAPSE review queue, per-client API keys and an activity log.

## Deploy on a NAS

[docs/setup-nas.md](docs/setup-nas.md) walks through it from nothing: folders, the two secret files, the config override, `docker compose`, and the first-run wizard. [docs/deployment.md](docs/deployment.md) covers shipping later versions; [docs/cheat-sheet.md](docs/cheat-sheet.md) covers day-to-day operations.

## Connect Claude

In the GUI, open **Connect**, create a key per client (for example `laptop-claude-code`) and copy it; it's shown once. The page fills in these snippets with your URL and key.

Claude Code, for every project on the machine:

```bash
# dev machine shell
claude mcp add --transport http --scope user cortex http://<nas>:8765/mcp \
  --header "Authorization: Bearer <your key>"
```

A project `.mcp.json` that reads the key from an environment variable, so the file can be committed:

```json
{
  "mcpServers": {
    "cortex": {
      "type": "http",
      "url": "http://<nas>:8765/mcp",
      "headers": { "Authorization": "Bearer ${CORTEX_API_KEY}" }
    }
  }
}
```

Claude Desktop, through the [mcp-remote](https://www.npmjs.com/package/mcp-remote) bridge (needs Node.js), in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cortex": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://<nas>:8765/mcp", "--header", "Authorization:${CORTEX_AUTH}"],
      "env": { "CORTEX_AUTH": "Bearer <your key>" }
    }
  }
}
```

Revoking a key in the GUI cuts that client off immediately.

## Day to day

| Say to Claude | What happens |
| --- | --- |
| `use the cortex` / `use your brain` (or the `/mcp__cortex__cortex` prompt) | Loads `CORTEX.md` for the session and mentions anything you approved that still needs writing |
| `review synapse` / `what's pending?` (or `/mcp__cortex__synapse`) | Lists entries pending review and entries approved but not yet written |
| `commit to memory HX0007` | Claude previews the exact edit, writes it into memory and moves the entry to ENGRAM |
| `reject HX0007` | Moves it to ENGRAM with the reason, so it isn't proposed again |
| `remember: …` | Queues and commits in one step |
| `turn off the brain` / `turn on the brain` | Flips the power switch for every client |

In the GUI's **Synapse** tab, **Approve** moves an entry to an `## Approved` section in `SYNAPSE.md`. The next time a client loads the brain, it's told which entries are approved and offers to write them. Writing needs judgment (merge with the existing rule, settle conflicts), so the AI does it, not the GUI. **Reject** records the entry in ENGRAM straight away. **Send back** returns an approved entry to review.

## How the files are used

| File | Role | Who changes it |
| --- | --- | --- |
| `CORTEX.md` | Routing table and always-on rules | Only through an approved SYNAPSE commit (protected) |
| `MEMORY/…` (or whatever you protect) | Instruction files per area | Only through an approved SYNAPSE commit (protected) |
| `HIPPOCAMPUS/SYNAPSE.md` | Queue: `Next ID`, `## Pending`, `## Approved` | The synapse tools and the GUI |
| `HIPPOCAMPUS/ENGRAM.md` | Trail of committed and rejected entries, newest first | The synapse tools and the GUI |
| Everything else (`PROJECTS/…`) | Project state, notes | Claude via `cortex_write`, you in Obsidian |

The protected list and the SYNAPSE and ENGRAM paths are in the GUI's **Settings**. A trailing `/` protects a folder. Cortex writes atomically and keeps each file's line endings; entries keep the `- [ ] HX0002 · date · → target · change · why · source` format, so the brain stays readable in Obsidian and compatible with `check_brain.py`.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `cortex_load` | Returns `CORTEX.md` plus a short note on how to use the tools |
| `cortex_read` | Reads a note by path, name or wikilink; `#Heading` reads one section |
| `cortex_search` | Line search, optionally within a folder or file (e.g. ENGRAM before queueing) |
| `cortex_list` | Notes with their tags (up to `mcp.list_limit`) |
| `cortex_write` | Creates or edits unprotected notes (exact-match edits or whole content) |
| `synapse_queue` | Adds a pending entry under the next ID |
| `synapse_list` | Pending and approved entries, optionally the recent trail |
| `synapse_commit` | Applies the memory edits (all validated before any is written) and moves the entry to ENGRAM |
| `synapse_reject` | Moves the entry to ENGRAM with a reason |
| `cortex_power` | `on`, `off` or `status` |

## Security

- **Two credentials.** The GUI uses the admin password (signed, HttpOnly, SameSite=Strict cookie; `gui.login_max_failures` wrong attempts lock that IP out for `gui.login_window_seconds`). MCP clients use per-client API keys, stored as SHA-256 hashes in `/data/cortex/state.json` and shown once.
- **Secrets are files.** The admin password and the session-signing key are Docker secrets (`cortex-admin-pwd`, `cortex-session-key`), never config or environment values in a deployment.
- **Host checking.** Cortex answers only to the names in `server.allowed_hosts`, for the GUI and `/mcp` alike.
- **Use HTTPS beyond your LAN.** Keys travel in a header. Put Cortex behind your NAS's reverse proxy with TLS ([docs/setup-nas.md](docs/setup-nas.md) › HTTPS).
- **Content is data.** Note HTML is rendered with raw HTML disabled and a strict Content-Security-Policy. Tools only touch `.md` files inside the brain folder; dot-folders (`.obsidian`, `.git`) and paths that escape the folder are refused.
- **Errors don't leak.** Every error response has one shape, `{"error": {"code", "message", "request_id"}}`; unexpected failures return a generic message and the request id, and the detail goes to the log.
- **Audit trail.** Loads, note reads, writes, SYNAPSE decisions, power changes, logins and key changes are logged, with the client's key label and the request id, to the Activity tab and `/logs/cortex.log` (plain text, rotated).

## Configuration

All settings live in [config/default.yaml](config/default.yaml). Each environment has an override that states only what differs: [local](config/override/local.yaml), [test](config/override/test.yaml), and the [nas](config/override/nas.yaml) template you copy to the NAS. The merged config is validated at startup, and every problem is reported at once.

### Environment variables (wiring only)

| Variable | Meaning | Set by |
| --- | --- | --- |
| `CORTEX_ENV` | Which override to load (`nas`, `local`, `test`) | `docker-compose.yml`, `.vscode/launch.json` |
| `CORTEX_CONFIG_DIR` | Folder holding `default.yaml` | the image (`/app/config`), launch config |
| `CORTEX_OVERRIDE_DIR` | Folder holding `<env>.yaml` | the image (`/app/config/override`); compose points it at `/config` |

### Secrets

| Name | Holds | Where |
| --- | --- | --- |
| `cortex-admin-pwd` | GUI admin password | file in `secrets.dir` (`/run/secrets` via compose `secrets:`) |
| `cortex-session-key` | Signs GUI session cookies | same |

Locally, the same files go in `./secrets/`; `config/override/local.yaml` also allows `CORTEX_ADMIN_PWD` / `CORTEX_SESSION_KEY` environment variables as a fallback.

### Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `server.host` / `server.port` | `0.0.0.0` / `8765` | Listen address inside the container |
| `server.public_url` | `""` | Base URL in the Connect snippets; empty uses the request's URL |
| `server.allowed_hosts` | must be set | Host names Cortex answers to |
| `server.secure_cookies` | must be set | `true` behind HTTPS |
| `server.forwarded_allow_ips` | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` |
| `paths.brain` / `paths.state` / `paths.logs` | `/data/brain` / `/data/cortex` / `/logs` | Where the brain, runtime state and logs live |
| `logging.level` | `INFO` | Lowest level written (`DEBUG` … `CRITICAL`) |
| `logging.file`, `max_bytes`, `backups` | `cortex.log`, 1 MB, 5 | Log file name and rotation |
| `logging.queue_size`, `drain_seconds`, `recent_events` | 10000, 5, 300 | Background writer's queue, shutdown drain bound, GUI activity buffer |
| `gui.session_days` | 30 | GUI session lifetime |
| `gui.login_window_seconds`, `login_max_failures` | 300, 5 | Login lockout |
| `gui.trail_limit`, `activity_limit` | 25, 150 | Items shown in the GUI |
| `mcp.list_limit` | 300 | Most notes `cortex_list` returns |
| `vault.max_note_bytes`, `scan_ttl_seconds` | 1 MB, 2 | Largest note read; how long the index is reused |
| `layout.synapse`, `engram`, `memory` | `HIPPOCAMPUS/SYNAPSE.md`, `HIPPOCAMPUS/ENGRAM.md`, `MEMORY/` | Defaults when setup can't find them; seeds the protected list |
| `setup.template_repo` | `jimmydagher/claude-brain` | GitHub `owner/repo` the wizard downloads |
| `setup.download_timeout_seconds`, `max_download_bytes` | 60, 50 MB | Template download limits |
| `secrets.dir`, `allow_env_fallback` | `/run/secrets`, `false` | Where secrets are read; env fallback (local only) |
| `secrets.admin_password`, `session_key` | `cortex-admin-pwd`, `cortex-session-key` | Secret names |
| `state.initial_power`, `last_used_flush_seconds` | `on`, 60 | Power for a new state file; key last-used write interval |
| `runtime.*` | derived | Environment, version (from `VERSION`), files loaded |

## Run it locally

Needs Python 3.14 with pip (`python --version`). The project keeps its packages in its own `.venv` folder, so nothing is installed into your system Python.

```powershell
# dev machine, repo root (PowerShell)
python -m venv .venv                                   # once
.venv\Scripts\Activate.ps1                             # each new terminal
python -m pip install -r requirements-dev.txt          # the same versions CI uses
mkdir secrets; Set-Content -NoNewline secrets\cortex-admin-pwd dev-password; Set-Content -NoNewline secrets\cortex-session-key dev-session-key
$env:PYTHONPATH = "src"; $env:CORTEX_ENV = "local"; $env:CORTEX_CONFIG_DIR = "config"; $env:CORTEX_OVERRIDE_DIR = "config/override"
python -m cortex validate-config
python -m cortex serve                                 # http://localhost:8765, brain in .\data\brain, log in .\logs
```

```bash
# dev machine, repo root (Git Bash / macOS / Linux)
python -m venv .venv && source .venv/Scripts/activate   # macOS/Linux: .venv/bin/activate
python -m pip install -r requirements-dev.txt
mkdir -p secrets && printf 'dev-password' > secrets/cortex-admin-pwd && printf 'dev-session-key' > secrets/cortex-session-key
export PYTHONPATH=src CORTEX_ENV=local CORTEX_CONFIG_DIR=config CORTEX_OVERRIDE_DIR=config/override
python -m cortex serve
```

In VS Code, pick `.venv` as the interpreter; the **Cortex: serve (local)** launch configuration then does the same with the debugger attached.

## Test

```bash
# dev machine, repo root, inside .venv
python scripts/python/check.py            # lint (ruff), type-check (mypy), tests (pytest): what CI runs
python -m pytest tests -m integration     # real GitHub download
python -m playwright install chromium     # once, for the browser tests
python -m pytest tests -m e2e             # sign-in, graph, approve, keys in a real browser
```

CI also runs a dependency vulnerability scan and the changelog check on every pull request.

## Dependencies

`requirements.in` lists what Cortex needs to run and `requirements-dev.in` what development adds; the matching `.txt` files pin every package to an exact version and are what gets installed. After editing an `.in` file, regenerate the `.txt` files (needs Docker, because they're resolved on the same Linux image the container runs):

```bash
# dev machine, repo root
python scripts/python/lock.py
```

Stack: Python 3.14, pip, the official MCP Python SDK (stateless Streamable HTTP at `/mcp`), Starlette, and a dependency-free vanilla JS GUI, so the container works without internet access once built. Project conventions: [CLAUDE.md](CLAUDE.md). All docs: [docs/index.md](docs/index.md).
