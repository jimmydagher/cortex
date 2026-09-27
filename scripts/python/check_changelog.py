#!/usr/bin/env python3
"""CI's copy of the release chain's changelog check, where `git commit --no-verify` can't reach.

    python scripts/python/check_changelog.py --pull-request origin/main
        A code change must add a bullet to CHANGELOG.md's "🚧 Unreleased" section.
    python scripts/python/check_changelog.py --main
        The commit on main must be a release: message "VERSION x.y.z[-updated]", VERSION
        matching the current 🆕 entry, and Unreleased promoted (empty).

Reuses release.py's own parsing so the two checks can't disagree. Exit 0 pass, 1 fail.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402 - the vendored release chain, next to this file

ROOT = Path(__file__).resolve().parents[2]
VERSION_LINE = re.compile(r"^VERSION (\d+\.\d+\.\d+)(-updated)?$")


def git(*arguments: str) -> str:
    """Run git in the repo and return its output.

    Args:
        arguments: git's arguments.

    Returns:
        stdout, stripped.
    """
    return subprocess.run(["git", *arguments], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def docs_patterns() -> list[str]:
    """The release chain's docs-only patterns (scripts/git/release.json, else its default)."""
    settings = ROOT / release.SETTINGS_FILE
    patterns: list[str] = json.loads(settings.read_text(encoding="utf-8")).get("docs_patterns", release.DEFAULT_DOCS_PATTERNS) \
        if settings.exists() else release.DEFAULT_DOCS_PATTERNS
    return patterns


def check_pull_request(base: str) -> list[str]:
    """A pull request that changes code must describe it in Unreleased.

    Args:
        base: the ref the pull request merges into, e.g. origin/main.

    Returns:
        Problems; empty when the check passes.
    """
    changed = [path for path in git("diff", "--name-only", f"{base}...HEAD").splitlines() if path]
    if not changed or release.is_docs_only(changed, docs_patterns()):
        return []
    changelog = (ROOT / release.CHANGELOG_FILE).read_text(encoding="utf-8")
    if release.has_entries(release.unreleased_body(changelog)):
        return []
    return [f"code changed ({len(changed)} files) but CHANGELOG.md's Unreleased section has no entries"]


def check_main() -> list[str]:
    """The commit on main must have gone through the release chain.

    Returns:
        Problems; empty when the check passes.
    """
    if len(git("rev-list", "--parents", "-n", "1", "HEAD").split()) > 2:
        return []  # a merge: its commits were released on their own branch
    message = git("log", "-1", "--format=%s")
    match = VERSION_LINE.match(message)
    if not match:
        return [f"commit message {message!r} isn't a version line: was the release chain bypassed (--no-verify)?"]
    version = (ROOT / release.VERSION_FILE).read_text(encoding="utf-8").strip()
    changelog = (ROOT / release.CHANGELOG_FILE).read_text(encoding="utf-8")
    problems = []
    if match.group(1) != version:
        problems.append(f"commit says VERSION {match.group(1)} but the VERSION file says {version}")
    if f"## 🆕VERSION {version} " not in changelog:
        problems.append(f"CHANGELOG.md has no current (🆕) entry for {version}")
    if release.has_entries(release.unreleased_body(changelog)):
        problems.append("CHANGELOG.md's Unreleased section still has entries: they should have been promoted")
    return problems


def main() -> int:
    """Run the check chosen on the command line.

    Returns:
        0 when it passes, 1 when it fails (problems printed to stderr).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pull-request", metavar="BASE_REF")
    mode.add_argument("--main", action="store_true")
    arguments = parser.parse_args()
    problems = check_main() if arguments.main else check_pull_request(arguments.pull_request)
    for problem in problems:
        print(f"CHANGELOG CHECK FAILED — {problem}", file=sys.stderr)
    if not problems:
        print("changelog check passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
