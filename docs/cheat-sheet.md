# Cheat sheet

The commands to reach for when running Cortex or when something's wrong.

## Commands

| Command | Does | Where |
| --- | --- | --- |
| `python -m cortex serve` | Runs the GUI, API and `/mcp` until stopped | the container's default command |
| `python -m cortex validate-config` | Checks config, secrets and folders; changes nothing | `.\scripts\ps1\deploy-nas.ps1 -PreflightOnly` for the NAS; `.\scripts\ps1\run-local.ps1 -CheckOnly` on your PC |

## Scripts (dev machine, PowerShell 7, repo root)

| Script | Does |
| --- | --- |
| `.\scripts\ps1\deploy-nas.ps1` | Deploys `VERSION` to the NAS through the `synology` context: folders, config upload, missing secrets, pre-flight, start, health check |
| `… -Version 0.1.0` | Deploys (or rolls back to) another published version |
| `… -PreflightOnly` | Everything up to `validate-config`; starts nothing |
| `… -ResetAdminPassword` / `-RotateSessionKey` | Replaces that secret on the NAS (everyone is signed out) |
| `… -SetGuestPassword` / `-RemoveGuest` | Creates or changes the read-only guest login (guests are signed out), or deletes it |
| `.\scripts\ps1\run-local.ps1` | Runs Cortex on your PC from `.venv` at http://localhost:8765 |
| `… -BrainPath S:\Backup\Markdown\claude-brain` | Serves that brain folder (linked as `data\brain`, not copied) |
| `… -CheckOnly` | Sets up `.venv` and secrets and runs `validate-config`; doesn't serve |

## Exit codes

| Code | Outcome | Means |
| --- | --- | --- |
| 0 | Succeeded | `validate-config` found nothing wrong |
| 1 | Failed | the work itself failed |
| 2 | Invalid input | no command, or an unknown one |
| 3 | Startup failure | config, secrets, folders or the port: read the listed problems; don't just restart |
| 130 | Interrupted | stopped by the platform (a normal `docker stop`) |

## Logs

- **Where:** `<CORTEX_LOGS_PATH>/cortex.log` on the NAS (rotated at `logging.max_bytes`, `logging.backups` old files kept), and the same lines in `docker --context synology compose --env-file .env.nas logs -f cortex` from your PC.
- **Format:** `2026-09-26T15:50:43-05:00 INFO    req=4f2a91c07b3e [laptop] commit: HX0002 → MEMORY/WRITING.md`. `[who]` lines are activity (also in the GUI's Activity tab), `(access)` lines are one per request, `(uvicorn.error)`-style lines come from libraries.
- **Trace one request:** every error response carries a `request_id`; `grep 'req=<id>' cortex.log` shows everything that request did.
- **More detail:** set `logging.level: DEBUG` in the override and recreate the container.

```bash
# NAS shell
tail -f /volume1/docker/cortex/logs/cortex.log
grep 'req=4f2a91c07b3e' /volume1/docker/cortex/logs/cortex.log
```

## Health

```bash
# any shell that can reach the NAS
curl http://<nas>:8765/healthz        # "ok"
```

## Access

| Task | How |
| --- | --- |
| Add a client | GUI › Connect › Create key; paste the shown command into the client |
| Cut a client off | GUI › Connect › Revoke (immediate) |
| Turn the brain off or on for everyone | the header's Brain on/off button, or tell Claude "turn off the brain" |
| Change or recover the admin password | `.\scripts\ps1\deploy-nas.ps1 -ResetAdminPassword` (everyone is signed out) |
| Rotate the session key | `.\scripts\ps1\deploy-nas.ps1 -RotateSessionKey` (everyone is signed out) |
| Give someone read-only GUI access | `.\scripts\ps1\deploy-nas.ps1 -SetGuestPassword`, then share that password; `-RemoveGuest` takes it away |
| Point Cortex at another brain | GUI › Settings › Run setup again |

## Where things live

| Path in the container | Holds |
| --- | --- |
| `/data/brain` | The vault (Markdown) |
| `/data/cortex/state.json` | Power switch, setup paths, API key hashes; don't edit while Cortex runs |
| `/config/nas.yaml` | This environment's config override |
| `/logs/cortex.log` | The log |
| `/run/secrets/*` | The two secrets |

## Development

```bash
# dev machine, repo root, inside .venv
python scripts/python/check.py                 # lint, type-check, tests (what CI runs)
python -m pytest tests -m integration          # downloads the real template from GitHub
python -m playwright install chromium          # once, for the browser tests
python -m pytest tests -m e2e                  # sign-in, graph, approve, keys in a real browser
python scripts/python/lock.py                  # after editing requirements*.in (needs Docker)
python scripts/python/make_favicon.py          # after changing the icon's shapes
```
