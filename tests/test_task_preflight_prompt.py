from theforge.task import build_preflight_prompt
from theforge.task.story import TaskStory


def test_build_preflight_prompt_requests_likely_files() -> None:
    task = TaskStory(name="Bug fix", slug="bug-fix")

    prompt = build_preflight_prompt(task, story_content="## Spec\n\n- Example")

    assert "likely_files:" in prompt
    assert "repo-relative file path likely to be edited if implementation proceeds" in prompt
    assert "Use `likely_files: []` when no likely edit targets can be identified." in prompt
