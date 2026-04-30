from theforge.task import build_preflight_prompt
from theforge.task.story import TaskStory


def test_build_preflight_prompt_requests_likely_files() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "likely_files:" in prompt
    assert "repo-relative file path likely to be edited if implementation proceeds" in prompt
    assert "Use `likely_files: []` when no likely edit targets can be identified." in prompt


def test_build_preflight_prompt_requires_baseline_branch_for_already_done() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "configured target baseline branch" in prompt
    assert "`config.workspace.base_branch`" in prompt
    assert "not the resumed worktree contents" in prompt


def test_build_preflight_prompt_bounded_forensic_classifier_role() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "bounded forensic classifier" in prompt
    assert "You are NOT reviewing code quality" in prompt
    assert "You are NOT planning implementation" in prompt
    assert "You are NOT critiquing requirements" in prompt


def test_build_preflight_prompt_proceed_is_default() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "PROCEED is the default verdict" in prompt


def test_build_preflight_prompt_ambiguity_is_not_blocked() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "Ambiguity is NOT grounds for BLOCKED" in prompt
    assert "needs_planning" in prompt


def test_build_preflight_prompt_investigation_budget_section() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "Investigation Budget" in prompt
    assert "3-5 files" in prompt


def test_build_preflight_prompt_files_checked_evidence_map() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "files_checked:" in prompt
    assert "evidence map" in prompt


def test_build_preflight_prompt_output_under_uncertainty() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "You MUST always produce output" in prompt
    assert "Withholding output" in prompt


def test_build_preflight_prompt_marks_concurrency_and_multiphase_work_as_large() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "Treat the following as **LARGE (score 8+) even if the local edit count" in prompt
    assert "Concurrency control" in prompt
    assert "Lifecycle or cancellation propagation across module boundaries" in prompt
    assert "Multi-phase state-machine modifications" in prompt
    assert "Cross-module coordinator surgery" in prompt
    assert "regardless of provider model, story wording, product area, or" in prompt
