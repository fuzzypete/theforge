---
name: "Plan review corroboration and severity gating"
slug: plan-review-corroboration
github_issue: 249
pytest_target: tests/
---

# Plan Review Corroboration and Severity Gating

## Problem

A single plan reviewer can block the entire pipeline with a P1 finding that no
other reviewer flagged. In the `on-approve-merge-pr` sprint, deepseek raised
P1s about implementation details (flag formatting, test file naming, merge
bookkeeping) that codex didn't flag. Each rejection burned a full regen cycle
without converging, because the planner kept fixing minor items while the one
real architectural blocker survived.

The plan review synthesis treats all P1s identically — a single-reviewer P1
about test placement carries the same weight as a corroborated P1 about
broken control flow. This is too conservative for autonomous operation.

## Goal

Plan review synthesis distinguishes between corroborated findings (multiple
reviewers flagged the same issue, or the finding recurred across regen
attempts) and uncorroborated findings (one reviewer, first occurrence). Only
corroborated or recurring findings block the plan. Single-reviewer,
first-occurrence P1s are downgraded to advisory and forwarded into the dev
prompt.

## Acceptance Criteria

- A P1 finding reported by only one reviewer on its first occurrence is
  downgraded to P1-impl (advisory) and does not reject the plan
- A P1 finding reported by 2+ reviewers rejects the plan as today
- A P1 finding that recurred from a previous regen attempt (same family per
  the existing finding registry) rejects the plan regardless of reviewer count
- P1-impl findings from plan review are injected into the dev prompt so the
  dev agent is aware of them
- P0 findings (if used) always block regardless of corroboration
- Existing code review (post-dev) severity behavior is unchanged — this only
  affects plan review synthesis
- Plan review audit records the original severity and the post-corroboration
  effective severity for each finding
- All existing tests pass

## Out of Scope

- Changing code review (post-dev) corroboration rules
- Modifying the plan reviewer prompt to change how reviewers assign severity
- Auto-decomposition of stories that touch multiple subsystems
- Model escalation on plan regen failure (separate story)

## Notes

- The corroboration check is a policy change in plan review synthesis, not in
  individual reviewer behavior. Reviewers still independently assign severity.
  The coordinator decides what to do with the aggregate.
- The finding registry already tracks recurrence across regen attempts. The
  corroboration check adds a second dimension: cross-reviewer agreement within
  a single regen attempt.
- "Same family" matching should use the same structural anchors the existing
  finding classifier uses (file path, snake_ident, dotted_path overlap).
- The plan review prompt could benefit from sharper definitions of P1 vs
  P1-impl, but prompt changes are best validated separately — don't couple
  them to the synthesis policy change.
