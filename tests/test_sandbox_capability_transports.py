"""Every dev transport either applies a capability declaration or refuses it (#2038).

Project-authored sandbox grants are only trustworthy if the transport that ran
the work actually applied them. Two transports do (claude, gemini — both wrap
their invocation in forge's host sandbox); the rest either bring their own
containment (codex), run the work off-host (gh-aw), or confine tools by path
check rather than by host sandbox (the API tool runtime). For those, the run
must fail closed *before* dispatch, or the audit record ends up claiming a
capability the agent never had — the exact disguise that let a capability gap
reach the operator as a code-quality verdict.

No real provider CLI is launched here: each runner entry point is patched, and
the assertions are about whether it was called at all.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.agent_types import AgentResult
from theforge.config import ModelProfile, TransportFallbackConfig
from theforge.runners.cli import run_agent

_MARKER = "SANDBOX_CAPABILITY_PROFILE_UNSUPPORTED"

_DECLARATION = {
    "sandbox_capability_profile": "xcode",
    "sandbox_write_roots": ("/opt/toolchain",),
    "sandbox_mach_services": ("com.example.toolchaind",),
}


def _profile(**kwargs) -> ModelProfile:
    defaults = dict(
        name="dev-profile",
        cli="codex",
        model="gpt-5.4",
        budget_usd=5.0,
        timeout_seconds=900,
        allowed_tools=("Read",),
        phase="dev",
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


def _success(profile: ModelProfile) -> AgentResult:
    return AgentResult(
        success=True,
        output="done",
        session_id=None,
        cost_usd=0.01,
        exit_code=0,
        raw={},
        profile_name=profile.name,
    )


# ── Transports that cannot express a declaration must refuse ──────────────


@pytest.mark.parametrize(
    ("cli", "runner_path"),
    [
        ("codex", "theforge.runners.runner_codex._run_codex"),
        ("ghaw", "theforge.runners.runner_ghaw._run_ghaw"),
    ],
)
def test_cli_transport_without_a_capability_axis_refuses_before_dispatch(
    tmp_path: Path, cli: str, runner_path: str
) -> None:
    profile = _profile(cli=cli, **_DECLARATION)
    runner = MagicMock(return_value=_success(profile))

    with patch(runner_path, runner):
        result = run_agent(prompt="work", profile=profile, working_dir=tmp_path, quiet=True)

    runner.assert_not_called()
    assert result.success is False
    assert result.startup_failure is True
    assert _MARKER in result.output
    # The refusal names what was asked for, so the operator can act on it.
    assert "xcode" in result.output
    assert "/opt/toolchain" in result.output
    assert "com.example.toolchaind" in result.output


def test_api_transport_refuses_before_dispatch(tmp_path: Path) -> None:
    """The API tool runtime has no write-root or mach-service axis to widen."""
    profile = _profile(cli=None, provider="anthropic", **_DECLARATION)
    api_runner = MagicMock(return_value=_success(profile))

    with patch("theforge.runners.api.run_api_agent", api_runner):
        result = run_agent(prompt="work", profile=profile, working_dir=tmp_path, quiet=True)

    api_runner.assert_not_called()
    assert result.success is False
    assert _MARKER in result.output


def test_api_fallback_from_a_declaring_cli_profile_does_not_fire(tmp_path: Path) -> None:
    """A fail-closed CLI must not be rescued by a transport that drops the grants."""
    profile = _profile(
        cli="claude",
        provider="anthropic",
        api_fallback=TransportFallbackConfig(
            provider="anthropic", model="claude-opus-5", timeout_seconds=900
        ),
        **_DECLARATION,
    )
    cli_failure = AgentResult(
        success=False,
        output="You've hit your usage limit.",
        session_id=None,
        cost_usd=0.0,
        exit_code=1,
        raw={},
        profile_name=profile.name,
    )
    api_runner = MagicMock(return_value=_success(profile))

    with (
        patch("theforge.runners.runner_claude._run_claude", return_value=cli_failure),
        patch("theforge.runners.api.run_api_agent", api_runner),
    ):
        result = run_agent(prompt="work", profile=profile, working_dir=tmp_path, quiet=True)

    api_runner.assert_not_called()
    assert result.transport_fallback_fired is False
    # Why the fallback was withheld is recorded, not merely implied.
    assert result.transport_fallback_not_applied_reason is not None
    assert "cannot express" in result.transport_fallback_not_applied_reason


# ── Nothing declared, or containment opted out: unchanged behaviour ───────


def test_transport_without_a_declaration_dispatches_normally(tmp_path: Path) -> None:
    profile = _profile(cli="codex")
    runner = MagicMock(return_value=_success(profile))

    with patch("theforge.runners.runner_codex._run_codex", runner):
        result = run_agent(prompt="work", profile=profile, working_dir=tmp_path, quiet=True)

    runner.assert_called_once()
    assert result.success is True


def test_sandbox_mode_none_is_an_explicit_opt_out_not_a_refusal(tmp_path: Path) -> None:
    """With containment off there is no boundary to widen, so nothing is denied."""
    profile = _profile(cli="codex", sandbox_mode="none", **_DECLARATION)
    runner = MagicMock(return_value=_success(profile))

    with patch("theforge.runners.runner_codex._run_codex", runner):
        result = run_agent(prompt="work", profile=profile, working_dir=tmp_path, quiet=True)

    runner.assert_called_once()
    assert result.success is True


# ── Transports that CAN express it pass the declaration through ───────────


def test_claude_runner_passes_the_declaration_to_the_sandbox_wrapper(tmp_path: Path) -> None:
    from theforge.runners import runner_claude

    profile = _profile(cli="claude", provider="anthropic", **_DECLARATION)
    wrapper = MagicMock(side_effect=RuntimeError("stop after wrapping"))

    with (
        patch.object(runner_claude, "workspace_effect_sandbox_command", wrapper),
        pytest.raises(RuntimeError, match="stop after wrapping"),
    ):
        runner_claude._run_claude(prompt="work", profile=profile, working_dir=tmp_path, quiet=True)

    kwargs = wrapper.call_args.kwargs
    assert kwargs["capability_profile"] == "xcode"
    assert kwargs["capability_write_roots"] == ("/opt/toolchain",)
    assert kwargs["capability_mach_services"] == ("com.example.toolchaind",)


def test_gemini_runner_passes_the_declaration_to_the_sandbox_wrapper(tmp_path: Path) -> None:
    from theforge.runners import runner_gemini

    profile = _profile(cli="gemini", provider="google", **_DECLARATION)
    wrapper = MagicMock(side_effect=RuntimeError("stop after wrapping"))

    with (
        patch.object(runner_gemini, "workspace_effect_sandbox_command", wrapper),
        pytest.raises(RuntimeError, match="stop after wrapping"),
    ):
        runner_gemini._run_gemini(prompt="work", profile=profile, working_dir=tmp_path, quiet=True)

    kwargs = wrapper.call_args.kwargs
    assert kwargs["capability_profile"] == "xcode"
    assert kwargs["capability_write_roots"] == ("/opt/toolchain",)
    assert kwargs["capability_mach_services"] == ("com.example.toolchaind",)
