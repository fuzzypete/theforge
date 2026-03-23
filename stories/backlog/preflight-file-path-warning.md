---
name: "Preflight: warn on missing file references instead of blocking"
slug: preflight-file-path-warning
pytest_target: tests/
---

# Preflight File Path Warning

## Problem

Preflight blocks (ESCALATE) when a story references file paths that don't exist
on disk. Stories shouldn't reference file paths at all (that's the plan's job),
but when they do, blocking is too aggressive. The plan agent can discover the
correct paths.

## Acceptance criteria

- File references in story text that don't exist → warning, not blocker
- Preflight still blocks on real issues (contradictions, missing deps)
- Warning message includes the missing paths for debugging
- All existing tests pass
