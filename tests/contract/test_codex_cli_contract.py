"""Codex CLI contract tests.

Validates that argv constructed by the runner is accepted by the installed
`codex` CLI on both fresh-run and resume paths. The 2026-04-24 incident
(#1011 + sibling) was exactly the resume-path argv contract drifting unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import require_cli
from tests.contract.conftest import assert_cli_accepts_argv
from theforge.config import ModelProfile
from theforge.runners import runner_codex

pytestmark = pytest.mark.cli_contract


def _profile(**kwargs) -> ModelProfile:
    defaults = dict(
        name="codex-contract",
        cli="codex",
        model="o4-mini",
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=(),
        sandbox_mode="workspace-write",
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


def _replace_npx_with_codex(argv: list[str]) -> list[str]:
    """Substitute the installed `codex` binary for `npx @openai/codex`.

    Avoids pulling a fresh package via npx on every CI run while still
    exercising the same argv shape the runner produces.
    """
    if argv[:2] == ["npx", "@openai/codex"]:
        return ["codex", *argv[2:]]
    return argv


@pytest.mark.parametrize(
    "shape,builder",
    [
        (
            "exec_fresh",
            lambda: runner_codex.build_argv(
                profile=_profile(),
                working_dir=Path("/tmp"),
                output_file=Path("/tmp/contract-out.txt"),
                prompt="contract-check",
            ),
        ),
        (
            "exec_fresh_no_sandbox",
            lambda: runner_codex.build_argv(
                profile=_profile(sandbox_mode="none"),
                working_dir=Path("/tmp"),
                output_file=Path("/tmp/contract-out.txt"),
                prompt="contract-check",
            ),
        ),
        (
            "exec_fresh_with_reasoning",
            lambda: runner_codex.build_argv(
                profile=_profile(reasoning_effort="medium"),
                working_dir=Path("/tmp"),
                output_file=Path("/tmp/contract-out.txt"),
                prompt="contract-check",
            ),
        ),
        (
            "exec_resume",
            lambda: runner_codex.build_resume_argv(
                profile=_profile(),
                output_file=Path("/tmp/contract-out.txt"),
                session_id="00000000-0000-0000-0000-000000000000",
            ),
        ),
        (
            "exec_resume_with_reasoning",
            lambda: runner_codex.build_resume_argv(
                profile=_profile(reasoning_effort="high"),
                output_file=Path("/tmp/contract-out.txt"),
                session_id="00000000-0000-0000-0000-000000000000",
            ),
        ),
        (
            # Reassertion contract: `-c sandbox_mode=read-only` under
            # `--strict-config` must be accepted on the resume path (issue #1012).
            "exec_resume_read_only",
            lambda: runner_codex.build_resume_argv(
                profile=_profile(sandbox_mode="read-only"),
                output_file=Path("/tmp/contract-out.txt"),
                session_id="00000000-0000-0000-0000-000000000000",
            ),
        ),
        (
            "exec_resume_no_sandbox",
            lambda: runner_codex.build_resume_argv(
                profile=_profile(sandbox_mode="none"),
                output_file=Path("/tmp/contract-out.txt"),
                session_id="00000000-0000-0000-0000-000000000000",
            ),
        ),
    ],
)
def test_codex_argv_accepted(shape: str, builder) -> None:
    require_cli("codex")
    argv = _replace_npx_with_codex(builder())
    assert_cli_accepts_argv(f"codex/{shape}", argv)
