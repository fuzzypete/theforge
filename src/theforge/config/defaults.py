"""Default configuration values, threshold constants, and generate_default_config."""

from __future__ import annotations

from .model_identity import PHASE_DEV, PHASE_PREFLIGHT, PHASE_REVIEW
from .types import ModelProfile, ValidationConfig, WorkspaceConfig

DEFAULT_DEV_PROFILE = ModelProfile(
    name="dev",
    cli="claude",
    provider=None,
    model="sonnet",
    budget_usd=2.00,
    timeout_seconds=900,
    allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep", "WebFetch"),
    phase=PHASE_DEV,
)

DEFAULT_REVIEW_PROFILE = ModelProfile(
    name="review",
    cli="claude",
    provider=None,
    model="opus",
    budget_usd=1.00,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash", "Glob", "Grep"),
    phase=PHASE_REVIEW,
)

# Preflight is a read-only classifier and is deliberately denied Bash (#2346).
# Bash can start work the agent cannot be resumed for: a detached/background
# process (``nohup``, ``setsid``, ``&``, a nested agent CLI) leaves the model
# with nothing to do but wait for an event the harness has no mechanism to
# deliver. It then ends its turn holding that wait, the runner reads the
# finished stream as a finished agent, and the process is killed when it does
# not exit within the post-stream grace period — a phase that inspected zero
# files and produced no classification.
#
# What preflight may hold is governed by PREFLIGHT_ALLOWED_CAPABILITIES below,
# not by this set. This one names the tool whose denial is *load-bearing*, so
# the resolver can report dropping it as the specific thing it is and a test can
# assert it is never granted under any spelling.
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

#: The tool surface the backlog-triage proposer runs with (#2228).
#:
#: The proposer decides from a packet that was assembled for it; the prompt tells
#: it not to investigate, and it has nothing legitimate to reach for. What it
#: cannot be given is the empty tuple: ``runner_claude.build_argv`` omits
#: ``--allowedTools`` for ``()`` and hands the CLI its *unrestricted* default, so
#: "no tools" spelled the obvious way is the widest surface there is. This is
#: therefore the narrowest surface that is explicit — read-only, and in
#: particular no shell, which is the capability an advisory stage would need to
#: run ``gh issue edit``. Its absence is what makes "this stage performs no
#: tracker writes" a property of the invocation rather than of the prompt.
TRIAGE_PROPOSER_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

#: The capabilities preflight may hold, in canonical (internal) tool names.
#:
#: An ALLOW-list, not a deny-list, and the distinction is the point. Denying
#: ``bash`` answers "is today's known-dangerous tool absent?"; allowing exactly
#: these answers "is every tool preflight holds one someone weighed against the
#: no-delegation invariant?" Only the second survives a new tool being added to
#: a default set — including this repo's own ``API_PROVIDER_DEFAULT_TOOLS``,
#: whose ``submit_review`` entry a deny-list silently admitted to a phase that
#: reviews nothing. Adding a capability here is a deliberate act; joining
#: preflight's surface by being new is not possible.
PREFLIGHT_ALLOWED_CAPABILITIES: frozenset[str] = frozenset({"read_file", "glob", "grep"})


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

    So every input maps to an explicit, non-empty tuple drawn only from
    :data:`PREFLIGHT_ALLOWED_CAPABILITIES`:

    - names are canonicalized through the runner's own map before the check, so
      both vocabularies — forge.yaml's ``"Read"`` and an API profile's
      ``"read_file"`` — are recognized as the same capability;
    - anything not on the allow-list is dropped, whether it is the forbidden
      ``Bash``, a phase-inappropriate extra like ``submit_review``, or a tool
      nobody has weighed against this invariant yet;
    - the surviving names keep the *spelling config supplied*, because a CLI
      profile's ``--allowedTools`` and an API profile's tool schema read
      different vocabularies;
    - an empty result — config was empty, or nothing it named is allowed —
      falls back to :data:`PREFLIGHT_READ_ONLY_TOOLS`.

    The invariant this guarantees is the one the story needs: the preflight
    invocation always carries an explicit allowlist, and that allowlist never
    grants a tool it could delegate unresumable work with. It lives here rather
    than in a config-load-time validation so it holds for every source of a
    preflight profile — forge.yaml overrides, API-transport tool defaults, and
    fallback profiles built at dispatch time — not only the ones config load
    can see.
    """
    # Local import: the canonical name map lives with the runner that applies
    # it, and ``config`` stays free of a module-level dependency on ``runners``
    # (same reason ``config.auth`` imports the sandbox probe lazily).
    from theforge.runners.tool_runtime import TOOL_NAME_MAP  # noqa: PLC0415

    names = tuple(str(t) for t in allowed) if isinstance(allowed, (list, tuple)) else ()
    kept = tuple(
        name for name in names if TOOL_NAME_MAP.get(name, name) in PREFLIGHT_ALLOWED_CAPABILITIES
    )
    return kept or PREFLIGHT_READ_ONLY_TOOLS


DEFAULT_PREFLIGHT_PROFILE = ModelProfile(
    name="preflight",
    cli="claude",
    provider=None,
    model="sonnet",
    budget_usd=1.00,
    timeout_seconds=300,
    allowed_tools=PREFLIGHT_READ_ONLY_TOOLS,
    phase=PHASE_PREFLIGHT,
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
  # Maximum runtime for setup_command before Forge kills it. Sprint start scales
  # the effective bound under host contention using validation.gate_timeout_scale.
  # setup_timeout: 120
  # Required when setup_command uses {forge_python}: the interpreter this project
  # develops against. TheForge never substitutes its own interpreter here.
  # python_interpreter: "python3.12"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  # Command to run validation checks. Gate passes on exit 0, fails on non-zero.
  gate_command: "make gate"
  # Optional: declare named validation profiles instead of the two command
  # slots above. Exactly one profile carries merge authority — its result is
  # the only one that can establish a gate verdict; every other profile's
  # result is advisory. Forge substitutes {test_target} and {slug}; what to do
  # with them is your command's decision. Omit this block entirely to keep the
  # gate_command/test_command behaviour above.
  # profiles:
  #   complete:
  #     command: "make gate"
  #     authority: merge
  #   fast: "make test-fast"
  #   targeted: "make test TARGET={test_target}"

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
  # Specification-gap pauses a run may open before a dev agent must proceed
  # under its own recorded assumption instead of asking again. 0 disables.
  max_spec_gap_pauses: 1
  # Preflight complexity gate: a PROCEED story scoring at or above this pauses
  # at the end of PREFLIGHT and asks whether to plan it as scoped or return it
  # to be split — before any later phase is charged. 9 is one below the ceiling
  # (10) that preflight's own scope_exceeded signal flags, so the two do not
  # cover the same stories. Active by default; a threshold above 10 disables it.
  preflight_complexity_gate_threshold: 9
  # What an EXPIRED gate does. Only "approve" or "decompose"; anything else
  # (absent, empty, misspelled) is treated as "decompose", so a typo can never
  # spend on a story nobody approved.
  preflight_complexity_gate_no_decision: decompose  # "approve" | "decompose"
  escalate_policy: prompt  # "prompt" | "auto_approve" | "reject"
  # What an escalate gate that EXPIRES without an operator selection does.
  # "preserve" (default) waits for an operator; "apply_advice" applies the
  # advisory recommendation as if an operator had selected it — except for
  # `elevate` or no usable recommendation, which still wait.
  escalate_timeout_policy: preserve  # "preserve" | "apply_advice"

# Optional: post-run knowledge capture. When enabled, a completed run emits an
# evidence-backed summary to .forge/knowledge/summaries/{run_id}.yaml. Every
# learned claim must cite a finding, plan step, review cycle, file, or diff ref
# from that run — unevidenced summaries are rejected, not persisted. Generation
# is a post-DONE side effect: it never changes a run's outcome. ``knowledge.ref``
# directly names the API model that authors summaries; when omitted, summaries
# inherit ``plan.ref`` only if planning already dispatches over API transport.
# CLI plan refs do not consult ``transport_fallback`` for summary authoring.
# Ships disabled.
# knowledge:
#   run_summaries: true
#   ref:
#     provider: openai
#     model: o4-mini

# Optional: run a headless `forge triage` proposal pass after a sprint reaches
# its terminal result. Disabled by default. The pass only proposes and reviews —
# it writes a pending operator decision to .forge/pending and never ratifies, so
# no issue is modified without a person. A failure in the pass is reported and
# never fails the sprint that triggered it. Resolve what it writes with
# `forge triage --ratify <id>` (or drop it with `forge triage --discard <id>`).
# sprint:
#   post_sprint_triage: false

context:
  preflight_budget: 200
  plan_budget: 120
  dev_budget: 80
  review_budget: 80

# Optional: CLI→API transport fallback for when a CLI is rate-limited or
# unavailable. The provider never changes — only the transport. For any CLI
# model in models: whose same-provider API variant is registered, TheForge wires
# the fallback automatically. Set auto_transport_fallback: false to disable
# auto-pairing, or declare transport_fallback to override it. This key does not
# choose the durable-knowledge summary model; configure ``knowledge.ref`` for
# that role.
# transport_fallback:
#   openai:
#     model: o4-mini
#   google:
#     model: gemini-2.5-flash
"""
