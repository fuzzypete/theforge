---
name: "Sprint default max_parallel from forge.yaml"
slug: sprint-default-parallel
pytest_target: tests/
---

# Sprint default max_parallel from forge.yaml

## Problem

`max_parallel` is only configurable per-sprint-manifest. Projects that always
want parallel execution must add `max_parallel: 3` to every manifest. There is
no project-level default.

## Solution

Add an optional `sprint.max_parallel` key to `forge.yaml`:

```yaml
sprint:
  max_parallel: 3
```

The sprint manifest value takes precedence if set. If the manifest omits
`max_parallel`, the `forge.yaml` default is used. If neither is set, the
default remains 1 (backward compatible).

## Acceptance criteria

- `forge.yaml` accepts `sprint.max_parallel` as an integer ≥ 1
- Sprint manifests that omit `max_parallel` inherit the forge.yaml default
- Sprint manifests that specify `max_parallel` override the forge.yaml default
- When neither is set, default is 1
- `ForgeConfig` exposes the sprint default (new field or nested config)
- `run_sprint()` reads the config default when manifest value is absent
- All existing tests pass
- New tests for config parsing and precedence logic
