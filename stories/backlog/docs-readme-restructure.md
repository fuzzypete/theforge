---
name: Restructure README around a landing funnel
slug: docs-readme-restructure
pytest_target: tests/
---

# Restructure README Around a Landing Funnel

## Problem

The README currently opens with architecture and philosophy. New users need a
faster path from "what is this?" to "can I run it?" The README should function
as a landing funnel: position → self-sort → quickstart → how it works → next docs.

## Acceptance criteria

- README sections appear in this order:
  1. Hero / positioning (deterministic orchestration, LLMs generate not control,
     phase gates, audit trail, resumability)
  2. "Is TheForge for you?" section with **Best for** and **Not ideal for** lists
  3. 5-minute quickstart: clone, install, check-providers, run hello-forge example,
     expected output summary
  4. "How it works" with lifecycle diagram and minimal prose
  5. "What gets created" explaining .forge/, logs, worktrees, audit outputs
  6. Configuration section (existing content, tightened)
  7. Documentation links table (existing)
  8. Architecture section (existing, moved lower)
  9. Development section (existing)
- The "Is TheForge for you?" section includes:
  - Best for: bounded feature work, bug fixes, repos with runnable tests/lints,
    teams wanting auditability
  - Not ideal for: vague greenfield ideation, repos without deterministic
    validation, giant unscoped refactors, UX-heavy exploratory work
- The quickstart references the hello-forge example as the canonical first run
- The "What gets created" section explains which files are user-authored vs
  generated vs safe-to-delete
- No content is lost — existing sections are reorganized, not removed
- Cost table remains (it's a trust signal)
