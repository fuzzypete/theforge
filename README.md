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

TheForge replaces that with a deterministic state machine. Write a story, run
`forge run`, and the coordinator handles the rest — planning, implementation,
testing, and multi-model code review. Every decision is logged. Every transition
is mechanical.

## Quickstart

### 1. Install

```bash
pip install -e .
```

You'll need at least one AI CLI installed:
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) — recommended to start
- [Codex CLI](https://github.com/openai/codex) (`codex`) — optional
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`) — optional

> **You don't need all providers.** A single Claude CLI handles both dev and
> review. Add more models later for cross-model coverage.

### 2. Initialize your project

```bash
cd your-project
forge init
```

Creates a starter `forge.yaml` and `specs/TEMPLATE.md`.

### 3. Write a story

Create `specs/my-feature.md`:

```markdown
---
name: "Add health check endpoint"
slug: add-health-check
pytest_target: tests/
---

# Add Health Check Endpoint

## Problem
The app has no way to verify it's running.

## Acceptance criteria
- GET /health returns {"status": "ok"} with HTTP 200
- A test verifies the response
- Existing tests continue to pass
```

### 4. Run

```bash
forge run specs/my-feature.md --verbose
```

The coordinator will:
1. **WORKSPACE** — Create a git worktree
2. **PREFLIGHT** — Check if already implemented
3. **PLAN** — Generate an implementation plan (if enabled)
4. **DEV** — Agent implements the story
5. **VALIDATE** — Run your test suite
6. **REVIEW** — Multi-model code review
7. **DONE** — or loop back to DEV if reviewer requests changes

### 5. Merge

```bash
# Auto-merge after approval
forge run specs/my-feature.md --auto-merge

# Or run multiple stories as a sprint
forge sprint sprints/my-sprint.yaml --verbose --auto-merge
```

## What things cost

| Complexity | Dev (Sonnet) | Review (Opus) | Total |
|-----------|-------------|--------------|-------|
| Small (1-2 files) | $0.50-1.50 | $0.30-0.80 | ~$1-2 |
| Medium (3-8 files) | $1.50-4.00 | $0.50-1.50 | ~$2-6 |
| Large (8+ files) | $3.00-8.00 | $1.00-3.00 | ~$5-12 |

Budget enforcement is built in — set `budget_usd` per profile to control spend.

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/guides/getting-started.md) | Full walkthrough: install → first run → merge |
| [CLI Reference](docs/guides/cli-reference.md) | All commands, flags, and examples |
| [Inputs Reference](docs/guides/inputs-reference.md) | Every file format: stories, sprints, config, briefs |
| [Vision](docs/vision.md) | Architecture philosophy, roadmap, principles |
| [Example Project](examples/hello-forge/) | Minimal working project you can fork and run |

## Configuration

TheForge is configured via `forge.yaml` in your project root. `forge init`
generates a minimal config. Key sections:

```yaml
profiles:
  dev:                          # who implements
    cli: claude
    model: sonnet
    budget_usd: 5.00
  review_pool:                  # who reviews (1 or more)
    - name: claude-reviewer
      cli: claude
      model: opus
      budget_usd: 2.00

retry:
  max_dev_iterations: 3         # attempts before escalation
  max_review_cycles: 2          # dev→review loops

validation:
  gate_command: "pytest tests/"  # your test command
```

See [Inputs Reference](docs/guides/inputs-reference.md) for the full config
schema with all options.

### Multi-model review

Different models catch different bugs. Add reviewers to the pool:

```yaml
  review_pool:
    - name: claude-reviewer
      cli: claude
      model: opus
      review_role: correctness
    - name: codex-reviewer
      provider: openai         # API mode — TheForge provides tool runtime
      model: o4-mini
      review_role: patterns
    - name: gemini-reviewer
      provider: google
      model: gemini-2.5-flash
      review_role: edge-cases
```

A single P1 from any reviewer triggers REQUEST_CHANGES. P2s are advisory.

### API keys

For API-mode agents, set up secrets:

```bash
forge secrets-init              # creates .forge/.env (gitignored)
```

CLI-mode agents (Claude Code, Codex CLI) handle their own authentication.

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
├── sprint.py          Sprint manifest loading and execution
├── cli.py             forge CLI entry point
└── ...                Support modules (state, phases, logging, audit)
```

**Key invariant:** The coordinator is not an LLM. Every state transition is
deterministic Python. If an LLM is deciding whether to retry or escalate,
the architecture is wrong.

## Development

TheForge develops itself. The `forge.yaml` in this repo configures a 4-model
review pool (Claude, Codex, Gemini, DeepSeek) and plans phase. Stories live in
`specs/`, sprints in `sprints/`.

```bash
make fmt        # ruff format + ruff check --fix
make lint       # ruff check + ruff format --check
make test       # pytest tests/ -v
make gate       # run tests + write handoff.yaml
```

## License

[MIT](LICENSE)
