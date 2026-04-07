# CLI subsystem guidance

## Purpose

The CLI subsystem exposes operator-facing commands for running TheForge,
inspecting audits, managing daemon behavior, checking configuration, and working
with sprint and review flows.

## Invariants

- CLI commands should remain thin orchestration layers over underlying modules;
  avoid embedding core business logic directly in command handlers.
- Preserve stable, comprehensible command behavior and error reporting. Prefer
  explicit user-facing failures over silent fallback behavior.
- Keep command-specific concerns separated so `main.py` and shared helpers do not
  become catch-all modules.
- CLI wiring must respect deterministic coordinator behavior rather than adding
  alternate process policy at the command layer.
- When commands surface structured run data, do not hide details that operators
  need for debugging or audit review.

## Context

- `main.py` is the primary entry point that assembles the command tree.
- Modules such as `run.py`, `review.py`, `sprint.py`, `daemon.py`, and
  `status.py` implement major command families.
- `shared.py` and `overrides.py` contain reusable CLI plumbing and option
  handling.
- `audit.py`, `check_config.py`, and `providers.py` are useful reference points
  for commands that inspect system state rather than launching work.
- If a change mostly affects terminal UX, argument parsing, or command dispatch,
  it likely belongs here; if it changes execution semantics, inspect the
  underlying subsystem first.
