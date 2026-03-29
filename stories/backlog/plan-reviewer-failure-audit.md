---
name: "bug: plan reviewer agent failure (empty output) silently ignored in audit"
slug: plan-reviewer-failure-audit
---

# bug: plan reviewer agent failure (empty output) silently ignored in audit

## Observed

During the `coordinator-relocate-modules` sprint, Codex hit its usage limit and returned empty output. The coordinator excluded Codex from the verdict and continued to APPROVE based on DeepSeek alone. No warning appeared in the audit log — the Codex failure is completely invisible unless you read the raw reviewer log file directly. (GH issue #183)

## Expected

When a plan reviewer returns empty or unparseable output, the failure is recorded in the audit log. If the number of successful plan reviewers drops below a minimum threshold, the run escalates rather than silently approving on reduced coverage.
