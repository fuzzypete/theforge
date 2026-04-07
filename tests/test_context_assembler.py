from __future__ import annotations

from pathlib import Path

from theforge.config.load import load_config
from theforge.task import ContextAssembler


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_context_assembler_includes_invariants_even_over_budget(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md",
        "- src/theforge/task: prompt builders and task parsing\n",
    )
    _write_file(
        tmp_path / "src" / "theforge" / "task" / "CLAUDE.md",
        (
            "# Task\n\n## Invariants\n\n- invariant one\n- invariant two\n\n"
            "## Context\n\nHelpful advisory line.\n"
        ),
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="dev",
        story_text="Update task prompt builders",
        file_list=["src/theforge/task/dev_prompts.py"],
        budget=1,
    )

    assert "## Invariants" in pack.content
    assert any(entry.required for entry in pack.included)
    assert pack.budget == 1
    assert pack.line_count > pack.budget


def test_context_assembler_ranks_and_truncates_advisory_context(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md", "- src/theforge/task: task prompt builders\n"
    )
    _write_file(
        tmp_path / "src" / "theforge" / "task" / "CLAUDE.md",
        "# Task\n\n## Context\n\nTask prompt builders task prompt builders\n",
    )
    _write_file(
        tmp_path / "src" / "theforge" / "coordinator" / "CLAUDE.md",
        "# Coordinator\n\n## Context\n\nUnrelated scheduler details\n",
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="preflight",
        story_text="task prompt builders",
        file_list=["src/theforge/task/dev_prompts.py", "src/theforge/coordinator/dev_phase.py"],
        budget=6,
    )

    included_sources = {entry.source for entry in pack.included}
    dropped_sources = {entry.source for entry in pack.dropped}
    assert "src/theforge/task/CLAUDE.md" in included_sources
    assert "src/theforge/coordinator/CLAUDE.md" in dropped_sources


def test_context_assembler_uses_story_keywords_for_preflight_scope(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md",
        (
            "- src/theforge/task: prompt builders and task parsing\n"
            "- src/theforge/coordinator: state machine\n"
        ),
    )
    _write_file(
        tmp_path / "src" / "theforge" / "task" / "CLAUDE.md",
        "# Task\n\n## Context\n\nPrompt builders live here.\n",
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="preflight",
        story_text="Need prompt builders updates",
        budget=50,
    )

    assert any(entry.source == "src/theforge/task/CLAUDE.md" for entry in pack.included)


def test_load_config_parses_context_budgets(tmp_path: Path) -> None:
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(
        """
project: demo
context:
  preflight_budget: 321
  plan_budget: 123
  dev_budget: 45
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.context.preflight_budget == 321
    assert config.context.plan_budget == 123
    assert config.context.dev_budget == 45


def test_context_assembler_extracts_generic_paths_from_structural_index(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md",
        "- app/services/payments/: payment orchestration and retries\n",
    )
    _write_file(
        tmp_path / "app" / "services" / "payments" / "CLAUDE.md",
        "# Payments\n\n## Context\n\nPayment orchestration lives here.\n",
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="preflight",
        story_text="Need payment orchestration retries",
        budget=50,
    )

    assert any(entry.source == "app/services/payments/CLAUDE.md" for entry in pack.included)
