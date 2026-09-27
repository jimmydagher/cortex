# Ship a change

How a change goes from your machine to the NAS. One image per version, built once by CI; the NAS pulls it by tag and never rebuilds.

## 1. Make the change

```bash
# dev machine, repo root, inside .venv
git switch -c my-change
# edit code; add a bullet under CHANGELOG.md › 🚧 Unreleased (cite "TODO #n" when it closes an item)
python scripts/python/check.py
```

Open a pull request. CI runs lint, type-check, tests, the vulnerability scan, the changelog check (a code change needs an Unreleased bullet) and a Docker build.

## 2. Release it

The release hooks run on commits to `main` on your machine, so land the reviewed branch with a local squash commit:

```bash
# dev machine, repo root
git switch main && git pull
git merge --squash my-change
git commit            # the hooks bump VERSION, promote Unreleased, close TODO items, set the message
git push
```

The commit message becomes `VERSION x.y.z` (or `VERSION x.y.z-updated` for a docs-only change, which doesn't bump). For a MINOR or MAJOR release, write the new number into `VERSION` and `git add VERSION` before committing; the hook keeps a staged bump.

The GitHub merge button skips the hooks: CI's `--main` check then fails the push, because the commit isn't a version line.

## 3. CI publishes the image

On a push to `main`, CI builds `ghcr.io/jimmydagher/cortex:<VERSION>` for amd64 and arm64 and pushes it, unless that tag already exists (a docs-only release reuses the current image).

## 4. Update the NAS

Read the version's `CHANGELOG.md` entry first: a MINOR or MAJOR release may need a new config key or secret.

```bash
# NAS shell, in the folder with docker-compose.yml
sed -i 's/^CORTEX_VERSION=.*/CORTEX_VERSION=0.1.1/' .env
docker compose pull
docker compose run --rm cortex validate-config
docker compose up -d
docker compose logs --tail 20 cortex
```

## 5. Roll back

Set `CORTEX_VERSION` back to the previous release and `docker compose up -d`. State (`/data/cortex/state.json`) and the brain are untouched by an image change.
