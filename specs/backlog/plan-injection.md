---
name: "Plan injection — --plan flag to skip PLAN phase with existing plan"
slug: plan-injection
pytest_target: tests/
---

# Plan Injection

## Problem

The PLAN phase always runs an Opus agent to produce `forge_plan.md`. When the user
already has a high-quality plan (e.g. produced externally via Claude Code plan mode,
multi-model review, or a prior planning session), running PLAN wastes budget and
often produces an inferior result.

## Solution

Add a `--plan <path>` flag to `forge run` that copies an existing plan file into
the worktree as `forge_plan.md` during WORKSPACE setup, then skips the PLAN phase
entirely.

## CLI change

```
forge run specs/foo.md --plan /path/to/plan.md
```

The path must exist and be a readable file. If it does not exist, abort with a
clear error before touching anything.

## Coordinator change

`run_task()` accepts an optional `plan_path: Path | None = None` parameter.

When `plan_path` is set:
1. After WORKSPACE completes, copy `plan_path` → `<workspace_path>/forge_plan.md`
2. Set `state.plan_output` to the file contents
3. Skip PLAN phase entirely (do not run planning agent, do not set `plan_result`)
4. Log: `"  ✓ PLAN   (injected from <filename>)"`

When `plan_path` is None, behaviour is unchanged.

## Config/forge.yaml

No forge.yaml changes. This is a per-invocation CLI flag only.

## Acceptance criteria

- [ ] `forge run <spec> --plan <path>` copies the file and skips PLAN agent
- [ ] If `--plan` path does not exist, abort with error before WORKSPACE runs
- [ ] `state.plan_output` is set to the injected plan contents
- [ ] `state.plan_result` remains None when plan is injected
- [ ] Log line shows `✓ PLAN   (injected from <filename>)`
- [ ] Without `--plan`, behaviour is identical to today
- [ ] New test: `--plan` path copied to worktree, PLAN agent not called
- [ ] New test: `--plan` with missing file raises error before WORKSPACE
- [ ] Existing tests pass unchanged
