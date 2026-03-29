---
name: "Worktree resume hygiene: sync config from root, clean stale plan files"
slug: worktree-resume-hygiene
---

# Worktree resume hygiene: sync config from root, clean stale plan files

## Observed

Two related problems when a worktree is reused across runs:

1. Resumed runs use the worktree's stale `forge.yaml` snapshot instead of the project root's current config. This caused wrong plan model (sonnet instead of opus), missing reviewer tool restrictions, and ignored profile changes — all because the worktree's `forge.yaml` was frozen at branch creation time. (GH issue #173)

2. Plan files from previous runs accumulate in the worktree's `plans/` directory. On resumed runs, stale `plan-review-v2-plan.md` files from prior sprints sit alongside the current plan, making it impossible to tell which plan was actually used without digging through logs. (GH issue #182)

## Expected

On resume, the coordinator syncs `forge.yaml` from the project root into the worktree before any phase runs. The worktree's code is the dev agent's workspace; orchestration config always comes from the project root.

Before a new PLAN phase starts in an existing worktree, any prior plan files are removed or archived so the active plan is unambiguous.
