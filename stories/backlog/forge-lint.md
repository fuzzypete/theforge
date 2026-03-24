---
name: "forge lint — static code health checks as a gate and standalone command"
slug: forge-lint
pytest_target: tests/
---

# forge lint

## Problem

Files grow unchecked. coordinator.py hit 2788 lines. config.py hit 1184.
run_task is a 1205-line function. Nobody notices until a story tries to
modify one of these files and the dev agent times out because it can't hold
the full file in working context.

There is no mechanical check that catches this. Ruff checks style. Pytest
checks correctness. Nothing checks structural health — file size, function
length, growth rate. These are the signals that predict "this file needs a
refactor before the next story touches it."

## Solution

`forge lint` — a fast (<2s) static analysis command that checks structural
health. Runs standalone, runs in CI, and runs as part of the forge gate via
`pre_validate_command`.

### Checks

| Check | Default threshold | Severity |
|-------|------------------|----------|
| File exceeds line limit | 500 lines | warning |
| Function exceeds line limit | 100 lines | error |
| File grew >30% in this branch | — | warning |
| Class exceeds line limit | 300 lines | warning |

Thresholds are opinionated defaults. Override via `.forge/lint.yaml` if
needed, but the defaults should be right for 90% of projects.

### Output

```
forge lint

src/theforge/coordinator.py
  ✗ run_task: 1205 lines (max 100)
  ⚠ file: 2788 lines (max 500)

src/theforge/cli.py
  ⚠ file: 2372 lines (max 500)

src/theforge/config.py
  ⚠ file: 1184 lines (max 500)

3 files, 1 error, 3 warnings
```

### Exit codes

- `0` — no errors (warnings OK)
- `1` — errors found
- `2` — lint itself failed (bad config, parse error)

### Gate integration

```yaml
validation:
  pre_validate_command: "forge lint"
```

When wired into the gate, errors fail the gate. Warnings are logged but
don't block. This means a dev agent that creates a 600-line file will get
a gate failure and be told to split it — mechanically, no LLM judgment.

### Implementation constraints

- Pure AST analysis (Python `ast` module). No LLM. No network.
- Must complete in <2s for a 20-file codebase.
- Works on any Python project, not just theforge.

## Acceptance criteria

- `forge lint` runs and reports file size, function size, class size
- Default thresholds: file 500, function 100, class 300
- Exit code 0/1/2 as described
- `.forge/lint.yaml` overrides thresholds (optional file)
- `pre_validate_command: forge lint` integration works in gate
- Runs in <2s on theforge's codebase
- All existing tests pass
- New tests for each check, threshold override, exit codes
