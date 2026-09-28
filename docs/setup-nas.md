# Set up Cortex on a NAS

From nothing to a running Cortex that Claude can reach, driven entirely from your PC. The process mirrors flammeau's and life-dashboard's: the `synology` Docker context runs everything on the NAS over SSH, so nothing is copied up by hand, and the NAS builds the image from your working copy. There's no registry and no CI step.

## 1. Before you start

- The `synology` Docker context and its SSH key, the same one flammeau and life-dashboard use (one-time setup in `life-dashboard/NAS.md`). Check it:

  ```powershell
  # dev machine, PowerShell
  docker context ls          # a "synology" row pointing at ssh://<you>@<nas>
  ```

- PowerShell 7 (`pwsh`), which the scripts need.

## 2. `.env.nas`, the one-time local file

```powershell
# dev machine, PowerShell, repo root
Copy-Item .env.nas.example .env.nas
```

Check its values: the four NAS folders (default `/volume1/docker/cortex/...`), `PUID`/`PGID` (the user Cortex runs as, default `1000:1000`), `CORTEX_PORT` and `TZ`. `.env.nas` is gitignored and never leaves your PC: Compose reads it locally. It holds no secrets.

To serve a vault that already lives elsewhere on the NAS (for example one Obsidian syncs), uncomment the extra `/data/brain` volume line in `docker-compose.yml` and point it at that folder; it must be writable by `PUID`. Otherwise the brain is `<CORTEX_DATA_PATH>/brain`, and the setup wizard fills it.

## 3. Deploy

```powershell
# dev machine, PowerShell, repo root
.\scripts\ps1\deploy-nas.ps1
```

Every run does the same safe steps, in order:

1. **Builds** the image `cortex:<VERSION>` on the NAS from your working copy (a few minutes on the NAS's hardware; `-Version` skips the build and runs an image an earlier deploy built, for rollback).
2. **Creates the NAS folders** from `.env.nas` and gives Cortex's own (state, config, logs, secrets) to `PUID:PGID`. An existing brain folder's ownership is left alone.
3. **Uploads the config override.** The first run creates `config/override/nas.local.yaml` (gitignored) from the `nas.yaml` template, with the NAS's address as `server.allowed_hosts`. Edit it later for more host names or HTTPS; every deploy uploads it as the NAS's `/config/nas.yaml`.
4. **Writes missing secrets** on the NAS. The first run asks for the GUI admin password (twice) and generates the session key. With `-SetGuestPassword` it also asks for a guest password (see [Guest account](#guest-account-optional)). Values go to the NAS over the SSH-tunneled Docker connection and are never saved on your PC. A run that changes a secret recreates the container so Cortex picks it up.
5. **Pre-flight:** runs `validate-config` with the real container wiring and stops on any problem. `-PreflightOnly` stops here without starting anything.
6. **Starts Cortex** and waits until `http://<nas>:<CORTEX_PORT>/healthz` answers.

## 4. First run

Open `http://<nas>:8765`, sign in with the admin password, and pick a setup option: use an existing `CORTEX.md`, download the claude-brain template, start blank, or type a path. Then open **Connect**, create a key per client, and paste the command it shows into each client (README › Connect Claude).

## Guest account (optional)

A guest signs in to the GUI with their own password and can browse the graph, read notes, and see SYNAPSE, the trail and activity. They can't approve or reject entries, switch the brain off, see or create API keys, change settings or run setup. The server refuses these actions, and the GUI hides them. There's no guest account until you create one:

```powershell
# dev machine, PowerShell, repo root
.\scripts\ps1\deploy-nas.ps1 -SetGuestPassword   # create it, or change its password (signs guests out)
.\scripts\ps1\deploy-nas.ps1 -RemoveGuest        # delete it
```

The guest password is the `cortex-guest-pwd` secret. It must be at least 12 characters and differ from the admin password; the pre-flight stops the deploy if it doesn't. The guest account covers the GUI only: MCP clients always use API keys.

## 5. HTTPS (recommended beyond your LAN)

API keys travel in a request header, so put Cortex behind the NAS's reverse proxy with TLS:

1. Proxy `https://cortex.example.com` → `http://localhost:8765`.
2. In `config/override/nas.local.yaml`: add the proxy host name to `allowed_hosts`, set `secure_cookies: true` and `public_url: https://cortex.example.com`, and set `forwarded_allow_ips` to the address the proxy connects from (the Docker bridge gateway, often `172.17.0.1`).
3. Redeploy: `.\scripts\ps1\deploy-nas.ps1`.

## By hand

The script is these steps plus the folder, config and secret setup. From the repo root on your PC:

```powershell
# dev machine, PowerShell, repo root
$env:CORTEX_VERSION = Get-Content VERSION
docker --context synology compose --env-file .env.nas build
docker --context synology compose --env-file .env.nas run --rm --no-deps cortex validate-config
docker --context synology compose --env-file .env.nas up -d
docker --context synology compose --env-file .env.nas logs -f cortex
```
