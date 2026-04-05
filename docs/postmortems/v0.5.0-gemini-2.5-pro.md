# Postmortem: Sprint v0.5.0 (2026-04-05)

## Summary
The `v0.5.0` sprint was a high-concurrency run (max_parallel: 4) that revealed critical architectural gaps in TheForge's parallel execution model. While individual agents performed well, the coordination layer failed to maintain isolation and visibility across "Resumed" stories.

- **Total Stories:** 13
- **Succeeded:** 6 (46%)
- **Failed/Escalated:** 7 (54%)
- **Key Themes:** Resume-mode blind spots, Python module leakage, and no-op merge failures.

---

## Detailed Analysis

### 1. Collision Detection Bypass (Issue #227)
**Symptom:** Squash-rebase failed during the merge phase because of conflicts in `completion.py` and `dag.py`.
**Root Cause:** Overlap detection relies on a `PLAN_DONE` signal to register a story's "file footprint." Resumed stories (like #227, which started at `REVIEW`) bypass the `PLAN` phase and never register their footprints. This made #227 "invisible" to #25, which was allowed to enter `DEV` and modify the same files.
**Impact:** serialization failed for the most critical coordinator files.

### 2. Python Workspace Leakage (Issue #391)
**Symptom:** `ImportError: cannot import name '_apply_provider_fallback' from 'theforge.config.profiles'`.
**Root Cause:** Parallel workers run in isolated git worktrees but the Python interpreter defaults to importing from the project root's `src/` directory. When PR #402 merged to `main`, it updated `profiles.py` in the root. Worker #391 caught the filesystem in a middle-state or saw a version mismatch between its worktree code and the root's metadata.
**Impact:** Non-deterministic worker crashes during concurrent merges.

### 3. "No-Op" PR Failure (Issue #360)
**Symptom:** `gh pr create failed: GraphQL: No commits between main and feat/issue-360`.
**Root Cause:** The story was already implemented on `main`. The dev agent produced a "no changes" handoff. The runner reached the merge phase and attempted to create a PR. GitHub prohibits PRs with zero unique commits.
**Impact:** Unnecessary escalation for stories that are already complete.

---

## Proposed Action Items

### P0: Critical Integrity Fixes
- **Resume Footprint Registration:** Modify `run_sprint` to call `_extract_plan_footprint` during triage for any story with an existing worktree containing a `.forge/plan.md`.
- **Sys.Path Shimming:** In `run_task`, explicitly prepend the worktree's `src` directory to `sys.path` to ensure module isolation from the project root.

### P1: Process Improvements
- **Pre-Merge Commits Check:** Add a guard in `_create_pr` to check if the branch is actually ahead of `main`. If not, skip PR creation and mark as `DONE`.
- **Preflight Overlap Bias:** Update the Preflight Agent to be more conservative (serialize more) when "Hotspot" files like `completion.py` or `engine.py` are listed in `likely_files`.

### P2: Observability
- **Collision Logging:** Add a `collision_audit.yaml` that tracks why stories were serialized or gated, making the scheduler's decisions transparent.
