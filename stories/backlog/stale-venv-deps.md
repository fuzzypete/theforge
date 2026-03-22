---
name: "Fix stale worktree venvs skipping dependency updates"
slug: stale-venv-deps
pytest_target: tests/
---

# Fix Stale Worktree Venvs

## Problem

`setup_command` uses `test -d .venv ||` guard so existing worktrees from prior
runs keep old venvs. When forge.yaml changes from `pip install -e .` to
`pip install -e '.[all]'`, existing worktrees don't pick up new extras.

This caused `ModuleNotFoundError: No module named 'anthropic'` when adaptive
assignment selected an API agent in a worktree created before the change.

## Acceptance criteria

- Package installation always runs, even if venv exists
- Venv creation is still guarded (don't recreate from scratch)
- setup_command change is detected and logged
- Existing tests pass
