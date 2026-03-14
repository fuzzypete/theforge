---
name: "auto-push to remote after successful merge"
slug: auto-push
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/config.py
  - tests/test_coordinator.py
  - tests/test_config.py
pytest_target: tests/
---

# Auto-Push

## Problem

After `--auto-merge` merges a feature branch to local main, the commits
sit on the local machine. The user must manually `git push`. With 89
commits piling up before anyone notices, this defeats the point of
autonomous operation.

## Design

New config flag `workspace.auto_push: true` (default `false`). When enabled
and `--auto-merge` succeeds, the coordinator runs `git push origin {base_branch}`
after the merge.

```yaml
workspace:
  auto_push: true
```

### Behavior

1. Only triggers when auto-merge succeeds (not on escalation, not on
   review rejection)
2. Runs `git push origin {base_branch}` from the project root
3. If push fails (auth error, network), log a warning but do NOT fail the
   run — the merge already succeeded locally
4. Push timeout: 30 seconds (network operation)

### Sprint integration

During a sprint, push happens after each successful spec merge (not
batched at sprint end). This keeps the remote current and allows
CI to run incrementally.

---

## Requirements

### R1: `auto_push` config field

Add `auto_push: bool = False` to `WorkspaceConfig` dataclass.
Parse from `workspace.auto_push` in forge.yaml.

### R2: Push after merge

In `_merge_branch()` (coordinator.py), after successful merge:

```python
if auto_push:
    try:
        subprocess.run(
            ["git", "push", "origin", base_branch],
            cwd=str(project_root),
            timeout=30,
            capture_output=True,
            check=True,
        )
        _log(f"  Pushed {base_branch} to origin")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        _log(f"  ⚠ Push failed: {e} (merge succeeded locally)")
```

### R3: CLI display

```
[forge]   ✓ MERGE   feat/my-spec → main
[forge]   Pushed main to origin
```

Or on failure:
```
[forge]   ✓ MERGE   feat/my-spec → main
[forge]   ⚠ Push failed: ... (merge succeeded locally)
```

### R4: Tests

- `test_auto_push_after_merge`: auto_push=True + merge success → push called
- `test_auto_push_disabled_by_default`: auto_push absent → no push
- `test_auto_push_failure_non_fatal`: push fails → warning logged, run still DONE
- `test_auto_push_config_parsed`: forge.yaml `auto_push: true` → config field set

---

## Acceptance Criteria

1. `workspace.auto_push: true` triggers push after successful auto-merge
2. Push failure is non-fatal (warning only)
3. Default is `false` (backward compatible)
4. Works in both single-run and sprint modes
5. All existing tests pass
