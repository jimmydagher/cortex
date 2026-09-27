# Set up Cortex on a NAS

From nothing to a running Cortex that Claude can reach. Paths use a Synology-style `/volume1/docker/cortex`; use any folder you like and keep `.env` in step.

## 1. Before you start

- Docker with the compose plugin on the NAS.
- The image published for the version you want: CI pushes `ghcr.io/jimmydagher/cortex:<VERSION>` when a release lands on `main` (see [deployment.md](deployment.md)). If the GitHub package is private, log the NAS in once with a token that has `read:packages`:

  ```bash
  # NAS shell
  echo "<token>" | docker login ghcr.io -u jimmydagher --password-stdin
  ```

- The user and group that should own Cortex's files. `id <user>` shows the numbers (Synology users are often `1026:100`).

## 2. Folders

```bash
# NAS shell
mkdir -p /volume1/docker/cortex/{data/brain,config,logs,secrets}
chown -R 1026:100 /volume1/docker/cortex
```

Put your vault in `data/brain`, or leave it empty for the setup wizard. If the vault already lives somewhere else (for example synced by Obsidian), keep it there and uncomment the extra `/data/brain` volume line in `docker-compose.yml`.

## 3. Secrets

Two files, one value each, readable only by the Cortex user. Docker mounts them at `/run/secrets/<name>`.

```bash
# NAS shell
cd /volume1/docker/cortex/secrets
printf '%s' 'a long admin password' > cortex-admin-pwd
openssl rand -base64 48 | tr -d '\n' > cortex-session-key
chmod 600 cortex-admin-pwd cortex-session-key
chown 1026:100 cortex-admin-pwd cortex-session-key
```

`cortex-admin-pwd` signs you into the GUI. `cortex-session-key` signs GUI session cookies; changing either signs everyone out.

## 4. Config override

Copy [config/override/nas.yaml](../config/override/nas.yaml) from the repo to `/volume1/docker/cortex/config/nas.yaml` and fill it in:

```yaml
server:
  allowed_hosts: ["nas.local", "192.168.1.20"]   # every name and address you'll open Cortex with
  secure_cookies: false                          # true behind an HTTPS reverse proxy
  public_url: ""                                 # e.g. https://cortex.example.com behind a proxy
```

Anything else in [config/default.yaml](../config/default.yaml) can be overridden here too; state only what differs.

## 5. Compose

Copy `docker-compose.yml` and `.env.example` into one folder on the NAS (for example `/volume1/docker/cortex`), rename `.env.example` to `.env`, and fill in `CORTEX_VERSION`, the four paths, `PUID`/`PGID`, `CORTEX_PORT` and `TZ`.

## 6. Pre-flight, then start

```bash
# NAS shell, in the folder with docker-compose.yml
docker compose pull
docker compose run --rm cortex validate-config   # checks config, secrets and folders; changes nothing
docker compose up -d
docker compose logs -f cortex                    # "[server] start" lines, then access lines
```

`validate-config` prints `OK configuration, secrets and folders are ready` and exits 0, or lists every problem and exits 3.

## 7. First run

Open `http://<nas>:8765`, sign in with the admin password, and pick a setup option: use an existing `CORTEX.md`, download the claude-brain template, start blank, or type a path. Then open **Connect**, create a key per client, and paste the command it shows into each client (README › Connect Claude).

## 8. HTTPS (recommended beyond your LAN)

API keys travel in a request header, so put Cortex behind the NAS's reverse proxy with TLS:

1. Proxy `https://cortex.example.com` → `http://localhost:8765`.
2. In `nas.yaml`: add the proxy host name to `allowed_hosts`, set `secure_cookies: true` and `public_url: https://cortex.example.com`, and set `forwarded_allow_ips` to the address the proxy connects from (the Docker bridge gateway, often `172.17.0.1`).
3. Recreate the container:

   ```bash
   # NAS shell
   docker compose up -d --force-recreate
   ```
