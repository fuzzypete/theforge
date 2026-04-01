# Hello Forge

The canonical first-run example for TheForge.

Use this example to answer three questions quickly:

- Does my provider setup actually work?
- What does a successful run look like?
- What files and branches should I expect afterward?

## Prerequisites

- Python 3.11+
- Claude Code CLI installed and authenticated
- TheForge installed from the repo root
- `pytest` available

Quick checks:

```bash
claude --version
forge --help
forge check-providers
```

## One-time setup

Initialize the example as its own git repository:

```bash
cd examples/hello-forge
git init
git add -A
git commit -m "initial: hello-forge scaffold"
```

## Run it

```bash
forge run specs/add-greeting.md --verbose
```

This example keeps its stories under `specs/` for continuity. New projects
created with `forge init` now scaffold `stories/TEMPLATE.md`, but any story
file path works.

Expected shape of a successful run:

```text
[forge] ▸ WORKSPACE   add-greeting
[forge]   Created worktree: .forge/worktrees/add-greeting
[forge] ▸ PREFLIGHT   sonnet
[forge]   Verdict: PROCEED
[forge] ▸ DEV         sonnet  iter 1
[forge]   ↳ Edit: src/app.py
[forge]   ↳ Write: tests/test_greet.py
[forge] ▸ VALIDATE    pytest
[forge]   Gate: PASS
[forge] ▸ REVIEW      opus
[forge]   ✓ REVIEW   APPROVE
[forge] ▸ DONE        add-greeting
```

For a more complete transcript, including retry behavior, see
[EXPECTED_OUTPUT.md](EXPECTED_OUTPUT.md).

## What success looks like

After a successful run you should have:

- A new branch: `forge/add-greeting`
- A managed worktree: `.forge/worktrees/add-greeting`
- A changed implementation in `src/app.py`
- New test coverage for the greeting behavior
- An audit trail in `.forge/worktrees/add-greeting/forge_audit.yaml`

Useful verification commands:

```bash
git log forge/add-greeting --oneline -1
git diff --stat HEAD..forge/add-greeting
cd .forge/worktrees/add-greeting
python -m pytest tests/ -v
```

## Merge or inspect

Auto-merge on the run:

```bash
forge run specs/add-greeting.md --auto-merge
```

Or inspect and merge manually:

```bash
git diff HEAD..forge/add-greeting
git merge forge/add-greeting
```

## Reset and rerun

```bash
git worktree remove .forge/worktrees/add-greeting --force
git branch -D forge/add-greeting
forge run specs/add-greeting.md --verbose
```

## Run the sprint

```bash
forge sprint sprints/hello-sprint.yaml --verbose --auto-merge
```

This runs `add-greeting` and `add-farewell` sequentially.

Typical expectation for this example: about $1-3 and roughly 5-10 minutes per
story, depending on review loops and provider latency.

## Files in this example

```text
forge.yaml
specs/
  add-greeting.md
  add-farewell.md
sprints/
  hello-sprint.yaml
src/
  app.py
tests/
  test_app.py
EXPECTED_OUTPUT.md
```

## See also

- [Getting Started](../../docs/guides/getting-started.md)
- [First-Run Walkthrough](../../docs/guides/first-run-walkthrough.md)
- [CLI Reference](../../docs/guides/cli-reference.md)
