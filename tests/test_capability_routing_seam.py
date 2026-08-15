"""Probe → durable record → routing seam for demonstrated capability (#2466).

Three boundaries, in the order the fact travels:

  1. ``forge check-providers`` turns probe attempts into capability evidence and
     writes it — and, critically, does *not* invent evidence from a probe that
     never validated the capability.
  2. ``assign_models`` consults that record: it declines only a *currently*
     demonstrated-absent candidate, leaves never-established and stale ones
     eligible, and says so in the routing_decision rather than at a later gate.
  3. The coordinator loads the record at both assignment boundaries and forwards
     it — in static mode as well as adaptive, because a demonstrated absence is
     an eligibility fact, not a learning signal.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import _make_config  # noqa: E402

from theforge.assignment import (  # noqa: E402
    EXCLUSION_REASONS,
    REASON_CAPABILITY_ABSENT,
    ROLE_REQUIRED_CAPABILITY,
    AssignmentConfig,
    NoCapableCandidateError,
    assign_models,
)
from theforge.cli import providers as _providers  # noqa: E402
from theforge.cli.provider_readiness import (  # noqa: E402
    READINESS_STATUS_FAILED,
    READINESS_STATUS_READY,
    READINESS_STATUS_UNVERIFIED,
    ReadinessProbe,
    build_readiness_probes,
    capability_observations,
    run_readiness_probe,
)
from theforge.config import (  # noqa: E402
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
from theforge.coordinator.state import CoordinatorState  # noqa: E402
from theforge.model_capabilities import (  # noqa: E402
    CAPABILITY_PLAIN_STRUCTURED,
    CAPABILITY_TOOL_STRUCTURED,
    OUTCOME_ABSENT,
    OUTCOME_DEMONSTRATED,
    capabilities_path,
    load_capabilities,
)
from theforge.runners import AgentResult  # noqa: E402

# ── Fixtures / builders ───────────────────────────────────────────────


@pytest.fixture()
def _authed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=2,
        prefer_cross_provider=True,
        max_cost_per_story_usd=100.0,
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="gpt",
            provider="openai",
            model="gpt-5.4",
            budget_usd=8.0,
            timeout_seconds=900,
            tier="strong",
        ),
    ]


def _records(
    *,
    outcome: str = OUTCOME_ABSENT,
    identity_key: str = "openai/gpt-5.4/api",
    signature: str = "",
    capability: str = CAPABILITY_TOOL_STRUCTURED,
) -> dict:
    provider, model, transport = identity_key.split("/")
    return {
        "version": 1,
        "identities": {
            identity_key: {
                "provider": provider,
                "model": model,
                "transport": transport,
                "capabilities": {
                    capability: {
                        "outcome": outcome,
                        "established_at": "2026-08-15T09:00:00Z",
                        "subject_signature": signature,
                        "detail": "no valid verdict in structured output",
                        "probe_role": "agent-code-review",
                    }
                },
            }
        },
    }


def _api_profile(name, *, provider="openai", model="gpt-5.4", tools=("Read", "Grep"), phase=None):
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=tools,
        phase=phase,
    )


def _probe_config(tmp_path: Path) -> ForgeConfig:
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
        preflight_profile=_api_profile("preflight", phase="preflight"),
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
            enabled=True, pool=[_api_profile("plan-reviewer", phase="plan_review")]
        ),
        log=LogConfig(enabled=False),
    )


def _result(*, success=True, structured=True, cost_usd=0.003, transport_used=None):
    return AgentResult(
        success=success,
        output='{"verdict":"APPROVE","summary":"ok"}' if structured else "prose, no verdict",
        session_id=None,
        cost_usd=cost_usd,
        exit_code=0,
        raw={},
        profile_name="probe",
        structured_data={"verdict": "APPROVE", "summary": "ok"} if structured else None,
        transport_used=transport_used,
    )


def _probe(config: ForgeConfig, role: str) -> ReadinessProbe:
    return next(probe for probe in build_readiness_probes(config) if probe.role == role)


# ── 1. Probe → evidence ───────────────────────────────────────────────


def test_structured_success_establishes_the_capability(tmp_path):
    probe = _probe(_probe_config(tmp_path), "review")

    with (
        patch("theforge.cli.provider_readiness.run_api_agent", return_value=_result()),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_result(transport_used="cli"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_READY
    observations = capability_observations([result])
    by_transport = {obs.identity.transport: obs for obs in observations}
    # Both transports were exercised, and each is evidence for its OWN identity.
    assert set(by_transport) == {"api", "cli"}
    assert {obs.outcome for obs in observations} == {OUTCOME_DEMONSTRATED}
    assert by_transport["api"].capability == CAPABILITY_TOOL_STRUCTURED
    assert by_transport["api"].identity.key == "openai/gpt-5.4/api"


def test_unstructured_phase_ready_establishes_nothing(tmp_path):
    """A preflight probe accepts prose by design — its READY is not evidence."""
    probe = _probe(_probe_config(tmp_path), "preflight")

    with (
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_result(structured=False),
        ),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_result(structured=False, transport_used="cli"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_READY
    assert capability_observations([result]) == []


def test_failed_structured_output_establishes_absence(tmp_path):
    probe = _probe(_probe_config(tmp_path), "review")

    with (
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_result(structured=False),
        ),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_result(structured=False, transport_used="cli"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    assert result.status == READINESS_STATUS_FAILED
    assert {obs.outcome for obs in capability_observations([result])} == {OUTCOME_ABSENT}


def test_cli_cost_unavailable_still_establishes_the_capability(tmp_path):
    """Overall status is degraded for a cost reason that says nothing about the
    capability the probe just validated."""
    config = _probe_config(tmp_path)
    config = replace(config, review_pool=[_api_profile("reviewer", phase="review")])
    probe = _probe(config, "review")

    with (
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_result(cost_usd=None),
        ),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_result(cost_usd=None, transport_used="cli"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    cli_attempt = next(a for a in result.attempts if a.attempted_transport_kind == "cli")
    assert cli_attempt.status == READINESS_STATUS_UNVERIFIED
    cli_obs = [obs for obs in capability_observations([result]) if obs.identity.transport == "cli"]
    assert [obs.outcome for obs in cli_obs] == [OUTCOME_DEMONSTRATED]


def test_check_providers_writes_the_record(tmp_path, capsys):
    config = _probe_config(tmp_path)
    probe = _probe(config, "review")

    with (
        patch("theforge.cli.provider_readiness.run_api_agent", return_value=_result()),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_result(transport_used="cli"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        result = run_readiness_probe(probe, secrets={})

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    with (
        patch.object(_providers, "load_config", return_value=config),
        patch.object(_providers, "build_readiness_probes", return_value=[probe]),
        patch.object(_providers, "run_readiness_probe", return_value=result),
    ):
        _providers.cmd_check_providers(
            SimpleNamespace(config=str(forge_yaml), profile=None, declared_only=False)
        )

    path = capabilities_path(tmp_path)
    assert path.exists(), "check-providers must persist what it demonstrated"
    stored = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = stored["identities"]["openai/gpt-5.4/api"]["capabilities"][CAPABILITY_TOOL_STRUCTURED]
    assert entry["outcome"] == OUTCOME_DEMONSTRATED
    assert entry["established_at"], "when it was established must be recorded"
    assert "model_capabilities.yaml" in capsys.readouterr().out


def test_check_providers_writes_nothing_when_no_capability_was_established(tmp_path):
    """A run of probes that validated nothing leaves the record untouched."""
    config = _probe_config(tmp_path)
    probe = _probe(config, "preflight")
    with (
        patch(
            "theforge.cli.provider_readiness.run_api_agent",
            return_value=_result(structured=False),
        ),
        patch(
            "theforge.cli.provider_readiness.run_agent",
            return_value=_result(structured=False, transport_used="cli"),
        ),
        patch("theforge.cli.provider_readiness.check_agent_auth", return_value=(True, "")),
    ):
        empty = run_readiness_probe(probe, secrets={})

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    with (
        patch.object(_providers, "load_config", return_value=config),
        patch.object(_providers, "build_readiness_probes", return_value=[probe]),
        patch.object(_providers, "run_readiness_probe", return_value=empty),
    ):
        _providers.cmd_check_providers(
            SimpleNamespace(config=str(forge_yaml), profile=None, declared_only=False)
        )

    assert not capabilities_path(tmp_path).exists()


# ── 2. Record → routing ───────────────────────────────────────────────


def test_demonstrated_absent_candidate_is_not_seated(_authed):
    baseline = assign_models(_agents(), _cfg(), complexity="HIGH", complexity_score=9)
    assert "gpt-5.4" in [p.model for p in baseline.code_reviewers] + [baseline.planner.model]

    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        capability_records=_records(),
    )

    assert "gpt-5.4" not in [p.model for p in decision.code_reviewers]
    assert "gpt-5.4" not in [p.model for p in decision.plan_reviewers]
    assert decision.planner.model != "gpt-5.4"


def test_never_established_candidate_stays_eligible(_authed):
    """The record must not narrow the pool to whatever has been probed."""
    with_record = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        # A record about a model that is not in the pool at all.
        capability_records=_records(identity_key="openai/some-other-model/api"),
    )
    without_record = assign_models(_agents(), _cfg(), complexity="HIGH", complexity_score=9)

    assert [p.model for p in with_record.code_reviewers] == [
        p.model for p in without_record.code_reviewers
    ]
    assert with_record.planner.model == without_record.planner.model


def test_stale_record_does_not_exclude(_authed):
    """The recorded outcome predates a change to the identity it describes."""
    stale = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        capability_records=_records(signature="a-signature-from-a-different-subject"),
    )
    baseline = assign_models(_agents(), _cfg(), complexity="HIGH", complexity_score=9)

    assert [p.model for p in stale.code_reviewers] == [p.model for p in baseline.code_reviewers]


def test_demonstrated_capability_does_not_exclude(_authed):
    demonstrated = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        capability_records=_records(outcome=OUTCOME_DEMONSTRATED),
    )
    baseline = assign_models(_agents(), _cfg(), complexity="HIGH", complexity_score=9)

    assert [p.model for p in demonstrated.code_reviewers] == [
        p.model for p in baseline.code_reviewers
    ]


def test_routing_decision_reports_the_reason_with_the_decision(_authed):
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        capability_records=_records(),
    )
    block = decision.routing_decision

    for role in ("planner", "plan_review", "code_review"):
        entry = next(e for e in block[role]["candidate_pool"] if e["name"] == "gpt")
        assert entry["included"] is False
        assert entry["reason"] == REASON_CAPABILITY_ABSENT
        assert entry["reason"] in EXCLUSION_REASONS
        assert entry["detail"]["capability"] == CAPABILITY_TOOL_STRUCTURED
        assert entry["detail"]["established_at"] == "2026-08-15T09:00:00Z"

    # And the operator-facing rationale names it too, with the decision.
    assert "demonstrated absent" in decision.rationale["code_review"]


def test_reviewer_pool_exhaustion_fallback_does_not_reseat_an_absent_candidate(_authed):
    """The widening fallbacks (self-exclusion, unauthed) must not reach past the
    capability filter to refill a short panel."""
    agents = _agents()
    records = _records()
    records["identities"].update(_records(identity_key="anthropic/opus/api")["identities"])

    decision = assign_models(
        agents,
        _cfg(min_reviewers=3, max_reviewers=3),
        complexity="HIGH",
        complexity_score=9,
        capability_records=records,
    )

    seated = {p.model for p in decision.code_reviewers}
    seated |= {p.model for p in decision.plan_reviewers}
    assert "gpt-5.4" not in seated
    assert "opus" not in seated
    # Only the one remaining eligible reviewer is seated — a short panel, not a
    # silently refilled one.
    assert seated == {"sonnet"}


def _all_absent_records() -> dict:
    """Every agent in ``_agents()`` recorded demonstrated-absent."""
    records = _records()
    for key in ("anthropic/opus/api", "anthropic/sonnet/api"):
        records["identities"].update(_records(identity_key=key)["identities"])
    return records


def test_routing_refuses_when_no_candidate_can_serve_a_required_capability(_authed):
    """The last-resort seat is the failure this record exists to prevent."""
    with pytest.raises(NoCapableCandidateError) as excinfo:
        assign_models(
            _agents(),
            _cfg(),
            complexity="HIGH",
            complexity_score=9,
            capability_records=_all_absent_records(),
        )

    error = excinfo.value
    assert error.capability == CAPABILITY_TOOL_STRUCTURED
    assert error.role in ROLE_REQUIRED_CAPABILITY
    assert set(error.excluded) == {"sonnet", "opus", "gpt"}
    # The refusal carries the evidence: which models, and when each was established.
    message = str(error)
    assert CAPABILITY_TOOL_STRUCTURED in message
    assert "2026-08-15T09:00:00Z" in message
    for name in ("sonnet", "opus", "gpt"):
        assert name in message
    # It is a ValueError, so it joins the module's existing unroutable-pool contract.
    assert isinstance(error, ValueError)


@pytest.mark.parametrize("role", ["planner", "plan_review", "code_review"])
def test_every_structured_role_refuses_rather_than_seating_an_absent_model(_authed, role):
    """Each role that requires the capability refuses on its own account — the
    guard is not a property of whichever role happens to be selected first."""
    explicit = {
        other: ModelProfile(
            name="pinned",
            provider="anthropic",
            model="pinned-model",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        )
        for other in ROLE_REQUIRED_CAPABILITY
        if other != role
    }

    with pytest.raises(NoCapableCandidateError) as excinfo:
        assign_models(
            _agents(),
            _cfg(),
            complexity="HIGH",
            complexity_score=9,
            explicit_profiles=explicit,
            capability_records=_all_absent_records(),
        )

    assert excinfo.value.role == role


def test_all_absent_record_for_an_exempt_role_does_not_refuse(_authed):
    """dev and preflight require no recorded capability, so a record about them
    is not an eligibility fact and must not stop the run."""
    records = _records(capability=CAPABILITY_PLAIN_STRUCTURED)
    for key in ("anthropic/opus/api", "anthropic/sonnet/api"):
        records["identities"].update(
            _records(identity_key=key, capability=CAPABILITY_PLAIN_STRUCTURED)["identities"]
        )

    decision = assign_models(
        _agents(), _cfg(), complexity="HIGH", complexity_score=9, capability_records=records
    )

    assert decision.dev.model
    assert decision.preflight.model


def test_explicit_override_survives_an_otherwise_all_absent_pool(_authed):
    """An operator pin never reaches candidate selection, so the refusal must
    not fire for a role the operator already decided."""
    pinned = ModelProfile(
        name="pinned",
        provider="anthropic",
        model="pinned-model",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read",),
    )
    explicit = dict.fromkeys(ROLE_REQUIRED_CAPABILITY, pinned)

    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        explicit_profiles=explicit,
        capability_records=_all_absent_records(),
    )

    assert decision.planner.model == "pinned-model"
    assert [p.model for p in decision.code_reviewers] == ["pinned-model"]


def test_post_plan_checkpoint_preserves_dev_rather_than_failing(_authed):
    """The checkpoint is an optional demotion: no capable cheaper candidate
    preserves the seated dev instead of stopping the run."""
    from theforge.assignment import apply_post_plan_checkpoint

    cfg = _cfg(plan_tier_reduction=True)
    decision = assign_models(_agents(), cfg, complexity="MEDIUM", complexity_score=5)

    updated = apply_post_plan_checkpoint(
        decision,
        _agents(),
        cfg,
        "MEDIUM",
        plan_review_decision="APPROVE",
        plan_review_cycles=1,
        p1_count=0,
        p2_count=0,
        capability_records=_all_absent_records(),
    )

    assert updated.dev.model == decision.dev.model


@pytest.mark.filterwarnings("ignore:.*routing cost target.*:UserWarning")
def test_cost_downgrade_does_not_seat_an_absent_candidate(_authed):
    """A budget downgrade is a routing decision and honors the same filter."""
    agents = [
        AgentDef(
            name="cheap-absent",
            provider="openai",
            model="gpt-mini",
            budget_usd=0.5,
            timeout_seconds=300,
            tier="cheap",
        ),
        AgentDef(
            name="cheap-ok",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
        ),
        AgentDef(
            name="mid",
            provider="anthropic",
            model="sonnet",
            budget_usd=6.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="strong",
            provider="anthropic",
            model="opus",
            budget_usd=20.0,
            timeout_seconds=900,
            tier="strong",
        ),
    ]
    cfg = _cfg(max_cost_per_story_usd=3.0, min_reviewers=1, max_reviewers=1)

    baseline = assign_models(agents, cfg, complexity="MEDIUM", complexity_score=5)
    # Without the record the cheapest candidate the enforcer reaches for IS the
    # one that cannot do the job — so this test would pass vacuously otherwise.
    assert baseline.planner.model == "gpt-mini"

    decision = assign_models(
        agents,
        cfg,
        complexity="MEDIUM",
        complexity_score=5,
        capability_records=_records(identity_key="openai/gpt-mini/api"),
    )

    assert decision.budget_audit["downgraded"] is True
    assert "gpt-mini" not in {step["to_model"] for step in decision.budget_audit["steps"]}
    assert decision.planner.model == "haiku"
    assert "gpt-mini" not in [p.model for p in decision.code_reviewers]
    assert "gpt-mini" not in [p.model for p in decision.plan_reviewers]


def test_explicit_override_is_honored_but_flagged(_authed):
    """Operator intent wins — the rationale must not read as unremarkable."""
    explicit = ModelProfile(
        name="gpt",
        provider="openai",
        model="gpt-5.4",
        budget_usd=8.0,
        timeout_seconds=900,
        allowed_tools=("Read",),
    )

    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        explicit_profiles={"code_review": explicit},
        capability_records=_records(),
    )

    assert [p.model for p in decision.code_reviewers] == ["gpt-5.4"]
    assert "demonstrated absent" in decision.rationale["code_review"]


# ── 3. Coordinator boundaries ─────────────────────────────────────────


def _pool_config(tmp_path, *, adaptive: bool):
    return replace(
        _make_config(tmp_path),
        agents=_agents(),
        assignment=_cfg(adaptive_enabled=adaptive, max_cost_per_story_usd=100.0),
        # Default (unpinned) role config, so the router selects rather than
        # honoring an explicit override.
        review_pool_is_default=True,
        plan_model_is_default=True,
    )


@pytest.mark.parametrize("adaptive", [True, False])
def test_preflight_forwards_capability_records_to_assign_models(
    tmp_path, monkeypatch, _authed, adaptive
):
    """Loaded outside the adaptive guard: a demonstrated absence is an
    eligibility fact, so static routing consults it too."""
    from theforge.coordinator import preflight as _pf

    config = _pool_config(tmp_path, adaptive=adaptive)
    path = capabilities_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_records()), encoding="utf-8")

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    captured: dict = {}
    real_assign = assign_models

    def _spy(*args, **kwargs):
        captured["capability_records"] = kwargs.get("capability_records")
        return real_assign(*args, **kwargs)

    monkeypatch.setattr("theforge.assignment.assign_models", _spy)
    _pf._apply_preflight_config(config, state)

    assert captured["capability_records"] == load_capabilities(path)
    pool = state.routing_decision["code_review"]["candidate_pool"]
    assert next(e for e in pool if e["name"] == "gpt")["reason"] == REASON_CAPABILITY_ABSENT


@pytest.mark.parametrize("adaptive", [True, False])
def test_post_plan_checkpoint_receives_capability_records(
    tmp_path, monkeypatch, _authed, adaptive
):
    from theforge.coordinator import plan_flow as _pf

    config = _pool_config(tmp_path, adaptive=adaptive)
    path = capabilities_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_records()), encoding="utf-8")

    state = CoordinatorState()
    state.preflight_complexity = "MEDIUM"
    state.preflight_complexity_score = 5
    state._adaptive_decision = assign_models(
        _agents(), config.assignment, complexity="MEDIUM", complexity_score=5
    )
    state.plan_attempt_metadata = [{"p1_count": 0, "p2_count": 0}]

    captured: dict = {}

    def _spy(*args, **kwargs):
        captured["capability_records"] = kwargs.get("capability_records")
        return args[0]

    monkeypatch.setattr("theforge.assignment.apply_post_plan_checkpoint", _spy)
    _pf._apply_post_plan_dev_checkpoint(state, config, plan_review_decision="APPROVE")

    assert captured["capability_records"] == load_capabilities(path)
