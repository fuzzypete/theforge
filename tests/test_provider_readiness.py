from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.config import (
    AgentDef,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
    transport_for,
)
from theforge.cli.provider_readiness import (
    READINESS_CAPABILITY_TOOL_STRUCTURED,
    READINESS_STATUS_READY,
    READINESS_STATUS_UNSUPPORTED,
    build_readiness_probes,
    run_readiness_probe,
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


def _pass_result(profile_name: str = "test", *, cost_usd: float | None = 0.003) -> AgentResult:
    return AgentResult(
        success=True,
        output='{"verdict":"APPROVE","summary":"ok"}',
        session_id=None,
        cost_usd=cost_usd,
        exit_code=0,
        raw={},
        profile_name=profile_name,
        structured_data={"verdict": "APPROVE", "summary": "ok"},
    )


def test_build_readiness_probes_includes_config_plan_and_advisor_shapes(tmp_path):
    config = _config(tmp_path)

    probes = build_readiness_probes(config)
    labels = {(probe.role, probe.profile.name, probe.profile.phase) for probe in probes}

    assert ("preflight", "preflight", "preflight") in labels
    assert ("review", "reviewer", "review") in labels
    assert ("plan", "plan", None) in labels
    assert ("plan-review", "plan-reviewer", "plan_review") in labels
    assert ("advisor", "preflight", "advisor") in labels


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
    assert agent_probes["agent-dev"].capability == READINESS_CAPABILITY_TOOL_STRUCTURED


def test_run_readiness_probe_marks_openai_unsupported_shape_not_ready(tmp_path):
    probe = next(
        probe
        for probe in build_readiness_probes(_config(tmp_path))
        if probe.role == "review"
    )

    with patch(
        "theforge.cli.provider_readiness.openai_function_tool_request_shape",
        return_value=OpenAIFunctionToolRequestShape("unsupported"),
    ):
        with patch("theforge.cli.provider_readiness.run_api_agent") as mock_api:
            result = run_readiness_probe(probe, working_dir=tmp_path, secrets={})

    assert result.status == READINESS_STATUS_UNSUPPORTED
    assert not result.ready
    mock_api.assert_not_called()


def test_run_readiness_probe_marks_successful_probe_ready(tmp_path):
    probe = next(
        probe
        for probe in build_readiness_probes(_config(tmp_path))
        if probe.role == "review"
    )

    with patch("theforge.cli.provider_readiness.run_api_agent", return_value=_pass_result()):
        result = run_readiness_probe(probe, working_dir=tmp_path, secrets={})

    assert result.status == READINESS_STATUS_READY
    assert result.ready is True
