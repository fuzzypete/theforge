# Backlog

Non-blocking findings, follow-ups, and future work. Items here came from
review cycles and should not be lost. Promote to a spec when ready to ship.

---

## P2: plan-review parser should consume criteria_coverage
`parse_plan_review_output()` in `review.py` drops the `criteria_coverage`
map that `build_plan_review_prompt()` now asks for. The AC-by-AC map
improves reviewer behavior but is not part of the mechanical boundary or
audit trail yet.
Source: prompt refactor review (2026-03-17)

## P2: dry-run path missing handoff_file param
`cli.py#L137` calls `build_dev_prompt()` without passing
`config.validation.handoff_file`, so exit-code-mode dry runs show the
wrong prompt (includes handoff instructions when they shouldn't).
Source: prompt refactor review (2026-03-17)

## P2: review schema doesn't enforce line on P1
The review prompt says P1 must cite file+line+failure, but
`validate_review_yaml()` in `schemas.py` still accepts P1 with no line
number. Prompt-only precision, not schema-enforced.
Source: prompt refactor review (2026-03-17)

## parallel-specs
Run independent specs concurrently in a sprint (isolated worktrees already support it).
Blocked by: API rate limits, merge ordering. Needs design — lower priority than hardening.
