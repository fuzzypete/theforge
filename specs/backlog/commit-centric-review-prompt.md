---
name: "Commit-centric review prompt"
slug: commit-centric-review-prompt
pytest_target: tests/
---

# Spec: Commit-centric review prompt

## Problem

The current review prompt in `build_review_prompt()` (task.py) provides reviewers
with the spec and a general instruction to review the implementation. Reviewers must
discover what changed by reading files and running git commands themselves. This is
inefficient — each reviewer independently runs `git diff`, `git log`, etc., burning
tokens on discovery rather than analysis.

The review prompt should be commit-centric: present the reviewer with exactly what
changed (like a PR diff), so they can focus on evaluating the changes against the
spec.

This aligns with the project's core direction: reviews should evaluate commits vs
spec, like PRs without GitHub. The dev agent implements freely, reviewers evaluate
the result.

## Solution

In `build_review_prompt()`, generate and include:

### 1. Commit manifest

```
git log main..HEAD --oneline
```

List of commits with SHAs and messages. Gives reviewers the narrative of what was
done.

### 2. Changed files summary

```
git diff main..HEAD --stat
```

Quick overview of what files were touched and how much.

### 3. Full diff (or key hunks)

```
git diff main..HEAD
```

The actual changes. For large diffs, include the full diff but note total line count
so reviewers can triage.

### 4. Dev handoff notes

If `handoff.yaml` exists, include the dev agent's self-reported notes (what they did,
what they're uncertain about, what acceptance criteria they believe are met).

### Prompt structure for reviewers

```
You are reviewing a code change against a spec.

## Spec
<spec content>

## What changed
### Commits
<git log output>

### Changed files
<diffstat>

### Diff
<full diff>

### Dev notes
<handoff notes if available>

## Your task
Review whether the changes satisfy the spec's acceptance criteria. Focus on:
1. Does the implementation match the spec?
2. Are there correctness issues (bugs, edge cases, security)?
3. Is test coverage adequate for the acceptance criteria?
```

### Implementation

- `build_review_prompt()` in `task.py` gains a `workspace_path` parameter.
- Runs `git log` and `git diff` via subprocess at prompt build time.
- Includes output in the prompt.
- Falls back gracefully if git commands fail (existing behavior as fallback).

### Size management

- If diff exceeds 50k characters, truncate with a note:
  "Diff truncated at 50k chars. Full diff available via `git diff main..HEAD` in the
  worktree."
- The reviewer still has Read/Grep/Glob tools for deeper investigation.

## Acceptance Criteria

1. `build_review_prompt()` includes git log, diffstat, and full diff in review prompt.
2. Dev handoff notes included when `handoff.yaml` exists.
3. Diff truncated with clear message when exceeding 50k chars.
4. Graceful fallback if git commands fail.
5. `workspace_path` parameter added to `build_review_prompt()`.
6. Existing tests pass.
7. New tests for prompt construction with git output, truncation, and fallback.
