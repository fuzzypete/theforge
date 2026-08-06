# TheForge

[![CI](https://github.com/fuzzypete/theforge/actions/workflows/ci.yml/badge.svg)](https://github.com/fuzzypete/theforge/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/github/v/tag/fuzzypete/theforge?sort=semver&label=version&color=orange)](CHANGELOG.md)

**Deterministic multi-LLM development orchestrator.**

TheForge runs a strict software development pipeline where Python coordinates
the process and models stay inside bounded roles: plan, implement, validate,
review. Every state transition is mechanical. Every run is auditable, resumable,
and isolated in a git worktree until you decide to merge.

- LLMs generate artifacts, not process decisions
- Validation and review act as mechanical gates
- Work happens on feature branches in managed worktrees
- Logs, audits, and review output make every run inspectable

## Why TheForge

The agentic-coding world is crowded — CLI agents, autonomous "AI engineer"
products, agentic CI workflows. All of them will happily generate code.
TheForge's bet is that the scarce property is not generation, it's **trust**:
knowing when work should *not* run, what actually happened when it did, and
why it failed when it did.

- **Refusal over guessing.** A typed intake gate (shape check → diagnosis →
  grooming) refuses underspecified work with a machine-readable reason instead
  of burning tokens on a confident guess. See
  [Refusal as Capability](docs/vision/refusal-capability.md).
- **Deterministic control flow.** Python coordinates; models stay inside
  bounded roles. If a model is deciding whether to retry, pass a gate, or
  escalate, the architecture is wrong.
- **An audit substrate, not a log.** Every run writes to a queryable SQLite
  history with no delete/redact API — failures cannot be erased to make
  current state look better.
- **Cross-model review as a gate,** not a suggestion: structured verdicts with
  per-acceptance-criterion verification, and escalation when models disagree.
- **Evidence-based routing.** Work is routed by measured complexity; the
  router learns from escalation history and periodically re-tests benched
  models — all grounded in the audit substrate. See
  [Adaptive Assignment](docs/guides/adaptive-assignment.md).

**How this differs from an autonomous coding agent:** a single-agent tool
answers "write this for me"; TheForge answers "run my backlog and leave me
evidence." Point it at GitHub issues or story files — it refuses the ones that
aren't ready, orders the rest into a dependency DAG, runs them in parallel
worktrees under one budget, gates every story on your tests plus multi-model
review, and records every decision. It orchestrates the same agent CLIs and
APIs you already use (Claude Code, Codex, Gemini, or any API/local model)
rather than replacing them.

## Is this for you?

**Best fit**
- Bounded feature work and bug fixes with clear acceptance criteria
- Repos with a runnable test or lint gate
- Teams that want auditability, resumability, and explicit review loops
- People who want multi-model review without giving models runtime authority

**Poor fit**
- Vague exploratory work without a clear done condition
- Repos with no deterministic validation step
- Huge refactors whose scope is still moving
- Design-heavy work where correctness is mostly subjective

## 5-minute quickstart

### 1. Install

```bash
pip install git+https://github.com/fuzzypete/theforge.git
```

For development:

```bash
git clone https://github.com/fuzzypete/theforge.git
cd theforge
pip install -e ".[dev]"
```

You need at least one AI CLI. Starting with Claude Code is the simplest path.

### 2. Check provider health

```bash
forge check-providers
```

### 3. Run the canonical example

```bash
cd examples/hello-forge
forge run specs/add-greeting.md --verbose
```

Start with [examples/hello-forge/README.md](examples/hello-forge/README.md) if
you want the fastest proof that your setup is sane before pointing TheForge at
a real repository.

### 4. Run your own story

```bash
cd your-project
forge init
forge run stories/my-feature.md --verbose
```

`forge init` now scaffolds `stories/TEMPLATE.md`. Story files can live anywhere,
but `stories/` is the default convention for new projects.

Expected shape of a successful run:

```text
[forge] ▸ WORKSPACE   my-feature
[forge] ▸ PREFLIGHT   sonnet
[forge]   Verdict: PROCEED
[forge] ▸ DEV         sonnet  iter 1
[forge] ▸ VALIDATE    pytest
[forge]   Gate: PASS
[forge] ▸ REVIEW      opus
[forge]   ✓ REVIEW   APPROVE
[forge] ▸ DONE        my-feature
```

## How a run works

The public-facing lifecycle is intentionally simple:

```text
INIT -> WORKSPACE -> PREFLIGHT -> PLAN -> PLAN_REVIEW
  -> DEV -> VALIDATE -> REVIEW -> DONE / ESCALATE
```

The coordinator creates a worktree, invokes the configured agents, runs your
gate command, parses structured review output, and decides what happens next.
Models do the planning, coding, and reviewing, but they do not decide whether
to retry, pass a gate, or escalate. When validation fails or review requests
changes, the run can loop back to `DEV` before it finishes.

```mermaid
stateDiagram-v2
    [*] --> WORKSPACE
    WORKSPACE --> PREFLIGHT
    PREFLIGHT --> PLAN
    PREFLIGHT --> DONE : already done
    PLAN --> PLAN_REVIEW
    PLAN_REVIEW --> DEV
    DEV --> VALIDATE
    VALIDATE --> DEV : FAIL (retry)
    VALIDATE --> REVIEW : PASS
    REVIEW --> DEV : REQUEST_CHANGES
    REVIEW --> DONE : APPROVE
    REVIEW --> ESCALATE : max cycles
    DEV --> ESCALATE : max iterations
    DONE --> [*]
    ESCALATE --> [*]
```

## What gets created

```text
your-project/
|- forge.yaml
|- stories/
|- sprints/
`- .forge/
   |- .env
   |- hooks/
   |- logs/
   |- audits/
   `- worktrees/
```

- `forge.yaml`, `stories/`, `sprints/`, and `.forge/.env` are yours.
- `.forge/logs/`, `.forge/audits/`, and `.forge/worktrees/` are generated by TheForge.
- Worktrees are where agent edits happen, so your main branch stays untouched until you merge.
- The latest persistent audit lands in `.forge/audits/forge_audit.yaml`; the
  authoritative, queryable run history is the SQLite substrate at
  `.forge/audits/index.sqlite` (`forge audits`, `forge explain`).

See the [Storage Layout guide](docs/guides/forge-storage.md) for the category
model behind `.forge/` — what's tracked, what's local-only, and why.

## Choose your setup

| Setup | Best for | Tradeoff |
|-------|----------|----------|
| Single CLI | Fastest first run | Fewer comparison angles in review |
| Multi-CLI review | Cross-model review coverage | More local setup friction |
| API reviewers/runtime | Fine-grained control and hosted providers | More env and secret management |

See [Provider Setup Guide](docs/guides/choose-your-provider-setup.md) for
recommended patterns.

## Minimal config

This is the smallest useful mental model for `forge.yaml`:

```yaml
models:
  - anthropic/sonnet/cli
  - anthropic/opus/cli

budget_usd: 30.0

validation:
  gate_command: "pytest tests/"
```

TheForge derives preflight, plan, dev, review, and synthesis roles from the model
list using the story's measured complexity. Use `overrides:` for targeted
changes to derived roles. (A legacy `profiles:` schema still loads for old
configs but is mutually exclusive with `models:` — new projects should not use
it.) The full schema lives in [Inputs Reference](docs/guides/inputs-reference.md).

## Sprints and GitHub issues

Single stories are the unit; sprints are the day-to-day workflow. `forge
sprint --milestone v1.2` pulls groomed GitHub issues, refuses unready ones
with labeled, machine-readable reasons, orders the rest into a dependency DAG
(explicit `depends_on` plus collision-derived edges from preflight), and runs
them in parallel worktrees under one budget. The intake tooling — `forge
shape`, `forge diagnose`, `forge groom`, `forge todo` — moves issues from
captured to ready, and `forge status --ready` shows what would run. See the
[Authoring guide](docs/guides/authoring.md).

## Operating Runs

Runs may detach by default so they can continue in the background. Use
`forge run --fg` or `forge sprint --fg` when you want foreground execution, and
use `forge status`, `forge logs <run-id>`, and `forge stop <run-id>` to monitor
or control active runs. Active sprint detail is folded into `forge status`; the
old standalone `forge sprint-status` parser entry is no longer exposed.

## What things cost

| Complexity | Dev (Sonnet) | Review (Opus) | Total |
|-----------|-------------|--------------|-------|
| Small (1-2 files) | $0.50-1.50 | $0.30-0.80 | ~$1-2 |
| Medium (3-8 files) | $1.50-4.00 | $0.50-1.50 | ~$2-6 |
| Large (8+ files) | $3.00-8.00 | $1.00-3.00 | ~$5-12 |

Actual cost varies a lot with repo shape, validation runtime, diff size, prompt
volume, and the number of review loops. Budget enforcement is built in via
`budget_usd`.

## Start here

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/guides/getting-started.md) | Full first-run walkthrough from install to merge |
| [Example Project](examples/hello-forge/) | Canonical proof path for a fresh setup |
| [Storage Layout](docs/guides/forge-storage.md) | The four-category model behind `.forge/`: what's tracked, what's local |
| [First-Run Walkthrough](docs/guides/first-run-walkthrough.md) | Narrated phase-by-phase terminal transcript |
| [Troubleshooting](docs/guides/troubleshooting.md) | Recovery guidance for setup, gate, and worktree issues |
| [CLI Reference](docs/guides/cli-reference.md) | Commands, flags, and when to use them |
| [Inputs Reference](docs/guides/inputs-reference.md) | Story, sprint, config, and brief file formats |
| [Provider Setup Guide](docs/guides/choose-your-provider-setup.md) | Choosing between CLI and API patterns |
| [Reviewer Prompt Template](docs/guides/reviewer-prompt-template.md) | Verification-gated reviewer prompt: tree-state proof, certainty tags, anti-flattery |
| [Local Models](docs/guides/local-models.md) | Ollama and vLLM setup for private or offline use |
| [Authoring](docs/guides/authoring.md) | Writing stories and GitHub issues that pass the intake gate |
| [Architecture](docs/architecture.md) | The two-level lifecycle, module map, and trust boundaries in one place |
| [Adaptive Assignment](docs/guides/adaptive-assignment.md) | How routing learns from recorded evidence |
| [Routing Policy](docs/guides/routing-policy.md) | Complexity buckets, tiers, and reasoning-effort defaults |
| [Vision](docs/vision.md) | Philosophy and doctrine: refusal-capability and compound engineering |
| [Docs index](docs/README.md) | Living guides vs. historical records — start here when browsing docs/ |

## Developing TheForge

The repo configures TheForge to develop itself. The most useful local commands are:

```bash
make fmt
make lint
make test
make gate
```

Core modules:

```text
src/theforge/
|- cli/            CLI entry points and subcommands
|- config/         forge.yaml loading, auth, and typed config
|- coordinator/    deterministic state machine and phase handlers
|- runners/        CLI and API runners plus provider adapters
|- sprint/         sprint manifest parsing and execution
|- task/           prompt builders and story helpers
|- review.py       review parsing and normalization
`- schemas.py      review schema validation
```

Key invariant: the coordinator is not an LLM. If a model is deciding whether
to retry, pass a gate, or escalate, the architecture is wrong.

## License

[MIT](LICENSE)
