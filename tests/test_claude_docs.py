from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBSYSTEMS = ["coordinator", "runners", "sprint", "task", "config", "cli"]
REQUIRED_SECTIONS = ["## Purpose", "## Invariants", "## Context"]


def test_major_subsystems_have_guidance_docs() -> None:
    for subsystem in SUBSYSTEMS:
        base = ROOT / "src" / "theforge" / subsystem
        assert (base / "CLAUDE.md").exists(), f"missing CLAUDE.md for {subsystem}"
        assert (base / "CONVENTIONS.md").exists(), f"missing CONVENTIONS.md for {subsystem}"


def test_subsystem_conventions_docs_have_required_sections() -> None:
    for subsystem in SUBSYSTEMS:
        text = (ROOT / "src" / "theforge" / subsystem / "CONVENTIONS.md").read_text()
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{subsystem} missing section {section}"


def test_subsystem_claude_docs_point_to_conventions() -> None:
    for subsystem in SUBSYSTEMS:
        text = (ROOT / "src" / "theforge" / subsystem / "CLAUDE.md").read_text()
        assert (
            "See `CONVENTIONS.md` in this directory for subsystem invariants and context." in text
        )
        assert "## Claude-specific notes" in text


def test_root_agent_docs_point_to_conventions() -> None:
    for filename, notes_heading in (
        ("CLAUDE.md", "## Claude-specific notes"),
        ("AGENTS.md", "## Codex-specific notes"),
    ):
        text = (ROOT / filename).read_text()
        assert (
            "See `CONVENTIONS.md` for all project conventions, architecture, testing "
            "rules, and workflow." in text
        )
        assert (
            "Directory-level `CONVENTIONS.md` files under `src/theforge/` provide "
            "subsystem-local guidance." in text
        )
        assert notes_heading in text
        assert (
            "Do not modify `CLAUDE.md` or `AGENTS.md` unless the story explicitly "
            "requires it." in text
        )


def test_root_conventions_doc_exists_and_records_shared_guidance() -> None:
    text = (ROOT / "CONVENTIONS.md").read_text()
    assert "Project conventions live in the repo." in text
    assert "User memory stays for operator-local" in text
    assert "docs/memory-migration.md" in text
    assert "## Architecture" in text
    assert "### Language and toolchain agnosticism" in text
    assert "## Testing" in text
    assert "## Cutting a Release" in text


def test_memory_migration_audit_doc_exists() -> None:
    text = (ROOT / "docs" / "memory-migration.md").read_text()
    assert "Project-level files moved into `CONVENTIONS.md`" in text
    assert "User-local files retained in user memory" in text
