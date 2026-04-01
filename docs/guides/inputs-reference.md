# Inputs Reference

Every file format TheForge accepts as input.

---

## Story file (`stories/*.md` by default)

The primary input. Describes WHAT to build and WHY. The dev agent implements
exactly what the acceptance criteria say. Story files can live anywhere; `stories/`
is simply the default directory created by `forge init`.

### Template

```markdown
---
name: "Short human-readable title"
slug: my-feature-slug
pytest_target: tests/
---

# Story Title

## Problem

One paragraph: WHY this change is needed. Context for the agent, not requirements.

## Acceptance criteria

- The system does X when Y
- GET /endpoint returns Z
- A test in tests/test_foo.py verifies the behavior
- Existing tests continue to pass

## Notes (optional)

Additional context, constraints, or design guidance. Read by the agent as
background, not as requirements.
```

### Frontmatter fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Human-readable title. Shows in logs and audit. |
| `slug` | Yes | — | Branch name (`forge/{slug}`), worktree path. Lowercase-with-dashes. |
| `pytest_target` | No | `tests/` | Path passed to pytest in the gate command. |
| `file_scope` | No | `[]` (unrestricted) | Restrict dev agent to these files/directories. Empty = no restriction. |
| `gate` | No | project default | Override gate: `"none"` (skip), `"lint"`, or custom command. |
| `depends_on` | No | `[]` | Slugs that must be merged before this story runs (sprint mode). |

### Writing good acceptance criteria

**Do:**
- Start each AC with a verb: "Returns", "Creates", "Rejects", "Logs"
- Be observable and testable: "GET /health returns `{status: ok}` with 200"
- Include a test AC: "A test in tests/test_health.py verifies the response"
- Include regression: "Existing tests continue to pass"

**Don't:**
- Put implementation details in ACs (that's the agent's job)
- Write vague ACs: "The code is clean" (not testable)
- Mix context with requirements (context goes in Problem section)

---

## Sprint manifest (`sprints/*.yaml`)

Bundles multiple stories into a sequential run with shared budget.

### Template

```yaml
name: "Sprint Name — brief description"
budget_usd: 50
auto_merge: true    # optional: merge each APPROVED story automatically
specs:
  - stories/story-one.md
  - stories/story-two.md
  - stories/story-three.md
```

### Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Human-readable sprint name |
| `budget_usd` | Yes | — | Total budget ceiling across all stories |
| `auto_merge` | No | `false` | Auto-merge approved stories to main |
| `specs` | Yes | — | Ordered list of spec file paths (relative to project root) |

### Behavior

- Stories run in order. Each goes through the full pipeline.
- A failed/escalated story doesn't block subsequent ones.
- Budget is shared — remaining budget carries to the next story.
- `--resume` flag auto-triages each story's worktree state.

---

## Project configuration (`forge.yaml`)

Controls everything: which models to use, budgets, timeouts, retry policies.

### Minimal config

```yaml
project: my-project

workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} main"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  gate_command: "python -m pytest tests/ -q"
  handoff_file: "handoff.yaml"
  gate_decision_key: "gate_decision"

profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 5.00
    timeout_seconds: 600
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
  review_pool:
    - name: claude-reviewer
      cli: claude
      model: opus
      budget_usd: 2.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]

retry:
  max_dev_iterations: 3
  max_review_cycles: 2
```

### Full config reference

```yaml
project: my-project                # project name for logging/audit

# ── Workspace ──────────────────────────────────────────────
workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} main"
  setup_command: "pip install -e ."    # optional: run once after worktree creation
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"
  base_branch: "main"                 # default: "main"

# ── Validation gate ────────────────────────────────────────
validation:
  gate_command: "make gate"            # must exit 0 on success
  handoff_file: "handoff.yaml"         # YAML artifact with gate decision
  gate_decision_key: "gate_decision"   # key in handoff: PASS | FAIL
  gate_timeout: 600                    # seconds; default varies
  pre_validate_command: ~              # optional: run before dirty check

# ── Retry policy ───────────────────────────────────────────
retry:
  max_dev_iterations: 3      # dev attempts within one review cycle
  max_review_cycles: 2       # full dev→review loops before ESCALATE
  max_review_parse_retries: 2  # reviewer output parse/schema error retries
  max_handoff_retries: 2     # dev handoff rewrite attempts after gate PASS
  max_plan_regen_attempts: 3 # plan review reject → regeneration cycles

# ── Agent profiles ─────────────────────────────────────────
profiles:
  # Dev agent (implements the story)
  dev:
    cli: claude                # CLI mode: "claude", "codex", or "gemini"
    # provider: openai         # API mode: "openai", "anthropic", "google", "deepseek"
    model: sonnet
    budget_usd: 5.00
    timeout_seconds: 600
    timeout_medium_seconds: 900     # optional: for medium-complexity stories
    timeout_large_seconds: 1800     # optional: for large-complexity stories
    max_iterations: 50              # optional: API-mode agent loop ceiling
    allowed_tools:
      - Read
      - Edit
      - Write
      - Bash
      - Glob
      - Grep

  # Preflight agent (classifies spec before dev)
  preflight:
    cli: claude
    model: sonnet
    budget_usd: 1.00
    timeout_seconds: 300
    allowed_tools: [Read, Bash, Glob, Grep]

  # Review pool (one or more reviewers)
  review_pool:
    - name: claude-reviewer      # unique name for logging
      cli: claude
      model: opus
      review_role: correctness   # optional: "correctness", "patterns", "edge-cases"
      budget_usd: 5.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]

    - name: codex-reviewer       # API-mode reviewer example
      provider: openai
      model: o4-mini
      review_role: patterns
      budget_usd: 2.00
      timeout_seconds: 300
      max_iterations: 50
      allowed_tools: [Read, Bash, Glob, Grep]

    - name: local-reviewer       # Local model via Ollama/vLLM/LM Studio
      provider: openai
      model: codellama
      base_url: http://localhost:11434/v1
      budget_usd: 0.00
      timeout_seconds: 60
      allowed_tools: [Read, Glob, Grep]

# ── Plan phase (optional) ─────────────────────────────────
plan:
  enabled: true
  cli: claude
  model: sonnet
  budget_usd: 1.00
  timeout: 600

# ── Plan review (optional) ────────────────────────────────
plan_agent_review:
  enabled: true
  pool:
    - name: claude-plan-reviewer
      cli: claude
      model: opus
      budget_usd: 2.00
      timeout_seconds: 600
      allowed_tools: [Read, Glob, Grep]

# ── Notifications (optional) ──────────────────────────────
notifications:
  backend: ntfy               # "none", "ntfy", "osascript"
  ntfy:
    priority: high
    # url resolved from NTFY_URL in .forge/.env

# ── Smart config (optional) ────────────────────────────────
smart_config_models:
  - claude/sonnet
  - openai/gpt-5.4

# ── Secrets (optional) ────────────────────────────────────
# API keys are read from .forge/.env (run `forge secrets-init` to create)
```

### Profile modes: CLI vs API

| Setting | CLI mode | API mode |
|---------|----------|----------|
| Config key | `cli: claude` | `provider: openai` |
| How it runs | Subprocess (`claude`, `codex`, `gemini`) | HTTP API call |
| Tool execution | Agent's own runtime | TheForge's tool runtime |
| When to use | Dev agent (needs full editor access) | Reviewers (read-only analysis) |
| `allowed_tools` | Forwarded to CLI as flags | TheForge executes tools locally |
| Cost tracking | Parsed from CLI output | Calculated from token usage |

---

## Brief file (for `forge ideate`)

A plain text or markdown problem description used as input to multi-LLM
deliberation. No required structure — just describe the problem.

### Template

```markdown
# Brief: [Feature Name]

## Background

What exists today and why it's insufficient.

## What we need

The capability or behavior we want. Be specific about outcomes,
not implementation.

## Constraints

- Must work with existing X
- Cannot break Y
- Budget/timeline concerns

## Open questions

- Should we do A or B?
- Is C in scope?
```

### Usage

```bash
forge ideate briefs/my-feature.md --output stories/my-feature.md
```

---

## Secrets file (`.forge/.env`)

Standard dotenv format. Created by `forge secrets-init`.

If you still have a legacy `.forge/secrets.yaml`, migrate those values into
`.forge/.env`.

```bash
# .forge/.env — project-scoped secrets (gitignored)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...
GOOGLE_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
NTFY_URL=https://ntfy.sh/your-topic
```

---

## Review output (produced by reviewers, not user input)

For reference — this is the schema that reviewers must produce. You don't write
this, but understanding it helps interpret audit output.

```yaml
verdict: APPROVE | REQUEST_CHANGES
summary: "One-line description of findings"
findings:
  - severity: P1 | P2         # P1 = blocker, P2 = advisory
    file: "src/foo.py"
    line: 42
    description: "What is wrong"
    suggestion: "How to fix it"
spec_compliance:
  matches_spec: true | false
  mismatches: []
test_coverage:
  adequate: true | false
  gaps: []
```

**Schema enforcement rules:**
- `APPROVE` with any P1 → overridden to `REQUEST_CHANGES`
- `REQUEST_CHANGES` with no P1 → schema error
- Invalid YAML → treated as `REQUEST_CHANGES`

---

## See also

- [Getting Started](getting-started.md) — full setup walkthrough including config examples
- [CLI Reference](cli-reference.md) — all commands and flags
- [Provider Setup Guide](choose-your-provider-setup.md) — forge.yaml profiles for different scenarios
- [Troubleshooting](troubleshooting.md) — common errors and fixes
