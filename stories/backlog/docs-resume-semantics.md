---
name: Document resume semantics with a state recovery matrix
slug: docs-resume-semantics
pytest_target: tests/
---

# Document Resume Semantics with a State Recovery Matrix

## Problem

Resume behavior is critical for a tool like this but insufficiently documented.
Users will ask: what exactly is resumed? Can I resume after manual edits? What
if the worktree changed? What if review failed? These questions need clear,
table-formatted answers.

## Acceptance criteria

- A "Resume behavior" section exists in both CLI Reference and the
  troubleshooting guide
- It includes a resume matrix table with columns: interrupted state, resume
  behavior, notes. Rows cover at minimum:
  - Interrupted during PLAN
  - Interrupted during DEV
  - Failed VALIDATE (gate FAIL)
  - Failed REVIEW parse / schema error
  - REVIEW returned REQUEST_CHANGES
  - Provider crashed / timed out mid-phase
  - Stale worktree from a previous run
  - Manual human edits made to worktree between runs
- The section clarifies which scenarios are safe vs which may produce
  unexpected results
- The section explains how to force a clean restart if resume produces
  bad state (delete worktree + rerun without --resume)
