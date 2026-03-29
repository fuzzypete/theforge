---
name: "Auto-downgrade reviewer findings contradicted by gate"
slug: gate-verified-finding-downgrade
pytest_target: tests/
---

# Gate-Verified Finding Downgrade

## Problem

Reviewers can claim "tests fail" or "build broken" as P1 findings when the gate
command has already passed in VALIDATE. This happened in an HDP sprint: deepseek
reported 435 test failures with "document is not defined" — a jsdom error that
doesn't reproduce. The gate command passed (84 tests, 0 failures), but the phantom
P1 blocked approval and prevented `on_approve: merge` from firing.

The coordinator already runs the gate command and knows whether it passed. But it
doesn't cross-reference that evidence against reviewer claims about test/build
failures. A single reviewer's false P1 about tests is enough to block the pipeline,
even when the coordinator has mechanical proof the claim is wrong.

## Solution

After finding classification, the coordinator checks whether the most recent gate
decision was PASS. If so, any P1 finding whose description matches gate-verifiable
patterns (test failures, build errors, lint failures) is downgraded to P2 with a
new disposition `gate_contradicted`. The finding is preserved in the audit trail
but no longer blocks approval.

This applies only to mechanically verifiable claims — subjective findings (design
issues, spec mismatches, security concerns) are never downgraded.

## Acceptance criteria

- P1 findings claiming test/build/lint failures are downgraded to P2 when the last gate decision was PASS
- Downgraded findings receive disposition `gate_contradicted`
- Downgrade only applies to findings matching gate-verifiable patterns, not subjective claims
- The downgrade is logged with the original severity, the gate decision, and the matched pattern
- `gate_contradicted` findings appear in the audit trail at their original severity with the downgrade noted
- `has_blocking_p1` treats `gate_contradicted` as non-blocking
- Pattern matching is keyword-based on finding descriptions, not LLM-interpreted
- If gate decision is FAIL or absent, no downgrade occurs (safe default)
- False negatives (missing a gate-verifiable claim) are acceptable — the P1 stays blocking
