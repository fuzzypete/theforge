---
name: Add key diagrams — lifecycle, control boundaries, failure recovery
slug: docs-diagrams
pytest_target: tests/
---

# Add Key Diagrams to Documentation

## Problem

Several diagrams would genuinely help users understand TheForge, but only
specific ones — not diagram confetti. The three highest-value diagrams are:
the lifecycle state machine, the coordinator-vs-models control boundary
diagram, and a failure recovery decision tree.

## Acceptance criteria

- A Mermaid lifecycle state machine diagram is added to README or Getting
  Started showing:
  - All phases: INIT, WORKSPACE, PREFLIGHT, PLAN, PLAN_REVIEW, DEV,
    VALIDATE, REVIEW, DONE, ESCALATE
  - Failure loops: VALIDATE fail back to DEV, REVIEW request-changes back
    to DEV
  - Escalation terminal edge
  - Resume entry points noted
- A Mermaid control boundaries diagram is added showing three lanes:
  - Coordinator: phase engine, worktree manager, validation gate, resume
    state, audit/log writer
  - Models: planner, developer, review pool
  - Repo/runtime: git repo, tests/lints, artifacts
  - Arrows showing who controls what (coordinator invokes models, models
    write to repo, coordinator gates repo)
- A Mermaid failure recovery decision tree is added to the troubleshooting
  guide showing:
  - Branch points: where did it fail? (provider/auth, workspace, validate,
    review, interrupted)
  - Recovery actions: check-providers, inspect git state, inspect gate
    output, use --resume
- Diagrams use Mermaid syntax for GitHub rendering compatibility
- No UML, no class diagrams, no cost heatmaps — only the three diagrams
  above
