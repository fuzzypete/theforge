# Hello Forge

A self-contained example project for TheForge. Run one command and watch the
full pipeline — plan, implement, test, review — complete from start to finish.

## Prerequisites

- **Python 3.11+**
- **Claude Code CLI** installed and authenticated (`claude --version`)
- **TheForge** installed (`pip install -e /path/to/theforge`)
- **pytest** installed (`pip install pytest`)

Verify your providers are ready:

```bash
forge check-providers
```

## Setup (one time)

After cloning the theforge repo, initialize this example as its own git repository:

```bash
cd examples/hello-forge
git init
git add -A
git commit -m "initial: hello-forge scaffold"
```

This is the only manual setup step. All source files, tests, and configuration
are already in place — no scaffolding needed.

## Run the example

```bash
forge run specs/add-greeting.md --verbose
```

**What to expect:**

```
[forge] ▸ WORKSPACE   add-greeting
[forge]   Created worktree: .forge/worktrees/add-greeting
[forge] ▸ PREFLIGHT   sonnet
[forge]   Verdict: PROCEED
[forge] ▸ DEV         sonnet  iter 1
[forge]   ↳ Read: src/app.py
[forge]   ↳ Read: tests/test_app.py
[forge]   ↳ Edit: src/app.py
[forge]   ↳ Write: tests/test_greet.py
[forge] ▸ VALIDATE    pytest
[forge]   Gate: PASS (4 passed in 0.3s)
[forge] ▸ REVIEW      opus
[forge]   ✓ REVIEW   APPROVE  0 P1  $1.10  4m 12s
[forge] ▸ DONE        add-greeting
```

See [EXPECTED_OUTPUT.md](EXPECTED_OUTPUT.md) for a complete, realistic terminal
transcript including what failed validation and a review request-changes look like.

## What changes after a successful run

- A new branch `forge/add-greeting` is created
- `src/app.py` is extended with the greeting feature
- New tests are added in the worktree
- `.forge/worktrees/add-greeting/forge_audit.yaml` contains the full run trace

**Verify success:**

```bash
# Check the branch was created
git log forge/add-greeting --oneline

# Inspect the audit trail
cat .forge/worktrees/add-greeting/forge_audit.yaml

# Run tests on the feature branch manually
cd .forge/worktrees/add-greeting
python -m pytest tests/ -v
```

## Merge the result

```bash
# Auto-merge (re-run with --auto-merge)
forge run specs/add-greeting.md --auto-merge

# Or manually
git merge forge/add-greeting
```

## Run a sprint (both stories)

```bash
forge sprint sprints/hello-sprint.yaml --verbose --auto-merge
```

Runs `add-greeting` then `add-farewell` sequentially.

## Reset and rerun

```bash
# Remove the worktree and branch to start fresh
git worktree remove .forge/worktrees/add-greeting --force
git branch -D forge/add-greeting

# Then rerun
forge run specs/add-greeting.md --verbose
```

## Files explained

```
forge.yaml                    # Project config — models, budgets, gate command
src/
  app.py                      # Minimal app — forge extends this
  __init__.py
tests/
  test_app.py                 # Baseline tests — pass before forge runs
  __init__.py
specs/
  add-greeting.md             # Story 1: what to build + acceptance criteria
  add-farewell.md             # Story 2: another feature
sprints/
  hello-sprint.yaml           # Sprint: run both stories sequentially
EXPECTED_OUTPUT.md            # Realistic terminal transcript of a successful run
```

## What things cost

- **Single story (add-greeting):** ~$1-3 (Sonnet dev + Opus review)
- **Time:** ~5-10 minutes per story
- **Budget cap:** Set in forge.yaml — hard-stops if exceeded

## See also

- [Getting Started](../../docs/guides/getting-started.md) — full setup walkthrough
- [First-Run Walkthrough](../../docs/guides/first-run-walkthrough.md) — narrated transcript
- [CLI Reference](../../docs/guides/cli-reference.md) — all commands and flags
