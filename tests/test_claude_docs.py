from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBSYSTEMS = ["coordinator", "runners", "sprint", "task", "config", "cli"]
REQUIRED_SECTIONS = ["## Purpose", "## Invariants", "## Context"]


def test_major_subsystems_have_claude_docs() -> None:
    for subsystem in SUBSYSTEMS:
        path = ROOT / "src" / "theforge" / subsystem / "CLAUDE.md"
        assert path.exists(), f"missing CLAUDE.md for {subsystem}"


def test_subsystem_claude_docs_have_required_sections() -> None:
    for subsystem in SUBSYSTEMS:
        text = (ROOT / "src" / "theforge" / subsystem / "CLAUDE.md").read_text()
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{subsystem} missing section {section}"


def test_root_claude_mentions_directory_level_convention() -> None:
    text = (ROOT / "CLAUDE.md").read_text()
    assert "Directory-level `CLAUDE.md` files under `src/theforge/`" in text
    assert "`CONVENTIONS.md` at the repo root is the canonical home" in text
    for subsystem in SUBSYSTEMS:
        assert f"`{subsystem}/`" in text


def test_root_conventions_doc_exists_and_records_memory_boundary() -> None:
    text = (ROOT / "CONVENTIONS.md").read_text()
    assert "Project conventions live in the repo." in text
    assert "User memory stays for operator-local" in text
    assert "docs/memory-migration.md" in text


def test_memory_migration_audit_doc_exists() -> None:
    text = (ROOT / "docs" / "memory-migration.md").read_text()
    assert "Project-level files moved into `CONVENTIONS.md`" in text
    assert "User-local files retained in user memory" in text
