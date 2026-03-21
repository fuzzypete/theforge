---
name: Add a first-run walkthrough with expected transcript
slug: docs-first-run-walkthrough
pytest_target: tests/
---

# Add a First-Run Walkthrough with Expected Transcript

## Problem

A lot of operator confidence comes from seeing a realistic terminal transcript
before running the tool. Users need to know what normal looks like so they can
recognize when something goes wrong. Abstract CLI reference doesn't serve this
purpose — a narrated transcript does.

## Acceptance criteria

- A new file docs/guides/first-run-walkthrough.md exists
- It walks through a complete forge run step by step:
  - Command entered
  - Phase-by-phase console output (realistic, based on actual hello-forge runs)
  - What a successful validation looks like
  - What a failed validation looks like (and what happens next)
  - What review approve vs request-changes looks like
  - How escalation appears in practice
  - How resume works after an interruption
- The walkthrough uses the hello-forge example as its subject
- It is linked from Getting Started and the hello-forge README
