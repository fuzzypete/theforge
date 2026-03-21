---
name: Add a dedicated troubleshooting guide
slug: docs-troubleshooting-guide
pytest_target: tests/
---

# Add a Dedicated Troubleshooting Guide

## Problem

When something goes wrong at 11:40 PM, users need a fast path from symptom to
fix. There is no troubleshooting doc. This is probably the single most important
missing doc after the quickstart — it will save more user frustration than any
amount of philosophy text.

## Acceptance criteria

- A new file docs/guides/troubleshooting.md exists with these sections:
  - **Install / environment**: `forge` command not found, editable install
    issues, wrong Python version, dependency problems
  - **Provider / auth**: provider binary not on PATH, auth expired, CLI opens
    but TheForge cannot invoke it, API key/env not loaded,
    `check-providers` failures
  - **Repo / workspace**: not in a git repo, dirty working tree, worktree
    branch collision, detached HEAD
  - **Execution**: PLAN failed, DEV timed out, VALIDATE failed, REVIEW failed
    to parse, ESCALATE triggered, resume confusion
  - **Cost / performance**: run is slow, review loop repeats, cost unexpectedly
    high, large diffs degrade quality
  - **Cleanup / recovery**: how to remove stale worktrees, how to restart a
    failed run cleanly, how to inspect logs/audit trail
- Each entry follows a consistent format: symptom, likely cause, fix
- The troubleshooting guide is linked from README, Getting Started, and
  CLI Reference "Next docs" sections
