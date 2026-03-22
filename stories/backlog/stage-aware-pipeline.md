---
name: "Stage-aware pipeline — --until, --from, and runtime config overrides"
slug: stage-aware-pipeline
pytest_target: tests/
---

# Stage-aware pipeline — `--until`, `--from`, and runtime config overrides

## Problem

`forge run` always runs the full pipeline from INIT to DONE/ESCALATE with the
full forge.yaml config. The `--plan` flag exists for plan injection and
`--dev-model` overrides the dev agent, but there's no general way to:

- Run only through PLAN to preview the plan without executing dev
- Run from DEV onward when you've manually set up the workspace
- Run just REVIEW on an existing worktree
- Scale down the reviewer pool for simple stories (e.g. docs changes)
- Skip plan review for trivial work
- Run lightweight one-offs in parallel with full-pipeline core work

This was identified in the discovery doc (Sprint 2, `pipeline-entry-points`) and
partially implemented (`--plan` exists), but the general `--until <phase>` and
`--from <phase>` flags were never built. Runtime config overrides are needed so
operators can right-size the pipeline per story without maintaining multiple
forge.yaml files.

## Solution

Add two CLI flags to `forge run`:

### `--until <phase>`
Run the pipeline up to and including the specified phase, then stop cleanly.

```bash
forge run spec.md --until plan          # INIT → WORKSPACE → PREFLIGHT → PLAN → stop
forge run spec.md --until plan-review   # ... → PLAN → PLAN_REVIEW → stop
forge run spec.md --until validate      # ... → DEV → VALIDATE → stop
```

On stop: write audit YAML with `outcome.final_phase` set to the --until phase, `outcome.message: "Stopped at --until <phase>"`. Worktree preserved. Exit code 0.

### `--from <phase>`
Resume the pipeline from a specified phase, skipping earlier phases. Requires an existing worktree (either from a previous `--until` run or manual setup).

```bash
forge run spec.md --from dev            # skip INIT/WORKSPACE/PREFLIGHT/PLAN → DEV → ...
forge run spec.md --from review         # skip everything before REVIEW → REVIEW → ...
```

Preconditions checked at startup:
- Worktree must exist at the expected path
- For `--from dev`: `forge_plan.md` must exist in worktree (or `--plan` provided)
- For `--from review`: dev handoff must exist

### Combined:
```bash
forge run spec.md --from dev --until validate   # DEV → VALIDATE → stop
```

### Phase name mapping:
CLI accepts lowercase hyphenated names: `init`, `workspace`, `preflight`, `plan`, `plan-review`, `dev`, `validate`, `review`.

### Coordinator changes:
- `CoordinatorState.start_phase` and `CoordinatorState.stop_phase` (both Optional[Phase])
- Phase loop skips phases before start_phase
- Phase loop breaks after stop_phase
- Precondition validation before entering first phase

### Subsumes `--plan`:
`--plan <file>` becomes equivalent to injecting a plan file + `--from dev`. Keep `--plan` as sugar but implement via the same mechanism.

### Runtime config overrides

CLI flags that override forge.yaml per-run, letting operators right-size the
pipeline without separate config files:

```bash
forge run story.md --reviewers 1               # use only first reviewer from pool
forge run story.md --max-cycles 1               # cap review cycles
```

Combined with phase control and --dev-model:
```bash
# Lightweight docs run: sonnet dev, 1 reviewer
forge run docs-fix.md --dev-model anthropic/sonnet --reviewers 1

# Quick validation: just run dev + gate, stop before review
forge run story.md --from dev --until validate
```

Override flags apply only to the current run — forge.yaml is never modified.

### Subsumes `pipeline-entry-points.md`:
This spec replaces `specs/backlog/pipeline-entry-points.md` — move it to done/ or archive when this ships.

## Acceptance criteria

- `--until <phase>` stops pipeline after specified phase with clean exit
- `--from <phase>` resumes from specified phase, skipping earlier phases
- Combined `--from` + `--until` works
- Precondition validation: worktree exists for --from, plan exists for --from dev
- Phase names accepted as lowercase hyphenated strings
- Audit YAML records start/stop phases
- `--plan` still works, implemented via same mechanism
- `--reviewers N` limits reviewer pool to first N entries
- `--max-cycles N` caps review→dev iteration count
- Override flags are not persisted to forge.yaml
- Existing tests pass
- New tests for --until, --from, combined, precondition failures, and override flags
