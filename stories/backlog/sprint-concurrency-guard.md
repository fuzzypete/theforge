---
name: "bug: concurrent sprint launches can double-spend against same worktrees"
slug: sprint-concurrency-guard
---

# bug: concurrent sprint launches can double-spend against same worktrees

## Observed

Two sprints launched within minutes of each other ran the same stories concurrently, producing duplicate dev and review cycles against the same worktrees. Total double-spend: ~$9.

## Expected

Launching a sprint whose stories already have active worktrees refuses to start and tells the user what is already running.
