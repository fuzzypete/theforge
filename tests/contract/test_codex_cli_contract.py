"""Codex CLI contract tests.

Validates that argv constructed by the runner is accepted by the installed
`codex` CLI on both fresh-run and resume paths. The 2026-04-24 incident
(#1011 + sibling) was exactly the resume-path argv contract drifting unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import require_cli
from tests.contract.conftest import assert_cli_accepts_argv
from theforge.config import ModelProfile
from theforge.runners import runner_codex

# NOTE: the cli_contract marker is applied per-test, not module-wide, because the
# JSON-mode contract below is asserted against argv and a recorded payload rather
# than a spawned binary — so it belongs in the default gate (repo invariant: no
# real provider CLI calls there), while the argv-acceptance test stays opt-in.


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
    """Substitute the installed `codex` binary for `npx @openai/codex@<pin>`.

    Avoids pulling a fresh package via npx on every CI run while still
    exercising the same argv shape the runner produces.

    Matches the package spec by prefix so the runner's version pin does not
    silently stop matching here — a mismatch would fall through and spawn a real
    npx download instead of the local binary. Note the tradeoff this substitution
    makes: it validates the argv *shape* against whatever codex happens to be
    installed, not against the pinned version the runner actually invokes. A
    flag removed in the pinned release is therefore not caught here.
    """
    if argv[:1] == ["npx"] and argv[1:2] and argv[1].split("@openai/codex")[0] == "":
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
@pytest.mark.cli_contract
def test_codex_argv_accepted(shape: str, builder) -> None:
    require_cli("codex")
    argv = _replace_npx_with_codex(builder())
    assert_cli_accepts_argv(f"codex/{shape}", argv)


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
            "exec_resume",
            lambda: runner_codex.build_resume_argv(
                profile=_profile(),
                output_file=Path("/tmp/contract-out.txt"),
                session_id="00000000-0000-0000-0000-000000000000",
            ),
        ),
    ],
)
def test_codex_invoked_in_json_event_mode(shape: str, builder) -> None:
    """Forge must ask codex for machine-readable events on BOTH paths (#2019).

    Cost measurement depends entirely on the `turn.completed` usage event, which
    only exists in `--json` mode. Parametrizing resume alongside fresh is
    deliberate: the resume subcommand already rejects flags the fresh path accepts
    (`--sandbox`), so a future CLI that also stops accepting `--json` there would
    otherwise silently return codex to cost-blind on resumed dev iterations.
    """
    argv = builder()
    assert "--json" in argv


def test_fresh_exec_skips_the_git_repo_trust_gate() -> None:
    """Forge must ask codex to skip its git-repo trust check (#2164).

    Forge launches agents against the ``git archive | tar -x`` baseline checkout,
    which has no ``.git`` at all. Without this flag codex refuses to start there
    ("Not inside a trusted directory…") and exits before reaching the model, so
    every escalation-advisor / preflight run routed to a Codex model produces no
    output. Pinned on argv (no binary spawned) so it runs in the default gate; if
    a future CLI renames the flag the cli_contract test above catches that.
    """
    argv = runner_codex.build_argv(
        profile=_profile(),
        working_dir=Path("/tmp"),
        output_file=Path("/tmp/contract-out.txt"),
        prompt="contract-check",
    )
    assert "--skip-git-repo-check" in argv


# Codex `turn.completed.usage` fields observed on the real CLI. The parser must
# accept this shape without depending on any human-readable summary text.
_OBSERVED_TURN_COMPLETED = {
    "type": "turn.completed",
    "usage": {
        "input_tokens": 12892,
        "cached_input_tokens": 9088,
        "cache_write_input_tokens": 0,
        "output_tokens": 21,
        "reasoning_output_tokens": 14,
    },
}


def test_parser_accepts_observed_turn_completed_usage_shape() -> None:
    """Lock the event shape the extraction path is built against.

    Runs without the real binary on purpose (repo invariant: no real provider CLI
    calls in the default gate). What it pins is the payload contract — if codex
    renames these fields, this fails here rather than as silent cost-unknown.
    """
    usage = runner_codex._usage_from_events(json.dumps(_OBSERVED_TURN_COMPLETED))
    assert usage is not None
    assert usage.input_tokens == 12892
    assert usage.cached_input_tokens == 9088
    assert usage.cache_write_input_tokens == 0
    assert usage.output_tokens == 21
    assert usage.reasoning_output_tokens == 14
