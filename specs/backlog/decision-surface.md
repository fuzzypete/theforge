---
name: Decision surface — structured human decision gates
slug: decision-surface
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/config.py
  - tests/test_coordinator.py
---

# Spec: Decision Surface — Structured Human Decision Gates

## Problem

When the coordinator encounters a branching decision (merge conflict, reviewer
disagreement, max cycles hit), it either picks a default unilaterally or
escalates with a wall of text. The human must then interpret what happened
and figure out what to do — often via a proxy (a Claude session) rather than
directly.

The existing `_remote_human_review` already implements the right pattern:
ntfy push with tappable action buttons, poll for reply, act on the response.
But it's hardcoded to one decision type (APPROVE/EXTEND/ESCALATE after review).

## Solution

Generalize the decision mechanism into a single `_await_human_decision()`
function that any coordinator decision point can call. Keep `Phase.HUMAN_REVIEW`
as the only suspension phase, but track `decision_type` in state for
logging/audit.

### Phase 1 (this spec): Foundation + 2 decision types

1. **`_await_human_decision()` abstraction**
2. **`DecisionConfig` in forge.yaml** — per-type mode (blocking/advisory/auto)
3. **Two new decision types:**
   - `PREFLIGHT_BLOCKED` — spec can't proceed as written
   - `CYCLES_EXHAUSTED` — max review cycles hit without APPROVE

### Phase 2 (future): Additional decision types
- `MERGE_CONFLICT`, `SYNTHESIS_FAILURE`, `BUDGET_WARNING`, etc.

## Design

### Decision Types

```python
class DecisionType(Enum):
    """Named decision points where the coordinator may suspend for human input."""
    REVIEW_VERDICT = "review_verdict"        # existing HUMAN_REVIEW behavior
    PREFLIGHT_BLOCKED = "preflight_blocked"  # spec blocked, can't proceed
    CYCLES_EXHAUSTED = "cycles_exhausted"    # max cycles hit without APPROVE
```

### Decision Options

```python
@dataclass(frozen=True)
class DecisionOption:
    label: str       # human-readable, shown in ntfy button
    action: str      # machine key returned to coordinator
    is_default: bool = False  # used for advisory auto-timeout
```

### Decision Modes

Each decision type has a mode that controls whether the coordinator blocks,
auto-resolves, or skips:

- **blocking** — must get a human response (no timeout auto-action)
- **advisory** — sends notification, auto-resolves to `default` after `timeout` seconds
- **auto** — no notification, immediately takes `default` action, logs only

### `DecisionPolicy` dataclass

```python
@dataclass(frozen=True)
class DecisionPolicy:
    mode: str = "advisory"  # "blocking" | "advisory" | "auto"
    default: str = ""       # action key for advisory/auto modes
    timeout: int = 900      # seconds before advisory auto-resolves (15 min)
```

### `forge.yaml` config

```yaml
decisions:
  review_verdict:
    mode: blocking
  preflight_blocked:
    mode: advisory
    default: abandon
    timeout: 900
  cycles_exhausted:
    mode: advisory
    default: escalate
    timeout: 900
```

### `DecisionConfig` in config.py

```python
@dataclass(frozen=True)
class DecisionConfig:
    """Per-decision-type policies. Missing types use sensible defaults."""
    policies: dict[str, DecisionPolicy] = field(default_factory=dict)

    def get_policy(self, decision_type: str) -> DecisionPolicy:
        return self.policies.get(decision_type, _DEFAULT_POLICIES.get(
            decision_type, DecisionPolicy()
        ))
```

Add `decisions: DecisionConfig` field to `ForgeConfig` with a default that
matches current behavior (review_verdict=blocking, others=advisory).

### `_await_human_decision()` function

```python
def _await_human_decision(
    decision_type: DecisionType,
    options: list[DecisionOption],
    context: str,
    *,
    state: CoordinatorState,
    task: TaskSpec,
    config: ForgeConfig,
    notify: bool,
) -> str:
    """Suspend coordinator for human decision. Returns chosen action key.

    Behavior depends on the DecisionPolicy for this decision_type:
    - auto: immediately return default action, log only
    - advisory: send ntfy notification, auto-resolve after timeout
    - blocking: send ntfy notification, wait indefinitely (4h hard cap)

    When ntfy is not configured (notify=False or no ntfy backend),
    blocking mode falls back to interactive terminal prompt,
    advisory/auto modes return the default immediately.
    """
```

Implementation:
1. Look up `DecisionPolicy` from config for this `decision_type`
2. If mode is `auto` → log decision, return default action
3. If ntfy is available → publish notification with action buttons, poll for reply
4. If ntfy unavailable + blocking → fall back to interactive terminal input
5. If ntfy unavailable + advisory → return default action
6. Track `state.decision_type = decision_type.value` for audit logging

### Ntfy notification format

```
Title: "[forge] {task.name} — Decision needed"
Body: "{context}"
Actions: one "view" button per option, labeled with option.label
Tags: decision type tag for filtering
```

Use the existing `_ntfy_publish()` and `_ntfy_poll_reply()` infrastructure.
The poll reply matching uses the option `action` keys (same pattern as
approve/extend/escalate today).

### Wire into coordinator

#### 1. Refactor existing HUMAN_REVIEW

Replace the current `_human_review()` / `_remote_human_review()` call site
with `_await_human_decision(DecisionType.REVIEW_VERDICT, ...)`. The existing
Approve/Extend/Escalate/Reject options map directly to `DecisionOption` instances.
Behavior is identical — this is a pure refactor of the existing flow.

#### 2. PREFLIGHT_BLOCKED decision

Currently: preflight returns BLOCKED → coordinator immediately sets
`state.phase = Phase.ESCALATE` and exits.

After: preflight returns BLOCKED → coordinator calls:
```python
action = _await_human_decision(
    DecisionType.PREFLIGHT_BLOCKED,
    options=[
        DecisionOption("Update spec & retry", "retry"),
        DecisionOption("Force proceed", "force", is_default=False),
        DecisionOption("Abandon", "abandon", is_default=True),
    ],
    context=f"Preflight BLOCKED: {state.preflight_reason}",
    ...
)
```
- `retry` → loop back to PREFLIGHT (human updates spec externally, then taps retry)
- `force` → skip preflight, proceed to DEV (set preflight_verdict to PROCEED)
- `abandon` → ESCALATE as today

#### 3. CYCLES_EXHAUSTED decision

Currently: max_review_cycles reached → coordinator immediately sets
`state.phase = Phase.ESCALATE`.

After: max_review_cycles reached → coordinator calls:
```python
action = _await_human_decision(
    DecisionType.CYCLES_EXHAUSTED,
    options=[
        DecisionOption("Extend 1 cycle", "extend", is_default=True),
        DecisionOption("Escalate", "escalate"),
        DecisionOption("Abandon", "abandon"),
    ],
    context=f"Max {config.retry.max_review_cycles} review cycles exhausted. "
            f"Last verdict: {last_verdict}. Cost so far: ${state.total_cost:.2f}",
    ...
)
```
- `extend` → increment extra cycles, continue DEV→REVIEW loop
- `escalate` → ESCALATE as today (with human review of current state)
- `abandon` → ESCALATE without human review

## State tracking

Add to `CoordinatorState`:
```python
decision_type: str | None = None  # e.g. "review_verdict", "preflight_blocked"
decision_action: str | None = None  # e.g. "approve", "retry", "extend"
decision_waited_seconds: float | None = None  # time spent waiting
```

These replace `human_review_decision` / `human_review_waited_seconds` and
are used by the audit log generator for any decision type.

## Audit log

The existing `human_review` section in audit YAML generalizes to:
```yaml
decisions:
  - type: review_verdict
    action: approve
    waited_seconds: 45.2
    mode: blocking
  - type: cycles_exhausted
    action: extend
    waited_seconds: 0  # auto mode
    mode: auto
```

## What NOT to do

- Do NOT add new `Phase` enum values for each decision type. Use `HUMAN_REVIEW`
  as the single suspension phase.
- Do NOT implement decision types beyond the three in this spec (REVIEW_VERDICT,
  PREFLIGHT_BLOCKED, CYCLES_EXHAUSTED). Additional types are Phase 2.
- Do NOT add durable state (pending decision to disk) yet — that's a prerequisite
  for Phase 2 when we add more suspension points.
- Do NOT batch notifications — out of scope for Phase 1.
- Do NOT prompt for budget warnings — those should be log lines, not decisions.

## Acceptance Criteria

1. `_await_human_decision()` function exists with the signature above.
2. `DecisionPolicy`, `DecisionConfig` dataclasses exist in config.py.
3. `decisions:` section in forge.yaml is parsed and loaded into `ForgeConfig.decisions`.
4. Existing HUMAN_REVIEW flow works identically via the new abstraction
   (refactor, not rewrite).
5. Preflight BLOCKED triggers a decision gate (advisory mode by default).
6. Max cycles exhausted triggers a decision gate (advisory mode by default).
7. `auto` mode decisions return immediately without any notification.
8. `advisory` mode decisions auto-resolve after timeout.
9. `blocking` mode decisions wait for human response.
10. All existing tests pass. New tests cover each decision type in each mode.
11. Audit log includes `decisions:` list with type/action/waited/mode.
