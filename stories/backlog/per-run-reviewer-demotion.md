---
name: "Per-run reviewer demotion on repeated parse failures"
slug: per-run-reviewer-demotion
github_issue: 256
pytest_target: tests/
---

# Per-Run Reviewer Demotion on Repeated Parse Failures

## Problem

When a reviewer in the pool returns empty or malformed output, the current merge
logic excludes that single result and continues. If the same reviewer fails
repeatedly across multiple cycles of the same run, it is called again each cycle
anyway, burning time and compute without contributing signal.

In a real run, gemini-reviewer returned empty/malformed output in both cycle 2
and cycle 3. It was invoked a third time (cycle 3) despite already failing twice,
adding wall-clock latency with no benefit.

## Goal

Within a single run, if a reviewer fails to produce parseable output a
configurable number of times (default: 2), it is excluded from all remaining
cycles of that run. This is a runtime efficiency optimization — it does not
affect reviewer configuration, persistent reputation, or other runs.

## Acceptance Criteria

- The coordinator tracks per-reviewer parse failure counts within a run
- When a reviewer's in-run failure count reaches `review.demotion_threshold`
  (default 2, configurable in forge.yaml), it is excluded from subsequent review
  cycles for that run
- A log line is emitted when a reviewer is demoted, naming the reviewer and the
  failure count, e.g. `⚠ gemini-reviewer demoted after 2 parse failures this run`
- Demotion is scoped to the current run only — it does not persist to the audit
  log as a reviewer quality signal and does not affect future runs
- If demotion would leave zero reviewers for a cycle, the run escalates rather
  than proceeding with an empty pool
- The audit YAML records which reviewers were demoted and at which cycle
- `review.demotion_threshold: 0` disables demotion (today's behavior)
- All existing tests pass; new tests cover the demotion path and the
  zero-reviewer escalation edge case

## Out of Scope

- Persistent reviewer reputation across runs
- Automatic reviewer pool reconfiguration in forge.yaml
- Changing the parse-error exclusion logic for individual cycle results (already
  exists in `review.py`)

## Notes

- Per-reviewer failure state lives naturally on `CoordinatorState` for the
  duration of the run.
- The reviewer pool is called from the review phase; demotion filtering happens
  before the pool invocation each cycle.
