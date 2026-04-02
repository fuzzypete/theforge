---
name: "Carry unresolved P1s forward verbatim into next dev prompt"
slug: unresolved-p1-carry-forward
github_issue: 255
pytest_target: tests/
---

# Carry Unresolved P1s Forward Verbatim Into Next Dev Prompt

## Problem

When a review cycle ends with unresolved P1 findings, the next dev pass receives
the full review result but no explicit call-out that specific findings survived
from prior cycles. The dev agent sees the current cycle's review as if every
finding were fresh. In practice, reviewers re-describe the same invariant
differently each cycle (because they re-read code independently), so the dev
agent sees a slightly different description and may not recognise it as the same
open issue.

A real example: a blood-pressure anchor bug was described as "max anchor skips
lagging component" in cycle 2 and "single present component anchor still unsafe"
in cycle 3. The underlying invariant was identical but the wording shifted
enough that the dev prompt gave no signal of recurrence. The bug was never
closed.

## Goal

Before each dev pass on cycle 2+, the coordinator injects a dedicated section
into the dev prompt listing every P1 finding that was unresolved at the end of
the previous review cycle, using the exact description text from the finding
registry. This makes the continuity explicit without requiring reviewers to use
identical wording across cycles.

## Acceptance Criteria

- On dev passes for cycle 2 and later, if the finding registry contains any P1
  findings with disposition `unresolved` or `regression` from prior cycles, the
  dev prompt includes a section headed "Still-open findings from prior review"
- Each entry in that section contains: the finding's file, line (if known), and
  the verbatim description from the registry (the text recorded when the finding
  first appeared, not re-summarised)
- The section is absent on cycle-1 dev passes and when no prior P1s are
  unresolved
- The injected section appears before the current cycle's review feedback in the
  prompt, so it is seen as higher-priority context
- The audit YAML records which finding IDs were injected for each dev pass
- Existing tests pass; new tests cover the injection path for cycle-2+ prompts

## Out of Scope

- Changing how findings are matched or classified across cycles
- Injecting P2 findings (P1s only — P2s are advisory and already present in the
  review output)
- Modifying reviewer prompts or reviewer behavior
- LLM-based finding deduplication or semantic matching

## Notes

- The finding registry already tracks `cycle_first_seen`, `cycle_last_seen`,
  and `disposition` per finding — the injection logic can read directly from
  `state.finding_registry`.
- The dev prompt is built in `src/theforge/task.py`; coordinator state is
  threaded through from `coordinator.py`.
- "Verbatim description" means the `description` field as stored in
  `FindingRecord` — the text from the first cycle in which the finding appeared.
