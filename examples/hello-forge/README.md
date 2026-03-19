# Hello Forge — Example Project

A minimal example showing how to use TheForge. Two tiny specs, one sprint.

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- TheForge installed (`pip install -e /path/to/theforge`)

## Setup

```bash
cd examples/hello-forge
git init && git add -A && git commit -m "initial"

# Create a basic app to modify
mkdir -p src
echo 'def greet(): return "placeholder"' > src/app.py
echo '' > tests/__init__.py
echo '' > tests/test_placeholder.py
git add -A && git commit -m "scaffold"
```

## Run a single spec

```bash
forge run specs/add-greeting.md --verbose
```

This will:
1. **WORKSPACE** — Create a git worktree at `.forge/worktrees/add-greeting/`
2. **PREFLIGHT** — Check if the spec is already implemented
3. **DEV** — Claude Sonnet implements the greeting endpoint
4. **VALIDATE** — Run `pytest tests/ -q`
5. **REVIEW** — Claude Opus reviews the implementation
6. **DONE** or loop back to DEV if reviewer requests changes

## Run a sprint (multiple specs)

```bash
forge sprint sprints/hello-sprint.yaml --verbose --auto-merge
```

Runs `add-greeting` then `add-farewell` sequentially. `--auto-merge` merges
each approved branch to main automatically.

## What to expect

- **Cost:** ~$1-3 per spec (Sonnet dev + Opus review)
- **Time:** ~5-10 minutes per spec
- **Output:** Feature branches with implementations, `forge_audit.yaml` with full trace

## Files explained

```
forge.yaml                    # Project config — models, budgets, timeouts
specs/
  add-greeting.md             # Story: what to build + acceptance criteria
  add-farewell.md             # Another story
sprints/
  hello-sprint.yaml           # Sprint manifest: run these specs in order
```
