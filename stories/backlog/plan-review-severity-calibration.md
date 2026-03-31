---
name: "Plan review distinguishes blocking architectural flaws from implementation-detail concerns"
slug: plan-review-severity-calibration
pytest_target: tests/test_plan_review.py
---

# Plan review distinguishes blocking architectural flaws from implementation-detail concerns

## Problem

Plan review uses the same severity model as code review. Any P1 finding forces
REJECT. But at plan level, many "P1s" are implementation-detail concerns -- which
counter is monotonic, how persistence overwrites keys, whether a guard clause
handles a specific edge case -- that the dev agent should discover and resolve
during implementation. The plan review prompt says "approve unless there's a
concrete blocker" but the merge logic makes any P1 a hard REJECT. The mechanical
contract overrides the social prompt.

This creates a frustrating loop: reviewers flag real observations as P1 (because
the severity definitions only offer P1 and P2), the merge logic dutifully rejects,
and the plan gets regenerated to pre-solve implementation details that the dev
agent would have handled naturally. Each regen cycle costs money and often
introduces new issues without improving the architectural quality of the plan.

## Expected behavior

Plan review should distinguish architectural/structural flaws (the approach
itself is wrong, an API doesn't exist, callers are unaccounted for) from
implementation-detail concerns (the approach is right but the plan doesn't
pre-solve every edge case). Only architectural flaws should block approval.

Implementation-detail findings should be recorded and passed to the dev agent
as advisory context rather than triggering plan rejection. The dev agent
receives these findings alongside the approved plan, giving it awareness of
known concerns without forcing the plan through a regen cycle.

The code review pipeline already has a precedent for this pattern --
disposition-gated P1 handling where not every P1 is treated as a hard blocker.

## Acceptance criteria

- Plan review severity definitions include a category for
  implementation-detail concerns that are distinct from architectural blockers
- The plan review merge logic only rejects on architectural/structural
  findings, not on implementation-detail findings
- Implementation-detail findings from plan review are preserved and forwarded
  to the dev agent as advisory context alongside the approved plan
- Plans that contain only implementation-detail findings (no architectural
  blockers) receive an APPROVE verdict from the merge logic
- Plans that contain at least one architectural blocker receive a REJECT
  verdict regardless of how many implementation-detail findings exist
- The plan review prompt's severity definitions align with the merge logic's
  blocking rules -- no conflict between what the prompt asks for and what the
  merge enforces
- All existing tests pass
- New tests cover: merge logic for each severity category, advisory forwarding
  to dev context, mixed-severity scenarios

## Notes

- The current severity tiers are P0 (impossible), P1 (must fix), P2
  (improvement). The tension is that P1 covers both "wrong API that will break
  at runtime" and "plan doesn't specify how to handle key collisions" -- these
  are fundamentally different failure modes at the plan level.
- The merge function in `review.py` (`merge_plan_review_results`) has the line
  `has_p0_or_p1 = any(f.severity in ("P0", "P1") for f in all_findings)` which
  is where the hard REJECT happens.
- The plan review prompt in `plan_prompts.py` defines P1 as "real gap that will
  probably cause dev to fail" -- this definition is reasonable for code review
  but too broad for plan review where "dev can work it out" covers a much wider
  range of concerns.
- Advisory findings forwarded to the dev agent could use the same injection
  point where plan P2 findings are already passed through (if that exists), or
  could be added to the plan section of the dev prompt.
