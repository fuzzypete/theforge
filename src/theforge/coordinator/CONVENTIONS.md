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
