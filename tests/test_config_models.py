"""Tests for config models — review pool, smart config, model registry, etc."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import (
    MODEL_REGISTRY,
    SUPPORTED_CLIS,
    generate_default_config,
    load_config,
)
from theforge.config.profiles import (
    _auto_assign_models,
    _resolve_model_info,
)


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


class TestGenerateDefaultConfig:
    def test_is_valid_yaml(self):
        content = generate_default_config()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)
        assert "project" in data
        assert "models" in data
        assert isinstance(data["models"], list)
        assert len(data["models"]) >= 1

    def test_loadable(self, tmp_path):
        """Default config should load without errors."""
        config_path = tmp_path / "forge.yaml"
        config_path.write_text(generate_default_config(), encoding="utf-8")
        config = load_config(config_path)
        assert config.review_pool == [config.review_profile]
        assert config.synthesis_profile is None


class TestSupportedClis:
    def test_supported_clis_contains_claude(self):
        assert "claude" in SUPPORTED_CLIS

    def test_supported_clis_contains_codex(self):
        assert "codex" in SUPPORTED_CLIS

    def test_supported_clis_contains_gemini(self):
        assert "gemini" in SUPPORTED_CLIS


class TestNotificationConfig:
    def test_notifications_default_when_absent(self, tmp_path):
        """Missing notifications section → default NotificationConfig."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.notifications.backend == "none"
        assert config.notifications.ntfy is None
        assert config.notifications.human_review_timeout_seconds == 14400

    def test_human_review_timeout_parsed(self, tmp_path):
        """human_review_timeout_seconds is read from forge.yaml."""
        config_path = _write_config(
            {"notifications": {"backend": "ntfy", "human_review_timeout_seconds": 7200}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.human_review_timeout_seconds == 7200

    def test_human_review_timeout_default(self, tmp_path):
        """Absent human_review_timeout_seconds defaults to 14400."""
        config_path = _write_config(
            {"notifications": {"backend": "ntfy"}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.human_review_timeout_seconds == 14400

    def test_ntfy_config_parsed(self, tmp_path):
        """ntfy url and priority are parsed correctly."""
        config_path = _write_config(
            {
                "notifications": {
                    "backend": "ntfy",
                    "ntfy": {
                        "url": "https://ntfy.sh/my-topic",
                        "priority": "urgent",
                    },
                    "human_review_timeout_seconds": 3600,
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.backend == "ntfy"
        assert config.notifications.ntfy is not None
        assert config.notifications.ntfy.url == "https://ntfy.sh/my-topic"
        assert config.notifications.ntfy.priority == "urgent"
        assert config.notifications.human_review_timeout_seconds == 3600

    def test_ntfy_default_priority(self, tmp_path):
        """ntfy priority defaults to 'high'."""
        config_path = _write_config(
            {
                "notifications": {
                    "backend": "ntfy",
                    "ntfy": {"url": "https://ntfy.sh/topic"},
                }
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.notifications.ntfy is not None
        assert config.notifications.ntfy.priority == "high"


# ── Smart config tests ────────────────────────────────────────────────


class TestAutoAssignModels:
    """Tests for the _auto_assign_models() function."""

    def test_auto_assign_4_models(self):
        """4 models → correct dev/preflight/pool/synthesis assignment."""
        models = [
            "claude/sonnet",
            "claude/opus",
            "openai/gpt-5.4",
            "google/gemini-2.5-pro",
        ]
        dev, preflight, pool, synthesis = _auto_assign_models(models, 50.0)

        # dev = cheapest (sonnet, cost_rank=1)
        assert dev.cli == "claude"
        assert dev.model == "sonnet"

        # preflight = first fast-tier model (sonnet)
        assert preflight.cli == "claude"
        assert preflight.model == "sonnet"

        # review_pool = all except dev (3 models)
        assert len(pool) == 3
        pool_models = {p.model for p in pool}
        assert "opus" in pool_models
        assert "gpt-5.4" in pool_models
        assert "gemini-2.5-pro" in pool_models

        # synthesis = highest capability from pool (opus, cap=10)
        assert synthesis is not None
        assert synthesis.model == "opus"

    def test_auto_assign_2_models(self):
        """2 models → cheaper dev, single reviewer, no synthesis."""
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus"], 50.0
        )
        assert dev.model == "sonnet"
        assert preflight.model == "sonnet"
        assert len(pool) == 1
        assert pool[0].model == "opus"
        assert synthesis is None

    def test_auto_assign_1_model(self):
        """1 model → used for everything."""
        dev, preflight, pool, synthesis = _auto_assign_models(["claude/sonnet"], 50.0)
        assert dev.model == "sonnet"
        assert preflight.model == "sonnet"
        assert len(pool) == 1
        assert pool[0].model == "sonnet"
        assert synthesis is None

    def test_auto_assign_budget_distribution(self):
        """Verify budget shares are allocated correctly."""
        budget = 100.0
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus", "openai/gpt-5.4"], budget
        )
        # dev = 60%
        assert abs(dev.budget_usd - 60.0) < 0.01
        # preflight = max(2%, $1) = max($2, $1) = $2
        assert abs(preflight.budget_usd - 2.0) < 0.01
        # synthesis = max(2%, $1) = $2
        assert synthesis is not None
        assert abs(synthesis.budget_usd - 2.0) < 0.01
        # remaining = 100 - 60 - 2 - 2 = 36; 2 reviewers → $18 each
        assert len(pool) == 2
        for p in pool:
            assert abs(p.budget_usd - 18.0) < 0.01


class TestModelsKeyConfig:
    """Tests for forge.yaml 'models' key loading."""

    def test_models_key_loads_config(self, tmp_path):
        """forge.yaml with models key produces valid ForgeConfig."""
        config_path = _write_config(
            {
                "project": "smart-test",
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.project == "smart-test"
        assert config.models == ["claude/sonnet", "claude/opus"]
        assert config.dev_profile.model == "sonnet"
        assert len(config.review_pool) == 1
        assert config.review_pool[0].model == "opus"
        assert config.synthesis_profile is None

    def test_models_key_populates_agents_for_assignment(self, tmp_path):
        """v0.8 models: list produces a non-empty ForgeConfig.agents pool.

        Regression guard: adaptive assignment (preflight + assign_models) needs
        agents to be derived from models: so `assignment.enabled: true` doesn't
        silently fall back to an empty pool.
        """
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus", "openai/gpt-5.4"],
                "budget_usd": 50.0,
                "assignment": {"enabled": True},
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.assignment.enabled is True
        assert len(config.agents) == 3
        tiers = {a.tier for a in config.agents}
        # Pool must span multiple tiers so assign_models can pick for LOW/MED/HIGH
        assert "cheap" in tiers
        assert "strong" in tiers
        models = {a.model for a in config.agents}
        assert models == {"sonnet", "opus", "gpt-5.4"}

    def test_models_key_supports_deepseek_api_models(self, tmp_path):
        """DeepSeek entries in models: derive API-backed profiles, not CLI profiles."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "deepseek/deepseek-reasoner"],
                "budget_usd": 50.0,
            },
            tmp_path,
        )
        config = load_config(config_path)
        deepseek_profiles = [
            p
            for p in [config.dev_profile, config.preflight_profile, *config.review_pool]
            if p.model == "deepseek-reasoner"
        ]
        assert deepseek_profiles
        assert all(p.cli is None for p in deepseek_profiles)
        assert all(p.provider == "deepseek" for p in deepseek_profiles)

    def test_models_with_profile_override(self, tmp_path):
        """Explicit overrides overlay auto-assigned values (v0.8: overrides: key)."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "overrides": {
                    "dev": {"budget_usd": 100.0},
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.model == "sonnet"  # still auto-assigned
        assert config.dev_profile.budget_usd == 100.0  # overridden

    def test_models_key_absent_backward_compat(self, tmp_path):
        """No models key → existing behavior unchanged."""
        from theforge.config import DEFAULT_DEV_PROFILE, DEFAULT_REVIEW_PROFILE

        config_path = _write_config({"project": "classic"}, tmp_path)
        config = load_config(config_path)
        assert config.models is None
        assert config.dev_profile == DEFAULT_DEV_PROFILE
        assert config.review_pool == [DEFAULT_REVIEW_PROFILE]

    def test_unknown_model_gets_defaults(self, tmp_path):
        """Model not in registry is accepted with default metadata."""
        config_path = _write_config(
            {
                "models": ["claude/future-model"],
                "budget_usd": 10.0,
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.dev_profile.cli == "claude"
        assert config.dev_profile.model == "future-model"

    def test_models_empty_raises(self, tmp_path):
        """Empty models list → ValueError."""
        config_path = _write_config({"models": []}, tmp_path)
        with pytest.raises(ValueError, match="non-empty"):
            load_config(config_path)

    def test_models_invalid_format_raises(self, tmp_path):
        """Model without '/' → ValueError."""
        config_path = _write_config({"models": ["badformat"]}, tmp_path)
        with pytest.raises(ValueError, match="provider/model"):
            load_config(config_path)

    def test_models_unknown_provider_raises(self, tmp_path):
        """Unknown provider with no registry entry → ValueError."""
        config_path = _write_config({"models": ["llama/llama3"]}, tmp_path)
        with pytest.raises(ValueError, match="Unknown provider"):
            load_config(config_path)

    def test_budget_negative_raises(self, tmp_path):
        """Negative budget_usd → ValueError."""
        config_path = _write_config({"models": ["claude/sonnet"], "budget_usd": -1.0}, tmp_path)
        with pytest.raises(ValueError, match="positive"):
            load_config(config_path)

    def test_4_model_auto_assign_via_load_config(self, tmp_path):
        """load_config with 4 models produces correct pool and synthesis."""
        config_path = _write_config(
            {
                "models": [
                    "claude/sonnet",
                    "claude/opus",
                    "openai/gpt-5.4",
                    "google/gemini-2.5-pro",
                ],
                "budget_usd": 50.0,
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 3
        assert config.synthesis_profile is not None
        assert config.synthesis_profile.model == "opus"

    def test_models_key_plan_uses_derived_role(self, tmp_path):
        """Smart-config plan phase uses the derived plan role, not legacy sonnet.

        Regression guard for #265: derive_roles() + bridge correctly produce a
        plan_profile, but load_config used to drop it and build PlanConfig from
        the hard-coded cli=claude/model=sonnet legacy defaults. The result was
        that adaptive routing never reached the PLAN phase when the user opted
        into smart config.
        """
        config_path = _write_config(
            {
                "models": ["openai/gpt-5.4", "claude/opus"],
                "budget_usd": 50.0,
                "plan": {"enabled": True},
            },
            tmp_path,
        )
        config = load_config(config_path)
        # derive_roles picks the cheapest dev-capable model for dev AND plan.
        # With [opus (rank 3), gpt-5.4 (rank 2)], dev = gpt-5.4.
        assert config.dev_profile.cli == "codex"
        assert config.dev_profile.model == "gpt-5.4"
        # Plan must track the derived dev role, not the legacy sonnet default.
        assert config.plan.enabled is True
        assert config.plan.cli == "codex"
        assert config.plan.model == "gpt-5.4"
        assert config.plan.provider is None
        # validate_spec flows through from the derived PlanRoleConfig default.
        assert config.plan.validate_spec is True

    def test_models_key_plan_explicit_override_wins(self, tmp_path):
        """Explicit plan.model in forge.yaml overrides the derived plan role."""
        config_path = _write_config(
            {
                "models": ["openai/gpt-5.4", "claude/opus"],
                "budget_usd": 50.0,
                "plan": {"enabled": True, "cli": "claude", "model": "opus"},
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan.cli == "claude"
        assert config.plan.model == "opus"

    def test_classic_config_plan_defaults_unchanged(self, tmp_path):
        """Classic config (no models: key) still gets the legacy plan defaults."""
        config_path = _write_config(
            {"project": "classic", "plan": {"enabled": True}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.plan.cli == "claude"
        assert config.plan.model == "sonnet"
        assert config.plan.provider is None


class TestAutoAssignBudgetClamping:
    """Tests for budget clamping (P1 fix: reviewer budget must not exceed total)."""

    def test_tight_budget_reviewer_does_not_exceed_total(self):
        """When dev+preflight+synthesis consume the budget, reviewers get $0 not $0.50.

        Old bug: max(remaining / pool_size, 0.5) would give each reviewer $0.50
        even when remaining=0, pushing total above budget_usd.
        """
        # With $5 and 3 models: dev=$3, preflight=$1, synthesis=$1 → remaining=$0
        budget = 5.0
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus", "openai/gpt-5.4"], budget
        )
        total = dev.budget_usd + preflight.budget_usd + sum(p.budget_usd for p in pool)
        if synthesis:
            total += synthesis.budget_usd
        assert total <= budget + 0.01  # allow tiny float rounding
        for p in pool:
            assert p.budget_usd >= 0.0  # reviewer budget never negative

    def test_reviewer_budget_never_negative(self):
        """Reviewer budget is never negative even when fixed costs eat the whole budget."""
        dev, preflight, pool, synthesis = _auto_assign_models(["claude/sonnet"], 1.0)
        for p in pool:
            assert p.budget_usd >= 0.0

    def test_normal_budget_reviewer_gets_remaining_share(self):
        """With adequate budget, reviewers split remaining after dev+preflight+synthesis."""
        budget = 50.0
        dev, preflight, pool, synthesis = _auto_assign_models(
            ["claude/sonnet", "claude/opus", "openai/gpt-5.4"], budget
        )
        assert synthesis is not None
        remaining = budget - dev.budget_usd - preflight.budget_usd - synthesis.budget_usd
        expected_per_reviewer = remaining / len(pool)
        for p in pool:
            assert abs(p.budget_usd - expected_per_reviewer) < 0.01


class TestModelsKeyReviewPoolOverride:
    """Tests for review_pool override in smart-config mode (P1 fix)."""

    def test_review_pool_override_by_name(self, tmp_path):
        """overrides.review_pool entries are matched by name and override auto-assigned values."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "overrides": {
                    "review_pool": [
                        {"name": "claude-opus", "budget_usd": 99.0},
                    ],
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert len(config.review_pool) == 1
        assert config.review_pool[0].model == "opus"
        assert abs(config.review_pool[0].budget_usd - 99.0) < 0.01

    def test_review_pool_override_partial(self, tmp_path):
        """Overriding only timeout_seconds preserves auto-assigned cli/model."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "overrides": {
                    "review_pool": [
                        {"name": "claude-opus", "timeout_seconds": 600},
                    ],
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        assert config.review_pool[0].cli == "claude"
        assert config.review_pool[0].model == "opus"
        assert config.review_pool[0].timeout_seconds == 600

    def test_review_pool_override_unknown_name_ignored(self, tmp_path):
        """Override with name not in auto-assigned pool is silently ignored."""
        config_path = _write_config(
            {
                "models": ["claude/sonnet", "claude/opus"],
                "budget_usd": 50.0,
                "overrides": {
                    "review_pool": [
                        {"name": "nonexistent", "budget_usd": 1.0},
                    ],
                },
            },
            tmp_path,
        )
        config = load_config(config_path)
        # Pool still has auto-assigned opus reviewer
        assert len(config.review_pool) == 1
        assert config.review_pool[0].model == "opus"


class TestResolveModelInfo:
    """Tests for _resolve_model_info()."""

    def test_known_model_from_registry(self):
        info = _resolve_model_info("claude/sonnet")
        assert info.cli == "claude"
        assert info.model == "sonnet"
        assert info.tier == "fast"
        assert info.cost_rank == 1

    def test_unknown_model_defaults(self):
        info = _resolve_model_info("claude/future-model")
        assert info.cli == "claude"
        assert info.model == "future-model"
        assert info.tier == "strong"
        assert info.capability == 5
        assert info.cost_rank == 2


class TestAutoPushConfig:
    """Tests for workspace.auto_push config field."""

    def test_auto_push_config_parsed(self, tmp_path):
        """forge.yaml workspace.auto_push: true → config.workspace.auto_push is True."""
        config_path = _write_config(
            {"project": "test", "workspace": {"auto_push": True}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.workspace.auto_push is True

    def test_auto_push_default_false(self, tmp_path):
        """auto_push absent from forge.yaml → defaults to False."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.workspace.auto_push is False

    def test_auto_push_explicit_false(self, tmp_path):
        """workspace.auto_push: false → config.workspace.auto_push is False."""
        config_path = _write_config(
            {"project": "test", "workspace": {"auto_push": False}},
            tmp_path,
        )
        config = load_config(config_path)
        assert config.workspace.auto_push is False


class TestProjectSecrets:
    """Tests for project-scoped secrets loading (AC-1, AC-2)."""

    def _make_forge_dir(self, tmp_path: Path) -> Path:
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        return forge_dir

    def test_secrets_loaded_into_config(self, tmp_path):
        """AC-1: .env file is loaded and stored on ForgeConfig.secrets."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\nOPENAI_API_KEY=sk-openai-test\n",
            encoding="utf-8",
        )
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.secrets == {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-openai-test",
        }

    def test_missing_secrets_file_defaults_to_empty(self, tmp_path):
        """AC-2: absent .forge/.env → secrets defaults to {}."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.secrets == {}

    def test_empty_secrets_file_defaults_to_empty(self, tmp_path):
        """AC-2: empty .forge/.env → secrets defaults to {}."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text("", encoding="utf-8")
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.secrets == {}

    def test_malformed_secrets_raises_value_error(self, tmp_path):
        """AC-2: malformed .env raises ValueError with file path."""
        forge_dir = self._make_forge_dir(tmp_path)
        env_path = forge_dir / ".env"
        # dotenv_values produces None value when a key has no value (bare key with no =)
        env_path.write_text("BARE_KEY_NO_EQUALS\n", encoding="utf-8")
        config_path = _write_config({"project": "test"}, tmp_path)
        with pytest.raises(ValueError, match=r"malformed \.env"):
            load_config(config_path)

    def test_secrets_yaml_triggers_migration_warning(self, tmp_path, caplog):
        """AC-7: .forge/secrets.yaml present without .env triggers a warning."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / "secrets.yaml").write_text(
            "ANTHROPIC_API_KEY: sk-ant-test\n", encoding="utf-8"
        )
        config_path = _write_config({"project": "test"}, tmp_path)
        import logging

        with caplog.at_level(logging.WARNING, logger="theforge.config"):
            config = load_config(config_path)
        assert any("secrets.yaml" in r.message for r in caplog.records)
        # secrets.yaml is NOT loaded — secrets defaults to {}
        assert config.secrets == {}

    def test_secret_satisfies_provider_api_key_validation(self, tmp_path):
        """AC-2: key in .env satisfies provider API key check even when not in os.environ."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text("OPENAI_API_KEY=sk-from-secrets\n", encoding="utf-8")
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "provider": "openai",
                    "model": "o4-mini",
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        assert config.plan_agent_review.provider == "openai"

    def test_plan_agent_review_secret_satisfies_validation(self, tmp_path):
        """AC-2: plan_agent_review provider key in .env satisfies validation."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-ant-from-secrets\n", encoding="utf-8"
        )
        config_path = _write_config(
            {
                "plan_agent_review": {
                    "enabled": True,
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                }
            },
            tmp_path,
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("importlib.import_module"),
        ):
            config = load_config(config_path)
        assert config.plan_agent_review.provider == "anthropic"

    def test_ntfy_url_from_env_when_forge_yaml_omits_url(self, tmp_path):
        """AC-4: NTFY_URL in .forge/.env is used when forge.yaml ntfy block has no url."""
        forge_dir = self._make_forge_dir(tmp_path)
        (forge_dir / ".env").write_text("NTFY_URL=https://ntfy.sh/my-topic\n", encoding="utf-8")
        config_path = _write_config(
            {
                "project": "test",
                "notifications": {"backend": "ntfy", "ntfy": {"priority": "default"}},
            },
            tmp_path,
        )
        with patch.dict("os.environ", {}, clear=True):
            config = load_config(config_path)
        assert config.notifications.ntfy is not None
        assert config.notifications.ntfy.url == "https://ntfy.sh/my-topic"

    def test_ntfy_url_missing_warns_and_disables(self, tmp_path, caplog):
        """AC-4: ntfy backend with no URL in forge.yaml, .env, or os.environ warns and disables."""
        self._make_forge_dir(tmp_path)
        config_path = _write_config(
            {
                "project": "test",
                "notifications": {"backend": "ntfy", "ntfy": {"priority": "high"}},
            },
            tmp_path,
        )
        import logging

        with (
            patch.dict("os.environ", {}, clear=True),
            caplog.at_level(logging.WARNING, logger="theforge.config"),
        ):
            config = load_config(config_path)
        assert config.notifications.ntfy is None
        assert any("no URL" in r.message for r in caplog.records)


class TestLocalModelRegistry:
    """The four local model keys must be present in MODEL_REGISTRY with correct metadata."""

    LOCAL_KEYS = [
        "openai/codestral",
        "openai/deepseek-coder",
        "openai/llama3.1",
        "openai/qwen2.5-coder",
    ]

    def test_all_local_keys_present(self):
        for key in self.LOCAL_KEYS:
            assert key in MODEL_REGISTRY, f"Missing local model key: {key}"

    def test_local_models_cost_rank_one(self):
        for key in self.LOCAL_KEYS:
            assert MODEL_REGISTRY[key].cost_rank == 1, f"{key} should have cost_rank=1"

    def test_local_models_dev_capable(self):
        for key in self.LOCAL_KEYS:
            assert MODEL_REGISTRY[key].dev_capable is True, f"{key} should be dev_capable"

    def test_local_models_provider_prefix_is_openai(self):
        """'openai/' prefix ensures routing through the existing OpenAI adapter."""
        for key in self.LOCAL_KEYS:
            assert key.startswith("openai/"), f"{key} must use 'openai/' prefix"


class TestCurrentGenModelRegistry:
    """Current-gen Gemini 3.1 and GPT-5.4 family models are registered."""

    @pytest.mark.parametrize(
        (
            "key",
            "cli",
            "provider",
            "model",
            "tier",
            "capability",
            "cost_rank",
            "dev_capable",
        ),
        [
            (
                "google/gemini-3-flash-preview",
                "gemini",
                None,
                "gemini-3-flash-preview",
                "cheap",
                7,
                1,
                False,
            ),
            (
                "google/gemini-3.1-pro-preview",
                "gemini",
                None,
                "gemini-3.1-pro-preview",
                "strong",
                9,
                2,
                False,
            ),
            (
                "deepseek/deepseek-reasoner",
                None,
                "deepseek",
                "deepseek-reasoner",
                "strong",
                9,
                2,
                True,
            ),
            (
                "openai/gpt-5.4-mini",
                "codex",
                None,
                "gpt-5.4-mini",
                "cheap",
                7,
                1,
                True,
            ),
            (
                "openai/gpt-5.4-pro",
                "codex",
                None,
                "gpt-5.4-pro",
                "strong",
                10,
                3,
                True,
            ),
        ],
    )
    def test_current_gen_models_present_with_expected_metadata(
        self, key, cli, provider, model, tier, capability, cost_rank, dev_capable
    ):
        info = MODEL_REGISTRY[key]
        assert info.cli == cli
        assert info.provider == provider
        assert info.model == model
        assert info.tier == tier
        assert info.capability == capability
        assert info.cost_rank == cost_rank
        assert info.dev_capable is dev_capable


class TestSprintConfig:
    """Tests for forge.yaml sprint.max_parallel parsing."""

    def test_sprint_max_parallel_parsed(self, tmp_path):
        """forge.yaml with sprint.max_parallel: 3 → config.sprint.max_parallel == 3."""
        config_path = _write_config({"project": "test", "sprint": {"max_parallel": 3}}, tmp_path)
        config = load_config(config_path)
        assert config.sprint.max_parallel == 3

    def test_sprint_section_absent_defaults_to_1(self, tmp_path):
        """No sprint section → config.sprint.max_parallel defaults to 1."""
        config_path = _write_config({"project": "test"}, tmp_path)
        config = load_config(config_path)
        assert config.sprint.max_parallel == 1

    def test_sprint_max_parallel_zero_raises(self, tmp_path):
        """sprint.max_parallel: 0 → raises ValueError."""
        config_path = _write_config({"project": "test", "sprint": {"max_parallel": 0}}, tmp_path)
        with pytest.raises(ValueError, match="max_parallel"):
            load_config(config_path)

    def test_sprint_max_parallel_non_integer_raises(self, tmp_path):
        """sprint.max_parallel: 'abc' → raises ValueError."""
        config_path = _write_config(
            {"project": "test", "sprint": {"max_parallel": "abc"}}, tmp_path
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_config(config_path)
