"""Claude CLI contract tests."""

from __future__ import annotations

import pytest

from tests.conftest import require_cli
from tests.contract.conftest import assert_cli_accepts_argv
from theforge.config import ModelProfile
from theforge.runners import runner_claude

pytestmark = pytest.mark.cli_contract


def _profile(**kwargs) -> ModelProfile:
    defaults = dict(
        name="claude-contract",
        cli="claude",
        model="sonnet",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=("Read", "Bash"),
        sandbox_mode="workspace-write",
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


@pytest.mark.parametrize(
    "shape,builder",
    [
        ("fresh", lambda: runner_claude.build_argv(profile=_profile())),
        (
            "fresh_no_sandbox",
            lambda: runner_claude.build_argv(profile=_profile(sandbox_mode="none")),
        ),
        (
            "fresh_no_tools",
            lambda: runner_claude.build_argv(profile=_profile(allowed_tools=())),
        ),
        (
            "resume",
            lambda: runner_claude.build_argv(
                profile=_profile(), session_id="00000000-0000-0000-0000-000000000000"
            ),
        ),
    ],
)
def test_claude_argv_accepted(shape: str, builder) -> None:
    require_cli("claude")
    argv = builder()
    # Claude blocks until stdin closes; sending an empty stdin lets it parse
    # the argv and exit cleanly without making a model call.
    assert_cli_accepts_argv(f"claude/{shape}", argv, stdin_input="")
