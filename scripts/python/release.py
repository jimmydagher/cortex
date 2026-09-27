#!/usr/bin/env python3
"""SDSI release chain: VERSION bump, CHANGELOG promotion, TODO.md closure.

Run by scripts/git/pre-commit on every commit to the target branch — not
meant to be run by hand. The AI's only job in a release is writing bullets
into CHANGELOG.md's "## 🚧 Unreleased" section (referencing any TODO.md
item a change fixes as "TODO #<n>"); everything after the human approves
the code and commits is done here, deterministically:

  code commit       bump VERSION (PATCH, unless a bump is already staged
                    by hand) → promote Unreleased to a new version heading
                    → close referenced TODO.md items with that version →
                    sync any mirrored version files → stage it all.
                    Refused if Unreleased is empty.
  docs-only commit  no bump → fold Unreleased into the current version's
                    entry → close referenced TODO.md items with the current
                    version → stage it. Nothing to do if Unreleased is empty.

scripts/git/commit-msg then overwrites the message with the version line.

Everything is computed before anything is written: on any error the files
are left untouched and the commit is refused (exit 1).

Per-project settings live in an optional scripts/git/release.json:
  {"docs_patterns": ["docs/*", "*.md"], "version_files": ["package.json"]}
docs_patterns decide what counts as a docs-only commit (fnmatch against
each staged path); version_files are JSON files whose top-level "version"
string is kept equal to VERSION.
"""
import fnmatch
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

VERSION_FILE = "VERSION"
CHANGELOG_FILE = "CHANGELOG.md"
TODO_FILE = "TODO.md"
SETTINGS_FILE = "scripts/git/release.json"

DEFAULT_DOCS_PATTERNS = ["docs/*", "*.md"]

# The fixed rotation every already-shipped entry's marker cycles through,
# wrapping back to the first after the last.
COLORS = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫"]

SUBSECTIONS = ["Added or New Features", "Removed", "Changed", "Bug/Issues/Fixes"]
NONE_MARKER = "(none)"
UNRELEASED_HEADING = "## 🚧 Unreleased"

UNRELEASED_RE = re.compile(r"^## 🚧 Unreleased\n(.*?)(?=^## |\Z)", re.M | re.S)
CURRENT_ENTRY_RE = re.compile(r"^(## 🆕VERSION [^\n]*\n)(.*?)(?=^## |\Z)", re.M | re.S)
TODO_REF_RE = re.compile(r"TODO #(\d+)")
OPEN_TODO_RE = r"^- \[ \] #{number} (.*)$"
DONE_TODO_RE = r"^- \[x\] #{number} "
JSON_VERSION_RE = re.compile(r'("version"\s*:\s*")[^"]*(")')


class ReleaseError(Exception):
    """A reason to refuse the commit. The message is shown to the committer."""


def empty_body() -> str:
    """The body of a fresh, contentless Unreleased section.

    Output:
        str: every subsection heading followed by (none).
    """
    return "".join(f"\n### {name}\n{NONE_MARKER}\n" for name in SUBSECTIONS)


def unreleased_body(changelog: str) -> str:
    """The text between the Unreleased heading and the next '## ' heading.

    Input:
        changelog (str): CHANGELOG.md contents.
    Output:
        str: the section body.
    Raises:
        ReleaseError: the changelog has no Unreleased section.
    """
    match = UNRELEASED_RE.search(changelog)
    if match is None:
        raise ReleaseError(f"{CHANGELOG_FILE} has no '{UNRELEASED_HEADING}' section.")
    return match.group(1)


def parse_subsections(body: str) -> dict[str, list[str]]:
    """Split a version entry's body into its subsections' content lines.

    Input:
        body (str): text under a '## ' heading.
    Output:
        dict[str, list[str]]: subsection name → its lines, with (none)
        markers and surrounding blank lines removed. Every name in
        SUBSECTIONS is present, empty when the entry had nothing.
    """
    sections: dict[str, list[str]] = {name: [] for name in SUBSECTIONS}
    current = None
    for line in body.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            sections.setdefault(current, [])
        elif current is not None and line.strip() != NONE_MARKER:
            sections[current].append(line)
    for name, lines in sections.items():
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
    return sections


def render_subsections(sections: dict[str, list[str]]) -> str:
    """Inverse of parse_subsections: a body with (none) for empty ones.

    Input:
        sections (dict[str, list[str]]): subsection name → content lines.
    Output:
        str: the rendered body, ending with a blank line.
    """
    parts = []
    for name, lines in sections.items():
        content = "\n".join(lines) if lines else NONE_MARKER
        parts.append(f"\n### {name}\n{content}\n")
    return "".join(parts) + "\n"


def has_entries(body: str) -> bool:
    """True if any subsection in body holds a bullet.

    Input:
        body (str): a version entry's body.
    Output:
        bool: whether there is anything to release.
    """
    return any(
        line.startswith("- ")
        for lines in parse_subsections(body).values()
        for line in lines
    )


def next_color(changelog: str) -> str:
    """The color the entry currently holding 🆕 takes when it's replaced.

    Input:
        changelog (str): CHANGELOG.md contents.
    Output:
        str: the next color in COLORS' rotation.
    """
    colored = re.findall(r"^## (?:" + "|".join(COLORS) + r")VERSION", changelog, re.M)
    return COLORS[len(colored) % len(COLORS)]


def promote(changelog: str, version: str, today: str) -> str:
    """Turn Unreleased into the new 🆕 version entry; roll the old 🆕 color.

    Input:
        changelog (str): CHANGELOG.md contents, with a non-empty Unreleased.
        version (str): the new version, e.g. "1.2.4".
        today (str): the release date, YYYY-MM-DD.
    Output:
        str: the updated changelog, with a fresh empty Unreleased on top.
    """
    body = unreleased_body(changelog)
    color = next_color(changelog)
    changelog = re.sub(r"^## 🆕VERSION", f"## {color}VERSION", changelog, count=1, flags=re.M)
    promoted = (
        f"{UNRELEASED_HEADING}\n{empty_body()}\n"
        f"## 🆕VERSION {version} 📅 {today}\n"
        f"{render_subsections(parse_subsections(body))}"
    )
    return UNRELEASED_RE.sub(lambda _: promoted, changelog, count=1)


def fold_into_current(changelog: str) -> str:
    """Move Unreleased's bullets into the current 🆕 entry (no version bump).

    Input:
        changelog (str): CHANGELOG.md contents.
    Output:
        str: the updated changelog, or the input unchanged when no version
        has been released yet (the notes then ride with the first release).
    """
    current = CURRENT_ENTRY_RE.search(changelog)
    if current is None:
        return changelog
    pending = parse_subsections(unreleased_body(changelog))
    merged = parse_subsections(current.group(2))
    for name, lines in pending.items():
        merged.setdefault(name, []).extend(lines)
    changelog = (
        changelog[: current.start(2)]
        + render_subsections(merged)
        + changelog[current.end(2):]
    )
    return UNRELEASED_RE.sub(
        lambda _: f"{UNRELEASED_HEADING}\n{empty_body()}\n", changelog, count=1
    )


def todo_refs(body: str) -> list[int]:
    """The TODO.md item numbers an Unreleased body says it closes.

    Input:
        body (str): the Unreleased section body.
    Output:
        list[int]: distinct item numbers, ascending.
    """
    return sorted({int(number) for number in TODO_REF_RE.findall(body)})


def close_todos(todo: str, numbers: list[int], version: str, today: str) -> str:
    """Check off the given items and move them to the end of the Done section.

    Input:
        todo (str): TODO.md contents.
        numbers (list[int]): items to close.
        version (str): the version that closed them.
        today (str): the closing date, YYYY-MM-DD.
    Output:
        str: the updated TODO.md.
    Raises:
        ReleaseError: a referenced item is neither open nor already done.
    """
    closed = []
    for number in numbers:
        match = re.search(OPEN_TODO_RE.format(number=number), todo, re.M)
        if match is None:
            if re.search(DONE_TODO_RE.format(number=number), todo, re.M):
                continue  # already closed by an earlier commit
            raise ReleaseError(
                f"CHANGELOG.md references TODO #{number}, but {TODO_FILE} has no open item #{number}."
            )
        todo = todo[: match.start()] + todo[match.end() + 1:]
        closed.append(f"- [x] #{number} {match.group(1)} — completed {today} · VERSION {version}")
    if not closed:
        return todo
    todo = todo.rstrip("\n") + "\n"
    if not re.search(r"^## Done\s*$", todo, re.M):
        todo += "\n---\n\n## Done\n"
    return todo + "\n".join(closed) + "\n"


def bump_patch(version: str) -> str:
    """The next PATCH version.

    Input:
        version (str): a MAJOR.MINOR.PATCH version.
    Output:
        str: the same version with PATCH incremented.
    Raises:
        ReleaseError: the version isn't MAJOR.MINOR.PATCH.
    """
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise ReleaseError(f"{VERSION_FILE} holds '{version}', not MAJOR.MINOR.PATCH.")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def set_json_version(text: str, path: str, version: str) -> str:
    """Rewrite the first "version" string in a JSON file's text.

    Input:
        text (str): the file's contents.
        path (str): the file's path, for the error message.
        version (str): the version to write.
    Output:
        str: the updated text.
    Raises:
        ReleaseError: the file has no "version" key.
    """
    updated, count = JSON_VERSION_RE.subn(rf"\g<1>{version}\g<2>", text, count=1)
    if count == 0:
        raise ReleaseError(f'{path} is listed in version_files but has no "version" key.')
    return updated


def is_docs_only(staged: list[str], patterns: list[str]) -> bool:
    """True if every staged path matches a docs pattern.

    Input:
        staged (list[str]): staged paths, relative to the repo root.
        patterns (list[str]): fnmatch patterns counting as documentation.
    Output:
        bool: whether the commit is docs-only.
    """
    return all(any(fnmatch.fnmatch(path, pattern) for pattern in patterns) for path in staged)


def git(*args: str) -> str:
    """Run a git command in the current directory and return its stdout."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run(root: Path, today: str) -> None:
    """Compute and write the release for whatever is staged under root.

    Input:
        root (Path): the repository root (the current directory).
        today (str): the release date, YYYY-MM-DD.
    Raises:
        ReleaseError: the commit must be refused; nothing was written.
    """
    staged = [line for line in git("diff", "--cached", "--name-only").splitlines() if line]
    if not staged:
        return

    settings_path = root / SETTINGS_FILE
    settings = json.loads(read(settings_path)) if settings_path.exists() else {}
    docs_patterns = settings.get("docs_patterns", DEFAULT_DOCS_PATTERNS)
    version_files = settings.get("version_files", [])

    changelog = read(root / CHANGELOG_FILE)
    pending = unreleased_body(changelog)
    todo_path = root / TODO_FILE
    refs = todo_refs(pending)
    if refs and not todo_path.exists():
        raise ReleaseError(f"CHANGELOG.md references TODO items, but there is no {TODO_FILE}.")

    current_version = read(root / VERSION_FILE).strip()
    writes: dict[Path, str] = {}

    if is_docs_only(staged, docs_patterns):
        if not has_entries(pending):
            return
        version = current_version
        writes[root / CHANGELOG_FILE] = fold_into_current(changelog)
    else:
        if not has_entries(pending):
            raise ReleaseError(
                f"'{UNRELEASED_HEADING}' in {CHANGELOG_FILE} has no entries. Add a bullet "
                "describing this change under Added/Removed/Changed/Bug fixes, then commit "
                "(or commit --no-verify if this genuinely isn't a release)."
            )
        version = current_version if VERSION_FILE in staged else bump_patch(current_version)
        writes[root / VERSION_FILE] = version + "\n"
        writes[root / CHANGELOG_FILE] = promote(changelog, version, today)
        for path in version_files:
            writes[root / path] = set_json_version(read(root / path), path, version)

    if refs:
        writes[todo_path] = close_todos(read(todo_path), refs, version, today)

    for path, text in writes.items():
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    git("add", "--", *(str(path.relative_to(root)) for path in writes))


def main() -> int:
    """Entry point for scripts/git/pre-commit.

    Output:
        int: 0 when the commit may proceed, 1 when it's refused.
    """
    try:
        run(Path.cwd(), date.today().isoformat())
    except ReleaseError as error:
        print(f"\n  COMMIT REFUSED — {error}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
