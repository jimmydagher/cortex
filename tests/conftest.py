"""Shared fixtures: a small brain in the claude-brain layout, and config loaded the real way
(config/default.yaml + override/test.yaml) with paths and secrets pointed at a temp folder.

Also the pytest settings: Cortex isn't installed as a package, so src/ goes on the path here,
and the opt-in markers are registered and left out of a plain `pytest` run.
"""
from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402 - after the path setup above

from cortex import config as config_loader  # noqa: E402
from cortex.config import Config  # noqa: E402
from cortex.errors import ErrorHandler  # noqa: E402
from cortex.logs import Logger  # noqa: E402
from cortex.secrets import Secrets  # noqa: E402

OPT_IN_MARKERS = {
    "integration": "talks to real external systems (GitHub); run with -m integration",
    "e2e": "drives the GUI in a real browser; needs `playwright install chromium`; run with -m e2e",
}


def pytest_configure(config: pytest.Config) -> None:
    """Register the opt-in markers and skip them unless asked for with -m."""
    for name, description in OPT_IN_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")
    if not config.option.markexpr:
        config.option.markexpr = " and ".join(f"not {name}" for name in OPT_IN_MARKERS)


ADMIN_PASSWORD = "test-password"
TEST_ENVIRON = {
    "CORTEX_ENV": "test",
    "CORTEX_CONFIG_DIR": str(REPO / "config"),
    "CORTEX_OVERRIDE_DIR": str(REPO / "config" / "override"),
}


def write(root: Path, relative: str, text: str) -> None:
    """Write a dedented note under root."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def make_config(tmp_path: Path, brain: Path, **sections: Any) -> Config:
    """Load the test config with every path and the secrets folder inside tmp_path."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    (secrets_dir / "cortex-admin-pwd").write_text(ADMIN_PASSWORD, encoding="utf-8")
    (secrets_dir / "cortex-session-key").write_text("test-session-key-0123456789", encoding="utf-8")
    extra: dict[str, Any] = {
        "paths": {"brain": str(brain), "state": str(tmp_path / "state"), "logs": str(tmp_path / "logs")},
        "secrets": {"dir": str(secrets_dir)},
    }
    return config_loader.load(TEST_ENVIRON, config_loader.deep_merge(extra, sections))


@pytest.fixture
def brain_dir(tmp_path: Path) -> Path:
    """A small brain in the claude-brain layout."""
    root = tmp_path / "brain"
    write(root, "CORTEX.md", """
        ---
        tags:
          - memory/core
        ---
        # AI Second Brain

        Areas: [[NEOCORTEX/NEOCORTEX|NEOCORTEX]]. Personal layer: [[PREFRONTAL/PREFRONTAL|PREFRONTAL]].

        ## Brain Upkeep
        Queue in [[HIPPOCAMPUS/SYNAPSE|SYNAPSE]]; trail in [[HIPPOCAMPUS/ENGRAM|ENGRAM]]; templates in [[HIPPOCAMPUS/HIPPOCAMPUS|HIPPOCAMPUS]].

        ```text
        [[NOT/A/LINK]]
        ```
        """)
    write(root, "NEOCORTEX/NEOCORTEX.md", """
        ---
        tags:
          - memory/core
        ---
        The area index. Up: [[CORTEX]].

        | Task | Go To |
        | --- | --- |
        | Code | [[NEOCORTEX/CODING\\|CODING]] |
        | Writing | [[NEOCORTEX/WRITING\\|WRITING]] |
        """)
    write(root, "HIPPOCAMPUS/HIPPOCAMPUS.md", """
        ---
        tags:
          - hippocampus
        ---
        > Guide: SYNAPSE and ENGRAM are in-transit files.

        ## SYNAPSE.md template
        ```markdown
        ---
        tags:
          - hippocampus
        ---
        > From the guide. Process: [[CORTEX#Brain Upkeep|CORTEX › Brain Upkeep]]

        Next ID: HX0001

        ## Pending
        <!-- Format: - [ ] HX0001 · YYYY-MM-DD · → NEOCORTEX/FILE › Section or → PREFRONTAL/FILE#Section · change · why · source -->
        ```

        ## ENGRAM.md template
        ```markdown
        ---
        tags:
          - hippocampus
        ---
        > From the guide: trail, newest first.

        ## Trail
        ```
        """)
    write(root, "PREFRONTAL/PREFRONTAL.md", """
        ---
        tags:
          - memory/personal
        ---
        > The human's own layer: its files' sections are the index. Up: [[CORTEX]].

        ## Where a lesson goes
        Another-dev test first.
        """)
    write(root, "PREFRONTAL/PERSONA.md", """
        ---
        tags:
          - memory/personal
        ---
        > Up: [[PREFRONTAL/PREFRONTAL|PREFRONTAL]]

        ## Always
        - Direct, no small talk.
        """)
    write(root, "PREFRONTAL/STYLE.md", """
        ---
        tags:
          - memory/personal
        ---
        > Up: [[PREFRONTAL/PREFRONTAL|PREFRONTAL]]

        ## CODING
        - Log files are plain text, one line per event.

        ## WRITING
        - Short sentences.
        """)
    write(root, "NEOCORTEX/CODING.md", """
        ---
        tags: [memory/technical]
        ---
        ## Core Rules
        - Debug by reproduction.
        Pairs with [[WRITING]] and `[[ALSO/NOT/A/LINK]]`.
        """)
    write(root, "NEOCORTEX/WRITING.md", """
        ---
        tags:
          - memory/communication
        ---
        ## Avoid
        - Filler.

        ## Output
        - Short.
        """)
    write(root, "PREFRONTAL/PROJECTS/Acme/ACME.md", """
        ---
        tags:
          - project/acme
        ---
        ## Now
        Starting. See [[Missing Note]] and ![[diagram.png]].
        Rules: [[NEOCORTEX/WRITING#Avoid]], [[NEOCORTEX/WRITING#Nope]], and back to [[#Now]].
        """)
    write(root, "PREFRONTAL/PROJECTS/Acme/NOTES.md", """
        ---
        tags:
          - project/acme
        ---
        Status: [[PREFRONTAL/PROJECTS/Acme/ACME#Now|now]] · [[ACME#Now]] · table | [[ACME#Now\\|here]] |
        Not a link: `[[ACME#Now]]`
        """)
    write(root, "HIPPOCAMPUS/SYNAPSE.md", """
        ---
        tags:
          - hippocampus
        ---
        > Queue.

        Next ID: HX0002

        ## Pending
        <!-- Format: - [ ] HX0001 · YYYY-MM-DD · → NEOCORTEX/FILE › Section · proposed change · why · source -->
        """)
    write(root, "HIPPOCAMPUS/ENGRAM.md", """
        ---
        tags:
          - hippocampus
        ---
        > Trail.

        ## Trail
        <!-- Committed: - [x] HX0001 · proposed YYYY-MM-DD · committed YYYY-MM-DD · landed in NEOCORTEX/FILE › Section · summary -->
        - [x] HX0001 · proposed 2026-09-26 · committed 2026-09-26 · landed in NEOCORTEX/CODING › Core Rules · first lesson
        """)
    return root


@pytest.fixture
def config(tmp_path: Path, brain_dir: Path) -> Config:
    """The test configuration."""
    return make_config(tmp_path, brain_dir)


@pytest.fixture
def logger(config: Config) -> Iterator[Logger]:
    """A central logger writing into the temp logs folder."""
    instance = Logger(config.logging, config.paths.logs)
    yield instance
    instance.close()


@pytest.fixture
def errors() -> ErrorHandler:
    """A global error handler (not installed into Python's hooks during tests)."""
    return ErrorHandler()


@pytest.fixture
def secret_store(config: Config) -> Secrets:
    """The secrets accessor over the temp secrets folder."""
    return Secrets(config.secrets)
