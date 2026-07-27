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

## The reviewer record is reviewer-only

`state.review_results`, `state.review_cycle_metadata`, and
`state.review_iteration_telemetry` carry an implicit contract: **an entry in them
means a reviewer pool ran and produced a verdict.** They are parallel lists, read
by index and by tail, and several consumers depend on that contract without
checking it:

- `model_profiles_bridge._extract_reviewers` pairs metadata with telemetry *by
  index* to attribute findings and cost per model, and persists the result to
  `model_profiles.yaml`, where adaptive routing reads it.
- `audit.review_cycles_total` feeds `adaptive_iterations._extract_review_used`,
  which percentiles it to set a future `max_review_cycles`.
- The persistent-P1 lookback reads `review_results[-2]` expecting the previous
  *reviewer* verdict; dev-model escalation hangs off that comparison.
- `audit_render.build_reviews` zips metadata against results to render cycles.

A coordinator-raised finding — a gate failure or a hard convention violation
observed in VALIDATE — is **not** a reviewer verdict, however similar it looks.
Record it in its own channel (`state.validate_blocks`, via
`validate_phase.record_validate_block`), never in the three lists above.

This is not hypothetical: #1981 first shipped the return path by writing a
synthetic REQUEST_CHANGES into the reviewer record, which invented a model named
`coordinator` holding the real reviewer's cost, zeroed the reviewer that actually
ran, taught the router that gate failures mean reviewers need more cycles, and
disabled persistent-P1 detection for the following cycle. Mirroring the review
loop-back's *control flow* was right; mirroring its *data structures* was the
defect. Sharing the review-cycle **budget** is likewise fine — it is the same
currency — but the budget counters are split for the same reason
(`reviewer_cycles_run` for reviewer demand, `review_cycles_spent` for budget
consumed), so a coordinator-opened cycle never reads as reviewer demand.

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
