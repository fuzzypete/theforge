"""TheForge configuration package.

Re-exports the full public API from sub-modules so that all existing
``from theforge.config import X`` and ``from .config import X`` statements
continue to work unchanged.
"""

from __future__ import annotations

from .defaults import (
    API_PROVIDER_DEFAULT_TOOLS,
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    DEFAULT_WORKSPACE,
    PROVIDER_API_KEY_MAP,
    PROVIDER_SDK_MAP,
    SUPPORTED_CLIS,
    generate_default_config,
)
from .load import load_config
from .models import (
    _PROVIDER_CLI_MAP,
    MODEL_REGISTRY,
    AgentDef,
    ModelInfo,
    _planner_candidate_models,
)
from .profiles import (
    _apply_profile_overrides,
    _auto_assign_models,
    _parse_profile,
    _resolve_model_info,
)
from .secrets import _resolve_secret
from .types import (
    SUPPORTED_PROVIDERS,
    AssignmentConfig,
    BackendConfig,
    EmailConfig,
    ForgeConfig,
    HooksConfig,
    LogConfig,
    ModelProfile,
    NotificationConfig,
    NtfyConfig,
    PlanAgentReviewConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    SlackConfig,
    SprintConfig,
    ValidationConfig,
    WorkspaceConfig,
)

__all__ = [
    # types
    "AgentDef",
    "AssignmentConfig",
    "BackendConfig",
    "EmailConfig",
    "ForgeConfig",
    "HooksConfig",
    "LogConfig",
    "ModelInfo",
    "ModelProfile",
    "NotificationConfig",
    "NtfyConfig",
    "PlanAgentReviewConfig",
    "PlanConfig",
    "PlanReviewConfig",
    "RetryPolicy",
    "SlackConfig",
    "SprintConfig",
    "SUPPORTED_PROVIDERS",
    "ValidationConfig",
    "WorkspaceConfig",
    # models
    "MODEL_REGISTRY",
    "_PROVIDER_CLI_MAP",
    "_planner_candidate_models",
    # defaults
    "API_PROVIDER_DEFAULT_TOOLS",
    "DEFAULT_DEV_PROFILE",
    "DEFAULT_PREFLIGHT_PROFILE",
    "DEFAULT_REVIEW_PROFILE",
    "DEFAULT_VALIDATION",
    "DEFAULT_WORKSPACE",
    "PROVIDER_API_KEY_MAP",
    "PROVIDER_SDK_MAP",
    "SUPPORTED_CLIS",
    "generate_default_config",
    # secrets
    "_resolve_secret",
    # profiles
    "_apply_profile_overrides",
    "_auto_assign_models",
    "_parse_profile",
    "_resolve_model_info",
    # load
    "load_config",
]
