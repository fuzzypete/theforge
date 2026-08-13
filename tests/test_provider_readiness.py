from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.cli.provider_readiness import (
    READINESS_CAPABILITY_TOOL_STRUCTURED,
    READINESS_STATUS_FAILED,
    READINESS_STATUS_READY,
    READINESS_STATUS_UNSUPPORTED,
    READINESS_STATUS_UNVERIFIED,
    build_readiness_probes,
    run_readiness_probe,
)
from theforge.config import (
    DEFAULT_VALIDATION,
    AgentDef,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
    transport_for,
)
from theforge.runners import AgentResult
from theforge.runners.schema_utils import OpenAIFunctionToolRequestShape


def _api_profile(
    name: str,
    *,
    provider: str = "openai",
    model: str = "gpt-5.4",
    allowed_tools: tuple[str, ...] = ("Read", "Grep"),
    phase: str | None = None,
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=allowed_tools,
        phase=phase,
    )


def _cli_profile(
    name: str,
    *,
    cli: str = "claude",
    model: str = "sonnet",
    allowed_tools: tuple[str, ...] = ("Read", "Grep"),
    phase: str | None = None,
    fallback_models: tuple[str, ...] = (),
) -> ModelProfile:
    return ModelProfile(
        name=name,
        cli=cli,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=allowed_tools,
        phase=phase,
        fallback_models=fallback_models,
    )


def _config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=_api_profile(
            "preflight",
            provider="openai",
            model="gpt-5.4",
            allowed_tools=("Read", "Glob", "Grep"),
            phase="preflight",
        ),
        review_pool=[_api_profile("reviewer", phase="review")],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan=PlanConfig.of(
            enabled=True,
            cli=None,
            provider="openai",
            model="gpt-5.4",
            budget_usd=0.5,
            timeout=300,
            transport=transport_for("openai", "api"),
        ),
        plan_agent_review=PlanAgentReviewConfig(
            enabled=True,
            pool=[_api_profile("plan-reviewer", phase="plan_review")],
        ),
        log=LogConfig(enabled=False),
    )


def _pass_result(
    profile_name: str = "test",
    *,
    cost_usd: float | None = 0.003,
    structured: bool = True,
    transport_used: str | None = None,
    model_used: str | None = None,
) -> AgentResult:
    structured_data = {"verdict": "APPROVE", "summary": "ok"} if structured else None
    output = '{"verdict":"APPROVE","summary":"ok"}' if structured else "Capability probe complete."
    return AgentResult(
        success=True,
        output=output,
        session_id=None,
        cost_usd=cost_usd,
        exit_code=0,
        raw={},
        profile_name=profile_name,
        structured_data=structured_data,
        transport_used=transport_used,
        model_used=model_used,
    )


def _attempt(result, transport_kind: str):
    return next(
        attempt
        for attempt in result.attempts
        if attempt.attempted_transport_kind == transport_kind
    )


def test_build_readiness_probes_includes_config_plan_and_advisor_shapes(tmp_path):
    config = _config(tmp_path)

    probes = build_readiness_probes(config)
    labels = {(probe.role, probe.profile.name, probe.profile.phase) for probe in probes}

    assert ("dev", "dev", "dev") in labels
    assert ("preflight", "preflight", "preflight") in labels
    assert ("review", "reviewer", "review") in labels
    assert ("plan", "plan", "plan") in labels
    assert ("plan-review", "plan-reviewer", "plan_review") in labels
    assert ("advisor", "preflight", "preflight") in labels


def test_build_readiness_probes_dedupes_exact_duplicates_but_not_roles(tmp_path):
    shared = _api_profile("shared")
    config = _config(tmp_path)
    config = ForgeConfig(
        **{
            **config.__dict__,
            "review_pool": [shared, shared],
            "plan_agent_review": PlanAgentReviewConfig(enabled=True, pool=[shared]),
        }
    )

    probes = build_readiness_probes(config)
    shared_rows = [probe for probe in probes if probe.profile.name == "shared"]

    assert len([probe for probe in shared_rows if probe.role == "review"]) == 1
    assert len([probe for probe in shared_rows if probe.role == "plan-review"]) == 1


def test_build_readiness_probes_projects_agent_pool_roles_with_tools(tmp_path):
    config = _config(tmp_path)
    config = ForgeConfig(
        **{
            **config.__dict__,
            "review_pool": [],
            "agents": [
                AgentDef(
                    name="pool-openai",
                    provider="openai",
                    model="gpt-5.4",
                    budget_usd=1.0,
                    timeout_seconds=120,
                    tier="mid",
                    transport=transport_for("openai", "api"),
                )
            ],
        }
    )

    probes = build_readiness_probes(config)
    agent_probes = {probe.role: probe for probe in probes if probe.profile.name == "pool-openai"}

    assert set(agent_probes) == {
        "agent-dev",
        "agent-preflight",
        "agent-planner",
        "agent-plan-review",
        "agent-code-review",
    }
    assert all(agent_probes[role].profile.allowed_tools for role in agent_probes)
    assert agent_probes["agent-dev"].profile.allowed_tools == ("Read", "Glob", "Grep")
    assert agent_probes["agent-dev"].capability == READINESS_CAPABILITY_TOOL_STRUCTURED
    assert agent_probes["agent-planner"].profile.phase == "plan"
    assert agent_probes["agent-plan-review"].profile.phase == "plan_review"
    assert agent_probes["agent-code-review"].profile.phase == "review"
    assert agent_probes["agent-planner"].profile != agent_probes["agent-code-review"].profile


def test_build_readiness_probes_narrows_configured_dev_api_tools_by_phase(tmp_path):
    config = _config(tmp_path)
    config = ForgeConfig(
        **{
            **config.__dict__,
            "dev_profile": _api_profile(
                "dev-api",
                allowed_tools=("Read", "Write", "Bash", "Glob", "Grep"),
            ),
        }
    )

    probes = build_readiness_probes(config)
    dev_probe = next(probe for probe in probes if probe.role == "dev")

    assert dev_probe.profile.phase == "dev"
    assert dev_probe.profile.allowed_tools == ("Read", "Glob", "Grep")


def test_run_readiness_probe_marks_openai_unsupported_shape_not_ready(tmp_path):
    probe = next(
        probe for probe in build_readiness_probes(_config(tmp_path)) if probe.role == "review"
    )

    with (
        patch(
            "theforge.cli.provider_readiness.openai_function_tool_request_shape",
            return_value=OpenAIFunctionToolRequestShape("unsupported"),
        ),
        patch("theforge.cli.provider_readiness.run_api_agent") as mock_api,
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(transport_used="cli"),
        ),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_UNSUPPORTED
    assert not result.ready
    mock_api.assert_not_called()
    assert _attempt(result, "api").status == READINESS_STATUS_UNSUPPORTED


def test_run_readiness_probe_marks_successful_probe_ready(tmp_path):
    probe = next(
        probe for probe in build_readiness_probes(_config(tmp_path)) if probe.role == "review"
    )

    with (
        patch("theforge.cli.provider_readiness.run_api_agent", return_value=_pass_result()),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(transport_used="cli"),
        ),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_READY
    assert result.ready is True
    assert {attempt.attempted_transport_kind for attempt in result.attempts} == {"api", "cli"}


def test_run_readiness_probe_uses_throwaway_working_dir(tmp_path):
    probe = next(
        probe for probe in build_readiness_probes(_config(tmp_path)) if probe.role == "review"
    )

    with (
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_pass_result(),
        ) as mock_api,
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(transport_used="cli"),
        ),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_READY
    called_dir = mock_api.call_args.kwargs["working_dir"]
    assert called_dir != tmp_path
    assert called_dir.name.startswith("forge-check-providers-")


def test_run_readiness_probe_no_submit_phase_accepts_unstructured_success(tmp_path):
    probe = next(
        probe for probe in build_readiness_probes(_config(tmp_path)) if probe.role == "preflight"
    )

    with (
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            side_effect=[
                _pass_result(structured=False),
                _pass_result(transport_used="api"),
            ],
        ),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(structured=False, transport_used="cli"),
        ),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_READY
    assert result.ready is True


def test_run_readiness_probe_exercises_declared_cli_profile_and_api_alternate(tmp_path):
    probe = next(
        probe for probe in build_readiness_probes(_config(tmp_path)) if probe.role == "dev"
    )

    with (
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(structured=False, transport_used="cli"),
        ) as mock_cli,
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_pass_result(structured=False, transport_used="api"),
        ) as mock_api,
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_READY
    assert {attempt.attempted_transport_kind for attempt in result.attempts} == {"cli", "api"}
    assert mock_cli.call_args.kwargs["profile"].mode == "cli"
    assert mock_cli.call_args.kwargs["profile"].api_fallback is None
    assert mock_cli.call_args.kwargs["profile"].fallback_models == ()
    assert mock_api.call_args.kwargs["profile"].mode == "api"
    assert mock_api.call_args.kwargs["profile"].api_fallback is None
    assert mock_api.call_args.kwargs["profile"].fallback_models == ()


def test_run_readiness_probe_preserves_declared_explicit_cli_runner(tmp_path):
    config = _config(tmp_path)
    config = ForgeConfig(
        **{
            **config.__dict__,
            "dev_profile": _cli_profile("dev", cli="ghaw", model="sonnet"),
        }
    )
    probe = next(probe for probe in build_readiness_probes(config) if probe.role == "dev")

    with (
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(structured=False, transport_used="cli"),
        ) as mock_cli,
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_pass_result(structured=False, transport_used="api"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    declared_profile = mock_cli.call_args.kwargs["profile"]
    assert result.status == READINESS_STATUS_READY
    assert declared_profile.cli == "ghaw"
    assert declared_profile.transport is not None
    assert declared_profile.transport.runner == "ghaw"
    assert declared_profile.transport.executable == "gh"


def test_run_readiness_probe_reports_unavailable_alternate_transport_unverified(tmp_path):
    config = _config(tmp_path)
    config = ForgeConfig(
        **{
            **config.__dict__,
            "review_pool": [
                _api_profile(
                    "deepseek-reviewer",
                    provider="deepseek",
                    model="deepseek-chat",
                    phase="review",
                )
            ],
        }
    )
    probe = next(probe for probe in build_readiness_probes(config) if probe.role == "review")

    with patch("theforge.cli.provider_readiness.run_api_agent", return_value=_pass_result()):
        result = run_readiness_probe(probe, secrets={})

    alternate = _attempt(result, "cli")
    assert result.status == READINESS_STATUS_READY
    assert alternate.status == READINESS_STATUS_UNVERIFIED
    assert "No CLI runner for provider 'deepseek'" in alternate.detail


def test_run_readiness_probe_rejects_transport_or_model_fallback_mismatch(tmp_path):
    config = _config(tmp_path)
    config = ForgeConfig(
        **{
            **config.__dict__,
            "dev_profile": _cli_profile(
                "dev",
                model="sonnet",
                fallback_models=("haiku",),
            ),
        }
    )
    probe = next(probe for probe in build_readiness_probes(config) if probe.role == "dev")

    with (
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(
                structured=False,
                transport_used="api",
                model_used="haiku",
            ),
        ),
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_pass_result(structured=False, transport_used="api"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_UNVERIFIED
    assert "requested transport 'cli'" in result.detail


def test_run_readiness_probe_marks_cli_cost_unavailable_unverified(tmp_path):
    config = _config(tmp_path)
    config = ForgeConfig(
        **{
            **config.__dict__,
            "review_pool": [_cli_profile("review-cli", phase="review")],
        }
    )
    probe = next(probe for probe in build_readiness_probes(config) if probe.role == "review")

    with (
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(cost_usd=None, transport_used="cli"),
        ),
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_pass_result(transport_used="api"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_UNVERIFIED
    assert "CLI cost is unavailable" in result.detail


def test_run_readiness_probe_does_not_treat_plain_401_reference_as_auth_unverified(tmp_path):
    probe = next(
        probe for probe in build_readiness_probes(_config(tmp_path)) if probe.role == "review"
    )
    numbered_failure = AgentResult(
        success=False,
        output="I updated src/theforge/coordinator/engine.py:401 to guard the branch.",
        session_id=None,
        cost_usd=None,
        exit_code=1,
        raw={},
        profile_name="reviewer",
    )

    with (
        patch("theforge.cli.provider_readiness.run_api_agent", return_value=numbered_failure),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_pass_result(transport_used="cli"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_FAILED
    assert result.detail == numbered_failure.output[:120]
