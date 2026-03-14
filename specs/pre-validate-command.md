---
name: "Pre-validate command for build artifact cleanup"
slug: pre-validate-command
file_scope:
  - src/theforge/config.py
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Pre-Validate Command

## Problem

Build/test steps can modify files after the dev agent's final commit. In iOS
projects, Xcode silently touches `project.pbxproj` and `.xcscheme` files
during builds. In Python projects, `make fmt` can auto-fix formatting. These
"build artifacts" create a dirty worktree that fails the coordinator's
`git status --porcelain` check, even though the gate passed and the code is
correct.

This burned an entire sprint task (fix-polar-ios) which completed successfully
but couldn't merge because of two Xcode project files the dev agent didn't
know to commit.

## Context

The coordinator checks for uncommitted files immediately after gate PASS
(coordinator.py ~line 1343). If dirty files are found, it either sends the
agent back to DEV with a "PROCESS VIOLATION" message (burning another full
dev iteration) or escalates if no retries remain.

The gate command itself can produce dirty files — HDP's gate starts with
`make fmt` which auto-fixes formatting. If those fixes happen after the
agent's commit, they appear as uncommitted changes.

## Solution

### 1. Add `pre_validate_command` to `ValidationConfig` (config.py)

```python
pre_validate_command: str | None = None  # runs in worktree before dirty check
```

### 2. Run it in coordinator before the porcelain check (coordinator.py)

After gate PASS but before `git status --porcelain`, if
`config.validation.pre_validate_command` is set, run it in the workspace.

If the command fails (non-zero exit), log a warning but continue to the dirty
check — the pre-validate is best-effort cleanup, not a gate.

### 3. Parse from forge.yaml (config.py)

```yaml
validation:
  gate_command: "..."
  pre_validate_command: "git add -A && git diff --cached --quiet || git commit -m 'chore: commit build artifacts'"
```

## Acceptance Criteria

- [ ] `ValidationConfig` has `pre_validate_command: str | None = None`
- [ ] Coordinator runs `pre_validate_command` after gate PASS, before dirty check
- [ ] Pre-validate command failure logs a warning but does not fail the run
- [ ] `forge.yaml` parsing reads `pre_validate_command`
- [ ] Default value is `None` (backward compatible — no behavior change)
- [ ] Existing tests pass without modification
- [ ] New test verifies pre-validate runs before dirty check
- [ ] New test verifies pre-validate failure is non-fatal
