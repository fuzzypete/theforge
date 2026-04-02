---
name: Adaptive assignment docs and backlog alignment
slug: adaptive-assignment-docs-alignment
depends_on: [adaptive-model-assignment]
---

# Adaptive Assignment Docs and Backlog Alignment

## Problem

Adaptive assignment is currently presented as several semi-hidden behaviors:
model routing, reviewer-count selection, budget downgrading, and
escalation-memory promotion. Even if the code is correct, the backlog and docs
state make it hard to tell what is actually shipped, what is hardening work,
and what is still future-facing.

This creates audit churn and weakens dogfooding confidence.

## Acceptance criteria

- Move the main adaptive-assignment story out of backlog if the shipped status
  is confirmed
- Reconcile, close, or explicitly supersede stale follow-up story state for
  override protection
- Add one short doc block that explains how adaptive assignment works today as
  one coherent capability
- The doc block clearly covers:
  - model routing
  - reviewer-count / reviewer-pool selection
  - budget enforcement / downgrading
  - escalation-memory promotion
- Update epic/backlog references so future audits do not imply the feature is
  still entirely unshipped
