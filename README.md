# TheForge

Deterministic multi-LLM development orchestrator.

TheForge runs a strict software development loop with **mechanical process control**:

- Coordinator (pure Python) makes all process decisions
- Agents write code and reviews — nothing else
- Validation and schema checks enforce boundaries
- Human can approve, reject, or escalate in interactive mode

## Why TheForge

Manual multi-agent development is high-friction: repeated copy/paste, inconsistent
handoffs, skipped process steps, and no audit trail.

TheForge replaces that with a deterministic state machine:

```
INIT → WORKSPACE → PREFLIGHT → DEV → VALIDATE → REVIEW → HUMAN_REVIEW → DONE/ESCALATE
```

No LLM decides retries, escalation, or control flow. If an agent can't pass the gate
after N attempts, the coordinator escalates to you — it doesn't guess.

## Quickstart

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Create config

Create `forge.yaml` at your project root:

```yaml
project: my-project

workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b feat/{slug}"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "feat/{slug}"

validation:
  gate_command: "make gate"
  handoff_file: "handoff.yaml"
  gate_decision_key: "gate_decision"

profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 2.00
    timeout_seconds: 1800

  review:
    cli: claude
    model: opus
    budget_usd: 1.00
    timeout_seconds: 600

retry:
  max_dev_iterations: 3
  max_review_cycles: 2
```

### 3. Write a spec

Create `specs/my-task.md`:

```md
---
name: "Add X feature"
slug: add-x-feature
file_scope:
  - src/myproject/
  - tests/
pytest_target: tests/test_x.py
---

# Add X Feature

## Problem
...

## Acceptance Criteria
1. ...
2. ...
```

### 4. Run

```bash
# Interactive (default) — pauses for human review before merge
forge run specs/my-task.md

# Unattended — auto-approves if review passes
forge run specs/my-task.md --auto

# Auto-merge after approval
forge run specs/my-task.md --auto --auto-merge
```

### 5. Read the audit

```bash
cat forge_audit.yaml
```

---

## Core Commands

```bash
# Run a single spec through the full pipeline
forge run <spec-file> [--config forge.yaml] [--auto] [--auto-merge]

# Run multiple specs sequentially from a campaign manifest
forge campaign <campaign.yaml> [--auto] [--auto-merge]
```

---

## Config Reference (`forge.yaml`)

### Gate modes

**Handoff-based** (default): gate_command writes a handoff file; coordinator reads
the decision key.

```yaml
validation:
  gate_command: "make gate"
  handoff_file: "handoff.yaml"
  gate_decision_key: "gate_decision"
  gate_timeout: 300   # optional, seconds
```

**Exit-code-based**: gate passes if the command exits 0. Set `handoff_file: ""`
to use this mode. Supports `{pytest_target}` and `{slug}` substitution.

```yaml
validation:
  gate_command: "make fmt && make lint && pytest {pytest_target} -q"
  handoff_file: ""
  gate_decision_key: ""
  gate_timeout: 600
```

### Multi-model review pool

```yaml
profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 2.00
    timeout_seconds: 1800

  review_pool:
    - name: opus
      cli: claude
      model: opus
      budget_usd: 1.00
      timeout_seconds: 600
    - name: codex
      cli: codex
      model: o4-mini
      reasoning_effort: high   # low | medium | high
      budget_usd: 1.00
      timeout_seconds: 600

  synthesis:
    cli: claude
    model: opus
    budget_usd: 0.50
    timeout_seconds: 300
```

When multiple reviewers are configured, each reviews independently and a synthesis
agent reconciles the findings. A single P1 from any reviewer triggers REQUEST_CHANGES.

### Campaign manifest (`campaign.yaml`)

```yaml
name: "Q1 backlog sprint"
budget_usd: 25.00
specs:
  - specs/feature-a.md
  - specs/feature-b.md
  - specs/feature-c.md
```

```bash
forge campaign campaign.yaml --auto --auto-merge
```

---

## Review Protocol

Review agents must return structured YAML inside a fenced block:

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
- `APPROVE` with any P1 finding → overridden to `REQUEST_CHANGES`
- `REQUEST_CHANGES` with no P1 finding → schema error → treated as `REQUEST_CHANGES`
- Invalid YAML → treated as `REQUEST_CHANGES`

---

## Development

```bash
make fmt        # ruff format + ruff check --fix
make lint       # ruff check + ruff format --check (no auto-fix)
make test       # pytest tests/ -v
make gate       # run tests + write handoff.yaml
```

---

## Architecture

```
src/theforge/
├── coordinator.py   Deterministic state machine — the heart of the system
├── runner.py        Subprocess invocation of agent CLIs (claude/codex/gemini)
├── config.py        forge.yaml parsing and model profiles
├── task.py          Prompt builders (dev + preflight + review + synthesis)
├── review.py        YAML review output parsing and normalization
├── schemas.py       Review schema validation and cross-validation rules
├── campaign.py      Multi-spec sequential execution with aggregate budget
└── cli.py           `forge` CLI entry point
```

**Key invariant:** The coordinator is not an LLM. Every state transition is
deterministic Python. If an LLM is deciding whether to retry or escalate,
the architecture is wrong.
