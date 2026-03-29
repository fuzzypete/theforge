---
name: "Worktree resume hygiene: sync config from root, clean stale plan files"
slug: worktree-resume-hygiene
---

# Worktree resume hygiene: sync config from root, clean stale plan files

## Observed

Resumed runs use the worktree's stale `forge.yaml` instead of the project root's current config, causing wrong models and ignored profile changes. Separately, plan files from prior runs accumulate in the worktree's `plans/` directory, making it impossible to tell which plan was actually used.

## Expected

On resume, the worktree's `forge.yaml` is synced from the project root before any phase runs. Before a new PLAN phase starts, stale plan files from prior runs are removed.
