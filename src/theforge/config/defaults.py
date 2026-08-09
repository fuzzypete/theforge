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
    allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep", "WebFetch"),
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

# Preflight is a read-only classifier and is deliberately denied Bash (#2346).
# Bash is the only tool in this set that can start work the agent cannot be
# resumed for: a detached/background process (``nohup``, ``setsid``, ``&``, a
# nested agent CLI) leaves the model with nothing to do but wait for an event
# the harness has no mechanism to deliver. It then ends its turn holding that
# wait, the runner reads the finished stream as a finished agent, and the
# process is killed when it does not exit within the post-stream grace period —
# a phase that inspected zero files and produced no classification. Read/Glob/
# Grep cover every investigation the prompt actually asks for.
PREFLIGHT_FORBIDDEN_TOOLS: frozenset[str] = frozenset({"bash"})

# The read-only investigation set shared by the *other* inspection roles (plan,
# plan-review, diagnose, escalation advisor). They run to completion inside a
# turn and legitimately shell out, so they keep Bash; only preflight is narrowed
# above. Roles that want an investigation tool surface must name this constant
# rather than reaching for ``config.preflight_profile.allowed_tools``: that
# expression once meant "the investigation set" and now means "the one surface
# that is deliberately narrower than it", so borrowing it silently removes Bash.
DEFAULT_INVESTIGATION_TOOLS: tuple[str, ...] = ("Read", "Bash", "Glob", "Grep")

#: The tool surface preflight runs with when config supplies nothing usable.
PREFLIGHT_READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")


def resolve_preflight_tools(allowed: object) -> tuple[str, ...]:
    """Return the tool surface preflight will actually run with.

    This *resolves* a surface rather than filtering one, and that distinction is
    the whole point. ``allowed_tools`` has an overloaded empty state: every
    construction site reads ``()`` as "no tools were requested, apply a
    default", while ``runner_claude.build_argv`` omits ``--allowedTools``
    entirely for an empty tuple and hands the CLI its *unrestricted* default —
    Bash included. A filter expressed as a diff against config therefore passes
    its single most dangerous input straight through untouched, because ``()``
    already looks like the answer.

    So every input maps to an explicit, non-empty, forbidden-free tuple:

    - forbidden tools are dropped, whatever their casing;
    - an empty result — whether config was empty to begin with, or every tool it
      named was forbidden — falls back to :data:`PREFLIGHT_READ_ONLY_TOOLS`.

    The invariant this guarantees is the one the story needs: the preflight
    invocation always carries an explicit allowlist, and that allowlist never
    grants a tool it could delegate unresumable work with. It lives here, beside
    the constant that defines the forbidden set, so it holds for every source of
    a preflight profile — forge.yaml overrides, API-transport tool defaults, and
    fallback profiles built at dispatch time — instead of only the ones a
    config-load-time validation could see.
    """
    names = tuple(str(t) for t in allowed) if isinstance(allowed, (list, tuple)) else ()
    kept = tuple(t for t in names if t.lower() not in PREFLIGHT_FORBIDDEN_TOOLS)
    return kept or PREFLIGHT_READ_ONLY_TOOLS


DEFAULT_PREFLIGHT_PROFILE = ModelProfile(
    name="preflight",
    cli="claude",
    provider=None,
    model="sonnet",
    budget_usd=1.00,
    timeout_seconds=300,
    allowed_tools=PREFLIGHT_READ_ONLY_TOOLS,
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
# "ghaw" dispatches to GitHub Actions via the `gh` binary (ADR-0004 spike).
SUPPORTED_CLIS: frozenset[str] = frozenset({"claude", "codex", "gemini", "ghaw"})
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
  # Required when setup_command uses {forge_python}: the interpreter this project
  # develops against. TheForge never substitutes its own interpreter here.
  # python_interpreter: "python3.12"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  # Command to run validation checks. Gate passes on exit 0, fails on non-zero.
  gate_command: "make gate"

# v0.8 simple model list — complexity-aware roles are derived automatically.
# Each entry is a canonical model identity: <provider>/<model>/<cli|api>.
# The equivalent mapping form makes the same identity explicit:
#   models:
#     enabled:
#       - provider: anthropic
#         model: sonnet
#         transport:
#           kind: cli
models:
  - anthropic/sonnet/cli
  - anthropic/opus/cli

budget_usd: 50.0

dev:
  p2_policy: in_scope           # "in_scope" | "all" | "p1_only"

retry:
  max_dev_iterations: 3    # retries within a review cycle
  max_dev_transport_retries: 1  # retry one transient dev provider failure
  max_plan_transport_retries: 2  # retry transient plan draft/regen provider failures
  max_review_cycles: 2     # full dev→review loops before escalation
  escalate_policy: prompt  # "prompt" | "auto_approve" | "reject"
  # What an escalate gate that EXPIRES without an operator selection does.
  # "preserve" (default) waits for an operator; "apply_advice" applies the
  # advisory recommendation as if an operator had selected it — except for
  # `elevate` or no usable recommendation, which still wait.
  escalate_timeout_policy: preserve  # "preserve" | "apply_advice"

context:
  preflight_budget: 200
  plan_budget: 120
  dev_budget: 80
  review_budget: 80

# Optional: CLI→API transport fallback for when a CLI is rate-limited or
# unavailable. The provider never changes — only the transport. For any CLI
# model in models: whose same-provider API variant is registered, TheForge wires
# the fallback automatically. Set auto_transport_fallback: false to disable
# auto-pairing, or declare transport_fallback to override it.
# transport_fallback:
#   openai:
#     model: o4-mini
#   google:
#     model: gemini-2.5-flash
"""
