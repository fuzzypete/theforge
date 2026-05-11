# ADR-0003: Intake State Authority and Label Reconciliation

- **Status:** Proposed
- **Date:** 2026-05-11 (proposed)
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** v0.10.0 (#1550 is the v0.10 instance), v0.11.x (intake-readiness substrate emission consequences)
- **Related issues:** #1550 (sprint re-exec discards in-memory intake-remediation state)
- **Related ADRs:** ADR-0001 (intake readiness workflow — defines the operator/product lifecycle but not the state-authority model), ADR-0002 (audit substrate — defines what's authoritative in run history)

---

## Context

ADR-0001 establishes intake readiness as a deliberate pre-sprint operator workflow: `forge shape` and `forge groom` repair issue bodies, the sprint runner refuses non-readiness-clean issues at dispatch, and labels (`needs-grooming`, `ready`, etc.) participate in that gate.

What ADR-0001 does not define is **who owns label state and on what synchronicity contract**. Today two parties write to the same label namespace:

1. **The `.github/workflows/shape-check.yml` GitHub Actions workflow** — fires on issue create/edit, evaluates the body against shape-check heuristics, adds/removes `needs-grooming` accordingly. This is the *passive ambient labeler*: it provides backlog visibility, runs outside any sprint context, and reflects "is this issue in shape today?"
2. **The sprint runner's intake-remediation pass** — when grooming auto-fix is enabled, the runner edits issue bodies to fix shape-gate violations and locally re-evaluates the gate. This is the *active in-sprint remediator*: it runs inside a specific sprint invocation and answers "is this issue dispatch-eligible right now?"

Both write the same label namespace. Both read it as if it were authoritative. Neither coordinates with the other.

The race surface manifested in sprint `a27d1367454a` on 2026-05-11 (#1550). The sprint remediated three issues' bodies (07:10:31, 07:10:57, 07:11:21 PT), then pulled new source and re-execed at 07:11:27 PT. The re-exec discarded the in-memory "remediated, dispatch" state and re-queried GitHub for label state. The GH Actions workflow that reconciles `needs-grooming` against the edited body is asynchronous: for two of the three issues it had completed the unlabel before the re-exec; for the third, whose body edit happened last, it had not. The re-exec read the stale label, refused dispatch, and silently dropped one substantively-eligible issue. The sprint shipped 2 of 3 stories. Operator-visible explanation: none beyond "needs-grooming label present."

The same race surface will recur for every async hook that writes to gate-relevant state — not just shape-check. Without an authority contract, every future cross-system labeler is a new instance of the same bug.

## Decision

### Headline invariant

> **Labels are operator-visible state hints. Local shape-gate verdicts are execution authority within a sprint run.**

GitHub Actions workflows that reconcile labels against body content remain useful for ambient backlog visibility — operators browsing the issue tracker see correct labels because the workflow runs on every body edit. They are not authoritative gates inside a sprint invocation.

### State-authority clauses

1. **Pre-sprint (ambient):** the GitHub Actions shape-check workflow is authoritative for label state. Operators inspecting the backlog see labels reflecting current body compliance. `forge shape` and `forge groom` are the intentional pre-sprint repair commands; they can write labels directly when their local gate result is known.

2. **At sprint start (seeding):** the sprint runner reads labels as candidate state — input to which issues are eligible for the run. This read is advisory; it determines what enters the runner's evaluation queue, not what dispatches.

3. **Within a sprint (execution authority):** once the runner has locally evaluated an issue's shape gate and recorded a verdict (clean, remediated, or refused), that local verdict is authoritative for the remainder of the sprint run. Subsequent reads of GitHub label state by the same run for the same issue must not override the local verdict.

4. **Across re-exec and resume:** sprint runs must persist local shape-gate verdicts and intake-remediation outcomes to run state (a sprint-scoped file or substrate record) before any external action that could trigger a code re-load. Re-exec entry points reconstruct from persisted run state, not from re-querying GitHub. Resume entry points (`forge sprint --resume`) reconstruct identically.

5. **Body-change invalidation:** if an issue body changes after a local verdict has been recorded, the local verdict is invalidated and must be recomputed. The runner detects this by recording the body content hash (or `updated_at` timestamp) alongside the verdict; on re-read, mismatch triggers recomputation. Body changes performed by the runner's own intake-remediation pass do not invalidate — the remediation pass writes the new verdict atomically with the new body.

6. **Background reconciliation is non-retroactive:** GitHub Actions workflows may continue to reconcile labels in the background. Their writes do not retroactively invalidate any local verdict recorded by an in-flight sprint run. After the sprint completes, the ambient label state may differ from the run-time verdict for a short window until reconciliation converges; this is expected and acceptable.

### Audit-substrate emission (consequence of ADR-0002)

Local shape-gate verdicts and intake-remediation outcomes are events that affect dispatch decisions. Per ADR-0002's clause on authoritative records, they must emit to the audit substrate with `provenance='native'`. Each emission includes: issue number, verdict (clean / remediated / refused), reason code (e.g., `bug_missing_diagnosis`, `needs_grooming_label`), body hash at evaluation time, and the trigger (`pre_dispatch` / `re_exec_replay` / `resume_replay`). This makes the "why was this issue dropped?" question answerable after the fact — closing the operator-trust gap that #1550 surfaced.

## Consequences

- The race in #1550 is closed by clause 4: re-exec reads persisted run state, not GitHub labels. The fix surface is the sprint runner's re-exec path, not the GH Actions workflow timing or any added wait/poll.
- `forge shape` and `forge groom` gain explicit license to write labels directly when their local gate verdict is known. This was implicit in ADR-0001's command contracts; this ADR makes it official.
- The GH Actions `shape-check.yml` workflow remains useful for the operator's backlog-browsing experience but is no longer load-bearing for sprint dispatch correctness. If it is buggy or slow, it cannot break a sprint.
- Future contributors adding new label-writing hooks must declare which authority clause their hook serves (ambient labeler? in-sprint authority?) and how it interacts with existing writers. Hooks that violate clause 3 (override in-sprint local verdicts) must be rejected at review.
- Intake-remediation auditability improves materially: every dispatch decision (clean, remediated, refused, dropped) emits an audit record with cause. The "silent drop" failure mode that surfaced in #1550 becomes impossible to repeat silently — even if a future bug drops an issue, the audit record explains why.

## Out of scope

- The shape-check workflow's content-detection heuristics. ADR-0001 owns that contract. This ADR is purely about state authority and write coordination.
- Cross-repo label authority. TheForge currently runs against a single repo; multi-repo label coordination is a v0.12+ concern if it arises.
- Synchronous label-write semantics inside `forge shape` / `forge groom`. Implementation decision for those commands; this ADR only mandates that local verdicts get persisted and emitted, not how the commands choose to surface them as labels.

## References

- ADR-0001: Intake Readiness Workflow (`docs/adr/0001-intake-readiness-workflow.md`)
- ADR-0002: Audit Substrate and Queryable Run History (`docs/adr/0002-audit-substrate-and-queryable-run-history.md`)
- #1550: Sprint re-exec discards in-memory intake-remediation state and re-reads stale GitHub label data
- `.github/workflows/shape-check.yml`: the ambient labeler this ADR contextualizes
