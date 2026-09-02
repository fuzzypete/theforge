from __future__ import annotations

from pathlib import Path

import yaml

from theforge.config.load import load_config
from theforge.task import ContextAssembler

_PRIOR_STORY = "Refactor the sprint runner retry loop"
_PRIOR_FILES = ["src/theforge/sprint/runner.py"]


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_prior_run_corpus(
    root: Path, run_id: str = "4f2a91c", *, admissible: bool = True
) -> None:
    """Write a knowledge index + summary artifact matching the prior-run story."""
    verdict = (
        {"status": "admissible", "rank": "full"}
        if admissible
        else {
            "status": "inadmissible",
            "rank": "excluded",
            "reasons": ["cited_source_deleted"],
        }
    )
    _write_file(
        root / ".forge" / "knowledge" / "index.yaml",
        yaml.safe_dump(
            {
                "schema_version": 2,
                "source_count": 1,
                "indexed_count": 1,
                "skipped_count": 0,
                "entries": [
                    {
                        "run_id": run_id,
                        "generated_at": "2026-08-01T00:00:00",
                        "story": {
                            "slug": run_id,
                            "name": "Sprint runner retry",
                            "github_issue": 1,
                        },
                        "story_shape": {"work_type": "refactor", "complexity": "medium"},
                        "domains": ["sprint"],
                        "changed_files": ["src/theforge/sprint/runner.py"],
                        "learned_patterns": ["retry-decorator"],
                        "summary_path": f".forge/knowledge/summaries/{run_id}.yaml",
                        "admissibility_verdict": verdict,
                    }
                ],
            },
            sort_keys=False,
        ),
    )
    _write_file(
        root / ".forge" / "knowledge" / "summaries" / f"{run_id}.yaml",
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": run_id,
                "what_changed": {
                    "description": "reworked the sprint runner retry loop",
                    "approach": "extracted a bounded helper",
                    "files_modified": ["src/theforge/sprint/runner.py"],
                },
                "what_was_learned": [
                    {
                        "claim": "retries need a jitter cap",
                        "evidence": [{"type": "file", "path": "src/theforge/sprint/runner.py"}],
                    }
                ],
                "review_insights": {
                    "recurring_findings": [
                        {
                            "finding_id": "f-007",
                            "description": "missing timeout coverage",
                            "cycles_seen": 2,
                        }
                    ],
                    "resolved_findings": [
                        {
                            "finding_id": "f-003",
                            "description": "race condition",
                            "resolution": "guarded helper",
                        }
                    ],
                    "observations": ["verify the timeout branch"],
                },
                "complexity_signal": {
                    "actual_iterations": 2,
                    "review_cycles": 2,
                    "plan_regenerations": 1,
                    "cost_usd": 4.25,
                    "dominant_difficulty": "edge case coverage",
                },
                "story_shape": {
                    "work_type": "refactor",
                    "complexity": "medium",
                    "complexity_score": 6,
                    "contract_change": False,
                },
            },
            sort_keys=False,
        ),
    )


def _config_with_prior_run_context(root: Path, *, enabled: bool) -> object:
    config_path = root / "forge.yaml"
    config_path.write_text(
        f"project: demo\nknowledge:\n  prior_run_context: {str(enabled).lower()}\n",
        encoding="utf-8",
    )
    return load_config(config_path)


def test_context_assembler_includes_invariants_even_over_budget(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md",
        "- src/theforge/task: prompt builders and task parsing\n",
    )
    _write_file(
        tmp_path / "src" / "theforge" / "task" / "CONVENTIONS.md",
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


def test_context_assembler_loads_claude_and_conventions_from_same_directory(
    tmp_path: Path,
) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md",
        "- src/theforge/task: prompt builders and task parsing\n",
    )
    _write_file(
        tmp_path / "src" / "theforge" / "task" / "CLAUDE.md",
        "# Task\n\n## Context\n\nClaude-specific advisory.\n",
    )
    _write_file(
        tmp_path / "src" / "theforge" / "task" / "CONVENTIONS.md",
        "# Task\n\n## Invariants\n\n- shared invariant\n\n## Context\n\nShared advisory.\n",
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="dev",
        story_text="Update task prompt builders",
        file_list=["src/theforge/task/dev_prompts.py"],
        budget=50,
    )

    included_sources = {entry.source for entry in pack.included}
    assert "src/theforge/task/CLAUDE.md" in included_sources
    assert "src/theforge/task/CONVENTIONS.md" in included_sources


def test_context_assembler_uses_story_keywords_for_preflight_scope(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md",
        (
            "- src/theforge/task: prompt builders and task parsing\n"
            "- src/theforge/coordinator: state machine\n"
        ),
    )
    _write_file(
        tmp_path / "src" / "theforge" / "task" / "CONVENTIONS.md",
        "# Task\n\n## Context\n\nPrompt builders live here.\n",
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="preflight",
        story_text="Need prompt builders updates",
        budget=50,
    )

    assert any(entry.source == "src/theforge/task/CONVENTIONS.md" for entry in pack.included)


def test_load_config_parses_context_budgets(tmp_path: Path) -> None:
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(
        """
project: demo
context:
  preflight_budget: 321
  plan_budget: 123
  dev_budget: 45
  review_budget: 67
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.context.preflight_budget == 321
    assert config.context.plan_budget == 123
    assert config.context.dev_budget == 45
    assert config.context.review_budget == 67


def test_context_assembler_extracts_generic_paths_from_structural_index(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md",
        "- app/services/payments/: payment orchestration and retries\n",
    )
    _write_file(
        tmp_path / "app" / "services" / "payments" / "CONVENTIONS.md",
        "# Payments\n\n## Context\n\nPayment orchestration lives here.\n",
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="preflight",
        story_text="Need payment orchestration retries",
        budget=50,
    )

    assert any(entry.source == "app/services/payments/CONVENTIONS.md" for entry in pack.included)


def test_context_manifest_records_types_drop_reason_and_git_sha(tmp_path: Path) -> None:
    _write_file(
        tmp_path / ".forge" / "STRUCTURAL_INDEX.md", "- src/theforge/task: task prompt builders\n"
    )
    _write_file(
        tmp_path / "src" / "theforge" / "CONVENTIONS.md",
        "# TheForge\n\n## Invariants\n\n- must keep\n\n## Context\n\nHelpful advisory line.\n",
    )

    assembler = ContextAssembler(tmp_path)
    pack = assembler.assemble(
        phase="dev",
        story_text="task prompt builders",
        file_list=["src/theforge/task/dev_prompts.py"],
        budget=2,
    )

    assert pack.phase == "dev"
    assert any(entry.item_type == "invariant" for entry in pack.included)
    assert any(entry.drop_reason == "budget exceeded" for entry in pack.dropped)
    assert pack.structural_index_git_sha is None or isinstance(pack.structural_index_git_sha, str)


def test_context_assembler_uses_review_budget_for_review_phase(tmp_path: Path) -> None:
    assembler = ContextAssembler(tmp_path)

    pack = assembler.assemble(phase="review", story_text="review prompt builders")

    assert pack.phase == "review"
    assert pack.budget == assembler.budgets.review_budget


def test_prior_run_summary_is_included_when_enabled_and_relevant(tmp_path: Path) -> None:
    _write_prior_run_corpus(tmp_path)
    config = _config_with_prior_run_context(tmp_path, enabled=True)

    pack = ContextAssembler.from_config(config).assemble(
        phase="dev", story_text=_PRIOR_STORY, file_list=_PRIOR_FILES, budget=100
    )

    assert "Related changed files: src/theforge/sprint/runner.py" in pack.content
    assert "Evidence-backed implementation patterns:" in pack.content
    assert any(entry.kind == "prior_run_summary" for entry in pack.included)
    assert all(
        entry.item_type == "advisory"
        for entry in pack.included
        if entry.kind == "prior_run_summary"
    )
    manifest = pack.prior_run_context
    assert manifest["enabled"] is True
    assert [item["run_id"] for item in manifest["included"]] == ["4f2a91c"]
    assert "file_overlap(src/theforge/sprint/runner.py)" in manifest["included"][0]["reason"]
    assert manifest["included"][0]["verdict"]["status"] == "admissible"
    assert manifest["included"][0]["rendered_size"]["kind"] == "rendered_prompt_contribution"


def test_prior_run_summary_is_disabled_by_default(tmp_path: Path) -> None:
    _write_prior_run_corpus(tmp_path)

    direct = ContextAssembler(tmp_path).assemble(
        phase="dev", story_text=_PRIOR_STORY, file_list=_PRIOR_FILES, budget=100
    )
    from_config = ContextAssembler.from_config(
        _config_with_prior_run_context(tmp_path, enabled=False)
    ).assemble(phase="dev", story_text=_PRIOR_STORY, file_list=_PRIOR_FILES, budget=100)

    for pack in (direct, from_config):
        assert "sprint runner retry loop" not in pack.content
        assert not any(entry.kind == "prior_run_summary" for entry in pack.included)
        assert not any(entry.kind == "prior_run_summary" for entry in pack.dropped)
        assert pack.prior_run_context["enabled"] is False


def test_prior_run_summary_reaches_preflight_as_signal_only_advisory_context(
    tmp_path: Path,
) -> None:
    _write_prior_run_corpus(tmp_path)
    config = _config_with_prior_run_context(tmp_path, enabled=True)

    pack = ContextAssembler.from_config(config).assemble(
        phase="preflight", story_text=_PRIOR_STORY, file_list=_PRIOR_FILES, budget=100
    )

    assert "Preflight note: Advisory prior-run signals only." in pack.content
    assert (
        "Run signals: actual_iterations=2, review_cycles=2, plan_regenerations=1, cost_usd=4.25"
    ) in pack.content
    assert any(entry.kind == "prior_run_summary" for entry in pack.included)
    manifest = pack.prior_run_context
    assert manifest["phase"] == "preflight"
    assert manifest["rendering_mode"] == "signal_only"
    assert manifest["included"][0]["phase"] == "preflight"
    assert manifest["included"][0]["rendering_mode"] == "signal_only"
    assert "reworked the sprint runner retry loop" not in pack.content
    assert "edge case coverage" not in pack.content


def test_inadmissible_prior_summary_is_excluded_with_verdict_in_manifest(tmp_path: Path) -> None:
    _write_prior_run_corpus(tmp_path, admissible=False)
    config = _config_with_prior_run_context(tmp_path, enabled=True)

    pack = ContextAssembler.from_config(config).assemble(
        phase="dev", story_text=_PRIOR_STORY, file_list=_PRIOR_FILES, budget=100
    )

    assert "sprint runner retry loop" not in pack.content
    manifest = pack.prior_run_context
    assert manifest["included"] == []
    assert manifest["dropped"][0]["reason"] == "inadmissible(cited_source_deleted)"
    assert manifest["dropped"][0]["verdict"]["reasons"] == ["cited_source_deleted"]
    assert "excluded on admissibility" in manifest["note"]


def test_prior_run_summary_is_dropped_before_required_context(tmp_path: Path) -> None:
    _write_prior_run_corpus(tmp_path)
    _write_file(
        tmp_path / "src" / "theforge" / "sprint" / "CONVENTIONS.md",
        "# Sprint\n\n## Invariants\n\n- invariant one\n- invariant two\n",
    )
    config = _config_with_prior_run_context(tmp_path, enabled=True)

    pack = ContextAssembler.from_config(config).assemble(
        phase="dev", story_text=_PRIOR_STORY, file_list=_PRIOR_FILES, budget=1
    )

    assert "## Invariants" in pack.content
    assert any(entry.item_type == "invariant" for entry in pack.included)
    dropped = [entry for entry in pack.dropped if entry.kind == "prior_run_summary"]
    assert dropped and dropped[0].drop_reason == "budget_pressure"
    manifest = pack.prior_run_context
    assert manifest["included"] == []
    assert manifest["dropped"][0]["reason"] == "budget_pressure"
    assert manifest["dropped"][0]["phase"] == "dev"
    assert manifest["dropped"][0]["rendering_mode"] == "phase_summary"
    assert "rendered_size" not in manifest["dropped"][0]
    assert "dropped under budget pressure" in manifest["note"]


def test_enabled_run_without_any_prior_knowledge_proceeds_normally(tmp_path: Path) -> None:
    config = _config_with_prior_run_context(tmp_path, enabled=True)

    pack = ContextAssembler.from_config(config).assemble(
        phase="dev", story_text=_PRIOR_STORY, file_list=_PRIOR_FILES, budget=100
    )

    assert pack.prior_run_context["enabled"] is True
    assert pack.prior_run_context["index_state"] == "ready"
    assert pack.prior_run_context["included"] == []
    assert pack.prior_run_context["note"] == (
        "no relevant prior knowledge exists (no indexed summaries)"
    )
