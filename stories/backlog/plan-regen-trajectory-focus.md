---
name: "Plan regen trajectory focus — dominant theme filtering"
slug: plan-regen-trajectory-focus
github_issue: 250
pytest_target: tests/
---

# Plan Regen Trajectory Focus

## Problem

When the planner receives a backtrack prompt after a rejected plan, it gets
the full list of findings — P1s and P2s alike. The planner tends to address
the easiest items first (renaming test files, fixing config error types) while
the core architectural blocker survives across all three regen attempts.

In the `on-approve-merge-pr` run, the dominant surviving finding ("approval
completion path cannot return failure") persisted across all 3 plan attempts
while the planner spent its budget fixing 5 different P2-level items that
changed between attempts. The plan never converged because attention was
diluted.

## Goal

Backtrack prompts for plan regen focus the planner on unresolved findings that
have survived previous attempts, and deprioritize or strip findings that are
new, low-severity, or already resolved.

## Acceptance Criteria

- Backtrack prompts for plan regen highlight surviving P1 findings from
  previous attempts as the primary focus
- P2 findings are omitted from the backtrack prompt when at least one
  unresolved P1 exists from a previous attempt
- New findings (first seen in the current attempt) are presented separately
  from recurring findings, with recurring findings given explicit priority
- The dominant surviving theme (the finding family with the longest survival
  streak) is called out explicitly in the backtrack prompt
- When no P1 findings survive from previous attempts, all findings are
  included as today (no behavior change for first-attempt rejections)
- Plan regen audit records which findings were filtered and which were
  highlighted
- All existing tests pass

## Out of Scope

- Changes to how plan reviewers assign severity (that's a prompt concern)
- Plan review corroboration rules (separate story)
- Model escalation during plan regen
- Code review (post-dev) trajectory behavior (already handled by
  surviving_families)

## Notes

- The backtrack prompt builder already receives the finding registry with
  cycle-first-seen and disposition fields. The filtering logic uses what's
  already tracked — no new data collection needed.
- "Dominant surviving theme" is the finding family with the most consecutive
  appearances in the registry without a "fixed" disposition.
- This is analogous to how the existing code review trajectory injects
  surviving_families into dev prompts — same principle applied to plan regen.
- Be careful not to strip ALL context — the planner still needs to understand
  the plan structure. Only the findings list in the backtrack section should
  be filtered.
