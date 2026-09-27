"""First-run setup: find an existing CORTEX.md, download a template brain, or start blank.

Pure file operations with no web or MCP knowledge; brain.Brain decides when to run them
(the template download runs as a background job so it never blocks a request).
"""
from __future__ import annotations

import io
import os
import shutil
import urllib.request
import zipfile
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .config import SetupConfig
from .errors import ConflictError, InvalidInputError, UpstreamError

CORTEX_FILE = "cortex.md"
BLANK_CORTEX = "CORTEX.md"
SEARCH_DEPTH = 4
ARCHIVE_URL = "https://github.com/{repo}/archive/HEAD.zip"
SKIPPED_ARCHIVE_PARTS = {".git", ".trash"}
SKIPPED_ARCHIVE_PREFIX = (".obsidian", "plugins")  # third-party plugin code and its secrets
SKELETON = Path(__file__).parent / "skeleton"


class SetupMode(StrEnum):
    """How the first-run wizard gets a brain."""

    EXISTING = "existing"
    TEMPLATE = "template"
    BLANK = "blank"


def find_cortex_files(root: Path) -> list[str]:
    """Every CORTEX.md within a few folders of root.

    Args:
        root: the brain folder.

    Returns:
        Vault-relative paths, shallowest first.
    """
    found = []
    for folder, subfolders, files in os.walk(root):
        depth = len(Path(folder).relative_to(root).parts)
        keep = sorted(name for name in subfolders if not name.startswith(".") and name != "node_modules")
        subfolders[:] = keep if depth < SEARCH_DEPTH else []
        found += [(Path(folder) / name).relative_to(root).as_posix() for name in files if name.lower() == CORTEX_FILE]
    return sorted(found, key=lambda path: (path.count("/"), path))


def is_empty(folder: Path) -> bool:
    """Whether a folder has nothing but hidden files.

    Args:
        folder: the folder to check.

    Returns:
        True when missing or holding only dot-files.
    """
    return not any(not entry.name.startswith(".") for entry in folder.iterdir()) if folder.is_dir() else True


def download_template(root: Path, config: SetupConfig) -> str:
    """Download setup.template_repo's default branch into root, or root/<repo> if root isn't empty.

    Args:
        root: the brain folder.
        config: the setup section (repo, timeout, size cap).

    Returns:
        The vault-relative path of the template's CORTEX.md.

    Raises:
        UpstreamError: GitHub couldn't be reached or answered with an error (detail in __cause__).
        InvalidInputError: the archive is too large or has no CORTEX.md.
        ConflictError: root/<repo> already exists.
    """
    repo = config.template_repo
    try:
        with urllib.request.urlopen(ARCHIVE_URL.format(repo=repo), timeout=config.download_timeout_seconds) as response:
            data = response.read(config.max_download_bytes + 1)
    except OSError as error:
        raise UpstreamError(f"couldn't download {repo} from GitHub") from error
    if len(data) > config.max_download_bytes:
        raise InvalidInputError(f"the {repo} archive is larger than setup.max_download_bytes")

    name = repo.split("/", 1)[1]
    target = root if is_empty(root) else root / name
    if target != root and target.exists():
        raise ConflictError(f"{target} already exists; remove it or use it as an existing brain")
    target_resolved = target.resolve()

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            parts = PurePosixPath(member.filename).parts[1:]  # drop "<repo>-<sha>/"
            if not parts or member.is_dir():
                continue
            if ".." in parts or set(parts) & SKIPPED_ARCHIVE_PARTS or parts[:2] == SKIPPED_ARCHIVE_PREFIX:
                continue
            destination = (target / Path(*parts)).resolve()
            if not destination.is_relative_to(target_resolved):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)

    cortex = find_cortex_files(target)
    if not cortex:
        raise InvalidInputError(f"{repo} has no CORTEX.md")
    return ("" if target == root else f"{name}/") + cortex[0]


def create_blank(root: Path) -> str:
    """Copy the starter brain into root without overwriting anything.

    Args:
        root: the brain folder.

    Returns:
        `CORTEX.md`, the new routing file's path.
    """
    for source in SKELETON.rglob("*.md"):
        destination = root / source.relative_to(SKELETON)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
    return BLANK_CORTEX
