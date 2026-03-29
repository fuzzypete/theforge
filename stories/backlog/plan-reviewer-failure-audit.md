---
name: "bug: plan reviewer agent failure (empty output) silently ignored in audit"
slug: plan-reviewer-failure-audit
---

# bug: plan reviewer agent failure (empty output) silently ignored in audit

## Observed

A plan reviewer returned empty output due to a usage limit. The coordinator excluded it from the verdict and approved on remaining reviewers alone. No warning appeared in the audit log.

## Expected

Plan reviewer failures are recorded in the audit log. If successful reviewers drop below a minimum, the run escalates rather than silently approving.
