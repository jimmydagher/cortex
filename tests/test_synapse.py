"""Hippocampus: SYNAPSE queue, GUI approval and the ENGRAM trail, in claude-brain's formats."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cortex.config import Config
from cortex.errors import ConflictError, InvalidInputError
from cortex.synapse import EntryStatus, Hippocampus
from cortex.vault import Vault

DAY = date(2026, 9, 27)


@pytest.fixture
def hippocampus(config: Config) -> Hippocampus:
    vault = Vault(config.paths.brain, config.vault, lambda _message: None)
    return Hippocampus(vault, "HIPPOCAMPUS/SYNAPSE.md", "HIPPOCAMPUS/ENGRAM.md")


def test_queue_writes_entry_and_bumps_next_id(hippocampus: Hippocampus, brain_dir: Path) -> None:
    entry = hippocampus.queue("MEMORY/WRITING › Avoid", "Cut hedging · always", "recurs\nin drafts", "correction", DAY)
    assert entry.id == "HX0002"
    text = (brain_dir / "HIPPOCAMPUS/SYNAPSE.md").read_text(encoding="utf-8")
    assert "- [ ] HX0002 · 2026-09-27 · → MEMORY/WRITING › Avoid · Cut hedging ; always · recurs in drafts · correction" in text
    assert "Next ID: HX0003" in text
    assert text.index("<!-- Format") < text.index("- [ ] HX0002")
    assert hippocampus.queue("X", "second", "why", "pref", DAY).id == "HX0003"


def test_next_id_skips_ids_already_in_engram(hippocampus: Hippocampus, brain_dir: Path) -> None:
    path = brain_dir / "HIPPOCAMPUS/SYNAPSE.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Next ID: HX0002", "Next ID: HX0001"), encoding="utf-8")
    assert hippocampus.queue("X", "c", "w", "s", DAY).id == "HX0002"


def test_approve_unapprove_and_list(hippocampus: Hippocampus) -> None:
    hippocampus.queue("MEMORY/CODING › Core Rules", "one", "w", "s", DAY)
    hippocampus.queue("MEMORY/CODING › Core Rules", "two", "w", "s", DAY)
    hippocampus.set_approved("hx0002", True)
    assert {entry.id: entry.status for entry in hippocampus.entries()} == {"HX0002": EntryStatus.APPROVED, "HX0003": EntryStatus.PENDING}
    with pytest.raises(ConflictError, match="already approved"):
        hippocampus.set_approved("HX0002", True)
    hippocampus.set_approved("HX0002", False)
    assert {entry.id: entry.status for entry in hippocampus.entries()} == {"HX0002": EntryStatus.PENDING, "HX0003": EntryStatus.PENDING}


def test_commit_moves_to_top_of_trail(hippocampus: Hippocampus) -> None:
    hippocampus.queue("MEMORY/WRITING › Avoid", "Cut hedging", "w", "correction", date(2026, 9, 26))
    hippocampus.set_approved("HX0002", True)
    hippocampus.commit("HX0002", "", "hedging joins the Avoid list", DAY)
    assert hippocampus.entries() == []
    trail = hippocampus.trail()
    assert [entry.id for entry in trail] == ["HX0002", "HX0001"]
    assert trail[0].raw == "- [x] HX0002 · proposed 2026-09-26 · committed 2026-09-27 · landed in MEMORY/WRITING › Avoid · hedging joins the Avoid list"
    with pytest.raises(ConflictError, match="already committed"):
        hippocampus.get("HX0002")


def test_reject_needs_reason(hippocampus: Hippocampus) -> None:
    hippocampus.queue("X", "idea", "w", "s", DAY)
    with pytest.raises(InvalidInputError, match="reason"):
        hippocampus.reject("HX0002", "  ", DAY)
    hippocampus.reject("HX0002", "already covered by CODING", DAY)
    assert hippocampus.trail()[0].raw == "- [-] HX0002 · proposed 2026-09-27 · rejected 2026-09-27 · idea · already covered by CODING"


def test_missing_files_are_created_from_templates(config: Config, tmp_path: Path) -> None:
    vault = Vault(tmp_path / "empty", config.vault, lambda _message: None)
    hippocampus = Hippocampus(vault, "H/SYNAPSE.md", "H/ENGRAM.md")
    hippocampus.ensure_files()
    assert hippocampus.queue("X", "c", "w", "s", DAY).id == "HX0001"
    hippocampus.commit("HX0001", "X", "done", DAY)
    assert hippocampus.trail()[0].id == "HX0001"
