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
    MODEL_REGISTRY,
    AgentDef,
    ModelInfo,
)
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
    "load_config",
]
