# Forge finding lifecycle: triage intake state and release-aware reporting

## What

Overhaul how forge findings move through their lifecycle: apply intake labels
on creation, retire the legacy disposition labels, add release-readiness
reporting, block release cut on untriaged findings, introduce a multi-agent
triage workflow, and require a rationale on every punt.

## Why

The old five-bucket disposition model described what a finding *is* but never
forced a triage decision. Findings accumulated forever.

## Acceptance Criteria

- Forge findings are created with `forge-finding` + `needs-triage` labels,
  and triage is represented by removing `needs-triage` once the finding has
  been reviewed; the old `accepted-risk`, `release-risk`, and
  `release-blocker` labels are retired from the label schema.
- The release-readiness report lists counts for open `needs-triage`
  forge-findings grouped by milestone, writes a JSON report under the audit
  trail, and emits a prompt summary for human review.
- Release cut fails (exit non-zero) when the target milestone contains any
  finding with the `needs-triage` label; the release workflow phase blocks
  until triage is complete.
- A multi-agent triage workflow runs during the triage phase: a proposer
  agent suggests disposition, a reviewer agent critiques the proposal, and
  a resolver agent writes the final label. Each agent exchange is logged to
  the audit record and attached to the finding.
- Every punt (close) requires a concrete rationale code from a fixed schema:
  `low-impact`, `stale`, `cosmetic`, `invalid`, `not-worth-scheduling`. A
  pre-close hook blocks the close if no rationale label is present.
- The forge.yaml config exposes a `triage` section with keys for the
  proposer/reviewer/resolver model profiles, release-report thresholds, and
  per-label retention policy; config validation rejects unknown keys.
- The sprint planning phase reads the latest release-readiness report and
  refuses to include any finding that still carries `needs-triage`; it emits
  a log line and an audit entry explaining each skipped issue.
