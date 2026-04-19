"""Default configuration values, threshold constants, and generate_default_config."""

from __future__ import annotations

from .types import ModelProfile, ValidationConfig, WorkspaceConfig

DEFAULT_DEV_PROFILE = ModelProfile(
    name="dev",
    cli="claude",
    provider=None,
    model="sonnet",
    budget_usd=2.00,
    timeout_seconds=900,
    allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    phase="dev",
)

DEFAULT_REVIEW_PROFILE = ModelProfile(
    name="review",
    cli="claude",
    provider=None,
    model="opus",
    budget_usd=1.00,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash", "Glob", "Grep"),
)

DEFAULT_PREFLIGHT_PROFILE = ModelProfile(
    name="preflight",
    cli="claude",
    provider=None,
    model="sonnet",
    budget_usd=1.00,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash", "Glob", "Grep"),
    phase="preflight",
)

DEFAULT_WORKSPACE = WorkspaceConfig(
    create_command="git worktree add .forge/worktrees/{slug} -b forge/{slug} {base_branch}",
    path_pattern=".forge/worktrees/{slug}",
    branch_pattern="forge/{slug}",
)

DEFAULT_VALIDATION = ValidationConfig(
    gate_command="make gate",
)

# CLIs supported by the runner. Unsupported CLIs are rejected at config load.
SUPPORTED_CLIS: frozenset[str] = frozenset({"claude", "codex", "gemini"})
PROVIDER_SDK_MAP = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google.genai",
    "deepseek": "openai",
}
PROVIDER_API_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
API_PROVIDER_DEFAULT_TOOLS = ("read_file", "bash", "glob", "grep", "submit_review")


def generate_default_config() -> str:
    """Generate a starter forge.yaml content string."""
    return """\
# forge.yaml — TheForge project configuration
# See https://github.com/your-org/theforge for documentation.

project: my-project

workspace:
  # Shell command to create an isolated workspace. {slug} and {base_branch} are replaced.
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} {base_branch}"
  # Optional: run once in the new workspace after creation (e.g. install deps).
  # setup_command: "pip install -e ."
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  # Command to run validation checks. Gate passes on exit 0, fails on non-zero.
  gate_command: "make gate"

# v0.8 simple model list — complexity-aware roles are derived automatically.
models:
  - claude/sonnet
  - claude/opus

budget_usd: 50.0

retry:
  max_dev_iterations: 3    # retries within a review cycle
  max_review_cycles: 2     # full dev→review loops before escalation

context:
  preflight_budget: 200
  plan_budget: 120
  dev_budget: 80
  review_budget: 80

# Optional: same-provider API fallback when a CLI is rate-limited/unavailable.
# provider_fallbacks:
#   openai:
#     model: o4-mini
#   google:
#     model: gemini-2.5-flash
"""
