# Design

What Cortex is for, what it must do, and the decisions that shape it. Recorded after the first build (the plan wasn't written down before the code; `sdsi:workflow` finding F-001), so later changes start from here.

## Intent

In the owner's words (2026-09-26):

> I want to build an MCP server to provide access to my 2nd brain, I'm calling it cortex. The application doesn't have to be complicated, I want it web-based running inside a container, most likely I will run it inside my NAS server; the MCP can be registered in Claude with a secure token / API key between the clients and cortex. I still want to keep the structure from Obsidian that we have, but the human can set up their own brain as they want to; the key requirement is that the CORTEX.md file is set up so it follows the networks of files for different instructions. It needs to have the same features as we spoke, which is putting stuff in the hippocampus and waiting for approval before committing the memory. The memory will sit outside the container. The human can ask what is to be reviewed or what is processing and we should list the things in SYNAPSE.md and a process to approve it. They can also turn the brain off or on, permanently, without having to uninstall the MCP. A GUI would be good to start with, including a graph view of the brain.

## Requirements

1. **Follow the routing file.** `cortex_load` returns `CORTEX.md`; `cortex_read` opens what it routes to. Wikilinks resolve like Obsidian (exact path, relative, shortest matching tail), so any folder layout works.
2. **Gate memory.** New knowledge is queued in SYNAPSE and waits for the human. Commit writes it into memory and moves the entry to ENGRAM; reject records the reason. Protected notes refuse direct writes, enforced by the server.
3. **Say what's waiting.** "What's pending / processing" lists SYNAPSE's pending and approved entries (MCP and GUI).
4. **Power switch.** Off makes every tool answer "off" for every client until switched back.
5. **Secure clients.** One API key per client, stored hashed, revocable; a separate admin password for the GUI.
6. **Brain outside the container.** Mounted at `/data/brain`; config in `/config`; logs in `/logs`.
7. **First-run setup.** Use an existing `CORTEX.md`, download the claude-brain template, start blank, or point at a custom path.
8. **GUI.** Graph view (Obsidian's colors), note reader, SYNAPSE review, keys, activity, settings.

## Decisions

- 2026-09-26: Python (3.14, pip and venv, no TOML: owner's choice) + the official MCP SDK (stateless Streamable HTTP at `/mcp`) + Starlette; vanilla JS GUI with no CDN, so an offline NAS works.
- 2026-09-26: GUI "Approve" moves an entry to SYNAPSE's `## Approved` section; the AI writes it into memory on the next load. Integrating a rule (merge, replace, settle conflicts) needs judgment, so the GUI doesn't write memory itself.
- 2026-09-26: The file formats are claude-brain's (SYNAPSE/ENGRAM lines, `Next ID`), so the brain stays readable in Obsidian and passes its own `check_brain.py`.
- 2026-09-26: Config is `config/default.yaml` + `override/<env>.yaml`, validated at startup; runtime state (API key hashes, power switch, setup paths) is data in `/data/cortex/state.json` (owner's choice).
- 2026-09-26: Secrets are Docker secrets files (`cortex-admin-pwd`, `cortex-session-key`); the environment fallback is for local development only.
- 2026-09-27: An optional read-only guest account for the GUI: a second password (`cortex-guest-pwd`) and a role in the signed session cookie, with each role's cookies signed by a key derived from its own password. The server enforces it on every write and admin-only read. Guests get no MCP access; clients keep per-client API keys.
- 2026-09-26: CI builds the image once per VERSION and pushes it to GHCR; the NAS pulls that tag (owner's choice). Superseded 2026-09-27.
- 2026-09-27: The NAS builds the image itself, from the working copy through the `synology` Docker context, like flammeau and life-dashboard; no registry, and CI only runs checks (owner's choice). Each version stays tagged on the NAS for rollback.
- 2026-09-26: The brain gained a personal layer (CEREBELLUM) and untracked in-transit files. Cortex follows it: the personal map loads with CORTEX, CEREBELLUM is protected, missing SYNAPSE/ENGRAM come from the brain's guide, headings rename with their links, and "check the brain" runs the brain's own script (the human's file; Cortex never writes `.py`).

## Out of scope for now

OAuth (claude.ai custom connectors can't send a bearer header), git auto-commit of memory changes, a `check the brain` audit tool, editing notes in the GUI. Tracked in `TODO.md`.
