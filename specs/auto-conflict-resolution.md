---
name: "auto-resolve merge conflicts after review approval"
slug: auto-conflict-resolution
file_scope:
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Auto-Resolve Merge Conflicts

## Problem

When auto-merge fails due to a merge conflict, forge reports DONE but the
branch isn't merged. The human must manually resolve the conflict — even
when the resolution is trivial (e.g., both branches added code at the end
of the same file).

Real example: smart-model-config was APPROVED with 0 P1s but couldn't merge
because gate-override had been manually merged to main, adding tests at the
end of test_coordinator.py. Both sides added code, no overlap. Resolution:
keep both, remove markers. Two minutes of human time that forge should have
handled.

## Design

When `git merge` fails with a conflict during auto-merge:

1. Run `git diff --name-only --diff-filter=U` to get conflicted files
2. Invoke the dev agent with a conflict-resolution prompt containing:
   - The conflicted file(s) with markers
   - The spec summary (what this branch does)
   - Instructions: resolve conflicts, preserve both sides' intent
3. Agent resolves the conflicts (edits files to remove markers)
4. Run the gate to verify the resolution didn't break anything
5. If gate passes → `git add` + `git commit` to complete the merge
6. If gate fails → abort merge, report as merge failure (human needed)

### Prompt template

```
You are resolving a git merge conflict. The branch `{branch}` is being
merged into `{base_branch}`.

Branch purpose: {spec_name}

The following files have conflicts:
{conflicted_files_with_content}

Resolve each conflict by editing the files to remove all conflict markers
(<<<<<<, =======, >>>>>>). Preserve the intent of both sides. When both
sides add code in the same location, keep both additions.

After resolving, run the project's test suite to verify nothing is broken.
```

### Safety constraints

- Maximum 1 conflict resolution attempt per merge
- If more than 5 files are conflicted, skip (too complex for automated resolution)
- If the gate fails after resolution, abort and revert
- Agent gets read + edit tools only (no new code, just conflict resolution)

---

## Requirements

### R1: Conflict detection

After `git merge` fails, check for conflict markers:

```python
conflicted = _run_shell("git diff --name-only --diff-filter=U", cwd=project_root)
```

If no conflicted files (merge failed for other reasons), don't attempt resolution.

### R2: Complexity guard

If `len(conflicted_files) > 5`, skip automated resolution. Log:
`[forge]   ⚠ Too many conflicted files (N) — skipping auto-resolution`

### R3: Conflict resolution agent

Invoke the dev agent (or a lightweight agent) with:
- Conflicted file contents (with markers)
- Spec name for context
- Edit-only tool access

Use the dev profile's model. Timeout: 120 seconds (conflicts should be fast).

### R4: Post-resolution gate

Run the project gate after conflict resolution. If gate fails:
- `git merge --abort`
- Report merge failure
- Log: `[forge]   ⚠ Conflict resolution broke tests — aborting merge`

### R5: Complete the merge

If gate passes:
- `git add` the resolved files
- `git commit --no-edit` to finalize the merge
- Continue with auto-push if configured

### R6: Logging

```
[forge]   Merge conflict in 2 file(s): tests/test_coordinator.py, src/theforge/config.py
[forge]   Attempting auto-resolution...
[forge]   ... resolution done (15s)
[forge]   Running gate to verify resolution...
[forge]   ✓ Conflict resolved and merged
```

### R7: Tests

- `test_conflict_resolution_succeeds`: mock conflict + agent resolves → merge completes
- `test_conflict_resolution_gate_fails`: resolution breaks tests → merge aborted
- `test_conflict_too_many_files_skipped`: >5 files → skipped
- `test_no_conflict_no_resolution`: clean merge → no resolution attempted
- `test_conflict_resolution_timeout`: agent times out → merge aborted

---

## Acceptance Criteria

1. Merge conflicts trigger automatic resolution attempt
2. Dev agent resolves conflicts with edit-only access
3. Gate runs after resolution to verify correctness
4. Failed resolution aborts the merge cleanly
5. More than 5 conflicted files skips auto-resolution
6. All existing tests pass unchanged

## Out of Scope

- Semantic conflict resolution (code compiles but logic is wrong)
- Multi-attempt resolution (retry with different model)
- Conflict prevention (rebasing before merge)
