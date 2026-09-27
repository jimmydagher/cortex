"""The brain on disk: notes, tags, wikilinks, and path-safe reads and writes.

Every path the rest of the app handles is vault-relative POSIX (`MEMORY/CODING.md`).
Link resolution follows Obsidian: exact path, then relative to the linking note,
then the shortest path whose tail matches, so any folder layout works.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .config import VaultConfig
from .errors import InvalidInputError, NotFoundError

WIKILINK = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")
MARKDOWN_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
INLINE_TAG = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]*)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
ATTACHMENT = re.compile(r"\.(?!md$)[A-Za-z0-9]{1,5}$", re.IGNORECASE)
FENCE_MARKERS = ("```", "~~~")
NOTE_SUFFIX = ".md"
SKIPPED_FOLDERS = {"node_modules", "__pycache__"}
OBSIDIAN_GRAPH = Path(".obsidian") / "graph.json"
SEARCH_LINE_CHARS = 240


@dataclass
class Note:
    """One indexed note: where it is, when it changed, its tags and outgoing link targets."""

    path: str
    modified: float
    size: int
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """The note's title: its file name without `.md`."""
        return PurePosixPath(self.path).stem


@dataclass
class Edit:
    """An exact-match replacement inside a note."""

    old_text: str
    new_text: str


def normalize_path(text: str) -> str:
    """Turn user input into a vault-relative POSIX path fragment.

    Args:
        text: a path as typed or sent by a client (`\\MEMORY\\CODING`, `/MEMORY/CODING.md`).

    Returns:
        The same path with forward slashes and no leading slash or surrounding spaces.
    """
    return text.strip().replace("\\", "/").lstrip("/")


def split_frontmatter(text: str) -> tuple[list[str], str]:
    """Separate YAML frontmatter from the note body.

    Args:
        text: the whole note.

    Returns:
        (frontmatter lines, body); the lines are empty when the note has none.
    """
    if text.startswith("---\n") or text.startswith("---\r\n"):
        lines = text.splitlines(keepends=True)
        for index in range(1, len(lines)):
            if lines[index].rstrip("\r\n") == "---":
                return [line.rstrip("\r\n") for line in lines[1:index]], "".join(lines[index + 1 :])
    return [], text


def frontmatter_tags(front: list[str]) -> list[str]:
    """Read `tags:` as a YAML block list, inline list or scalar.

    A full YAML parse isn't needed for this one key, and notes often carry YAML that
    other tools wrote loosely; this reads the three shapes Obsidian itself accepts.

    Args:
        front: frontmatter lines.

    Returns:
        The tags, without `#` or quotes.
    """
    tags: list[str] = []
    for index, line in enumerate(front):
        key, _, value = line.partition(":")
        if key.strip().lower() not in ("tags", "tag"):
            continue
        value = value.strip()
        if value.startswith("["):
            tags += [tag.strip(" '\"#") for tag in value.strip("[]").split(",")]
        elif value:
            tags += [tag.strip(" '\"#") for tag in re.split(r"[,\s]+", value)]
        else:
            for item in front[index + 1 :]:
                if not item.lstrip().startswith("-"):
                    break
                tags.append(item.lstrip()[1:].strip(" '\"#"))
    return [tag for tag in tags if tag]


def scan_lines(body: str) -> Iterator[tuple[str, bool]]:
    """Walk a note body line by line, saying which lines sit inside fenced code.

    The one fence-aware scanner: link parsing and the GUI's renderer both use it.

    Args:
        body: note text without frontmatter.

    Yields:
        (line, in_code): fence lines themselves count as code.
    """
    fence: str | None = None
    for line in body.splitlines():
        marker = line.lstrip()[:3]
        if marker in FENCE_MARKERS:
            fence = marker if fence is None else (None if marker == fence else fence)
            yield line, True
            continue
        yield line, fence is not None


def link_target(raw: str) -> str:
    """The note a wikilink points at.

    Args:
        raw: the text inside `[[...]]`, e.g. `MEMORY/CODING#Rules|CODING` or the
            table-escaped `MEMORY/CODING\\|CODING`.

    Returns:
        The target without alias or heading, e.g. `MEMORY/CODING`.
    """
    return raw.split("|", 1)[0].rstrip("\\").split("#", 1)[0].strip()


def parse_note(text: str) -> tuple[list[str], list[str]]:
    """Find a note's tags and link targets, ignoring anything inside code.

    Args:
        text: the whole note.

    Returns:
        (tags, link targets), each deduplicated in order of appearance.
    """
    front, body = split_frontmatter(text)
    tags = frontmatter_tags(front)
    links: list[str] = []
    for line, in_code in scan_lines(body):
        if in_code:
            continue
        line = INLINE_CODE.sub("", line)
        links += [target for raw in WIKILINK.findall(line) if (target := link_target(raw))]
        links += [target for target in MARKDOWN_LINK.findall(line) if "://" not in target]
        if not line.lstrip().startswith("#"):
            tags += INLINE_TAG.findall(line)
    return list(dict.fromkeys(tags)), list(dict.fromkeys(links))


def section(text: str, heading: str) -> str | None:
    """One heading's section: from the heading to the next heading of equal or higher level.

    Args:
        text: the whole note.
        heading: the heading text, case-insensitive.

    Returns:
        The section text, or None when the note has no such heading.
    """
    wanted = heading.strip().lower()
    lines = text.splitlines()
    start = level = None
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if not match:
            continue
        if start is None and match.group(2).strip().lower() == wanted:
            start, level = index, len(match.group(1))
        elif start is not None and level is not None and len(match.group(1)) <= level:
            return "\n".join(lines[start:index]).rstrip() + "\n"
    return "\n".join(lines[start:]).rstrip() + "\n" if start is not None else None


def apply_edits(text: str, edits: list[Edit], label: str) -> str:
    """Apply exact-match replacements in order; each old_text must occur exactly once.

    Args:
        text: the note's current text.
        edits: the replacements.
        label: the note's path, for error messages.

    Returns:
        The edited text.

    Raises:
        InvalidInputError: an old_text is empty, missing, or ambiguous.
    """
    for number, edit in enumerate(edits, 1):
        if not edit.old_text:
            raise InvalidInputError(f"{label}: edit {number} has an empty old_text; use content to write a whole note")
        count = text.count(edit.old_text)
        if count != 1:
            where = "not found" if count == 0 else f"found {count} times; include more surrounding text"
            raise InvalidInputError(f"{label}: edit {number} old_text {where}")
        text = text.replace(edit.old_text, edit.new_text, 1)
    return text


class Vault:
    """Index and access for the Markdown notes under one root folder."""

    def __init__(self, root: Path, config: VaultConfig, warn: Callable[[str], None]) -> None:
        self.root = Path(root)
        self.home = ""  # folder holding CORTEX.md, where a nested vault keeps its .obsidian
        self._config = config
        self._warn = warn
        self._lock = threading.RLock()
        self._notes: dict[str, Note] = {}
        self._by_path: dict[str, str] = {}
        self._by_name: dict[str, list[str]] = {}
        self._scanned = 0.0
        self._scanned_once = False

    # ---------- index ----------

    def notes(self) -> dict[str, Note]:
        """Every note, rescanned when the index is older than vault.scan_ttl_seconds.

        Returns:
            Notes by vault-relative path.
        """
        with self._lock:
            if not self._scanned_once or time.monotonic() - self._scanned > self._config.scan_ttl_seconds:
                self._scan()
            return self._notes

    def invalidate(self) -> None:
        """Force a rescan on the next access (after Cortex writes a file)."""
        with self._lock:
            self._scanned_once = False

    def _scan(self) -> None:
        found: dict[str, Note] = {}
        if self.root.is_dir():
            for folder, subfolders, files in os.walk(self.root):
                subfolders[:] = sorted(name for name in subfolders if not name.startswith(".") and name not in SKIPPED_FOLDERS)
                for file_name in files:
                    if file_name.lower().endswith(NOTE_SUFFIX) and not file_name.startswith("."):
                        self._index(Path(folder) / file_name, found)
        self._notes = found
        self._by_path = {path[: -len(NOTE_SUFFIX)].lower(): path for path in found}
        self._by_name = {}
        for path in found:
            self._by_name.setdefault(PurePosixPath(path).stem.lower(), []).append(path)
        self._scanned = time.monotonic()
        self._scanned_once = True

    def _index(self, full: Path, found: dict[str, Note]) -> None:
        relative = full.relative_to(self.root).as_posix()
        try:
            stat = full.stat()
        except OSError as error:
            self._warn(f"skipped {relative}: {error.strerror}")
            return
        cached = self._notes.get(relative)
        if cached and cached.modified == stat.st_mtime and cached.size == stat.st_size:
            found[relative] = cached
            return
        note = Note(relative, stat.st_mtime, stat.st_size)
        if stat.st_size > self._config.max_note_bytes:
            self._warn(f"{relative} is over vault.max_note_bytes; indexed without tags or links")
        else:
            try:
                note.tags, note.links = parse_note(full.read_text(encoding="utf-8", errors="replace"))
            except OSError as error:
                self._warn(f"{relative} couldn't be read ({error.strerror}); indexed without tags or links")
        found[relative] = note

    # ---------- resolution ----------

    def resolve(self, target: str, source: str | None = None) -> str | None:
        """Resolve a wikilink target or path to a note the way Obsidian does.

        Args:
            target: `MEMORY/CODING`, `CODING`, `Acme/ACME.md`...
            source: the linking note, for relative links.

        Returns:
            The note's vault path, or None when nothing matches.
        """
        self.notes()
        wanted = normalize_path(target)
        if wanted.lower().endswith(NOTE_SUFFIX):
            wanted = wanted[: -len(NOTE_SUFFIX)]
        key = wanted.lower()
        if not key or ATTACHMENT.search(key):
            return None
        with self._lock:
            if key in self._by_path:
                return self._by_path[key]
            if source:
                relative = posixpath.normpath(posixpath.join(posixpath.dirname(source), wanted)).lower()
                if relative in self._by_path:
                    return self._by_path[relative]
            candidates = [
                path
                for path in self._by_name.get(PurePosixPath(key).name, [])
                if path[: -len(NOTE_SUFFIX)].lower() == key or path[: -len(NOTE_SUFFIX)].lower().endswith("/" + key)
            ]
        if not candidates:
            return None
        return min(candidates, key=lambda path: (path.count("/"), path))

    def is_attachment(self, target: str) -> bool:
        """Whether a link target is a non-note file (an image, a PDF...).

        Args:
            target: a link target.

        Returns:
            True for attachments, which are neither graph nodes nor dead links.
        """
        return bool(ATTACHMENT.search(target.strip()))

    # ---------- paths ----------

    def safe_path(self, relative: str) -> tuple[str, Path]:
        """Normalize a vault-relative note path and refuse anything outside the vault.

        Args:
            relative: a path from a client; `.md` is added when missing.

        Returns:
            (vault path, absolute path).

        Raises:
            InvalidInputError: empty, hidden-folder, `..`, drive-letter or escaping paths.
        """
        relative = normalize_path(relative)
        if not relative:
            raise InvalidInputError("empty path")
        if not relative.lower().endswith(NOTE_SUFFIX):
            relative += NOTE_SUFFIX
        parts = PurePosixPath(relative).parts
        if any(part in ("..", ".") or part.startswith(".") for part in parts) or ":" in parts[0]:
            raise InvalidInputError(f"refused path: {relative}")
        full = (self.root / relative).resolve()
        if not full.is_relative_to(self.root.resolve()):
            raise InvalidInputError(f"refused path: {relative}")
        return PurePosixPath(*parts).as_posix(), full

    # ---------- read / write ----------

    def read(self, relative: str) -> str:
        """Read a note as text with LF line endings.

        Args:
            relative: the note's vault path.

        Returns:
            The note's text.

        Raises:
            NotFoundError: no such note.
            InvalidInputError: the path is refused or the note is over vault.max_note_bytes.
        """
        relative, full = self.safe_path(relative)
        if not full.is_file():
            raise NotFoundError(f"no note at {relative}")
        if full.stat().st_size > self._config.max_note_bytes:
            raise InvalidInputError(f"{relative} is larger than {self._config.max_note_bytes // 1000} KB")
        return full.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")

    def exists(self, relative: str) -> bool:
        """Whether a note exists at a (safe) vault path.

        Args:
            relative: the note's vault path.

        Returns:
            True when the file exists; False for missing or refused paths.
        """
        try:
            return self.safe_path(relative)[1].is_file()
        except InvalidInputError:
            return False

    def prepare(self, relative: str, content: str | None = None, edits: list[Edit] | None = None) -> tuple[str, str]:
        """Validate a change without writing it.

        Args:
            relative: the note's vault path.
            content: the whole new text, or
            edits: exact-match replacements (exactly one of the two).

        Returns:
            (vault path, new text).

        Raises:
            InvalidInputError: both or neither given, or an edit doesn't match exactly once.
            NotFoundError: editing a note that doesn't exist.
        """
        relative, _ = self.safe_path(relative)
        if (content is None) == (not edits):
            raise InvalidInputError(f"{relative}: pass either content or edits")
        if content is not None:
            return relative, content.replace("\r\n", "\n")
        return relative, apply_edits(self.read(relative), edits or [], relative)

    def write_text(self, relative: str, text: str) -> str:
        """Write a note atomically, keeping the file's existing line endings.

        Args:
            relative: the note's vault path.
            text: the new text, with LF line endings.

        Returns:
            The normalized vault path written.
        """
        relative, full = self.safe_path(relative)
        with self._lock:
            crlf = full.is_file() and b"\r\n" in full.read_bytes()[:65536]
            data = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
            full.parent.mkdir(parents=True, exist_ok=True)
            temporary = full.with_name(f".{full.name}.cortex-tmp")
            temporary.write_bytes(data)
            os.replace(temporary, full)
            self.invalidate()
        return relative

    # ---------- search / graph ----------

    def search(self, query: str, prefix: str = "", limit: int = 20) -> list[tuple[str, int, str]]:
        """Case-insensitive line search, optionally within a folder or file.

        Args:
            query: the text to find.
            prefix: only notes whose path starts with this.
            limit: the most hits to return.

        Returns:
            (path, line number, line) per hit; line 0 marks a match on the note's name.
        """
        wanted = query.strip().lower()
        if not wanted:
            return []
        prefix = normalize_path(prefix).lower()
        hits: list[tuple[str, int, str]] = []
        for relative in sorted(self.notes()):
            if prefix and not relative.lower().startswith(prefix):
                continue
            if wanted in relative.lower():
                hits.append((relative, 0, "(name match)"))
            try:
                text = self.read(relative)
            except (NotFoundError, InvalidInputError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if wanted in line.lower():
                    hits.append((relative, number, line.strip()[:SEARCH_LINE_CHARS]))
                    if len(hits) >= limit:
                        return hits
        return hits[:limit]

    def color_groups(self) -> list[dict[str, str]]:
        """Obsidian's graph colors (`.obsidian/graph.json`), so the GUI matches the vault.

        Returns:
            Groups of kind (`tag` or `path`), value and hex color, in Obsidian's order.
        """
        groups: list[Any] = []
        for base in dict.fromkeys([self.root, self.root / self.home]):
            try:
                groups = json.loads((base / OBSIDIAN_GRAPH).read_text(encoding="utf-8"))["colorGroups"]
                break
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._warn(f"ignored {base / OBSIDIAN_GRAPH}: {error}")
                continue
        found = []
        for group in groups:
            try:
                query = str(group["query"]).strip()
                rgb = int(group["color"]["rgb"])
            except (KeyError, TypeError, ValueError):
                continue
            kind, _, value = query.partition(":")
            if kind in ("tag", "path") and value.strip():
                found.append({"kind": kind, "value": value.strip().lstrip("#"), "color": f"#{rgb:06x}"})
        return found

    def _color(self, relative: str, note: Note, groups: list[dict[str, str]]) -> str | None:
        for group in groups:
            if group["kind"] == "tag" and any(tag == group["value"] or tag.startswith(group["value"] + "/") for tag in note.tags):
                return group["color"]
            if group["kind"] == "path" and relative.lower().startswith(group["value"].lower().strip("/")):
                return group["color"]
        return None

    def graph(self) -> dict[str, Any]:
        """The whole brain as a graph for the GUI.

        Returns:
            nodes (id, label, tags, color), links, dead links, orphans and color groups.
        """
        notes = self.notes()
        groups = self.color_groups()
        nodes, links, dead = [], [], []
        for relative, note in sorted(notes.items()):
            nodes.append({"id": relative, "label": note.name, "tags": note.tags, "color": self._color(relative, note, groups)})
            seen = set()
            for target in note.links:
                resolved = self.resolve(target, relative)
                if resolved and resolved != relative and resolved not in seen:
                    seen.add(resolved)
                    links.append({"source": relative, "target": resolved})
                elif not resolved and not self.is_attachment(target):
                    dead.append({"source": relative, "target": target})
        linked = {end for link in links for end in (link["source"], link["target"])}
        return {
            "nodes": nodes,
            "links": links,
            "dead": dead,
            "orphans": [node["id"] for node in nodes if node["id"] not in linked],
            "groups": groups,
        }

    def outgoing(self, relative: str) -> list[str]:
        """Notes a note links to.

        Args:
            relative: the note's vault path.

        Returns:
            Resolved target paths, sorted, without the note itself.
        """
        note = self.notes().get(relative)
        targets = (self.resolve(target, relative) for target in (note.links if note else []))
        return sorted({target for target in targets if target and target != relative})

    def backlinks(self, relative: str) -> list[str]:
        """Notes that link to a note.

        Args:
            relative: the note's vault path.

        Returns:
            Linking note paths, sorted.
        """
        return sorted(
            source for source, note in self.notes().items()
            if source != relative and any(self.resolve(target, source) == relative for target in note.links)
        )
