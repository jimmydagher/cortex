# Ship a change

How a change goes from your machine to the NAS. Like flammeau and life-dashboard, the NAS builds the image itself from your working copy, through the `synology` Docker context; there's no registry and nothing to wait for on GitHub.

## 1. Make the change

```bash
# dev machine, repo root, inside .venv
git switch -c my-change
# edit code; add a bullet under CHANGELOG.md › 🚧 Unreleased (cite "TODO #n" when it closes an item)
python scripts/python/check.py
```

Pull requests and pushes to `main` run CI: lint, type-check, tests, the vulnerability scan and the changelog check (a code change needs an Unreleased bullet). CI doesn't build or publish anything, and deploying doesn't wait for it.

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

## 3. Update the NAS

Read the version's `CHANGELOG.md` entry first: a MINOR or MAJOR release may need a new config key or secret. Then, right after the release commit:

```powershell
# dev machine, PowerShell, repo root, on main after the release commit
.\scripts\ps1\deploy-nas.ps1
```

It builds `cortex:<VERSION>` on the NAS from your working copy, then uploads the config, runs the pre-flight, starts it and checks its health ([setup-nas.md](setup-nas.md) › Deploy lists every step). Deploy from a clean `main`: the build includes whatever is in your working copy, and the script warns when there are uncommitted changes.

## 4. Roll back

Every deploy leaves its image on the NAS, tagged with its version, so going back doesn't rebuild:

```powershell
# dev machine, PowerShell, repo root
.\scripts\ps1\deploy-nas.ps1 -Version 0.1.0
```

If that version was never built on the NAS, the script lists the ones that were; to build an older version, check out its commit and deploy without `-Version`. State (`/data/cortex/state.json`) and the brain are untouched by an image change.
