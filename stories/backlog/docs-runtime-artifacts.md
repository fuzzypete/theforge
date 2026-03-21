---
name: Add runtime artifacts and filesystem layout documentation
slug: docs-runtime-artifacts
pytest_target: tests/
---

# Add Runtime Artifacts and Filesystem Layout Documentation

## Problem

Users get calmer when they know which files are "theirs" versus "runtime
machinery." There is no doc explaining what .forge/ contains, what's safe to
delete, what persists across runs, and what's generated vs user-authored. This
makes the tool feel opaque.

## Acceptance criteria

- A "What gets created" section exists in either README or Getting Started
  (or both, with the README version being a summary linking to the full version)
- The section includes an annotated filesystem tree showing:
  - forge.yaml (user-authored config)
  - specs/ and briefs/ (user-authored inputs)
  - sprints/ (user-authored manifests)
  - .forge/.env (user secrets, gitignored)
  - .forge/logs/ (generated, per-run logs)
  - .forge/worktrees/ (generated, managed git worktrees)
  - .forge/hooks/ (user-authored lifecycle hooks)
  - forge_audit.yaml (generated, per-run audit trail)
  - handoff.yaml (generated, gate output)
- Each entry is annotated with: user-authored vs generated, safe-to-delete vs
  important, persisted vs ephemeral
- The mental model section clarifies: TheForge is a coordinator not an
  autonomous IDE, each phase has a narrow job, models produce artifacts not
  runtime authority, validation and review are gates not vibes
