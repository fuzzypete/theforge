"""TheForge configuration package.

Re-exports the public API from sub-modules. Internal helpers
(underscore-prefixed symbols) are not re-exported here; import them
directly from their owning sub-modules (e.g. ``theforge.config.profiles``).
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
from .load import _validate_plan_provider, load_config
from .models import (
    MODEL_REGISTRY,
    AgentDef,
    ModelInfo,
)
from .types import (
    SUPPORTED_PROVIDERS,
    ApiFallbackConfig,
    AssignmentConfig,
    BackendConfig,
    EmailConfig,
    ForgeConfig,
    GithubConfig,
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
    "ApiFallbackConfig",
    "AssignmentConfig",
    "BackendConfig",
    "EmailConfig",
    "ForgeConfig",
    "GithubConfig",
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
    # models / registry
    "MODEL_REGISTRY",
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
    # load
    "_validate_plan_provider",
    "load_config",
]
