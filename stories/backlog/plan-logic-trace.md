---
name: "Require logic traces in plans for algorithmic stories"
slug: plan-logic-trace
github_issue: 263
pytest_target: tests/
---

# Require Logic Traces in Plans for Algorithmic Stories

## Problem

Plans describe algorithms in prose. Prose is ambiguous — "groups findings by
anchor overlap" can mean per-finding corroboration or connected-component
corroboration, and both readings are internally consistent. The plan agent
picks one interpretation, writes coherent prose around it, and plan reviewers
approve because the prose reads well. The ambiguity only surfaces when a code
reviewer runs the algorithm against a concrete input.

The plan-review-corroboration story demonstrated this: the plan's grouping
description naturally led to a component-level implementation that passed
every AC's wording but violated the intent. A single worked example in the
plan — "given these inputs, here is the expected output" — would have forced
the plan agent to commit to a concrete interpretation and given plan reviewers
something to falsify.

## Goal

When preflight classifies a story as algorithmic or policy work, the plan
prompt requires the plan agent to include a logic trace section: concrete
input/output examples that exercise the proposed algorithm, including at least
one edge case. This gives plan reviewers a testable artifact instead of prose
to evaluate.

## Acceptance Criteria

- When the story is flagged as algorithmic (using the same signal as the
  adversarial-plan-review story), build_plan_prompt injects an instruction
  requiring a "Logic Trace" section in the plan output
- The logic trace instruction asks for 2-3 concrete scenarios: one happy path,
  one edge case, and one case that a plausible-but-wrong implementation would
  get wrong
- The plan review prompt instructs reviewers to verify the logic trace against
  the proposed algorithm and the story's ACs
- Plans for non-algorithmic stories do not require logic traces
- The plan schema (if structured) accepts an optional logic_trace section
  without breaking existing plan parsing
- All existing tests pass

## Non-goals

- Enforcing that the dev agent's implementation matches the trace (that's the
  code reviewer's job)
- Auto-generating traces from ACs
- Requiring traces for non-algorithmic stories
