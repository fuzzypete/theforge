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
from .gate_scrub import SCRUBBED_CLI_LAUNCHERS, SCRUBBED_ENV_VARS, SCRUBBED_HOME_PATHS
from .load import _validate_plan_provider, load_config
from .models import (
    AGENT_REGISTRY,
    MODEL_REGISTRY,
    AgentDef,
    AgentSpec,
    ModelInfo,
    TransportSpec,
    apply_model_info,
    canonical_id_for_spec,
    canonical_model_id,
    is_canonical_model_id,
    resolve_agent_spec,
)
from .types import (
    SUPPORTED_PROVIDERS,
    AdvisoryConventionsConfig,
    AdvisoryIssueFilingConfig,
    ApiFallbackConfig,
    AssignmentConfig,
    BackendConfig,
    DiagnoseConfig,
    EmailConfig,
    FindingClassifierConfig,
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
    StuckDetectionConfig,
    ValidationConfig,
    WorkspaceConfig,
)

__all__ = [
    # types
    "AgentDef",
    "AdvisoryConventionsConfig",
    "AdvisoryIssueFilingConfig",
    "ApiFallbackConfig",
    "AssignmentConfig",
    "BackendConfig",
    "DiagnoseConfig",
    "EmailConfig",
    "FindingClassifierConfig",
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
    "StuckDetectionConfig",
    "SUPPORTED_PROVIDERS",
    "ValidationConfig",
    "WorkspaceConfig",
    # models / registry
    "AGENT_REGISTRY",
    "AgentSpec",
    "MODEL_REGISTRY",
    "TransportSpec",
    "apply_model_info",
    "canonical_id_for_spec",
    "canonical_model_id",
    "is_canonical_model_id",
    "resolve_agent_spec",
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
    "SCRUBBED_CLI_LAUNCHERS",
    "SCRUBBED_ENV_VARS",
    "SCRUBBED_HOME_PATHS",
    # load
    "_validate_plan_provider",
    "load_config",
]
