"""Vault: parsing, Obsidian-style link resolution, path safety and exact edits."""
from __future__ import annotations

from pathlib import Path

import pytest

from cortex.config import Config
from cortex.errors import ConflictError, InvalidInputError, NotFoundError
from cortex.vault import Edit, Vault, parse_note, scan_lines, section


def make_vault(config: Config, warnings: list[str] | None = None) -> Vault:
    sink = warnings if warnings is not None else []
    return Vault(config.paths.brain, config.vault, sink.append)


def test_parse_tags_links_and_headings_skip_code() -> None:
    # Regression: links inside code fences or inline code must not become graph edges.
    parsed = parse_note("---\ntags:\n  - a/b\n---\n## Intro\nSee [[X|x]] and [[Y#Head]] #inline [[#Intro]]\n```\n## Not a heading\n[[Z]]\n```\n`[[Q]]`")
    assert parsed.tags == ["a/b", "inline"]
    assert parsed.links == ["X", "Y"]
    assert parsed.anchors == [("Y", "Head"), ("", "Intro")]
    assert parsed.headings == {"intro"}


def test_table_escaped_pipe_link() -> None:
    # Regression: claude-brain's routing table escapes the alias pipe as \|.
    parsed = parse_note("| [[MEMORY/CODING\\|CODING]] | [[CEREBELLUM/STYLE#CODING\\|STYLE › CODING]] |")
    assert parsed.links == ["MEMORY/CODING", "CEREBELLUM/STYLE"]
    assert parsed.anchors == [("CEREBELLUM/STYLE", "CODING")]


def test_dead_heading_links_are_reported(config: Config) -> None:
    # Regression (brain audit parity): [[note#Heading]] to a missing heading is a dead link.
    dead = make_vault(config).dead_links()
    assert {"source": "PROJECTS/Acme/ACME.md", "target": "MEMORY/WRITING#Nope"} in dead
    assert not any(item["target"] in ("MEMORY/WRITING#Avoid", "#Now") for item in dead)
    assert not any(item["source"] == "CEREBELLUM/MAP.md" for item in dead)


def test_rename_heading_plan_updates_every_link_form(config: Config) -> None:
    plan = make_vault(config).rename_heading_plan("PROJECTS/Acme/ACME.md", "now", "Status", {})
    assert set(plan) == {"PROJECTS/Acme/ACME.md", "PROJECTS/Acme/NOTES.md"}
    assert "## Status" in plan["PROJECTS/Acme/ACME.md"] and "[[#Status]]" in plan["PROJECTS/Acme/ACME.md"]
    notes = plan["PROJECTS/Acme/NOTES.md"]
    assert "[[PROJECTS/Acme/ACME#Status|now]]" in notes and "[[ACME#Status]]" in notes
    assert "[[ACME#Status\\|here]]" in notes  # table-escaped pipe kept
    assert "`[[ACME#Now]]`" in notes  # code is left alone


def test_rename_heading_plan_refuses_missing_duplicate_and_taken(config: Config, brain_dir: Path) -> None:
    vault = make_vault(config)
    with pytest.raises(NotFoundError):
        vault.rename_heading_plan("PROJECTS/Acme/ACME.md", "Nope", "X", {})
    with pytest.raises(ConflictError):
        vault.rename_heading_plan("MEMORY/WRITING.md", "Avoid", "output", {})
    (brain_dir / "TWICE.md").write_text("## A\n## A\n", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="2 headings"):
        vault.rename_heading_plan("TWICE.md", "A", "B", {})


def test_scan_lines_marks_fenced_code() -> None:
    assert [in_code for _, in_code in scan_lines("a\n```\nb\n```\nc")] == [False, True, True, True, False]


def test_resolve_by_path_name_and_suffix(config: Config) -> None:
    vault = make_vault(config)
    assert vault.resolve("MEMORY/CODING") == "MEMORY/CODING.md"
    assert vault.resolve("coding") == "MEMORY/CODING.md"
    assert vault.resolve("Acme/ACME") == "PROJECTS/Acme/ACME.md"
    assert vault.resolve("Missing Note") is None
    assert vault.resolve("diagram.png") is None


def test_graph_counts_dead_links_but_not_attachments(config: Config) -> None:
    graph = make_vault(config).graph()
    assert {"source": "PROJECTS/Acme/ACME.md", "target": "Missing Note"} in graph["dead"]
    assert not any(dead["target"] == "diagram.png" for dead in graph["dead"])
    assert not any("NOT/A/LINK" in dead["target"] for dead in graph["dead"])
    assert {"source": "CORTEX.md", "target": "MEMORY/CODING.md"} in graph["links"]


@pytest.mark.parametrize("bad", ["../escape.md", ".obsidian/app", "MEMORY/../../x", "C:/Windows/x", "a/.git/config"])
def test_safe_path_refuses_escapes(config: Config, bad: str) -> None:
    with pytest.raises(InvalidInputError):
        make_vault(config).safe_path(bad)


def test_leading_slash_is_vault_root(config: Config) -> None:
    relative, full = make_vault(config).safe_path("/etc/passwd")
    assert relative == "etc/passwd.md" and full.is_relative_to(config.paths.brain.resolve())


def test_edits_must_match_exactly_once(config: Config) -> None:
    vault = make_vault(config)
    with pytest.raises(InvalidInputError, match="not found"):
        vault.prepare("MEMORY/WRITING.md", edits=[Edit("nope", "x")])
    with pytest.raises(InvalidInputError, match="found 2 times"):
        vault.prepare("MEMORY/WRITING.md", edits=[Edit("## ", "### ")])
    _, text = vault.prepare("MEMORY/WRITING.md", edits=[Edit("- Filler.", "- Filler and hedging.")])
    assert "- Filler and hedging." in text


def test_missing_note_is_not_found(config: Config) -> None:
    with pytest.raises(NotFoundError):
        make_vault(config).read("NOPE.md")


def test_write_keeps_crlf(config: Config, brain_dir: Path) -> None:
    (brain_dir / "CRLF.md").write_bytes(b"line one\r\nline two\r\n")
    vault = make_vault(config)
    relative, text = vault.prepare("CRLF.md", edits=[Edit("two", "2")])
    vault.write_text(relative, text)
    assert (brain_dir / "CRLF.md").read_bytes() == b"line one\r\nline 2\r\n"


def test_oversized_note_warns_instead_of_silently_skipping(tmp_path: Path, brain_dir: Path) -> None:
    # Regression: unreadable or oversized notes used to be indexed without a word (F-022).
    from conftest import make_config

    config = make_config(tmp_path, brain_dir, vault={"max_note_bytes": 1000})
    (brain_dir / "BIG.md").write_text("x" * 2000, encoding="utf-8")
    warnings: list[str] = []
    make_vault(config, warnings).notes()
    assert any("BIG.md is over vault.max_note_bytes" in warning for warning in warnings)


def test_section() -> None:
    text = "# T\n## A\none\n### A1\ntwo\n## B\nthree\n"
    assert section(text, "A") == "## A\none\n### A1\ntwo\n"
    assert section(text, "b") == "## B\nthree\n"
    assert section(text, "Z") is None
