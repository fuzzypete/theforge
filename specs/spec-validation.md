---
name: "Spec validation — catch requirement bugs before planning starts"
slug: spec-validation
pytest_target: tests/
---

# Spec Validation

## Problem

Requirement bugs are the most expensive bugs. When a spec has an internal
contradiction — a requirement that conflicts with an acceptance criterion, an
ambiguous edge case, or acceptance criteria that describe internal structure
instead of observable behaviour — the plan agent cannot produce a correct plan.
The plan reviewer correctly rejects it. The cycle repeats until `max_plan_regen_attempts`
is exhausted, burning expensive model time on an unsolvable problem.

This happened concretely with `trace-capture`: requirement 4 said "no overwrites
across multiple passes" but the acceptance criteria said `plan.txt` (a fixed name
that would be overwritten on plan regen). Three Opus plan cycles, $1.85, nothing
shipped. One clarifying sentence in the spec would have prevented all of it.

The fix is to validate specs **before PLAN runs** — not as a gate that blocks
execution, but as a fast, cheap check that surfaces issues early so the human
can fix the spec rather than burn planning cycles.

## Requirements

1. Before the PLAN phase runs, validate the spec with a lightweight model call
2. Validation checks for:
   - Internal contradictions between Requirements and Acceptance Criteria
   - Acceptance criteria that describe implementation internals (function names,
     dataclass shapes, file-internal steps) rather than observable behaviour
   - Requirements that are ambiguous or cannot be satisfied simultaneously
   - Acceptance criteria with no corresponding requirement (orphaned criteria)
3. Validation output is a structured verdict: PASS or WARN with findings
4. On WARN, findings are logged clearly and the human is not blocked — the run
   continues to PLAN. This is advisory, not a gate.
5. Findings are recorded in the audit log under `spec_validation`
6. Validation is fast and cheap — use a small/fast model, not opus
7. No forge.yaml changes required to enable — spec validation runs whenever
   the PLAN phase is enabled
8. Validation is skipped when `--plan` is injected (spec already past planning)

## Acceptance Criteria

- [ ] Spec validation runs after PREFLIGHT, before PLAN
- [ ] Validation produces PASS or WARN verdict
- [ ] On WARN, findings are logged to the console before PLAN starts
- [ ] On WARN, the run continues — validation never blocks execution
- [ ] Validation findings are recorded in `forge_audit.yaml` under
      `spec_validation.findings`
- [ ] Validation is skipped when `--plan` flag is used
- [ ] Validation uses a fast/cheap model (sonnet or equivalent), not opus
- [ ] Existing tests pass
- [ ] New tests cover: PASS verdict continues to PLAN, WARN verdict logs and
      continues, skipped on --plan injection

## Out of Scope

- Blocking execution on validation failure — advisory only, always continues
- Validating forge.yaml structure — that's config parsing, already done
- Linting spec prose style or length
- Auto-fixing spec contradictions — surface them to the human, that's enough
- Validating specs that don't use the PLAN phase
