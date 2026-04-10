"""Section-specific YAML parsing helpers extracted from load_config."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from .auth import check_agent_auth
from .defaults import DEFAULT_WORKSPACE, PROVIDER_SDK_MAP, SUPPORTED_CLIS
from .models import AgentDef, _planner_candidate_models
from .profiles import _parse_profile
from .types import (
    SUPPORTED_PROVIDERS,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    WorkspaceConfig,
)

log = logging.getLogger("theforge.config")


def _parse_workspace(ws_data: dict[str, Any]) -> WorkspaceConfig:
    """Parse workspace section from raw YAML dict."""
    on_approve = {"ask": "pr"}.get(
        str(ws_data.get("on_approve", "none")),
        str(ws_data.get("on_approve", "none")),
    )
    auto_push = bool(ws_data.get("auto_push", DEFAULT_WORKSPACE.auto_push))
    if on_approve == "merge-pr" and not auto_push:
        raise ValueError(
            "on_approve: merge-pr requires auto_push: true — PR creation needs a remote branch"
        )
    merge_strategy = str(ws_data.get("merge_strategy", DEFAULT_WORKSPACE.merge_strategy))
    if on_approve == "merge-pr":
        _valid_strategies = {"merge", "squash", "rebase"}
        if merge_strategy not in _valid_strategies:
            raise ValueError(
                "merge_strategy must be one of "
                f"{sorted(_valid_strategies)}, got {merge_strategy!r}"
            )
    return WorkspaceConfig(
        create_command=ws_data.get("create_command", DEFAULT_WORKSPACE.create_command),
        path_pattern=ws_data.get("path_pattern", DEFAULT_WORKSPACE.path_pattern),
        branch_pattern=ws_data.get("branch_pattern", DEFAULT_WORKSPACE.branch_pattern),
        base_branch=ws_data.get("base_branch", DEFAULT_WORKSPACE.base_branch),
        stale_worktree_days=ws_data.get(
            "stale_worktree_days", DEFAULT_WORKSPACE.stale_worktree_days
        ),
        auto_push=auto_push,
        setup_command=ws_data.get("setup_command", DEFAULT_WORKSPACE.setup_command),
        on_approve=on_approve,
        merge_strategy=merge_strategy,
        pr_labels=tuple(ws_data.get("pr_labels", [])),
        pr_draft=bool(ws_data.get("pr_draft", DEFAULT_WORKSPACE.pr_draft)),
        ci_check_timeout_seconds=int(
            ws_data.get("ci_check_timeout_seconds", DEFAULT_WORKSPACE.ci_check_timeout_seconds)
        ),
        merge_wait_timeout_seconds=int(
            ws_data.get("merge_wait_timeout_seconds", DEFAULT_WORKSPACE.merge_wait_timeout_seconds)
        ),
    )


def _parse_plan_agent_review(
    par_data: dict[str, Any],
    secrets: dict[str, str],
    plan_cfg: PlanConfig,
    agents_list: list[AgentDef],
    assignment_cfg_enabled: bool,
    plan_model_is_default: bool,
) -> PlanAgentReviewConfig:
    """Parse plan_agent_review section and validate planner/reviewer independence."""
    par_enabled = bool(par_data.get("enabled", False))
    par_cli = par_data.get("cli")
    par_provider = par_data.get("provider")

    if par_enabled:
        if par_cli and par_provider:
            raise ValueError(
                "plan_agent_review cannot have both 'cli' and 'provider' set. Use one."
            )
        if not par_cli and not par_provider:
            par_cli = "claude"

        if par_cli and par_cli not in SUPPORTED_CLIS:
            raise ValueError(
                f"Unsupported CLI {par_cli!r} in plan_agent_review. "
                f"Supported: {sorted(SUPPORTED_CLIS)}"
            )
        if par_provider:
            if par_provider not in SUPPORTED_PROVIDERS:
                raise ValueError(
                    f"Unsupported provider {par_provider!r} in plan_agent_review. "
                    f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
                )
            sdk = PROVIDER_SDK_MAP.get(par_provider)
            if sdk:
                try:
                    importlib.import_module(sdk)
                except ImportError:
                    raise ValueError(
                        f"plan_agent_review uses provider '{par_provider}' but the required "
                        f"SDK '{sdk}' is not installed. Please install it."
                    )
            _stub = ModelProfile(
                name="plan_agent_review",
                cli=None,
                provider=par_provider,
                model="",
                budget_usd=0.0,
                timeout_seconds=0,
                allowed_tools=(),
            )
            _ready, _reason = check_agent_auth(_stub, secrets)
            if not _ready:
                raise ValueError(f"plan_agent_review uses provider '{par_provider}': {_reason}")

    par_pool: list[ModelProfile] = []
    if "pool" in par_data:
        pool_data = par_data["pool"]
        if not isinstance(pool_data, list) or len(pool_data) == 0:
            raise ValueError("plan_agent_review.pool must be a non-empty list")
        pool_names = [e.get("name") for e in pool_data]
        if any(n is None for n in pool_names):
            raise ValueError("Each plan_agent_review.pool entry must have a 'name' field")
        if len(pool_names) != len(set(pool_names)):
            raise ValueError(f"Duplicate names in plan_agent_review.pool: {pool_names}")
        par_pool = [
            _parse_profile(e["name"], e, role="review", secrets=secrets) for e in pool_data
        ]

    plan_agent_review_cfg = PlanAgentReviewConfig(
        enabled=par_enabled,
        cli=par_cli,
        provider=par_provider,
        model=str(par_data.get("model", "sonnet")),
        budget_usd=float(par_data.get("budget_usd", 0.50)),
        timeout=int(par_data.get("timeout", 300)),
        pool=par_pool,
        min_reviewers=int(par_data.get("min_reviewers", 1)),
    )

    if plan_agent_review_cfg.enabled:
        if assignment_cfg_enabled and agents_list and plan_model_is_default:
            planner_models = _planner_candidate_models(agents_list)
        else:
            planner_models = {plan_cfg.model}

        for profile in plan_agent_review_cfg.profiles:
            if profile.model in planner_models:
                raise ValueError(
                    f"plan_agent_review member '{profile.name}' uses model '{profile.model}' "
                    "which matches the planner — the reviewer must use a different model "
                    "for independent review."
                )

    return plan_agent_review_cfg
