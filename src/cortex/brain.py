"""The service layer: every capability the MCP tools and the GUI share.

Brain takes plain, validated values and never sees a request, a response or an MCP
context, so a handler's only jobs are parsing input, calling one Brain method and
shaping the result. Activity is logged here, once, whichever door the call came in by.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar

from . import bootstrap
from .bootstrap import SetupMode
from .config import Config
from .errors import (
    ConflictError,
    CortexError,
    ForbiddenError,
    InvalidInputError,
    NotConfiguredError,
    NotFoundError,
    Reporter,
    describe,
)
from .logs import Logger
from .state import Power, State, now_utc
from .synapse import Entry, EntryStatus, Hippocampus
from .vault import Edit, Vault, normalize_path, section


class JobStatus(StrEnum):
    """Progress of the background setup job."""

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class NoteChange:
    """One note's part of a memory commit: exact-match edits, or whole content."""

    note: str
    edits: list[Edit] = field(default_factory=list)
    content: str | None = None


@dataclass(frozen=True)
class NoteRead:
    """A note (or one section of it) as read for a client."""

    path: str
    tags: list[str]
    protected: bool
    text: str


@dataclass(frozen=True)
class Overview:
    """What cortex_load needs: the routing file and the state of the hippocampus."""

    cortex_path: str
    engram_path: str
    notes: int
    pending: int
    approved: list[Entry]
    text: str


@dataclass
class SetupJob:
    """The one setup run at a time, polled by the GUI."""

    status: JobStatus = JobStatus.IDLE
    mode: str = ""
    error: str = ""
    finished: str = ""


class Brain:
    """Cortex's capabilities over one brain folder."""

    def __init__(self, config: Config, logger: Logger, report: Reporter) -> None:
        self.config = config
        self.logger = logger
        self._report = report
        config.paths.brain.mkdir(parents=True, exist_ok=True)
        self.state = State(config.paths.state, config.state, config.layout)
        self.vault = Vault(config.paths.brain, config.vault, logger.warn)
        self.hippocampus = Hippocampus(self.vault, self.state.get("synapse_path"), self.state.get("engram_path"))
        self.vault.home = self._home(self.cortex_path)
        self._job = SetupJob()
        self._job_lock = threading.Lock()
        if self.configured:
            self.hippocampus.ensure_files()

    # ---------- configuration ----------

    @property
    def cortex_path(self) -> str:
        """The routing file's vault path, empty before setup."""
        return str(self.state.get("cortex_path"))

    @property
    def configured(self) -> bool:
        """Whether setup is done and the routing file exists."""
        return bool(self.cortex_path) and self.vault.exists(self.cortex_path)

    @property
    def power(self) -> Power:
        """The brain's switch."""
        return self.state.power

    def require_configured(self) -> None:
        """Fail a task that needs a brain instead of returning an empty result.

        Raises:
            NotConfiguredError: setup isn't finished.
        """
        if not self.configured:
            raise NotConfiguredError("the brain isn't set up yet: finish setup in the Cortex web GUI")

    @staticmethod
    def _home(cortex_path: str) -> str:
        parent = str(PurePosixPath(cortex_path).parent) if cortex_path else ""
        return "" if parent == "." else parent

    def _find(self, stem: str, base: str) -> str | None:
        found = [path for path in self.vault.notes() if PurePosixPath(path).stem.upper() == stem and path.startswith(base)]
        return min(found, key=lambda path: (path.count("/"), path)) if found else None

    def configure(self, cortex_path: str, synapse_path: str | None = None, engram_path: str | None = None,
                  protected: list[str] | None = None) -> None:
        """Point Cortex at a routing file; find or place SYNAPSE and ENGRAM; set the protected list.

        Args:
            cortex_path: the CORTEX.md path inside the brain folder.
            synapse_path: SYNAPSE's path; found near CORTEX.md or from layout.synapse when omitted.
            engram_path: ENGRAM's path; found or from layout.engram when omitted.
            protected: protected paths; CORTEX.md plus layout.memory (when it exists) when omitted.

        Raises:
            NotFoundError: there's no file at cortex_path.
            InvalidInputError: a path is refused.
        """
        cortex, _ = self.vault.safe_path(cortex_path)
        if not self.vault.exists(cortex):
            raise NotFoundError(f"no CORTEX file at {cortex}")
        home = self._home(cortex)
        base = f"{home}/" if home else ""
        layout = self.config.layout
        synapse = self.vault.safe_path(synapse_path or self._find("SYNAPSE", base) or base + layout.synapse)[0]
        engram = self.vault.safe_path(engram_path or self._find("ENGRAM", base) or base + layout.engram)[0]
        if protected is None:
            memory = base + layout.memory
            protected = [cortex] + ([memory] if (self.config.paths.brain / memory).is_dir() else [])
        self.state.update(
            cortex_path=cortex, synapse_path=synapse, engram_path=engram,
            protected=[normalize_path(path) for path in protected if path.strip()],
        )
        self.hippocampus.synapse_path, self.hippocampus.engram_path = synapse, engram
        self.vault.home = home
        self.vault.invalidate()
        self.hippocampus.ensure_files()

    def apply_settings(self, who: str, cortex_path: str, synapse_path: str | None, engram_path: str | None,
                       protected: list[str]) -> dict[str, Any]:
        """Save the GUI's settings form.

        Args:
            who: the actor, for the activity log.
            cortex_path: the routing file.
            synapse_path: SYNAPSE's path, or None to find it.
            engram_path: ENGRAM's path, or None to find it.
            protected: the protected paths (may be empty).

        Returns:
            The new status.
        """
        self.configure(cortex_path, synapse_path, engram_path, protected)
        self.logger.event(who, "settings saved", self.cortex_path)
        return self.status()

    # ---------- guards ----------

    def is_hippocampus(self, relative: str) -> bool:
        """Whether a path is SYNAPSE or ENGRAM (managed only by the synapse operations).

        Args:
            relative: a vault path.

        Returns:
            True for the two hippocampus files.
        """
        return relative.lower() in (self.hippocampus.synapse_path.lower(), self.hippocampus.engram_path.lower())

    def is_protected(self, relative: str) -> bool:
        """Whether a note changes only through an approved SYNAPSE commit.

        Args:
            relative: a vault path.

        Returns:
            True when a protected rule matches (a trailing `/` protects a folder), or for the hippocampus.
        """
        lowered = relative.lower()
        for rule in self.state.get("protected"):
            rule = rule.lower()
            if (rule.endswith("/") and lowered.startswith(rule)) or lowered in (rule, rule + ".md"):
                return True
        return self.is_hippocampus(relative)

    # ---------- status and reads ----------

    def status(self) -> dict[str, Any]:
        """Everything the GUI header and dashboard show.

        Returns:
            Power, setup, paths, counts.
        """
        entries = self.hippocampus.entries() if self.configured else []
        return {
            "power": self.power.value,
            "power_changed": self.state.get("power_changed"),
            "configured": self.configured,
            "brain_dir": str(self.config.paths.brain),
            "cortex_path": self.cortex_path,
            "synapse_path": self.hippocampus.synapse_path,
            "engram_path": self.hippocampus.engram_path,
            "protected": self.state.get("protected"),
            "notes": len(self.vault.notes()),
            "pending": sum(entry.status == EntryStatus.PENDING for entry in entries),
            "approved": sum(entry.status == EntryStatus.APPROVED for entry in entries),
            "keys": len(self.state.get("keys")),
            "version": self.config.runtime.version,
            "public_url": self.config.server.public_url,
            "template_repo": self.config.setup.template_repo,
        }

    def overview(self, who: str) -> Overview:
        """The routing file plus hippocampus counts, for loading the brain.

        Args:
            who: the actor, for the activity log.

        Returns:
            The overview.
        """
        self.require_configured()
        entries = self.hippocampus.entries()
        self.logger.event(who, "load", self.cortex_path)
        return Overview(
            cortex_path=self.cortex_path,
            engram_path=self.hippocampus.engram_path,
            notes=len(self.vault.notes()),
            pending=sum(entry.status == EntryStatus.PENDING for entry in entries),
            approved=[entry for entry in entries if entry.status == EntryStatus.APPROVED],
            text=self.vault.read(self.cortex_path),
        )

    def read_note(self, who: str, target: str, heading: str = "") -> NoteRead:
        """Read a note by path, name or wikilink target, or one section of it.

        Args:
            who: the actor, for the activity log.
            target: `MEMORY/CODING`, `CODING`...; empty reads the routing file.
            heading: optional heading whose section alone is returned.

        Returns:
            The note.

        Raises:
            NotFoundError: no note matches, or it has no such heading.
        """
        self.require_configured()
        relative = self.vault.resolve(target) if target else self.cortex_path
        if not relative:
            raise NotFoundError(f"no note matches '{target}'; try searching or listing notes")
        text = self.vault.read(relative)
        if heading:
            part = section(text, heading)
            if part is None:
                raise NotFoundError(f"{relative} has no heading '{heading}'")
            text = part
        note = self.vault.notes().get(relative)
        self.logger.event(who, "read", relative + (f"#{heading}" if heading else ""))
        return NoteRead(relative, note.tags if note else [], self.is_protected(relative), text)

    def note_view(self, target: str) -> dict[str, Any]:
        """A note with its links in both directions, for the GUI's reader.

        Args:
            target: the note's path or name.

        Returns:
            path, tags, text, protected, backlinks, outgoing.

        Raises:
            NotFoundError: no such note.
        """
        relative = self.vault.resolve(target)
        if not relative:
            raise NotFoundError("no such note")
        note = self.vault.notes().get(relative)
        return {
            "path": relative,
            "tags": note.tags if note else [],
            "text": self.vault.read(relative),
            "protected": self.is_protected(relative),
            "backlinks": self.vault.backlinks(relative),
            "outgoing": self.vault.outgoing(relative),
        }

    def search(self, query: str, prefix: str, limit: int) -> list[tuple[str, int, str]]:
        """Line search across the brain.

        Args:
            query: text to find.
            prefix: folder or file to search within.
            limit: most hits.

        Returns:
            (path, line, text) hits.
        """
        self.require_configured()
        return self.vault.search(query, prefix, limit)

    def list_notes(self, folder: str) -> tuple[list[tuple[str, list[str], bool]], int]:
        """Notes under a folder with tags and protection, capped at mcp.list_limit.

        Args:
            folder: a folder prefix; empty for the whole brain.

        Returns:
            (up to list_limit rows of (path, tags, protected), total matching).
        """
        self.require_configured()
        prefix = normalize_path(folder).lower()
        rows = [(path, note.tags, self.is_protected(path)) for path, note in sorted(self.vault.notes().items())
                if path.lower().startswith(prefix)]
        return rows[: self.config.mcp.list_limit], len(rows)

    # ---------- note writes ----------

    def write_note(self, who: str, note: str, content: str | None, edits: list[Edit]) -> tuple[str, bool]:
        """Create or edit an unprotected note.

        Args:
            who: the actor, for the activity log.
            note: the note's vault path.
            content: whole content, or None when editing.
            edits: exact-match edits.

        Returns:
            (path, created).

        Raises:
            ForbiddenError: the note is protected.
        """
        self.require_configured()
        relative, _ = self.vault.safe_path(note)
        if self.is_protected(relative):
            raise ForbiddenError(f"{relative} is protected: memory changes go through a SYNAPSE entry and an approved commit")
        created = not self.vault.exists(relative)
        relative, text = self.vault.prepare(relative, content, edits)
        self.vault.write_text(relative, text)
        self.logger.event(who, "write", relative)
        return relative, created

    # ---------- hippocampus ----------

    def queue_entry(self, who: str, target: str, change: str, why: str, source: str) -> Entry:
        """Queue a proposed memory change in SYNAPSE.

        Args:
            who: the actor.
            target: where it would land.
            change: the change.
            why: why it matters.
            source: correction, preference or research.

        Returns:
            The queued entry.
        """
        self.require_configured()
        entry = self.hippocampus.queue(target, change, why, source)
        self.logger.event(who, "queue", entry.line())
        return entry

    def synapse_view(self) -> dict[str, Any]:
        """SYNAPSE and the recent trail, for the GUI's review page.

        Returns:
            pending, approved, trail (gui.trail_limit entries) and next_id.
        """
        self.require_configured()
        entries = self.hippocampus.entries()
        return {
            "pending": [entry.as_dict() for entry in entries if entry.status == EntryStatus.PENDING],
            "approved": [entry.as_dict() for entry in entries if entry.status == EntryStatus.APPROVED],
            "trail": [entry.as_dict() for entry in self.hippocampus.trail(self.config.gui.trail_limit)],
            "next_id": self.hippocampus.next_id(),
        }

    def commit_entry(self, who: str, entry_id: str, changes: list[NoteChange], summary: str,
                     landed_in: str) -> tuple[Entry, list[str]]:
        """Write an entry's memory edits and move it to ENGRAM; every edit is validated first.

        Args:
            who: the actor.
            entry_id: the SYNAPSE entry.
            changes: one item per note to change (protected notes allowed).
            summary: one line for the trail.
            landed_in: where it landed; the entry's target when empty.

        Returns:
            (committed entry, changed paths).

        Raises:
            InvalidInputError: no changes, or an edit doesn't apply.
            ForbiddenError: a change targets SYNAPSE or ENGRAM.
        """
        self.require_configured()
        entry = self.hippocampus.get(entry_id)
        if not changes:
            raise InvalidInputError("changes is empty: include the memory edit that integrates this entry")
        prepared = []
        for change in changes:
            relative, _ = self.vault.safe_path(change.note)
            if self.is_hippocampus(relative):
                raise ForbiddenError(f"{relative} is managed by the synapse operations and can't be a commit target")
            prepared.append(self.vault.prepare(relative, change.content, change.edits))
        for relative, text in prepared:
            self.vault.write_text(relative, text)
        self.hippocampus.commit(entry.id, landed_in, summary)
        paths = [relative for relative, _ in prepared]
        self.logger.event(who, "commit", f"{entry.id} → {', '.join(paths)}")
        return entry, paths

    def reject_entry(self, who: str, entry_id: str, reason: str) -> Entry:
        """Reject an entry into ENGRAM with the reason.

        Args:
            who: the actor.
            entry_id: the SYNAPSE entry.
            reason: why.

        Returns:
            The rejected entry.
        """
        self.require_configured()
        entry = self.hippocampus.reject(entry_id, reason)
        self.logger.event(who, "reject", f"{entry.id}: {reason}")
        return entry

    def set_approved(self, who: str, entry_id: str, approved: bool) -> Entry:
        """Approve an entry in the GUI, or send it back to review.

        Args:
            who: the actor.
            entry_id: the SYNAPSE entry.
            approved: True to approve.

        Returns:
            The moved entry.
        """
        self.require_configured()
        entry = self.hippocampus.set_approved(entry_id, approved)
        self.logger.event(who, "approve" if approved else "send back", entry.line())
        return entry

    # ---------- power and keys ----------

    def set_power(self, who: str, power: Power) -> None:
        """Flip the brain's switch for every client.

        Args:
            who: the actor.
            power: on or off.
        """
        self.state.set_power(power)
        self.logger.event(who, "power", power.value)

    def create_key(self, who: str, label: str) -> tuple[dict[str, str], str]:
        """Create a client API key.

        Args:
            who: the actor.
            label: the client's name.

        Returns:
            (record without hash, raw key shown once).
        """
        record, raw = self.state.create_key(label)
        self.logger.event(who, "key created", record["label"])
        return record, raw

    def revoke_key(self, who: str, key_id: str) -> None:
        """Revoke a client API key.

        Args:
            who: the actor.
            key_id: the key's id.
        """
        label = self.state.revoke_key(key_id)
        self.logger.event(who, "key revoked", label)

    # ---------- setup ----------

    def setup_scan(self) -> dict[str, Any]:
        """What the setup wizard offers.

        Returns:
            CORTEX.md candidates, whether the folder is empty, the folder and template repo.
        """
        root = self.config.paths.brain
        return {
            "candidates": bootstrap.find_cortex_files(root),
            "empty": bootstrap.is_empty(root),
            "brain_dir": str(root),
            "template_repo": self.config.setup.template_repo,
        }

    def _setup_existing(self, cortex_path: str) -> str:
        if not cortex_path.strip():
            raise InvalidInputError("give the path to your CORTEX file")
        return cortex_path

    def _setup_template(self, _cortex_path: str) -> str:
        return bootstrap.download_template(self.config.paths.brain, self.config.setup)

    def _setup_blank(self, _cortex_path: str) -> str:
        return bootstrap.create_blank(self.config.paths.brain)

    _SETUP_RUNNERS: ClassVar[dict[SetupMode, Callable[[Brain, str], str]]] = {
        SetupMode.EXISTING: _setup_existing,
        SetupMode.TEMPLATE: _setup_template,
        SetupMode.BLANK: _setup_blank,
    }

    def run_setup(self, who: str, mode: SetupMode, cortex_path: str) -> dict[str, Any]:
        """Finish setup now (existing, blank) or start the template download in the background.

        Args:
            who: the actor.
            mode: how to get a brain.
            cortex_path: the CORTEX path for `existing`.

        Returns:
            The setup job's state; status is `running` for a template download.

        Raises:
            ConflictError: a setup job is already running.
        """
        with self._job_lock:
            if self._job.status == JobStatus.RUNNING:
                raise ConflictError("setup is already running")
            self._job = SetupJob(status=JobStatus.RUNNING, mode=mode.value)
        if mode == SetupMode.TEMPLATE:
            threading.Thread(target=self._setup_in_background, args=(who, mode), name="cortex-setup", daemon=True).start()
            return self.setup_job()
        try:
            self._complete_setup(who, mode, cortex_path)
        except CortexError:
            with self._job_lock:
                self._job = SetupJob()  # a refused request leaves no failed job behind; the error goes to the caller
            raise
        return self.setup_job()

    def _complete_setup(self, who: str, mode: SetupMode, cortex_path: str) -> None:
        found = self._SETUP_RUNNERS[mode](self, cortex_path)
        self.vault.invalidate()
        self.configure(found)
        self.logger.event(who, f"setup ({mode.value})", self.cortex_path)
        with self._job_lock:
            self._job = SetupJob(status=JobStatus.SUCCEEDED, mode=mode.value, finished=now_utc())

    def _setup_in_background(self, who: str, mode: SetupMode) -> None:
        try:
            self._complete_setup(who, mode, "")
        except CortexError as error:
            detail = f"{error.message} ({describe(error.__cause__)})" if error.__cause__ else error.message
            self.logger.warn(f"setup ({mode.value}) failed: {detail}")
            failed = SetupJob(status=JobStatus.FAILED, mode=mode.value, error=error.message, finished=now_utc())
            with self._job_lock:
                self._job = failed
        except Exception as error:  # noqa: BLE001 - a worker's error must reach the global handler, not vanish
            self._report(error, {"where": "setup job"})
            failed = SetupJob(status=JobStatus.FAILED, mode=mode.value, error="setup failed unexpectedly; see the log",
                              finished=now_utc())
            with self._job_lock:
                self._job = failed

    def setup_job(self) -> dict[str, Any]:
        """The current setup job, polled by the GUI.

        Returns:
            status, mode, error, finished, and whether the brain is now configured.
        """
        with self._job_lock:
            job = self._job
        return {"status": job.status.value, "mode": job.mode, "error": job.error, "finished": job.finished,
                "configured": self.configured}
