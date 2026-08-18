from pathlib import Path

from theforge.sprint.preserved_resume import (
    PRESERVED_REVIEW_COMMAND,
    preserved_review_command,
)

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
    # AGENTS.md is the AI-agnostic master; CLAUDE.md and GEMINI.md redirect to it.
    def _norm(name: str) -> str:
        return " ".join((ROOT / name).read_text().split())

    agents = _norm("AGENTS.md")
    assert "CONVENTIONS.md" in agents
    assert (
        "Directory-level `CONVENTIONS.md` files under `src/theforge/` provide "
        "subsystem-local guidance." in agents
    )
    assert "docs/guides/controller-runbook.md" in agents
    assert (
        "Do not modify `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md` unless the task "
        "explicitly requires it." in agents
    )

    for filename in ("CLAUDE.md", "GEMINI.md"):
        text = _norm(filename)
        assert "`AGENTS.md`" in text, f"{filename} must redirect to AGENTS.md"
        assert "CONVENTIONS.md" in text
        assert "docs/guides/controller-runbook.md" in text


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


def test_controller_runbook_preserved_section_matches_runtime_guidance() -> None:
    text = (ROOT / "docs" / "guides" / "controller-runbook.md").read_text()
    preserved_section = text.split("### PRESERVED", 1)[1].split("### Auth readiness gate", 1)[0]

    assert (
        f"resolve with `{preserved_review_command(path='Issue #2475', slug='issue-2475')}`"
        in preserved_section
    )
    assert f"resolve with `{PRESERVED_REVIEW_COMMAND}`" in preserved_section
    assert "forge run --resume <story-file>" not in preserved_section
