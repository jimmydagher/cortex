"""Real external systems, run on purpose: `pytest -m integration` (not in the default run)."""
from __future__ import annotations

from pathlib import Path

import pytest

from cortex import bootstrap
from cortex.config import Config


@pytest.mark.integration
def test_download_real_template_into_empty_and_occupied_folders(config: Config, tmp_path: Path) -> None:
    # Guards the GitHub archive URL, the zip layout and the skipped plugin folders against the real repo.
    empty = tmp_path / "fresh"
    empty.mkdir()
    assert bootstrap.download_template(empty, config.setup) == "CORTEX.md"
    assert (empty / "HIPPOCAMPUS" / "SYNAPSE.md").is_file()
    assert (empty / ".obsidian" / "graph.json").is_file() and not (empty / ".obsidian" / "plugins").exists()

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "notes.md").write_text("mine", encoding="utf-8")
    name = config.setup.template_repo.split("/", 1)[1]
    assert bootstrap.download_template(occupied, config.setup) == f"{name}/CORTEX.md"
