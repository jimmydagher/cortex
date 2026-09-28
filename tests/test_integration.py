"""Real external systems, run on purpose: `pytest -m integration` (not in the default run)."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_config

from cortex import bootstrap
from cortex.brain import Brain
from cortex.config import Config
from cortex.errors import ErrorHandler
from cortex.logs import Logger


@pytest.mark.integration
def test_download_real_template_into_empty_and_occupied_folders(config: Config, tmp_path: Path, logger: Logger) -> None:
    # Guards the GitHub archive URL, the zip layout and the skipped plugin folders against the real repo.
    empty = tmp_path / "fresh"
    empty.mkdir()
    assert bootstrap.download_template(empty, config.setup) == "CORTEX.md"
    assert (empty / ".obsidian" / "graph.json").is_file() and not (empty / ".obsidian" / "plugins").exists()
    # The repo keeps personal and in-transit files out: only the guides ship; setup creates SYNAPSE/ENGRAM from the guide.
    assert (empty / "HIPPOCAMPUS" / "HIPPOCAMPUS.md").is_file() and (empty / "PREFRONTAL" / "PREFRONTAL.md").is_file()
    assert (empty / "NEOCORTEX" / "NEOCORTEX.md").is_file() and not (empty / "THALAMUS.md").exists()
    brain = Brain(make_config(tmp_path, empty), logger, ErrorHandler().handle)
    brain.configure("CORTEX.md")
    synapse = (empty / "HIPPOCAMPUS" / "SYNAPSE.md").read_text(encoding="utf-8")
    assert "Next ID: HX0001" in synapse and "PREFRONTAL/FILE#Section" in synapse  # the guide's template, not Cortex's fallback
    assert brain.is_protected("PREFRONTAL/ANY.md") and brain.is_protected("HIPPOCAMPUS/HIPPOCAMPUS.md")
    assert not brain.is_protected("PREFRONTAL/PROJECTS/Any/ANY.md")

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "notes.md").write_text("mine", encoding="utf-8")
    name = config.setup.template_repo.split("/", 1)[1]
    assert bootstrap.download_template(occupied, config.setup) == f"{name}/CORTEX.md"
