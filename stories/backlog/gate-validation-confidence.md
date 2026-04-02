---
name: "Gate validation confidence: PASS_PARTIAL for uncovered file types"
slug: gate-validation-confidence
github_issue: 254
pytest_target: tests/
---

# Gate Validation Confidence: PASS_PARTIAL for Uncovered File Types

## Problem

The gate phase runs a single configured command and records PASS or FAIL based
on its exit code. When a story touches file types the configured gate cannot
exercise (e.g. Swift files in a project whose gate is `pytest tests/`), the
gate still records PASS. The coordinator has no signal that the riskiest changed
surface was never validated.

This caused a real failure: a Swift sync bug survived three review cycles in a
project whose gate only ran Python tests. The gate said PASS throughout; the
coordinator treated the run as fully validated.

## Goal

The gate phase detects when changed files include types that the configured gate
command is unlikely to exercise, and downgrades the result to `PASS_PARTIAL`
rather than `PASS`. The coordinator surfaces this in logs and the audit trail.
`PASS_PARTIAL` does not block review — it is an honesty signal, not a new
failure mode.

## Acceptance Criteria

- A new gate outcome value `PASS_PARTIAL` is valid alongside `PASS`, `FAIL`,
  and `BLOCKED`
- After a PASS, the coordinator checks whether any changed files have extensions
  listed in `validation.unverified_extensions` in forge.yaml; if so, the outcome
  is downgraded to `PASS_PARTIAL`
- `PASS_PARTIAL` proceeds to the review phase identically to `PASS`; it does
  not trigger a retry or escalation
- The forge log line for `PASS_PARTIAL` includes which extensions were
  unverified, e.g. `⚠ PASS_PARTIAL — gate did not exercise: .swift, .kt`
- The audit YAML records `gate_decision: PASS_PARTIAL` and an
  `unverified_extensions` list
- The review prompt receives a note when the gate was `PASS_PARTIAL`, naming
  the unverified extensions, so reviewers can weight platform-specific findings
  accordingly
- When `validation.unverified_extensions` is absent or empty, behavior is
  identical to today (no change for existing projects)
- All existing tests pass; new tests cover the PASS→PASS_PARTIAL downgrade path

## Out of Scope

- Automatic detection of which gate commands cover which languages — the
  operator declares `unverified_extensions` explicitly
- Blocking the pipeline on `PASS_PARTIAL`
- Per-extension gate commands or multi-gate orchestration

## Notes

- `unverified_extensions` is a simple list in forge.yaml, e.g.
  `[".swift", ".kt", ".m"]`. The operator knows their stack; TheForge does not
  need to infer it.
- Changed file extensions are read from `git diff --name-only` against the
  worktree's base commit — the same mechanism `finding_classifier.py` already
  uses.
- The gate phase lives in `src/theforge/coordinator/gate.py`; the audit write
  is in the same module.
