# TheForge

Deterministic multi-LLM development orchestrator.

TheForge runs a strict software development pipeline — plan, implement, test,
review — with **mechanical process control**:

- The coordinator (pure Python) makes all process decisions
- AI agents write code and write reviews — nothing else
- Validation gates and schema checks enforce boundaries
- No LLM decides retries, escalation, or control flow

```
INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW → DONE/ESCALATE
```

## Why TheForge

Manual multi-agent development is high-friction: repeated copy/paste, inconsistent
handoffs, skipped process steps, and no audit trail.

TheForge replaces that with a deterministic state machine. Write a spec, run
`forge run`, and the coordinator handles the rest — planning, implementation,
testing, and multi-model code review. Every decision is logged. Every transition
is mechanical.

## Quickstart

### 1. Install

```bash
pip install -e .

# Optional: install provider SDKs for API-mode reviewers
pip install openai        # for OpenAI/Codex API reviewers
pip install anthropic     # for Anthropic API reviewers
pip install google-genai  # for Google Gemini API reviewers
```

### 2. Initialize

```bash
cd your-project
forge init
```

This creates a starter `forge.yaml`. You'll also need at least one AI CLI
installed:
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [Codex CLI](https://github.com/openai/codex) (`codex`)
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`)

### 3. Configure API keys (optional)

For API-mode reviewers, set up project-scoped secrets:

```bash
forge secrets-init
```

This creates `.forge/secrets.yaml` (gitignored). Uncomment and fill in the keys
you need:

```yaml
# ANTHROPIC_API_KEY: sk-ant-...
# OPENAI_API_KEY: sk-proj-...
# GOOGLE_API_KEY: AIza...
```

### 4. Write a spec

Create `specs/my-feature.md`:

```markdown
---
name: "Add user authentication"
slug: add-auth
pytest_target: tests/
---

# Add User Authentication

## Problem
The app has no authentication...

## Acceptance Criteria
1. Users can register with email/password
2. Users can log in and receive a session token
3. Protected routes return 401 without a valid token
4. All auth endpoints have tests
```

### 5. Run

```bash
# Full pipeline with verbose logging
forge run specs/my-feature.md --verbose

# Review-only (skip plan/dev, review existing worktree)
forge review specs/my-feature.md --worktree .forge/worktrees/add-auth
```

## Configuration (`forge.yaml`)

### Profiles

Profiles define which AI models handle each role. Two transport modes:

**CLI mode** — agent runs as a subprocess with its own tool runtime:
```yaml
profiles:
  dev:
    cli: claude          # "claude", "codex", or "gemini"
    model: sonnet
    budget_usd: 50.00
    timeout_seconds: 1800
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
```

**API mode** — agent runs via HTTP with TheForge providing the tool runtime:
```yaml
  review_pool:
    - name: codex-reviewer
      provider: openai     # "openai", "anthropic", or "google"
      model: gpt-5.1-codex-mini
      review_role: patterns
      budget_usd: 2.00
      timeout_seconds: 120
      allowed_tools: [Read, Bash, Glob, Grep]
```

API-mode agents with `allowed_tools` get a full tool-use loop — TheForge
executes tool calls locally in the worktree and feeds results back to the
model. Agents without `allowed_tools` run as stateless text-judgment calls.

### Multi-model review pool

Different models catch different things. Configure multiple reviewers and
their findings are merged deterministically:

```yaml
  review_pool:
    - name: claude-reviewer
      cli: claude
      model: opus
      review_role: correctness
      budget_usd: 5.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]

    - name: codex-reviewer
      provider: openai
      model: gpt-5.1-codex-mini
      review_role: patterns
      budget_usd: 2.00
      timeout_seconds: 120
      allowed_tools: [Read, Bash, Glob, Grep]

    - name: gemini-reviewer
      provider: google
      model: gemini-2.5-pro
      review_role: edge-cases
      budget_usd: 1.00
      timeout_seconds: 120
      allowed_tools: [Read, Bash, Glob, Grep]
```

A single P1 from any reviewer triggers REQUEST_CHANGES. P2s are advisory.

### Plan review

An agent reviews the implementation plan before dev starts:

```yaml
plan:
  enabled: true
  model: claude
  model_name: sonnet
  budget_usd: 1.00
  timeout: 600

plan_agent_review:
  enabled: true
  cli: claude
  model: sonnet
  budget_usd: 0.50
  timeout: 600
```

### Validation gate

```yaml
validation:
  gate_command: "python -m pytest tests/ -q"
  handoff_file: "handoff.yaml"
  gate_decision_key: "gate_decision"
```

The gate runs after each dev iteration. If it fails, the dev agent gets
another attempt (up to `max_dev_iterations`).

### Local models

API-mode profiles support `base_url` for local model servers (Ollama,
LM Studio, vLLM):

```yaml
  review_pool:
    - name: local-reviewer
      provider: openai
      model: codellama
      base_url: http://localhost:11434/v1
      budget_usd: 0.00
      timeout_seconds: 60
      allowed_tools: [Read, Glob, Grep]
```

## Review Protocol

Review agents return structured YAML:

```yaml
verdict: APPROVE | REQUEST_CHANGES
summary: "One-line summary"
findings:
  - severity: P1 | P2
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

Schema rules (enforced mechanically):
- `APPROVE` with any P1 → overridden to `REQUEST_CHANGES`
- `REQUEST_CHANGES` with no P1 → schema error → treated as `REQUEST_CHANGES`
- Invalid YAML → treated as `REQUEST_CHANGES`

## Architecture

```
src/theforge/
├── coordinator.py     Deterministic state machine — the heart of the system
├── runner.py          CLI agent subprocess invocation
├── runner_api.py      API agent runner with tool-use loop
├── tool_runtime.py    Tool registry and handlers for API agents
├── config.py          forge.yaml parsing and model profiles
├── task.py            Prompt builders (dev, preflight, review, plan review)
├── review.py          Review output parsing and normalization
├── schemas.py         Review schema validation
├── coord_state.py     Coordinator state management
├── coord_phases.py    Phase implementations (dev, review, validate)
├── coord_util.py      Logging, formatting, run ID generation
├── sessions.py        Agent session ID persistence (for --resume)
└── cli.py             `forge` CLI entry point
```

**Key invariant:** The coordinator is not an LLM. Every state transition is
deterministic Python. If an LLM is deciding whether to retry or escalate,
the architecture is wrong.

## Development

```bash
make fmt        # ruff format + ruff check --fix
make lint       # ruff check + ruff format --check
make test       # pytest tests/ -v
make gate       # run tests + write handoff.yaml
```

## License

[MIT](LICENSE)
