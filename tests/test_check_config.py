"""Tests for forge check-config command."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from theforge.cli.check_config import _provider_label, cmd_check_config, register_parser
from theforge.config import (
    DEFAULT_VALIDATION,
    AssignmentConfig,
    ForgeConfig,
    HooksConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    SprintConfig,
    TransportSpec,
    WorkspaceConfig,
)
from theforge.config.model_identity import (
    IDENTITY_STATUS_SERVED,
    UNCONFIRMED_IDENTITY,
    IdentityVerification,
)
from theforge.config.models import AGENT_REGISTRY, AgentDef, AgentSpec, RoutingPolicy
from theforge.config.models import TransportSpec as ModelTransportSpec

# ── Helpers ──────────────────────────────────────────────────────────────


def _cli_profile(name: str, cli: str = "claude", model: str = "sonnet") -> ModelProfile:
    return ModelProfile(
        name=name,
        cli=cli,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read",),
    )


def _api_profile(
    name: str,
    provider: str = "anthropic",
    model: str = "claude-opus-4-6",
    review_role: str | None = None,
    budget_usd: float = 1.0,
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=budget_usd,
        timeout_seconds=120,
        allowed_tools=("Read", "Grep"),
        review_role=review_role,
    )


def _make_forge_config(
    tmp_path: Path,
    review_pool: list[ModelProfile] | None = None,
    synthesis_profile: ModelProfile | None = None,
    plan: PlanConfig | None = None,
    plan_agent_review: PlanAgentReviewConfig | None = None,
    agents: list[AgentDef] | None = None,
    assignment: AssignmentConfig | None = None,
    models: list[str] | None = None,
    models_budget_usd: float | None = None,
    models_overrides: dict | None = None,
    model_registry: dict[str, AgentSpec] | None = None,
    model_registry_sources: dict[str, str] | None = None,
    custom_models: tuple[str, ...] = (),
) -> ForgeConfig:
    if review_pool is None:
        review_pool = [_api_profile("claude-reviewer")]
    return ForgeConfig(
        project="test-project",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            on_approve="none",
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
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=review_pool,
        synthesis_profile=synthesis_profile,
        retry=RetryPolicy(),
        plan=plan or PlanConfig.of(enabled=False),
        plan_agent_review=plan_agent_review or PlanAgentReviewConfig.of(enabled=False),
        log=LogConfig(enabled=False),
        agents=agents or [],
        assignment=assignment or AssignmentConfig(enabled=False),
        sprint=SprintConfig(max_parallel=1),
        models=models,
        models_budget_usd=models_budget_usd,
        models_overrides=models_overrides,
        model_registry=model_registry or dict(AGENT_REGISTRY),
        model_registry_sources=model_registry_sources or {k: "builtin" for k in AGENT_REGISTRY},
        custom_models=custom_models,
    )


def _make_args(config_path: str | None = None) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.config = config_path
    return ns


# ── Tests ────────────────────────────────────────────────────────────────


class TestCheckConfigHappyPath:
    def test_exit_0_all_auth_ok(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Project: test-project" in out
        assert "PHASES" in out
        assert "REVIEW POOL" in out
        assert "SETTINGS" in out
        assert "WARNINGS" not in out

    def test_warns_for_stale_generated_post_run_hook(self, tmp_path: Path, capsys) -> None:
        hooks_dir = tmp_path / ".forge" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "post_run.sh"
        hook.write_text(
            "#!/usr/bin/env bash\n"
            "*Filed by theforge post_run hook.*\n"
            'gh issue create --label "forge-finding"\n',
            encoding="utf-8",
        )
        config = replace(
            _make_forge_config(tmp_path),
            hooks=HooksConfig(post_run=".forge/hooks/post_run.sh"),
        )
        with (
            patch(
                "theforge.cli.check_config._find_config",
                return_value=tmp_path / "forge.yaml",
            ),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            exit_code = cmd_check_config(_make_args())

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "generated finding hook is stale" in out
        assert "needs-triage" in out
        assert "forge init-hooks" in out
        assert "--update" not in out

    def test_package_roots_printed_and_pass(self, tmp_path: Path, capsys) -> None:
        """check-config prints effective package_roots and exits 0 when they exist (AC1)."""
        from theforge.config.types import HardConventionsConfig

        (tmp_path / "src" / "pipeline").mkdir(parents=True)
        (tmp_path / "analysis").mkdir(parents=True)
        (tmp_path / "api").mkdir(parents=True)
        config = replace(
            _make_forge_config(tmp_path),
            conventions_hard=HardConventionsConfig(
                package_roots=("src/pipeline", "analysis", "api")
            ),
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "CONVENTIONS (hard)" in out
        assert "src/pipeline, analysis, api" in out
        assert "WARNINGS" not in out

    def test_missing_package_root_warns(self, tmp_path: Path, capsys) -> None:
        """A configured package_root that doesn't exist surfaces a warning (exit 1)."""
        from theforge.config.types import HardConventionsConfig

        (tmp_path / "src" / "pipeline").mkdir(parents=True)
        config = replace(
            _make_forge_config(tmp_path),
            conventions_hard=HardConventionsConfig(package_roots=("src/pipeline", "api")),
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "CONVENTIONS (hard)" in out
        assert "WARNINGS" in out
        assert "'api' does not exist" in out

    def test_sections_present(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "preflight" in out
        assert "dev" in out
        assert "on_approve:" in out

    def test_transport_label_cli(self, tmp_path: Path, capsys) -> None:
        """CLI profiles render provider and transport in separate columns."""
        config = _make_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        # claude CLI → provider=anthropic, transport=cli:claude, model=sonnet
        assert "anthropic" in out
        assert "cli:claude" in out
        assert "sonnet" in out

    def test_transport_label_provider(self, tmp_path: Path, capsys) -> None:
        """API profiles render provider and transport='api' in separate columns."""
        review_pool = [_api_profile("reviewer", provider="openai", model="gpt-4")]
        config = _make_forge_config(tmp_path, review_pool=review_pool)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "openai" in out
        # API transport is rendered as a separate 'api' column, not as 'openai / gpt-4'
        assert "api" in out
        assert "gpt-4" in out

    def test_transport_label_uses_explicit_transport(self, tmp_path: Path, capsys) -> None:
        """Display follows TransportSpec.kind, not the provider token."""
        review_pool = [
            ModelProfile(
                name="api-reviewer",
                cli=None,
                provider="openai",
                model="gpt-5.4",
                budget_usd=1.0,
                timeout_seconds=120,
                allowed_tools=("Read",),
                transport=TransportSpec(kind="api", runner="openai"),
            )
        ]
        config = _make_forge_config(tmp_path, review_pool=review_pool)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        reviewer_line = next(line for line in out.splitlines() if "api-reviewer" in line)
        assert "openai" in reviewer_line
        assert "api" in reviewer_line
        assert "cli:codex" not in reviewer_line

    def test_simple_mode_providers_header_distinguishes_provider_and_transport(
        self, tmp_path: Path, capsys
    ) -> None:
        """Simple-mode provider summary keeps API and CLI transports distinct."""
        config = _make_forge_config(
            tmp_path,
            models=[
                "anthropic/sonnet/cli",
                "openai/gpt-5.4/cli",
                "deepseek/deepseek-v4-pro/api",
                "openai/gpt-5.4/api",
            ],
            models_budget_usd=10.0,
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        providers_line = next(line for line in out.splitlines() if line.startswith("Providers:"))
        assert "anthropic cli:claude" in providers_line
        assert "openai cli:codex" in providers_line
        assert "deepseek api" in providers_line
        assert "openai api" in providers_line

    def test_simple_mode_rows_render_api_backed_deepseek_profile(
        self, tmp_path: Path, capsys
    ) -> None:
        """Derived PHASES/REVIEW POOL rows use TransportSpec for API-backed models."""
        config = _make_forge_config(
            tmp_path,
            models=[
                "anthropic/sonnet/cli",
                "openai/gpt-5.4/cli",
                "deepseek/deepseek-v4-pro/api",
                "openai/gpt-5.4/api",
            ],
            models_budget_usd=10.0,
            review_pool=[
                _api_profile("deepseek-reviewer", provider="deepseek", model="deepseek-v4-pro")
            ],
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "DERIVED ROLES (complexity-aware)" in out
        assert "deepseek  api       deepseek-v4-pro" in out
        deepseek_review_line = next(
            line for line in out.splitlines() if "deepseek-reviewer" in line
        )
        assert "deepseek" in deepseek_review_line
        assert "api" in deepseek_review_line
        assert "cli:deepseek" not in deepseek_review_line

    def test_model_registry_section_distinguishes_builtin_and_forge_yaml_models(
        self, tmp_path: Path, capsys
    ) -> None:
        custom_registry = dict(AGENT_REGISTRY)
        custom_registry["openai/gpt-5.5/cli"] = AgentSpec(
            provider="openai",
            model="gpt-5.5",
            transport=ModelTransportSpec(kind="cli", runner="codex", executable="codex"),
            routing=RoutingPolicy(tier="strong", capability=9, cost_rank=3),
            registry_source="forge.yaml",
            input_cost_per_mtok=5.0,
            output_cost_per_mtok=30.0,
        )
        config = _make_forge_config(
            tmp_path,
            models=["anthropic/sonnet/cli", "openai/gpt-5.5/cli"],
            models_budget_usd=10.0,
            model_registry=custom_registry,
            model_registry_sources={
                **{k: "builtin" for k in AGENT_REGISTRY},
                "openai/gpt-5.5/cli": "forge.yaml",
            },
            custom_models=("openai/gpt-5.5/cli",),
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "MODEL REGISTRY" in out
        assert "selected builtin:" in out
        assert "anthropic/sonnet/cli" in out
        assert "selected forge.yaml:" in out
        assert "gpt-5.5" in out
        assert "declared forge.yaml:" in out

    def test_provider_label_unknown_model_warns_without_prefix_fallback(self) -> None:
        warnings: list[str] = []

        assert _provider_label("openai/future-model", warnings) == "provider=? transport=?"
        assert len(warnings) == 1
        assert "not in AGENT_REGISTRY" in warnings[0]

    def test_explicit_thinking_budget_is_rendered(self, tmp_path: Path, capsys) -> None:
        review_pool = [
            ModelProfile(
                name="gemini-reviewer",
                provider="google",
                model="gemini-2.5-pro",
                budget_usd=1.0,
                timeout_seconds=120,
                allowed_tools=("Read", "Grep"),
                thinking_budget=2048,
            )
        ]
        config = _make_forge_config(tmp_path, review_pool=review_pool)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "thinking_budget=2048" in out


class TestCheckConfigWarnings:
    def test_exit_1_auth_failure(self, tmp_path: Path, capsys) -> None:
        review_pool = [
            _api_profile("claude-reviewer"),
            _api_profile("deepseek-reviewer", provider="deepseek", model="deepseek-v4-flash"),
        ]
        config = _make_forge_config(tmp_path, review_pool=review_pool)

        def _mock_auth(profile, secrets=None):
            if profile.provider == "deepseek":
                return (False, "DEEPSEEK_API_KEY not set")
            return (True, "")

        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", side_effect=_mock_auth),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "WARNINGS" in out
        assert "DEEPSEEK_API_KEY not set" in out
        assert "✗" in out

    def test_auth_failure_shows_checkmark_for_ok(self, tmp_path: Path, capsys) -> None:
        review_pool = [_api_profile("claude-reviewer")]
        config = _make_forge_config(tmp_path, review_pool=review_pool)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "✓ ready" in out


class TestCheckConfigInvalidConfig:
    def test_exit_2_on_load_error(self, tmp_path: Path) -> None:
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.cli.check_config.load_config",
                side_effect=ValueError("bad config"),
            ),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 2

    def test_exit_2_on_file_not_found(self, tmp_path: Path) -> None:
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.cli.check_config.load_config",
                side_effect=FileNotFoundError("not found"),
            ),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 2

    def test_exit_2_no_config_found(self, tmp_path: Path) -> None:
        with patch("theforge.cli.check_config._find_config", return_value=None):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 2

    def test_exit_2_prints_error(self, tmp_path: Path, capsys) -> None:
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.cli.check_config.load_config",
                side_effect=ValueError("yaml parse error"),
            ),
        ):
            cmd_check_config(_make_args())
        err = capsys.readouterr().err
        assert "yaml parse error" in err


class TestCheckConfigAgentsSection:
    def test_agents_section_when_assignment_enabled(self, tmp_path: Path, capsys) -> None:
        agents = [
            AgentDef(
                name="codex-cli",
                cli="codex",
                provider=None,
                model="gpt-4",
                budget_usd=2.0,
                timeout_seconds=300,
                tier="mid",
            )
        ]
        assignment = AssignmentConfig(
            enabled=True,
            min_reviewers=1,
            max_reviewers=3,
            max_cost_per_story_usd=15.0,
        )
        config = _make_forge_config(tmp_path, agents=agents, assignment=assignment)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "AGENTS" in out
        assert "codex-cli" in out
        assert "tier=mid" in out

    def test_agents_section_not_shown_when_disabled(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)  # assignment disabled, no agents
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "AGENTS" not in out

    def test_assignment_budget_shown_in_header(self, tmp_path: Path, capsys) -> None:
        assignment = AssignmentConfig(enabled=True, max_cost_per_story_usd=20.0)
        config = _make_forge_config(tmp_path, assignment=assignment)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "Per-story routing cost target: $20.00/story" in out

    def test_unset_per_story_cap_reported_in_header_and_settings(
        self, tmp_path: Path, capsys
    ) -> None:
        """Adaptive routing with no per-story cap says so in both places it appears."""
        assignment = AssignmentConfig(
            enabled=True,
            min_reviewers=1,
            max_reviewers=3,
            max_cost_per_story_usd=None,
        )
        config = _make_forge_config(tmp_path, assignment=assignment)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert (
            "Per-story routing cost target: unset "
            "(adaptive routes by complexity; only budget_usd enforces spend)" in out
        )
        assert (
            "assignment:    enabled  (min=1, max=3, "
            "no per-story routing cost target configured)" in out
        )
        assert "/story" not in out

    def test_no_budget_header_when_assignment_disabled(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)  # assignment disabled
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "Per-story routing cost target" not in out
        assert "Budget:" not in out


class TestCheckConfigPlanReviewers:
    def test_plan_reviewers_section_when_enabled(self, tmp_path: Path, capsys) -> None:
        plan_reviewer = _api_profile("codex-plan-reviewer", provider="openai", model="gpt-4")
        plan_agent_review = PlanAgentReviewConfig.of(
            enabled=True,
            pool=[plan_reviewer],
        )
        config = _make_forge_config(tmp_path, plan_agent_review=plan_agent_review)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "PLAN REVIEWERS" in out
        assert "codex-plan-reviewer" in out

    def test_plan_reviewers_not_shown_when_disabled(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)  # plan_agent_review disabled
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "PLAN REVIEWERS" not in out


class TestCheckConfigPlanPhase:
    def test_plan_phase_shown_when_enabled(self, tmp_path: Path, capsys) -> None:
        plan = PlanConfig.of(enabled=True, cli="claude", model="opus", budget_usd=3.0, timeout=600)
        config = _make_forge_config(tmp_path, plan=plan)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "plan" in out
        assert "anthropic" in out
        assert "cli:claude" in out
        assert "opus" in out
        assert "budget=$3.00" in out

    def test_plan_phase_not_shown_when_disabled(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)  # plan disabled
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        # plan disabled, so "plan:" settings line still shows but not as a phase row
        # The PHASES section shouldn't have a "plan" row
        lines = out.split("\n")
        phases_section = False
        plan_in_phases = False
        for line in lines:
            if line.strip() == "PHASES":
                phases_section = True
            elif phases_section and line.strip() == "":
                phases_section = False
            elif phases_section and line.strip().startswith("plan"):
                plan_in_phases = True
        assert not plan_in_phases


class TestCheckConfigPhaseAuth:
    def test_exit_1_dev_auth_failure(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)

        def _mock_auth(profile, secrets=None):
            if profile.cli == "claude" and profile.name == "dev":
                return (False, "'claude' not found in PATH")
            return (True, "")

        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", side_effect=_mock_auth),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "WARNINGS" in out
        assert "'claude' not found in PATH" in out
        assert "✗" in out

    def test_exit_1_preflight_auth_failure(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)

        def _mock_auth(profile, secrets=None):
            if profile.name == "preflight":
                return (False, "'claude' not found in PATH")
            return (True, "")

        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", side_effect=_mock_auth),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "WARNINGS" in out
        assert "✗" in out

    def test_exit_1_plan_auth_failure(self, tmp_path: Path, capsys) -> None:
        plan = PlanConfig.of(
            enabled=True, provider="anthropic", cli=None, model="claude-opus-4-6", budget_usd=1.0
        )
        config = _make_forge_config(tmp_path, plan=plan)

        def _mock_auth(profile, secrets=None):
            if profile.provider == "anthropic" and profile.name == "plan":
                return (False, "ANTHROPIC_API_KEY not set")
            return (True, "")

        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", side_effect=_mock_auth),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "WARNINGS" in out
        assert "ANTHROPIC_API_KEY not set" in out

    def test_plan_reviewer_named_plan_does_not_mask_phase_plan_auth(
        self, tmp_path: Path, capsys
    ) -> None:
        """Regression: a plan reviewer named 'plan' must not mask plan-phase auth failure."""
        plan = PlanConfig.of(
            enabled=True, provider="anthropic", cli=None, model="claude-opus-4-6", budget_usd=1.0
        )
        # Plan reviewer also named "plan" — would collide if keyed by name only
        plan_reviewer = _api_profile("plan", provider="openai", model="gpt-4")
        plan_agent_review = PlanAgentReviewConfig.of(enabled=True, pool=[plan_reviewer])
        config = _make_forge_config(tmp_path, plan=plan, plan_agent_review=plan_agent_review)

        def _mock_auth(profile, secrets=None):
            # Plan-phase stub: provider=anthropic, name=plan → auth fails
            if profile.provider == "anthropic" and profile.name == "plan":
                return (False, "ANTHROPIC_API_KEY not set")
            # Plan reviewer: provider=openai, name=plan → auth ok
            return (True, "")

        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", side_effect=_mock_auth),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "WARNINGS" in out
        assert "ANTHROPIC_API_KEY not set" in out


class TestCheckConfigDeprecatedFields:
    def test_deprecated_field_produces_warning_and_exit_1(self, tmp_path: Path, capsys) -> None:
        import logging

        config = _make_forge_config(tmp_path)

        def _load_with_deprecation(path):
            logging.getLogger("theforge.config").warning(
                "plan.old_field is deprecated — use plan.new_field instead"
            )
            return config

        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", side_effect=_load_with_deprecation),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                return_value=(True, ""),
            ),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "WARNINGS" in out
        assert "plan.old_field is deprecated" in out


class TestRegisterParser:
    def test_register_parser(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        register_parser(subparsers)
        args = parser.parse_args(["check-config"])
        assert args.command == "check-config"
        assert args.config is None

    def test_register_parser_with_config_arg(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        register_parser(subparsers)
        args = parser.parse_args(["check-config", "/path/to/forge.yaml"])
        assert args.config == "/path/to/forge.yaml"


class TestCheckConfigSandboxReadiness:
    def test_cli_launcher_sandbox_warning_is_reported(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                side_effect=[
                    (
                        False,
                        "launcher sandbox unavailable: sandbox-exec not usable; "
                        "CLI filesystem isolation will not hold",
                    ),
                    (True, ""),
                    (True, ""),
                ],
            ),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "launcher sandbox unavailable" in out
        assert "CLI filesystem isolation will not hold" in out

    def test_workspace_sandbox_warning_is_reported(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                side_effect=[
                    (
                        False,
                        "workspace sandbox unavailable: sandbox-exec not usable; "
                        "bash/tool effects will run unsandboxed",
                    ),
                    (True, ""),
                    (True, ""),
                ],
            ),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "workspace sandbox unavailable" in out
        assert "bash/tool effects will run unsandboxed" in out

    def test_workspace_sandbox_warning_targets_api_bash_profiles(
        self, tmp_path: Path, capsys
    ) -> None:
        config = _make_forge_config(
            tmp_path,
            review_pool=[
                ModelProfile(
                    name="api-bash-reviewer",
                    provider="openai",
                    model="gpt-4.1",
                    budget_usd=1.0,
                    timeout_seconds=120,
                    allowed_tools=("bash",),
                )
            ],
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch(
                "theforge.cli.check_config.check_agent_auth",
                side_effect=[
                    (True, ""),
                    (True, ""),
                    (
                        False,
                        "workspace sandbox unavailable: bwrap not usable; "
                        "bash/tool effects will run unsandboxed",
                    ),
                ],
            ),
        ):
            exit_code = cmd_check_config(_make_args())
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "api-bash-reviewer" in out
        assert "workspace sandbox unavailable" in out


# ── v0.8 simple-mode complexity-aware display ────────────────────────────────


class TestComplexityAwareDisplay:
    """Tests for the DERIVED ROLES section shown in v0.8 simple-mode configs."""

    def _make_v08_forge_config(self, tmp_path: Path) -> ForgeConfig:
        """ForgeConfig as produced by the v0.8 loader path (models set)."""
        return ForgeConfig(
            project="test-v08",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="feat/{slug}",
                on_approve="none",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=ModelProfile(
                name="dev",
                cli="claude",
                model="sonnet",
                budget_usd=30.0,
                timeout_seconds=600,
                allowed_tools=("Read",),
            ),
            preflight_profile=ModelProfile(
                name="preflight",
                cli="claude",
                model="sonnet",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=("Read",),
            ),
            review_pool=[
                ModelProfile(
                    name="claude-opus",
                    cli="claude",
                    model="opus",
                    budget_usd=9.0,
                    timeout_seconds=300,
                    allowed_tools=("Read",),
                )
            ],
            synthesis_profile=None,
            retry=RetryPolicy(),
            plan=PlanConfig.of(enabled=True),
            log=LogConfig(enabled=False),
            models=["anthropic/sonnet/cli", "anthropic/opus/cli"],
            models_budget_usd=50.0,
        )

    def test_derived_roles_section_shown_for_v08_config(self, tmp_path: Path, capsys) -> None:
        config = self._make_v08_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "DERIVED ROLES (complexity-aware)" in out
        assert "preflight:" in out
        assert "dev:" in out
        assert "plan:" in out
        assert "code_review:" in out

    def test_timeout_only_dev_override_shows_routing_active(self, tmp_path: Path, capsys) -> None:
        """#1764: a resource-only overrides.dev is surfaced as routing-active."""
        config = self._make_v08_forge_config(tmp_path)
        config = replace(
            config,
            models_overrides={"dev": {"timeout_large_seconds": 3600}},
            dev_profile_is_default=True,
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "complexity-aware routing active" in out
        assert "routing disabled" not in out

    def test_model_dev_override_shows_routing_disabled(self, tmp_path: Path, capsys) -> None:
        """#1764: a model-pinning overrides.dev is surfaced as routing-disabled."""
        config = self._make_v08_forge_config(tmp_path)
        config = replace(
            config,
            models_overrides={"dev": {"model": "opus"}},
            dev_profile_is_default=False,
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "routing disabled" in out
        assert "pinned by overrides.dev" in out

    def test_no_dev_override_shows_no_routing_note(self, tmp_path: Path, capsys) -> None:
        """Without an overrides.dev block, no routing annotation is added."""
        config = self._make_v08_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "routing disabled" not in out
        assert "routing active" not in out

    def test_mode_simple_shown_in_header(self, tmp_path: Path, capsys) -> None:
        config = self._make_v08_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "Mode:    simple" in out
        assert "Budget:  $50.00/story" in out

    def test_providers_listed_in_header(self, tmp_path: Path, capsys) -> None:
        config = self._make_v08_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "Providers:" in out
        assert "anthropic cli:claude" in out

    def test_providers_header_reports_api_transport(self, tmp_path: Path, capsys) -> None:
        config = self._make_v08_forge_config(tmp_path)
        config = replace(config, models=["openai/gpt-5.4/api", "deepseek/deepseek-v4-pro/api"])
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        providers_line = next(line for line in out.splitlines() if line.startswith("Providers:"))
        assert "openai api" in providers_line
        assert "deepseek api" in providers_line
        assert "openai-api" not in providers_line

    def test_derived_roles_not_shown_for_classic_config(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "DERIVED ROLES" not in out
        assert "Mode:    simple" not in out

    def test_preflight_marked_static(self, tmp_path: Path, capsys) -> None:
        config = self._make_v08_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "static" in out

    def test_advanced_overrides_none_shown(self, tmp_path: Path, capsys) -> None:
        config = self._make_v08_forge_config(tmp_path)
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "Advanced overrides: none" in out

    def test_advanced_overrides_listed_when_present(self, tmp_path: Path, capsys) -> None:
        """models_overrides populated → 'Advanced overrides' shows actual keys."""
        config = ForgeConfig(
            project="test-v08-overrides",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="feat/{slug}",
                on_approve="none",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=ModelProfile(
                name="dev",
                cli="claude",
                model="sonnet",
                budget_usd=30.0,
                timeout_seconds=1200,
                allowed_tools=("Read",),
            ),
            preflight_profile=ModelProfile(
                name="preflight",
                cli="claude",
                model="sonnet",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=("Read",),
            ),
            review_pool=[
                ModelProfile(
                    name="claude-opus",
                    cli="claude",
                    model="opus",
                    budget_usd=9.0,
                    timeout_seconds=300,
                    allowed_tools=("Read",),
                )
            ],
            synthesis_profile=None,
            retry=RetryPolicy(),
            plan=PlanConfig.of(enabled=False),
            log=LogConfig(enabled=False),
            models=["anthropic/sonnet/cli", "anthropic/opus/cli"],
            models_budget_usd=50.0,
            models_overrides={"dev": {"timeout_seconds": 1200}},
        )
        with (
            patch("theforge.cli.check_config._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.check_config.load_config", return_value=config),
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            cmd_check_config(_make_args())
        out = capsys.readouterr().out
        assert "Advanced overrides: none" not in out
        assert "dev" in out


# ── Integration tests: real YAML → check_config output ───────────────────────


class TestCheckConfigIntegration:
    """Integration tests: load real YAML via load_config (no patching), verify display."""

    _auth_ok = patch(
        "theforge.config._loaders.check_agent_auth",
        return_value=(True, ""),
    )

    def _write_yaml(self, tmp_path: Path, text: str) -> Path:
        p = tmp_path / "forge.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_v08_yaml_produces_simple_mode_header(self, tmp_path: Path, capsys) -> None:
        """Real v0.8 YAML loaded through load_config produces simple-mode header."""
        from theforge.config import load_config

        cfg_path = self._write_yaml(
            tmp_path,
            """
project: integration-test
models:
  - anthropic/sonnet/cli
  - anthropic/opus/cli
budget_usd: 30.0
""",
        )
        with (
            self._auth_ok,
            patch("theforge.cli.check_config.check_agent_auth", return_value=(True, "")),
        ):
            config = load_config(cfg_path)
            output, _ = __import__(
                "theforge.cli.check_config", fromlist=["_format_config"]
            )._format_config(config, {})
        assert "Mode:    simple" in output
        assert "Budget:  $30.00/story" in output
        assert "DERIVED ROLES (complexity-aware)" in output
        assert "Advanced overrides: none" in output

    def test_v08_yaml_with_overrides_shows_override_keys(self, tmp_path: Path) -> None:
        """v0.8 YAML with overrides: → display lists the override keys, not 'none'."""
        from theforge.cli.check_config import _format_config
        from theforge.config import load_config

        cfg_path = self._write_yaml(
            tmp_path,
            """
project: integration-test-ov
models:
  - anthropic/sonnet/cli
  - anthropic/opus/cli
budget_usd: 30.0
overrides:
  dev:
    timeout_seconds: 1200
""",
        )
        with self._auth_ok:
            config = load_config(cfg_path)
        output, _ = _format_config(config, {})
        assert "DERIVED ROLES (complexity-aware)" in output
        assert "Advanced overrides: none" not in output
        assert "dev" in output


class TestCliReadinessIsUnverified:
    """A CLI-transport row must not claim more than the check established (#2909).

    ``check_agent_auth`` clears a CLI profile on launcher presence alone. That
    cannot distinguish a model the account may call from one the provider will
    refuse outright, so the row reports a third state rather than resolving its
    own uncertainty toward ``ready``.
    """

    def _format(self, config: ForgeConfig) -> str:
        from theforge.cli.check_config import _format_config

        output, _ = _format_config(config, {})
        return output

    def _codex_registry(self, identity: IdentityVerification) -> dict[str, AgentSpec]:
        return {
            "openai/gpt-5.4/cli": AgentSpec(
                provider="openai",
                model="gpt-5.4",
                transport=ModelTransportSpec(kind="cli", runner="codex", executable="codex"),
                routing=RoutingPolicy(tier="strong", capability=9, cost_rank=3),
                identity=identity,
            )
        }

    def test_cli_profile_reports_unverified_not_ready(self, tmp_path: Path) -> None:
        config = _make_forge_config(
            tmp_path,
            review_pool=[_cli_profile("openai-gpt-5.4-cli", cli="codex", model="gpt-5.4")],
            model_registry=self._codex_registry(UNCONFIRMED_IDENTITY),
        )
        output = self._format(config)
        row = next(ln for ln in output.splitlines() if "openai-gpt-5.4-cli" in ln)
        assert "? unverified" in row
        assert "✓ ready" not in row
        # Names what was actually established, and what was not.
        assert "codex launcher on PATH" in row
        assert "neither credentials nor this account's entitlement to call 'gpt-5.4'" in row

    def test_identity_caveat_rides_the_same_row_as_the_verdict(self, tmp_path: Path) -> None:
        """The qualifying fact sits with the verdict, not in a separate section."""
        config = _make_forge_config(
            tmp_path,
            review_pool=[_cli_profile("openai-gpt-5.4-cli", cli="codex", model="gpt-5.4")],
            model_registry=self._codex_registry(UNCONFIRMED_IDENTITY),
        )
        row = next(ln for ln in self._format(config).splitlines() if "openai-gpt-5.4-cli" in ln)
        assert "never checked against the provider's published model list" in row

    def test_confirmed_identity_drops_only_the_identity_clause(self, tmp_path: Path) -> None:
        """A checked identifier removes its caveat; entitlement stays unverified."""
        confirmed = IdentityVerification(
            status=IDENTITY_STATUS_SERVED,
            verified_against="the provider's published model list",
            verified_on=date.today(),
        )
        config = _make_forge_config(
            tmp_path,
            review_pool=[_cli_profile("openai-gpt-5.4-cli", cli="codex", model="gpt-5.4")],
            model_registry=self._codex_registry(confirmed),
        )
        row = next(ln for ln in self._format(config).splitlines() if "openai-gpt-5.4-cli" in ln)
        assert "? unverified" in row
        assert "never checked against the provider's published model list" not in row

    def test_api_profile_still_reports_ready(self, tmp_path: Path) -> None:
        """API profiles resolve a real credential — a different, narrower claim."""
        config = _make_forge_config(tmp_path, review_pool=[_api_profile("claude-reviewer")])
        row = next(ln for ln in self._format(config).splitlines() if "claude-reviewer" in ln)
        assert "✓ ready" in row
        assert "unverified" not in row

    def test_failed_check_still_reports_the_failure(self, tmp_path: Path) -> None:
        from theforge.cli.check_config import _auth_key, _format_config

        profile = _cli_profile("openai-gpt-5.4-cli", cli="codex", model="gpt-5.4")
        config = _make_forge_config(tmp_path, review_pool=[profile])
        output, exit_code = _format_config(
            config, {_auth_key("review", profile.name): (False, "npx not found in PATH")}
        )
        row = next(ln for ln in output.splitlines() if "openai-gpt-5.4-cli" in ln)
        assert "✗ npx not found in PATH" in row
        assert "unverified" not in row
        assert exit_code == 1

    def test_unverified_is_not_a_warning_and_does_not_change_exit_code(
        self, tmp_path: Path
    ) -> None:
        """Unverified qualifies a row; it is not a problem to fix, so exit stays 0.

        Promoting it to a warning would flip every CLI-only config to exit 1 and
        bury the failures that do need action.
        """
        config = _make_forge_config(
            tmp_path,
            review_pool=[_cli_profile("openai-gpt-5.4-cli", cli="codex", model="gpt-5.4")],
            model_registry=self._codex_registry(UNCONFIRMED_IDENTITY),
        )
        output, exit_code = __import__(
            "theforge.cli.check_config", fromlist=["_format_config"]
        )._format_config(config, {})
        assert "? unverified" in output
        assert "WARNINGS" not in output
        assert exit_code == 0

    def test_phases_section_is_qualified_too(self, tmp_path: Path) -> None:
        """The reported symptom included the dev phase row, not just the pool."""
        output = self._format(_make_forge_config(tmp_path))
        dev_row = next(ln for ln in output.splitlines() if ln.strip().startswith("dev "))
        assert "? unverified" in dev_row
        assert "claude launcher on PATH" in dev_row
