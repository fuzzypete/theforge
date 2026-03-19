---
name: "Spec validation — catch requirement bugs and scope issues before planning"
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

**Scope bloat is equally expensive.** A spec that covers 9 acceptance criteria
spanning iOS native, React UI, and watchOS is not one story — it's a sprint.
When the dev agent receives an oversized spec, it either produces a superficial
implementation that fails review, or burns its entire timeout on 30% of the
work and leaves the rest unfinished. The plan phase catches some of this
(complexity=large gets more time) but does not flag that the spec itself should
be split.

This happened with `redesign-spv-workout`: persistent header, state-swap
presentation, background rest timer, weight field/rep calculator, quick-adjust
suggestions, and watch app timer — all in one spec. The dev agent got confused
about what to prioritize and the scope was clearly 4-5 separate stories.

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
   - **Scope assessment**: whether the spec covers multiple independent
     functional areas, distinct technology domains, or too many unrelated ACs
   - **Split recommendation**: when scope is too large, suggest concrete story
     boundaries with dependency ordering
3. Validation output is a structured verdict: PASS or WARN with findings
4. Scope findings use a separate category (e.g., `scope` vs `requirement`) so
   they can be distinguished from requirement quality issues
5. On WARN, findings are logged clearly and the human is not blocked — the run
   continues to PLAN. This is advisory, not a gate.
6. Findings are recorded in the audit log under `spec_validation`
7. Validation is fast and cheap — use a small/fast model, not opus. Derive
   the model from the first available configured profile (dev or review_pool)
   rather than hardcoding a CLI/model pair — do not add a new forge.yaml key
8. No forge.yaml changes required to enable — spec validation runs whenever
   PLAN would actually run (same `should_plan` condition: plan enabled AND
   preflight complexity is medium or large). Small/trivial specs that skip
   PLAN also skip validation.
9. Validation is skipped when `--plan` is injected (spec already past planning)

### Scope Assessment Heuristics

The scope check should consider:

- **AC count**: 7+ acceptance criteria is a signal (not a hard rule)
- **Technology domains**: specs touching 2+ distinct tech stacks (e.g., iOS
  native + React + watchOS) should almost always be split
- **Independent subsystems**: ACs that have no dependency on each other and
  touch different parts of the codebase are separate stories
- **The "one PR" test**: could a single developer implement and review this
  as one coherent pull request? If not, it's too big.

Split suggestions should identify:
- Natural story boundaries based on functional cohesion
- Dependency ordering between the suggested stories
- Which ACs belong to which suggested story

## Acceptance Criteria

- [ ] Spec validation runs after PREFLIGHT, before PLAN
- [ ] Validation produces PASS or WARN verdict
- [ ] On WARN, findings are logged to the console before PLAN starts
- [ ] On WARN, the run continues — validation never blocks execution
- [ ] Validation findings are recorded in `forge_audit.yaml` under
      `spec_validation.findings`
- [ ] Validation is skipped when `--plan` flag is used
- [ ] Validation uses a fast/cheap model (sonnet or equivalent), not opus
- [ ] Scope findings are categorized separately from requirement findings
      (e.g., `category: scope` vs `category: requirement`)
- [ ] When scope is flagged, the finding includes concrete split suggestions
      with story names and AC assignments
- [ ] Existing tests pass
- [ ] New tests cover: PASS verdict continues to PLAN, WARN verdict logs and
      continues, skipped on --plan injection, scope WARN with split suggestion

## Out of Scope

- Blocking execution on validation failure — advisory only, always continues
- Validating forge.yaml structure — that's config parsing, already done
- Linting spec prose style or length
- Auto-fixing spec contradictions — surface them to the human, that's enough
- Validating specs that don't use the PLAN phase
- Auto-splitting specs — suggest splits, don't execute them
