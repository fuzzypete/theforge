---
name: "bug: concurrent sprint launches can double-spend against same worktrees"
slug: sprint-concurrency-guard
---

# bug: concurrent sprint launches can double-spend against same worktrees

## Observed

Two sprints launched within minutes of each other ran the same stories concurrently against overlapping worktrees. Both ran to completion independently, producing duplicate dev and review cycles. Total double-spend: ~$9 (GH issue #181).

## Expected

Launching a sprint whose stories already have active worktrees (or an in-flight lock) should refuse to start and tell the user what is already running. No double-spend should be possible without an explicit override.
