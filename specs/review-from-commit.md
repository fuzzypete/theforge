---
name: Review from commit
slug: review-from-commit
---

# Spec: Review from Commit

## Problem

The review prompt currently injects the full `git diff main...HEAD` output as a
text blob into every reviewer's prompt. This is wrong for several reasons:

1. **Context window destruction** — a large refactor produces 500KB–1MB of diff
   text. Gemini-2.5-pro silently fails after 4 seconds. Codex degrades. The
   review is broken before it starts.

2. **Bypasses source control** — the worktree IS the handoff artifact. Reviewers
   have Read/Bash/Glob/Grep tools. They should read the actual committed source,
   not a reconstructed text artifact.

3. **Reviewers work from bad data** — they flag things that aren't actually
   changed (diff context lines), miss things that are (diff doesn't show
   surrounding context well), and can't follow cross-file references.

The correct model: a PR reviewer on GitHub opens the PR, sees the changed file
list, and reads the source. They do not receive a 50,000-line text dump.

## Solution

Replace `diff_text` in the review prompt with a compact changed-files manifest
derived from `git diff --stat main...HEAD`. Reviewers use their tools to read
the actual source in the worktree.

## Changes Required

### `src/theforge/coordinator.py`

- Replace `_get_diff()` call before review with `_get_diff_stat()` — a new
  helper that runs `git diff --stat main...HEAD` (summary only, ~20 lines max).
- Pass `diff_stat` instead of `diff_text` to `build_review_prompt()`.
- Remove `_get_diff()` usage in the review path entirely. Keep it only if used
  elsewhere (audit log, dev feedback).

### `src/theforge/task.py`

- Rename `diff_text` parameter on `build_review_prompt()` to `diff_stat`.
- Update the prompt template: replace the full diff section with a changed-files
  section that lists the files and line counts from `git diff --stat`.
- Add explicit instruction: "Use your Read, Bash, Glob, and Grep tools to
  inspect the actual source in the worktree. Do not rely solely on the summary
  below."

### Example prompt section (after fix)

```
## Changed Files (git diff --stat)

 src/theforge/coordinator.py  | 312 +++---
 src/theforge/coord_state.py  | 118 +++
 src/theforge/coord_notify.py | 379 +++
 tests/test_coordinator.py    | 892 ++-
 ...
 12 files changed, 2847 insertions(+), 1203 deletions(-)

Use your Read, Bash, Glob, and Grep tools to inspect the changed files in the
worktree at: {workspace_path}

The branch under review is: {branch}
```

## Acceptance Criteria

1. `build_review_prompt()` no longer accepts or uses a full diff string.
2. The review prompt contains only `git diff --stat` output (line count summary),
   not raw diff hunks.
3. Gemini and Codex reviewers complete successfully on a large refactor task
   (coordinator-refactor is the test case — diff was ~800KB before this fix).
4. Reviewer prompts explicitly instruct use of Read/Bash/Glob/Grep tools.
5. `make test` passes. `make lint` passes.

## File Scope

```
src/theforge/coordinator.py
src/theforge/task.py
tests/test_task.py
tests/test_coordinator.py
```
