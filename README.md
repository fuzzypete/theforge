# TheForge

[![CI](https://github.com/fuzzypete/theforge/actions/workflows/ci.yml/badge.svg)](https://github.com/fuzzypete/theforge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-orange)](CHANGELOG.md)

**Deterministic multi-LLM development orchestrator.**

TheForge runs a strict software development pipeline — plan, implement, test,
review — with **mechanical process control**:

- **LLMs generate, not control** — AI agents write code and reviews; the coordinator decides everything else
- **Phase gates enforce boundaries** — validation runs your test suite before any review
- **Full audit trail** — every agent call, decision, and cost is logged to `forge_audit.yaml`
- **Resumable by design** — interrupted runs pick up where they left off

## Is TheForge for you?

**Best for:**
- Bounded feature work and bug fixes with clear acceptance criteria
- Repos with runnable tests and lints (the gate needs something to run)
- Teams that want auditability: who changed what, which model approved it, what it cost
- Cross-model review coverage — different LLMs catch different bugs

**Not ideal for:**
- Vague greenfield ideation where requirements aren't yet defined
- Repos without deterministic validation (no tests, no lint, no gate command)
- Giant unscoped refactors that span the whole codebase
- UX-heavy exploratory work where "done" is subjective

## 5-Minute Quickstart

### 1. Clone and install

```bash
git clone https://github.com/fuzzypete/theforge.git
cd theforge
pip install -e ".[dev]"
```

Or install directly:

```bash
pip install git+https://github.com/fuzzypete/theforge.git
```

**Supported:** Python 3.11+ · macOS, Linux

### 2. Check providers

You'll need at least one AI CLI installed:
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) — recommended to start
- [Codex CLI](https://github.com/openai/codex) (`codex`) — optional
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`) — optional

> **You don't need all providers.** A single Claude CLI handles both dev and
> review. Add more models later for cross-model coverage.

Verify your CLI is working:

```bash
claude --version   # or: codex --version / gemini --version
```

### 3. Run hello-forge

The canonical first run uses the included example project:

```bash
cd examples/hello-forge

# One-time: initialize a git repo for the example
git init && git add -A && git commit -m "initial"
mkdir -p src tests
echo 'def greet(): return "placeholder"' > src/app.py
echo '' > tests/__init__.py && echo '' > tests/test_placeholder.py
git add -A && git commit -m "scaffold"

# Run the first spec
forge run specs/add-greeting.md --verbose
```

### 4. Expected output

```
[WORKSPACE] Creating worktree at .forge/worktrees/add-greeting/
[PREFLIGHT] Story not yet implemented — proceeding
[PLAN]      Generating implementation plan...
[DEV]       Claude Sonnet implementing story...
[VALIDATE]  pytest tests/ -q ... PASSED
[REVIEW]    Claude Opus reviewing implementation...
[DONE]      Verdict: APPROVE — branch feat/add-greeting ready to merge
```

Merge the result:

```bash
forge run specs/add-greeting.md --auto-merge

# Or run multiple stories as a sprint
forge sprint sprints/hello-sprint.yaml --verbose --auto-merge
```

**Cost per spec:** ~$1-3 (Sonnet dev + Opus review) · **Time:** ~5-10 minutes

## What things cost

| Complexity | Dev (Sonnet) | Review (Opus) | Total |
|-----------|-------------|--------------|-------|
| Small (1-2 files) | $0.50-1.50 | $0.30-0.80 | ~$1-2 |
| Medium (3-8 files) | $1.50-4.00 | $0.50-1.50 | ~$2-6 |
| Large (8+ files) | $3.00-8.00 | $1.00-3.00 | ~$5-12 |

Budget enforcement is built in — set `budget_usd` per profile to control spend.

## How it works

TheForge is a deterministic state machine. The coordinator (pure Python) drives
every transition. No LLM decides whether to retry, escalate, or move on.

```
INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW → DONE/ESCALATE
```

1. **WORKSPACE** — Creates an isolated git worktree for the story
2. **PREFLIGHT** — Checks if the story is already implemented; skips if so
3. **PLAN** — Dev agent generates an implementation plan (if enabled)
4. **PLAN_REVIEW** — Reviewer approves or rejects the plan before any code is written
5. **DEV** — Dev agent implements the story in the worktree
6. **VALIDATE** — Runs your gate command (`pytest`, `make test`, etc.)
7. **REVIEW** — One or more reviewer agents evaluate the diff against the spec
8. **DONE** — Branch is ready to merge; or loops back to DEV if reviewer requests changes
9. **ESCALATE** — Surfaced for human attention if iteration limits are exceeded

**Key invariant:** The coordinator is not an LLM. If an LLM is deciding whether
to retry or escalate, the architecture is wrong.

## What gets created

Running TheForge produces a predictable set of files:

```
.forge/
├── worktrees/<slug>/     Git worktree where the agent works (branch: feat/<slug>)
│                         Safe to delete after merging.
├── .env                  API keys for API-mode agents (gitignored)
└── logs/                 Per-run coordinator logs

forge_audit.yaml          Full trace: every agent call, cost, input, output.
                          User-authored? No — generated per run.
                          Safe to delete? Yes, but you lose the audit trail.

forge.yaml                Project config — models, budgets, gate command.
                          User-authored. Checked into your repo.

specs/<slug>.md           Story files describing what to build.
                          User-authored. Checked into your repo.
```

**User-authored (check in):** `forge.yaml`, `specs/*.md`, `sprints/*.yaml`
**Generated (do not check in):** `.forge/worktrees/`, `forge_audit.yaml`, `.forge/.env`

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

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/guides/getting-started.md) | Full walkthrough: install → first run → merge |
| [CLI Reference](docs/guides/cli-reference.md) | All commands, flags, and examples |
| [Inputs Reference](docs/guides/inputs-reference.md) | Every file format: stories, sprints, config, briefs |
| [Vision](docs/vision.md) | Architecture philosophy, roadmap, principles |
| [Example Project](examples/hello-forge/) | Minimal working project you can fork and run |

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
