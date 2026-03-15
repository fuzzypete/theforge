# Epic: Decision Surface — Human-in-the-Loop Decision Gates

## Vision

Wherever the coordinator encounters a branching decision, it surfaces a
crisp choice to the human via ntfy push notification with tappable options.
The human taps, forge acts. No wall-of-text escalation, no proxy
interpretation needed.

The coordinator becomes a system that **never silently picks a default on
something ambiguous** — it either knows the right answer (and acts) or
asks (and waits).

## Decision Types (full scope)

| Type | Trigger | Options | Priority |
|---|---|---|---|
| `review_verdict` | APPROVE in interactive/remote mode | Approve / Extend / Escalate / Reject | **P0 — exists** |
| `preflight_blocked` | Preflight returns BLOCKED | Retry / Force proceed / Abandon | **P1** |
| `cycles_exhausted` | Max review cycles hit | Extend 1 cycle / Escalate / Abandon | **P1** |
| `merge_conflict` | Auto-merge fails on conflict | Rerun fresh / Manual resolve / Skip | P2 |
| `synthesis_failure` | Synthesis agent fails with 2 reviews available | Use reviewer A / Use reviewer B / Escalate | P2 |
| `gate_failure` | Gate infra error (not test failure) | Retry gate / Escalate / Skip gate | P2 |
| `dev_crash` | Dev agent exit != 0 with partial commits | Retry dev / Review partial / Escalate | P3 |
| `budget_warning` | Cost within 20% of limit | Continue / Pause | P3 (log only, not a gate) |

## Decision Modes

Each type has a configurable mode in `forge.yaml`:

- **blocking** — must get human response (hard 4h cap)
- **advisory** — ntfy notification, auto-resolves to default after timeout
- **auto** — no notification, take default immediately, log only

## Stories

### Phase 1: Foundation (P0 + P1)
- [x] `ntfy-terminal-notifications.md` — ntfy DONE/ESCALATE notifications
- [x] `remote-hitl.md` — ntfy-based HUMAN_REVIEW with action buttons
- [ ] `decision-surface.md` — `_await_human_decision()` abstraction, DecisionConfig,
      refactor REVIEW_VERDICT, add PREFLIGHT_BLOCKED + CYCLES_EXHAUSTED

### Phase 2: Expand decision types (P2)
- [ ] `decision-merge-conflict.md` — MERGE_CONFLICT decision gate (needs spec)
- [ ] `decision-synthesis-failure.md` — SYNTHESIS_FAILURE decision gate (needs spec)
- [ ] `decision-gate-failure.md` — GATE_FAILURE decision gate (needs spec)

### Phase 3: Durability + scale (P3)
- [ ] `decision-durable-state.md` — write pending decisions to disk, resume
      after process restart (prerequisite for >2 suspension points)
- [ ] `decision-batch-notify.md` — batch multiple pending decisions into
      single notification when they arrive within 60s
- [ ] `decision-dev-crash.md` — DEV_CRASH decision gate (needs spec)

## Dependencies

```
Phase 1 (decision-surface.md)
  └─ Phase 2 (individual decision type specs)
       └─ Phase 3 (durable-state prerequisite before adding more suspension points)
```

`coordinator-refactor.md` is not a hard dependency but makes all phases
easier to implement (decision logic in smaller, focused modules).

## Definition of Done

- Every branching decision in the coordinator goes through `_await_human_decision()`
- `forge.yaml` `decisions:` block configures mode/default/timeout per type
- Notification fatigue stays manageable (<5 decisions per day in normal use)
- Audit log captures every decision with type/action/waited/mode
- Process restart can resume a pending decision (Phase 3)

## Out of Scope

- Budget warnings as decision gates (these are log lines + info-priority ntfy)
- Decisions during `forge ideate` (ideate has its own deliberation flow)
- Multi-user decision routing (single human operator assumed)
