"""YAML loading and ForgeConfig construction."""

from __future__ import annotations

import dataclasses
import importlib
import logging
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from ._loaders import _parse_plan_agent_review, _parse_workspace, _validate_v0_8_schema
from .auth import check_agent_auth
from .bridge import role_assignment_to_profiles
from .defaults import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    PROVIDER_SDK_MAP,
    SUPPORTED_CLIS,
)
from .models import _PROVIDER_CLI_MAP, MODEL_REGISTRY, _parse_agents, _parse_assignment
from .profiles import (
    CLI_PROVIDER_MAP,
    _apply_profile_overrides,
    _apply_provider_fallback,
    _parse_profile,
    _parse_provider_fallbacks,
)
from .role_derivation import derive_roles
from .secrets import _parse_notifications
from .types import (
    SUPPORTED_PROVIDERS,
    ContextConfig,
    FindingClassifierConfig,
    ForgeConfig,
    GithubConfig,
    HardConventionsConfig,
    HooksConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    SprintConfig,
    ValidationConfig,
)

log = logging.getLogger("theforge.config")


def _validate_plan_provider(plan_cfg: "PlanConfig", secrets: dict[str, str]) -> None:
    """Raise ValueError if plan_cfg has an invalid or unconfigured provider.

    Called both from load_config (YAML path) and after --plan-model CLI override.
    """
    if plan_cfg.provider is None:
        return
    if plan_cfg.provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider {plan_cfg.provider!r} in plan section. "
            f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
        )
    sdk = PROVIDER_SDK_MAP.get(plan_cfg.provider)
    if sdk:
        try:
            importlib.import_module(sdk)
        except ImportError:
            raise ValueError(
                f"plan section uses provider '{plan_cfg.provider}' but the required "
                f"SDK '{sdk}' is not installed. Please install it."
            )
    # Build a stub profile to reuse check_agent_auth for key validation
    _stub = ModelProfile(
        name="plan",
        cli=None,
        provider=plan_cfg.provider,
        model=plan_cfg.model,
        budget_usd=plan_cfg.budget_usd,
        timeout_seconds=plan_cfg.timeout,
        allowed_tools=(),
    )
    _ready, _reason = check_agent_auth(_stub, secrets)
    if not _ready:
        raise ValueError(f"plan section uses provider '{plan_cfg.provider}': {_reason}")


def load_config(config_path: Path) -> ForgeConfig:
    """Load forge.yaml and return a typed ForgeConfig.

    The config file path is used to derive the project root (its parent directory).
    Missing sections fall back to sensible defaults.

    Raises ValueError for invalid configurations (empty pool, duplicate names,
    unsupported CLI, missing synthesis profile when pool size > 1).
    """
    project_root = config_path.parent.resolve()

    # Load project-scoped secrets before profile validation so _resolve_secret() works.
    env_path = project_root / ".forge" / ".env"
    secrets_yaml_path = project_root / ".forge" / "secrets.yaml"
    secrets: dict[str, str] = {}
    if env_path.exists():
        raw = dotenv_values(env_path)
        if any(v is None for v in raw.values()):
            raise ValueError(f"{env_path}: malformed .env")
        secrets = {k: v for k, v in raw.items() if v is not None}
    elif secrets_yaml_path.exists():
        log.warning(
            "⚠ .forge/secrets.yaml detected — migrate to .forge/.env (see .forge/.env.example)"
        )

    with open(config_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    _validate_v0_8_schema(raw)

    provider_fallbacks = _parse_provider_fallbacks(
        raw.get("provider_fallbacks", {}),
        secrets=secrets,
    )

    workspace = _parse_workspace(raw.get("workspace", {}))

    # Validation
    val_data = raw.get("validation", {})
    validation = ValidationConfig(
        gate_command=val_data.get("gate_command", DEFAULT_VALIDATION.gate_command),
        handoff_file=val_data.get("handoff_file", DEFAULT_VALIDATION.handoff_file),
        gate_decision_key=val_data.get("gate_decision_key", DEFAULT_VALIDATION.gate_decision_key),
        gate_timeout=val_data.get("gate_timeout"),
        gate_output_tail_chars=int(
            val_data.get("gate_output_tail_chars", DEFAULT_VALIDATION.gate_output_tail_chars)
        ),
        gate_debug_command=val_data.get("gate_debug_command"),
        test_command=val_data.get("test_command"),
        pre_validate_command=val_data.get("pre_validate_command"),
    )

    # ── Smart config: models key ──────────────────────────────────────
    smart_config_models: list[str] | None = None
    _review_pool_is_default = False
    _derived_plan_profile: ModelProfile | None = None
    _derived_plan_validate_spec: bool | None = None
    _derived_par_profile: ModelProfile | None = None

    if "models" in raw:
        models_list = raw["models"]
        if not isinstance(models_list, list) or len(models_list) == 0:
            raise ValueError("'models' must be a non-empty list")
        for m in models_list:
            if "/" not in str(m):
                raise ValueError(
                    f"Model entry {m!r} must be in 'provider/model' format (contains '/')"
                )
            provider = str(m).split("/", 1)[0]
            if str(m) not in MODEL_REGISTRY and provider not in _PROVIDER_CLI_MAP:
                raise ValueError(
                    f"Unknown provider {provider!r} in model {m!r}. "
                    f"Supported providers: {sorted(_PROVIDER_CLI_MAP)}. "
                    "Or add the model to MODEL_REGISTRY."
                )
        budget_usd_raw = raw.get("budget_usd", 50.0)
        budget_usd_val = float(budget_usd_raw)
        if budget_usd_val <= 0:
            raise ValueError("budget_usd must be positive")

        # v0.8: overrides: key replaces the classic profiles: key for partial overrides.
        # plan_agent_review overrides are passed into derive_roles() so the bridge
        # can lower them to a ModelProfile (fixes silent loss of that config).
        overrides = raw.get("overrides", {})
        _par_derive_overrides: dict[str, Any] | None = (
            {"plan_agent_review": overrides["plan_agent_review"]}
            if "plan_agent_review" in overrides
            else None
        )
        _ra = derive_roles(
            [str(m) for m in models_list],
            overrides=_par_derive_overrides,
            budget_usd=budget_usd_val,
        )
        _bridge = role_assignment_to_profiles(_ra)
        dev_profile = _bridge["dev_profile"]
        preflight_profile = _bridge["preflight_profile"]
        review_pool = _bridge["review_pool"]
        synthesis_profile = _bridge["synthesis_profile"]
        _derived_plan_profile = _bridge["plan_profile"]
        _derived_plan_validate_spec = _bridge["plan_validate_spec"]
        _derived_par_profile = _bridge.get("plan_agent_review_profile")

        # Apply explicit profile overrides (partial override supported)
        if "dev" in overrides:
            dev_profile = _apply_profile_overrides(dev_profile, overrides["dev"])
        if "preflight" in overrides:
            preflight_profile = _apply_profile_overrides(preflight_profile, overrides["preflight"])
        if synthesis_profile is not None and "synthesis" in overrides:
            synthesis_profile = _apply_profile_overrides(synthesis_profile, overrides["synthesis"])
        # Apply per-reviewer overrides matched by name
        if "review_pool" in overrides:
            pool_overrides = overrides["review_pool"]
            if isinstance(pool_overrides, list):
                override_by_name: dict[str, dict[str, Any]] = {
                    e["name"]: e for e in pool_overrides if isinstance(e, dict) and "name" in e
                }
                review_pool = [
                    _apply_profile_overrides(p, override_by_name[p.name])
                    if p.name in override_by_name
                    else p
                    for p in review_pool
                ]

        smart_config_models = [str(m) for m in models_list]
        # The review pool is auto-assigned from the models list, not user-specified.
        # Treat it as default so the assignment reviewer auth guard applies.
        _review_pool_is_default = True

    else:
        # ── Classic config: profiles key ──────────────────────────────────
        profiles = raw.get("profiles", {})
        dev_profile = (
            _parse_profile("dev", profiles["dev"], role="dev", secrets=secrets)
            if "dev" in profiles
            else DEFAULT_DEV_PROFILE
        )
        preflight_profile = (
            _parse_profile("preflight", profiles["preflight"], role="preflight", secrets=secrets)
            if "preflight" in profiles
            else DEFAULT_PREFLIGHT_PROFILE
        )

        # review_pool precedence: review_pool > review > default
        if "review_pool" in profiles:
            pool_data = profiles["review_pool"]
            if not isinstance(pool_data, list) or len(pool_data) == 0:
                raise ValueError("profiles.review_pool must be a non-empty list")
            names = [e.get("name") for e in pool_data]
            if any(n is None for n in names):
                raise ValueError("Each profiles.review_pool entry must have a 'name' field")
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate names in profiles.review_pool: {names}")
            review_pool = [
                _parse_profile(e["name"], e, role="review", secrets=secrets) for e in pool_data
            ]
            if "synthesis" in profiles:
                synthesis_profile = _parse_profile(
                    "synthesis", profiles["synthesis"], role="review", secrets=secrets
                )
            else:
                synthesis_profile = None

        elif "review" in profiles:
            review_pool = [
                _parse_profile("review", profiles["review"], role="review", secrets=secrets)
            ]
            synthesis_profile = None

        else:
            review_pool = [DEFAULT_REVIEW_PROFILE]
            synthesis_profile = None
            _review_pool_is_default = True

    dev_profile = _apply_provider_fallback(dev_profile, provider_fallbacks)
    preflight_profile = _apply_provider_fallback(preflight_profile, provider_fallbacks)
    review_pool = [
        _apply_provider_fallback(profile, provider_fallbacks) for profile in review_pool
    ]
    if synthesis_profile is not None:
        synthesis_profile = _apply_provider_fallback(synthesis_profile, provider_fallbacks)

    # smart_config_models — escalation chain; works alongside explicit profiles
    if smart_config_models is None and "smart_config_models" in raw:
        models_raw = raw["smart_config_models"]
        if isinstance(models_raw, list) and models_raw:
            smart_config_models = [str(m) for m in models_raw]

    # Retry
    retry_data = raw.get("retry", {})
    retry = RetryPolicy(
        max_dev_iterations=int(retry_data.get("max_dev_iterations", 3)),
        max_review_cycles=int(retry_data.get("max_review_cycles", 2)),
        max_review_parse_retries=int(retry_data.get("max_review_parse_retries", 2)),
        max_handoff_retries=int(retry_data.get("max_handoff_retries", 2)),
        max_plan_regen_attempts=int(retry_data.get("max_plan_regen_attempts", 3)),
        demotion_threshold=int(retry_data.get("demotion_threshold", 2)),
        escalate_policy=str(retry_data.get("escalate_policy", "prompt")),
        auto_model_escalation=bool(retry_data.get("auto_model_escalation", False)),
    )

    notifications = _parse_notifications(raw.get("notifications", {}), secrets)

    github_data = raw.get("github", {})
    github_cfg = GithubConfig(enabled=bool(github_data.get("enabled", False)))

    # Plan
    plan_data = raw.get("plan", {})

    # model_name deprecation: map to model and emit a warning
    if "model_name" in plan_data and "model" not in plan_data:
        log.warning("plan.model_name is deprecated — use plan.model instead")
        plan_data = {**plan_data, "model": plan_data["model_name"]}

    _plan_model_is_default = (
        "cli" not in plan_data and "model" not in plan_data and "provider" not in plan_data
    )

    # Mutual exclusivity check before construction
    if "cli" in plan_data and "provider" in plan_data and bool(plan_data.get("enabled", False)):
        raise ValueError(
            "forge.yaml plan section cannot have both 'cli' and 'provider' set. Use one."
        )

    plan_timeout_medium_raw = plan_data.get("timeout_medium")
    plan_timeout_large_raw = plan_data.get("timeout_large")

    # Smart-config: when the user supplies `models:` and has not overridden the
    # plan section's transport/model, source PlanConfig from the derived plan
    # role so adaptive routing actually reaches the PLAN phase. Otherwise fall
    # back to the legacy defaults (cli=claude, model=sonnet).
    if _derived_plan_profile is not None and _plan_model_is_default:
        _plan_default_cli: str | None = _derived_plan_profile.cli
        _plan_default_model: str = _derived_plan_profile.model
        _plan_default_provider: str | None = _derived_plan_profile.provider
        _plan_default_budget: float = _derived_plan_profile.budget_usd
        _plan_default_timeout: int = _derived_plan_profile.timeout_seconds
        _plan_default_timeout_medium: int | None = _derived_plan_profile.timeout_medium_seconds
        _plan_default_timeout_large: int | None = _derived_plan_profile.timeout_large_seconds
        _plan_default_validate_spec: bool = (
            _derived_plan_validate_spec if _derived_plan_validate_spec is not None else True
        )
    else:
        _plan_default_cli = "claude"
        _plan_default_model = "sonnet"
        _plan_default_provider = None
        _plan_default_budget = 0.50
        _plan_default_timeout = 600
        _plan_default_timeout_medium = None
        _plan_default_timeout_large = None
        _plan_default_validate_spec = True

    _plan_provider = plan_data.get("provider", _plan_default_provider)
    _plan_cli = plan_data.get("cli", _plan_default_cli) if _plan_provider is None else None
    plan_cfg = PlanConfig(
        enabled=bool(plan_data.get("enabled", False)),
        cli=_plan_cli,
        model=str(plan_data.get("model", _plan_default_model)),
        provider=_plan_provider,
        budget_usd=float(plan_data.get("budget_usd", _plan_default_budget)),
        timeout=int(plan_data.get("timeout", _plan_default_timeout)),
        timeout_medium=int(plan_timeout_medium_raw)
        if plan_timeout_medium_raw is not None
        else _plan_default_timeout_medium,
        timeout_large=int(plan_timeout_large_raw)
        if plan_timeout_large_raw is not None
        else _plan_default_timeout_large,
        validate_spec=bool(plan_data.get("validate_spec", _plan_default_validate_spec)),
    )

    # ── Load-time validation for plan section ────────────────────────────
    if plan_cfg.enabled:
        if plan_cfg.cli is not None and plan_cfg.cli not in SUPPORTED_CLIS:
            raise ValueError(
                f"Unsupported CLI {plan_cfg.cli!r} in plan section. "
                f"Supported: {sorted(SUPPORTED_CLIS)}"
            )
        _validate_plan_provider(plan_cfg, secrets)

    # Plan review
    plan_review_data = raw.get("plan_review", {})
    plan_review_cfg = PlanReviewConfig(
        enabled=bool(plan_review_data.get("enabled", False)),
        mode=str(plan_review_data.get("mode", "blocking")),
        timeout_seconds=int(plan_review_data.get("timeout_seconds", 14400)),
    )

    agents_list = _parse_agents(raw.get("agents", []))
    agents_list = [
        agent
        if agent.provider or not agent.cli
        else dataclasses.replace(
            agent,
            api_fallback=provider_fallbacks.get(CLI_PROVIDER_MAP.get(agent.cli, "")),
        )
        for agent in agents_list
    ]
    assignment_cfg = _parse_assignment(raw.get("assignment", {}))

    _raw_par = raw.get("plan_agent_review", {})
    if not _raw_par and _derived_par_profile is not None:
        # v0.8: plan_agent_review was configured via overrides.plan_agent_review;
        # the bridge lowered it to a ModelProfile. Wrap it in PlanAgentReviewConfig.
        plan_agent_review_cfg = PlanAgentReviewConfig(enabled=True, pool=[_derived_par_profile])
    else:
        plan_agent_review_cfg = _parse_plan_agent_review(
            _raw_par,
            secrets,
            plan_cfg,
            agents_list,
            assignment_cfg.enabled,
            _plan_model_is_default,
        )
    if plan_agent_review_cfg.pool:
        plan_agent_review_cfg = dataclasses.replace(
            plan_agent_review_cfg,
            pool=[
                _apply_provider_fallback(profile, provider_fallbacks)
                for profile in plan_agent_review_cfg.pool
            ],
        )

    # Logging
    log_data = raw.get("logging", {})
    log_cfg = LogConfig(
        log_file=str(log_data.get("log_file", LogConfig.log_file)),
        enabled=bool(log_data.get("enabled", True)),
    )

    # Hooks
    hooks_data = raw.get("hooks")
    hooks_cfg: HooksConfig | None = None
    if hooks_data:
        hooks_cfg = HooksConfig(
            post_run=hooks_data.get("post_run"),
            post_merge=hooks_data.get("post_merge"),
            post_sprint=hooks_data.get("post_sprint"),
            pre_run=hooks_data.get("pre_run"),
            timeout_seconds=int(hooks_data.get("timeout_seconds", 30)),
        )

    # ── Assignment reviewer auth cross-check ────────────────────────────
    # When assignment is enabled, every review cycle will select from mid/strong
    # agents (PHASE_TIER["code_review"] in assignment.py maps all complexity levels
    # to "mid" or "strong").  Raise at load time if all reviewer-eligible agents
    # lack auth so the sprint fails fast rather than at the first review cycle.
    #
    # Skip the check when an explicit review_pool is configured: preflight_flow
    # preserves explicit reviewer overrides and bypasses adaptive code-reviewer
    # selection entirely (mirrors the `review_pool not in _explicit_roles` guard
    # in preflight_flow.py).
    _adaptive_reviewers_active = assignment_cfg.enabled and _review_pool_is_default
    if _adaptive_reviewers_active and agents_list:
        _REVIEWER_TIERS = {"mid", "strong"}
        reviewer_candidates = [a for a in agents_list if a.tier in _REVIEWER_TIERS]
        if reviewer_candidates:
            failed_agents: list[tuple[str, str]] = []
            for _agent in reviewer_candidates:
                _ready, _reason = check_agent_auth(_agent.to_model_profile(), secrets)
                if not _ready:
                    failed_agents.append((_agent.name, _reason))
            if len(failed_agents) == len(reviewer_candidates):
                _names = ", ".join(f"{n!r} ({r})" for n, r in failed_agents)
                raise ValueError(
                    f"assignment.enabled is true but no reviewer-eligible agents have auth. "
                    f"Failed: {_names}"
                )

    # Sprint config
    sprint_data = raw.get("sprint", {})
    sprint_max_parallel_raw = sprint_data.get("max_parallel", 1)
    if not isinstance(sprint_max_parallel_raw, int):
        raise ValueError(
            f"forge.yaml 'sprint.max_parallel' must be an integer, got {sprint_max_parallel_raw!r}"
        )
    if sprint_max_parallel_raw < 1:
        raise ValueError(
            f"forge.yaml 'sprint.max_parallel' must be >= 1, got {sprint_max_parallel_raw}"
        )
    sprint_worker_timeout_raw = sprint_data.get("worker_timeout_seconds", 3600)
    if not isinstance(sprint_worker_timeout_raw, int):
        raise ValueError(
            f"forge.yaml 'sprint.worker_timeout_seconds' must be an integer, "
            f"got {sprint_worker_timeout_raw!r}"
        )
    if sprint_worker_timeout_raw < 1:
        raise ValueError(
            f"forge.yaml 'sprint.worker_timeout_seconds' must be >= 1, "
            f"got {sprint_worker_timeout_raw}"
        )
    sprint_cfg = SprintConfig(
        max_parallel=sprint_max_parallel_raw,
        worker_timeout_seconds=sprint_worker_timeout_raw,
    )

    context_data = raw.get("context", {})
    context_cfg = ContextConfig(
        preflight_budget=int(context_data.get("preflight_budget", ContextConfig.preflight_budget)),
        plan_budget=int(context_data.get("plan_budget", ContextConfig.plan_budget)),
        dev_budget=int(context_data.get("dev_budget", ContextConfig.dev_budget)),
        review_budget=int(context_data.get("review_budget", ContextConfig.review_budget)),
    )

    # Conventions config — soft
    conventions_soft_raw = raw.get("conventions", {}).get("soft", [])
    if not isinstance(conventions_soft_raw, list):
        raise ValueError(
            "forge.yaml 'conventions.soft' must be a list of strings,"
            f" got {conventions_soft_raw!r}"
        )
    for _item in conventions_soft_raw:
        if not isinstance(_item, str):
            raise ValueError(f"forge.yaml 'conventions.soft' items must be strings, got {_item!r}")
    conventions_soft_list: list[str] = conventions_soft_raw

    # Conventions config — hard
    conventions_hard_raw = raw.get("conventions", {}).get("hard", None)
    if conventions_hard_raw is None:
        conventions_hard_cfg: HardConventionsConfig | None = None
    else:
        _max_module = conventions_hard_raw.get("max_module_lines", 500)
        _max_test = conventions_hard_raw.get("max_test_file_lines", 1000)
        _no_circular = conventions_hard_raw.get("no_circular_imports", True)
        _test_mirrors = conventions_hard_raw.get("test_mirrors_source", True)
        _no_scratch = conventions_hard_raw.get("no_scratch_files", True)
        if not isinstance(_max_module, int):
            raise ValueError(
                "forge.yaml 'conventions.hard.max_module_lines' must be an int,"
                f" got {_max_module!r}"
            )
        if not isinstance(_max_test, int):
            raise ValueError(
                "forge.yaml 'conventions.hard.max_test_file_lines' must be an int,"
                f" got {_max_test!r}"
            )
        if not isinstance(_no_circular, bool):
            raise ValueError(
                "forge.yaml 'conventions.hard.no_circular_imports' must be a bool,"
                f" got {_no_circular!r}"
            )
        if not isinstance(_test_mirrors, bool):
            raise ValueError(
                "forge.yaml 'conventions.hard.test_mirrors_source' must be a bool,"
                f" got {_test_mirrors!r}"
            )
        if not isinstance(_no_scratch, bool):
            raise ValueError(
                "forge.yaml 'conventions.hard.no_scratch_files' must be a bool,"
                f" got {_no_scratch!r}"
            )
        conventions_hard_cfg = HardConventionsConfig(
            max_module_lines=_max_module,
            max_test_file_lines=_max_test,
            no_circular_imports=_no_circular,
            test_mirrors_source=_test_mirrors,
            no_scratch_files=_no_scratch,
        )

    _fc_raw = raw.get("finding_classifier", {})
    _allow_bypass = _fc_raw.get("allow_net_new_bypass", False)
    if not isinstance(_allow_bypass, bool):
        raise ValueError(
            "forge.yaml 'finding_classifier.allow_net_new_bypass' must be a bool,"
            f" got {_allow_bypass!r}"
        )
    finding_classifier_cfg = FindingClassifierConfig(allow_net_new_bypass=_allow_bypass)

    return ForgeConfig(
        project=raw.get("project", project_root.name),
        project_root=project_root,
        workspace=workspace,
        validation=validation,
        dev_profile=dev_profile,
        preflight_profile=preflight_profile,
        review_pool=review_pool,
        synthesis_profile=synthesis_profile,
        retry=retry,
        notifications=notifications,
        github=github_cfg,
        smart_config_models=smart_config_models,
        plan=plan_cfg,
        plan_review=plan_review_cfg,
        plan_agent_review=plan_agent_review_cfg,
        log=log_cfg,
        hooks=hooks_cfg,
        sprint=sprint_cfg,
        context=context_cfg,
        secrets=secrets,
        agents=agents_list,
        assignment=assignment_cfg,
        provider_fallbacks=provider_fallbacks,
        review_pool_is_default=_review_pool_is_default,
        plan_model_is_default=_plan_model_is_default,
        conventions_hard=conventions_hard_cfg,
        conventions_soft=conventions_soft_list,
        finding_classifier=finding_classifier_cfg,
    )
