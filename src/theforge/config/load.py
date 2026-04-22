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
from .models import AGENT_REGISTRY, _parse_assignment, resolve_agent_spec
from .profiles import (
    CLI_PROVIDER_MAP,
    _agents_from_models,
    _apply_profile_overrides,
    _apply_provider_fallback,
    _parse_provider_fallbacks,
)
from .role_derivation import derive_roles
from .secrets import _parse_notifications
from .types import (
    SUPPORTED_PROVIDERS,
    ApiFallbackConfig,
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
    ShapeCheckConfig,
    SprintConfig,
    ValidationConfig,
)

log = logging.getLogger("theforge.config")


def _derive_auto_provider_fallbacks(models: list[str]) -> dict[str, ApiFallbackConfig]:
    """Auto-wire same-provider API fallbacks for CLI transport models.

    Returns only unambiguous per-provider fallbacks. If multiple CLI models from the
    same provider appear in models:, auto-wiring is skipped for that provider so we
    never attach the wrong API model to sibling CLI profiles.
    """
    cli_models_by_provider: dict[str, set[str]] = {}
    api_models_by_provider: dict[str, set[str]] = {}

    for spec in AGENT_REGISTRY.values():
        if spec.transport.kind == "api":
            api_models_by_provider.setdefault(spec.provider, set()).add(spec.model)

    for model_key in models:
        spec = resolve_agent_spec(model_key)
        if spec.transport.kind != "cli":
            continue
        cli_models_by_provider.setdefault(spec.provider, set()).add(spec.model)

    fallbacks: dict[str, ApiFallbackConfig] = {}
    for provider, cli_models in cli_models_by_provider.items():
        if len(cli_models) != 1:
            continue
        model = next(iter(cli_models))
        if model not in api_models_by_provider.get(provider, set()):
            continue
        fallbacks[provider] = ApiFallbackConfig(provider=provider, model=model)
    return fallbacks


def _validate_auto_api_fallback_schema(raw: dict[str, Any]) -> None:
    """Reject legacy plan_agent_review scalar config only when auto-pairing needs it.

    v0.8 generally rejects legacy scalar plan_agent_review fields alongside models:.
    For this story we still need to support the legacy scalar shape when it is a CLI
    profile that can receive the same-provider auto API fallback. Keep the integrity
    boundary strict for all other mixed-mode cases.
    """
    if "models" not in raw:
        return
    if not isinstance(raw.get("models"), list):
        return

    par_raw = raw.get("plan_agent_review")
    if not isinstance(par_raw, dict):
        return

    legacy_fields = {"model", "cli", "provider", "budget_usd"} & par_raw.keys()
    if not legacy_fields:
        return

    cli = par_raw.get("cli")
    provider = par_raw.get("provider")
    model = par_raw.get("model")
    if provider is not None or not isinstance(cli, str) or not isinstance(model, str):
        _validate_v0_8_schema(raw)
        return

    model_key = next(
        (
            key
            for key in (raw.get("models") or [])
            if key in AGENT_REGISTRY
            and (spec := resolve_agent_spec(str(key))).transport.kind == "cli"
            and spec.provider == CLI_PROVIDER_MAP.get(cli)
            and spec.model == model
        ),
        None,
    )
    if model_key is None:
        _validate_v0_8_schema(raw)


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

    try:
        _validate_v0_8_schema(raw)
    except ValueError as exc:
        if "plan_agent_review has legacy scalar field(s)" not in str(exc):
            raise
    _validate_auto_api_fallback_schema(raw)

    provider_fallbacks = _parse_provider_fallbacks(
        raw.get("provider_fallbacks", {}),
        secrets=secrets,
    )
    auto_api_fallback = bool(raw.get("auto_api_fallback", True))

    workspace = _parse_workspace(raw.get("workspace", {}))

    # Validation
    val_data = raw.get("validation", {})
    _stale_keys = [k for k in ("handoff_file", "gate_decision_key") if k in val_data]
    if _stale_keys:
        raise ValueError(
            f"forge.yaml sets {_stale_keys} under validation: "
            "— these fields were removed in v0.8. "
            "Gate pass/fail is now determined by exit code only. "
            "Remove handoff_file and gate_decision_key from your forge.yaml."
        )
    validation = ValidationConfig(
        gate_command=val_data.get("gate_command", DEFAULT_VALIDATION.gate_command),
        gate_timeout=val_data.get("gate_timeout"),
        gate_output_tail_chars=int(
            val_data.get("gate_output_tail_chars", DEFAULT_VALIDATION.gate_output_tail_chars)
        ),
        gate_debug_command=val_data.get("gate_debug_command"),
        gate_debug_timeout=val_data.get("gate_debug_timeout"),
        test_command=val_data.get("test_command"),
        pre_validate_command=val_data.get("pre_validate_command"),
    )

    # ── v0.8 models: key ──────────────────────────────────────────────
    models: list[str] | None = None
    _review_pool_is_default = False
    _dev_profile_is_default = False
    _derived_plan_profile: ModelProfile | None = None
    _derived_plan_validate_spec: bool | None = None
    _derived_par_profile: ModelProfile | None = None
    budget_usd_val: float | None = None
    _raw_overrides: dict[str, Any] | None = None

    if "models" in raw:
        models_list = raw["models"]
        if not isinstance(models_list, list) or len(models_list) == 0:
            raise ValueError("'models' must be a non-empty list")
        for m in models_list:
            if "/" not in str(m):
                raise ValueError(
                    f"Model entry {m!r} must be in 'provider/model' format (contains '/')"
                )
            if str(m) not in AGENT_REGISTRY:
                known_providers = sorted({k.split("/", 1)[0] for k in AGENT_REGISTRY})
                raise ValueError(
                    f"Unknown model {m!r}: not in AGENT_REGISTRY. "
                    f"Known providers: {known_providers}. "
                    "Add an explicit registry entry to support a new model."
                )
        budget_usd_raw = raw.get("budget_usd", 50.0)
        budget_usd_val = float(budget_usd_raw)
        if budget_usd_val <= 0:
            raise ValueError("budget_usd must be positive")

        # v0.8: overrides: key carries partial role overrides.
        # plan_agent_review overrides are passed into derive_roles() so the bridge
        # can lower them to a ModelProfile (fixes silent loss of that config).
        overrides = raw.get("overrides") or {}
        _raw_overrides = dict(overrides) if overrides else None
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
        preflight_fallback_profile = None
        if "preflight_fallback" in overrides:
            preflight_fallback_profile = _apply_profile_overrides(
                preflight_profile,
                overrides["preflight_fallback"],
            )
            preflight_fallback_profile = dataclasses.replace(
                preflight_fallback_profile,
                name="preflight_fallback",
                phase="preflight",
            )
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

        models = [str(m) for m in models_list]
        if auto_api_fallback:
            auto_provider_fallbacks = _derive_auto_provider_fallbacks(models)
            provider_fallbacks = {**auto_provider_fallbacks, **provider_fallbacks}
        # Track which roles were auto-derived vs explicitly overridden. Complexity-aware
        # adaptation (preflight._apply_complexity_adaptation) only rewrites auto-derived
        # roles so explicit overrides bypass routing.
        _dev_profile_is_default = "dev" not in overrides
        _review_pool_is_default = "review_pool" not in overrides

    else:
        # No v0.8 models: key — fall back to built-in defaults.
        dev_profile = DEFAULT_DEV_PROFILE
        preflight_profile = DEFAULT_PREFLIGHT_PROFILE
        preflight_fallback_profile = None
        review_pool = [DEFAULT_REVIEW_PROFILE]
        synthesis_profile = None
        _review_pool_is_default = True

    dev_profile = _apply_provider_fallback(dev_profile, provider_fallbacks)
    preflight_profile = _apply_provider_fallback(preflight_profile, provider_fallbacks)
    if preflight_fallback_profile is not None:
        preflight_fallback_profile = _apply_provider_fallback(
            preflight_fallback_profile, provider_fallbacks
        )
    review_pool = [
        _apply_provider_fallback(profile, provider_fallbacks) for profile in review_pool
    ]
    if synthesis_profile is not None:
        synthesis_profile = _apply_provider_fallback(synthesis_profile, provider_fallbacks)

    # Retry
    retry_data = raw.get("retry", {})
    retry = RetryPolicy(
        max_dev_iterations=int(retry_data.get("max_dev_iterations", 3)),
        max_review_cycles=int(retry_data.get("max_review_cycles", 2)),
        max_review_parse_retries=int(retry_data.get("max_review_parse_retries", 2)),
        max_plan_regen_attempts=int(retry_data.get("max_plan_regen_attempts", 3)),
        demotion_threshold=int(retry_data.get("demotion_threshold", 2)),
        escalate_policy=str(retry_data.get("escalate_policy", "prompt")),
        auto_model_escalation=bool(retry_data.get("auto_model_escalation", False)),
        adaptive_iterations=bool(retry_data.get("adaptive_iterations", True)),
        max_dev_iterations_cap=int(retry_data.get("max_dev_iterations_cap", 0)),
        max_review_cycles_cap=int(retry_data.get("max_review_cycles_cap", 0)),
        review_zero_findings_stop=int(retry_data.get("review_zero_findings_stop", 0)),
    )

    notifications = _parse_notifications(raw.get("notifications", {}), secrets)

    github_data = raw.get("github", {})
    github_cfg = GithubConfig(enabled=bool(github_data.get("enabled", False)))

    # Plan
    plan_data = raw.get("plan", {})

    _plan_model_is_default = (
        "cli" not in plan_data and "model" not in plan_data and "provider" not in plan_data
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
        api_fallback=_derived_plan_profile.api_fallback
        if _derived_plan_profile is not None
        else None,
    )
    if plan_cfg.cli is not None:
        plan_profile = ModelProfile(
            name="plan",
            cli=plan_cfg.cli,
            provider=plan_cfg.provider,
            model=plan_cfg.model,
            budget_usd=plan_cfg.budget_usd,
            timeout_seconds=plan_cfg.timeout,
            timeout_medium_seconds=plan_cfg.timeout_medium,
            timeout_large_seconds=plan_cfg.timeout_large,
            allowed_tools=(),
            api_fallback=plan_cfg.api_fallback,
        )
        plan_profile = _apply_provider_fallback(plan_profile, provider_fallbacks)
        plan_cfg = dataclasses.replace(plan_cfg, api_fallback=plan_profile.api_fallback)

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

    # Derive the adaptive agent pool from the v0.8 models: list so adaptive
    # assignment (assign_models) has candidates to pick from. When no models:
    # key is configured, the pool is empty and assignment falls back to the
    # static dev/review profiles above.
    agents_list = _agents_from_models(models, budget_usd_val) if models else []
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
        # v0.8: if overrides.plan_agent_review provided a derived profile but the
        # explicit plan_agent_review: section didn't specify a pool, inject the
        # derived profile so overrides are never silently dropped.
        if _derived_par_profile is not None and not plan_agent_review_cfg.pool:
            plan_agent_review_cfg = dataclasses.replace(
                plan_agent_review_cfg, pool=[_derived_par_profile]
            )
    if plan_agent_review_cfg.pool:
        plan_agent_review_cfg = dataclasses.replace(
            plan_agent_review_cfg,
            pool=[
                _apply_provider_fallback(profile, provider_fallbacks)
                for profile in plan_agent_review_cfg.pool
            ],
        )
    elif plan_agent_review_cfg.cli is not None:
        legacy_plan_review_profile = ModelProfile(
            name="plan-review",
            cli=plan_agent_review_cfg.cli,
            provider=plan_agent_review_cfg.provider,
            model=plan_agent_review_cfg.model or "sonnet",
            budget_usd=plan_agent_review_cfg.budget_usd,
            timeout_seconds=plan_agent_review_cfg.timeout,
            allowed_tools=(),
        )
        legacy_plan_review_profile = _apply_provider_fallback(
            legacy_plan_review_profile, provider_fallbacks
        )
        plan_agent_review_cfg = dataclasses.replace(
            plan_agent_review_cfg,
            api_fallback=legacy_plan_review_profile.api_fallback,
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

    shape_check_data = raw.get("shape_check", {}) or {}
    if not isinstance(shape_check_data, dict):
        raise ValueError(f"forge.yaml 'shape_check' must be a mapping, got {shape_check_data!r}")
    shape_check_classifier = shape_check_data.get("classifier", "heuristic")
    if not isinstance(shape_check_classifier, str) or not shape_check_classifier.strip():
        raise ValueError(
            "forge.yaml 'shape_check.classifier' must be a non-empty string, "
            f"got {shape_check_classifier!r}"
        )
    shape_check_cfg = ShapeCheckConfig(classifier=shape_check_classifier.strip())

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
        _allowed_root_files = conventions_hard_raw.get("allowed_root_files", [])
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
        if not isinstance(_allowed_root_files, list) or not all(
            isinstance(item, str) for item in _allowed_root_files
        ):
            raise ValueError(
                "forge.yaml 'conventions.hard.allowed_root_files' must be a list of strings,"
                f" got {_allowed_root_files!r}"
            )
        conventions_hard_cfg = HardConventionsConfig(
            max_module_lines=_max_module,
            max_test_file_lines=_max_test,
            no_circular_imports=_no_circular,
            test_mirrors_source=_test_mirrors,
            no_scratch_files=_no_scratch,
            allowed_root_files=tuple(_allowed_root_files),
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
        preflight_fallback_profile=preflight_fallback_profile,
        review_pool=review_pool,
        synthesis_profile=synthesis_profile,
        retry=retry,
        notifications=notifications,
        github=github_cfg,
        models=models,
        plan=plan_cfg,
        plan_review=plan_review_cfg,
        plan_agent_review=plan_agent_review_cfg,
        log=log_cfg,
        hooks=hooks_cfg,
        sprint=sprint_cfg,
        shape_check=shape_check_cfg,
        context=context_cfg,
        secrets=secrets,
        agents=agents_list,
        assignment=assignment_cfg,
        provider_fallbacks=provider_fallbacks,
        auto_api_fallback=auto_api_fallback,
        review_pool_is_default=_review_pool_is_default,
        plan_model_is_default=_plan_model_is_default,
        dev_profile_is_default=_dev_profile_is_default,
        conventions_hard=conventions_hard_cfg,
        conventions_soft=conventions_soft_list,
        finding_classifier=finding_classifier_cfg,
        models_budget_usd=budget_usd_val,
        models_overrides=_raw_overrides,
    )
