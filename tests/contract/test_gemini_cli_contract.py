"""Gemini CLI contract tests."""

from __future__ import annotations

import pytest

from tests.conftest import require_cli
from tests.contract.conftest import assert_cli_accepts_argv
from theforge.config import ModelProfile
from theforge.runners import runner_gemini

pytestmark = pytest.mark.cli_contract


def _profile(**kwargs) -> ModelProfile:
    defaults = dict(
        name="gemini-contract",
        cli="gemini",
        model="gemini-2.5-pro",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=(),
        sandbox_mode="none",
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


def _replace_npx_with_gemini(argv: list[str]) -> list[str]:
    if argv[:2] == ["npx", "@google/gemini-cli"]:
        return ["gemini", *argv[2:]]
    return argv


@pytest.mark.parametrize(
    "shape,builder",
    [
        (
            "fresh",
            lambda: runner_gemini.build_argv(profile=_profile(), prompt="contract-check"),
        ),
        (
            "resume",
            lambda: runner_gemini.build_argv(
                profile=_profile(), prompt="contract-check", session_id="latest"
            ),
        ),
    ],
)
def test_gemini_argv_accepted(shape: str, builder) -> None:
    require_cli("gemini")
    argv = _replace_npx_with_gemini(builder())
    assert_cli_accepts_argv(f"gemini/{shape}", argv)
