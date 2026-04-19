"""Tests for forge check-config command."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from theforge.cli.check_config import cmd_check_config, register_parser
from theforge.config import (
    DEFAULT_VALIDATION,
    AssignmentConfig,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    SprintConfig,
    WorkspaceConfig,
)
from theforge.config.models import AgentDef

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
        plan=plan or PlanConfig(enabled=False),
        plan_agent_review=plan_agent_review or PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
        agents=agents or [],
        assignment=assignment or AssignmentConfig(enabled=False),
        sprint=SprintConfig(max_parallel=1),
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
        assert "claude / sonnet" in out

    def test_transport_label_provider(self, tmp_path: Path, capsys) -> None:
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
        assert "openai / gpt-4" in out

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
            _api_profile("deepseek-reviewer", provider="deepseek", model="deepseek-chat"),
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
            budget_per_story_usd=15.0,
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
        assignment = AssignmentConfig(enabled=True, budget_per_story_usd=20.0)
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
        assert "Budget:  $20.00/story" in out

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
        assert "Budget:" not in out


class TestCheckConfigPlanReviewers:
    def test_plan_reviewers_section_when_enabled(self, tmp_path: Path, capsys) -> None:
        plan_reviewer = _api_profile("codex-plan-reviewer", provider="openai", model="gpt-4")
        plan_agent_review = PlanAgentReviewConfig(
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
        plan = PlanConfig(enabled=True, cli="claude", model="opus", budget_usd=3.0, timeout=600)
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
        assert "claude / opus" in out
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
        plan = PlanConfig(
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
        plan = PlanConfig(
            enabled=True, provider="anthropic", cli=None, model="claude-opus-4-6", budget_usd=1.0
        )
        # Plan reviewer also named "plan" — would collide if keyed by name only
        plan_reviewer = _api_profile("plan", provider="openai", model="gpt-4")
        plan_agent_review = PlanAgentReviewConfig(enabled=True, pool=[plan_reviewer])
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
            plan=PlanConfig(enabled=True),
            log=LogConfig(enabled=False),
            models=["claude/sonnet", "claude/opus"],
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
        assert "claude cli" in out

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
            plan=PlanConfig(enabled=False),
            log=LogConfig(enabled=False),
            models=["claude/sonnet", "claude/opus"],
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
  - claude/sonnet
  - claude/opus
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
  - claude/sonnet
  - claude/opus
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
