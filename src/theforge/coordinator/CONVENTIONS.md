# Coordinator subsystem guidance

## Purpose

The coordinator owns deterministic process control for TheForge. It advances work
through the state machine, prepares workspaces, invokes runners, validates phase
outputs, records audit data, and decides whether a run proceeds, retries,
escalates, or completes.

## Invariants

- Coordinator control flow is pure Python. Do not introduce LLM-driven routing,
  retry, escalation, or state-transition decisions here.
- State transitions must remain explicit and auditable. When phase inputs or
  outputs change, preserve or extend audit visibility so reviewers can inspect
  the real values that drove coordinator behavior.
- Schema and validation boundaries are load-bearing. Do not weaken checks just to
  accept malformed runner output or make tests pass.
- Keep coordinator modules focused by phase or concern. Extract helpers instead
  of growing `engine.py` or phase modules into mixed-responsibility monoliths.
- Workspace and gate handling must be conservative: prefer failing closed over
  silently continuing with an invalid repository, branch, or validation result.

## Adaptive routing symmetry invariant

Adaptive routing (`assignment.py`) learns from run history to move a story's dev
tier and reorder reviewers. Every adaptive mechanism that ratchets in one
direction — promote, escalate, deprioritize, exclude — **must** have a
corresponding mechanism that ratchets the opposite way — demote, de-risk,
reprioritize, re-include — under defined conditions. Each direction needs:

- **Explicit trigger conditions** — a deterministic, documented rule for when it
  fires (e.g. "2+ ESCALATE outcomes in the last 10 matching records" ↔ "clean
  plan-review APPROVE on a medium story in one cycle").
- **Audit attribution** — the routing decision records which symmetric path fired
  (or was checked and did not) so the operator can trace it in production data,
  not just in test fixtures. The dev role carries a single `routing_rationale`
  field reporting `stayed_at_preflight_tier`, `promoted_by <mechanism>`, or
  `demoted_by <mechanism>`.
- **Tests for both directions** — a promotion path and its inverse are each
  exercised by at least one test.

Static score-band routing (the `PHASE_TIER` tier table) and hard tier floors are
**exempt** — they re-derive from the current story's complexity, not from
accumulated history, so they are not one-way ratchets.

Enforcement is mechanical, not advisory: `ROUTING_SYMMETRY_REGISTRY` in
`assignment.py` catalogues each adaptive promotion path and its inverse (or a
named open follow-up when the inverse has not yet landed), and
`tests/test_routing_symmetry_invariant.py` fails when a promotion path has
neither a landed+tested demotion nor a catalogued follow-up. Adding a new
promotion mechanism therefore requires either landing its inverse or registering
the asymmetry as a tracked follow-up before the enforcement test passes. See
`docs/routing-symmetry-followups.md` for the current open asymmetries and
ADR-0006 for the adaptive-router trust boundary.

## Context

- `engine.py` is the orchestration center for the run lifecycle and state
  machine.
- `preflight.py` and `preflight_flow.py` classify readiness and complexity before
  downstream work begins; these decisions affect model assignment and whether
  planning runs.
- `plan_flow.py`, `dev_phase.py`, `validate_phase.py`, and `review_phase.py`
  implement the major execution phases.
- `audit.py`, `audit_render.py`, and `review_context.py` shape the traceability
  story for runs; changes that hide intermediate values make sprint failures much
  harder to diagnose.
- `workspace.py`, `run_setup.py`, and related helpers manage repository state and
  should be treated as safety-critical because mistakes can invalidate the whole
  run.
- If a change feels like prompt construction or output parsing rather than
  process control, it probably belongs in `task/` or `runners/`, not here.
