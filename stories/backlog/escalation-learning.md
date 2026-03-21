---
name: "Escalation learning — auto-promotion from escalation history"
slug: escalation-learning
pytest_target: tests/
depends_on: [adaptive-model-assignment]
---

# Escalation Learning

## Problem

Adaptive model assignment picks the right starting tier, but it has no memory.
If sonnet is assigned to MEDIUM stories and keeps escalating, the next MEDIUM
story still gets sonnet. The human has to notice the pattern and manually
override the config.

The escalation history file already exists (`.forge/assignment_history.yaml`)
as part of adaptive-model-assignment. This story closes the loop: read that
history, detect repeat failures at a tier, and auto-promote the assignment
before the story even starts.

## Design Principles

1. **Deterministic** — same history + same config = same promotion decision.
   No LLM in the loop.
2. **Explicit overrides win** — if forge.yaml names a specific dev profile,
   escalation learning is skipped entirely for that role.
3. **Sprint-scoped** — promotions are sticky within a sprint and reset between
   sprints. History persists; promotions derived from it do not carry forward.
4. **Transparent** — every promotion decision is logged with the exact
   calculation that triggered it.

## Escalation History Format

Written to `.forge/assignment_history.yaml` by the coordinator after each
story completes. One record per story run:

```yaml
runs:
  - story: fix-auth-bug
    sprint: sprint-2026-03-15
    complexity: MEDIUM
    dev_model: sonnet
    outcome: ESCALATE
    reason: "max review cycles exceeded"
    timestamp: 2026-03-15T10:30:00Z

  - story: add-search-filter
    sprint: sprint-2026-03-15
    complexity: MEDIUM
    dev_model: sonnet
    outcome: DONE
    timestamp: 2026-03-15T11:00:00Z
```

`outcome` is one of `DONE` or `ESCALATE`. The `sprint` field is the sprint
slug from forge.yaml (or the run slug if no sprint context). `reason` is
optional and only present on ESCALATE outcomes.

## Promotion Logic

### Threshold Check

Before assigning a dev model for a new story, evaluate:

1. Filter history to records where `dev_model == current_assigned_model`
   and `complexity == current_complexity`.
2. Take the **last 10 runs** matching that filter (ordered by timestamp).
3. Count escalations in that window.
4. If escalation count >= 2: promote to the next tier.

"Next tier" follows the ordering: `cheap → mid → strong`. If the current model
is already `strong` tier, no further promotion is possible — log a warning
instead.

### Sprint Stickiness

Promotions are computed once per sprint per (complexity, model) pair and
reused for all subsequent stories in that sprint. They are not persisted to
disk — they are re-derived from history at the start of each story assignment
within a sprint.

"Re-derived" means: if a new story is the 3rd MEDIUM story in a sprint and
the first two were ESCALATE, the threshold check for the 3rd story will see
2 escalations in the last 10 runs and promote. This is correct behavior — the
sprint stickiness means the promotion applies forward from when the threshold
is crossed, not retroactively.

Between sprints the slate is clean: a new sprint starts fresh promotion
evaluation against the same persistent history.

### Tier Promotion Target

Select the cheapest available model in the next tier from the configured agent
pool. If no model exists in the next tier, use the cheapest model in the
current tier that is not the current model. If no alternative exists, keep the
current model and log a warning.

### Explicit Override

If `profiles.dev` is explicitly set in forge.yaml, skip all escalation
learning for the dev role. The explicit profile is used as-is. Log at debug:
`"[escalation-learning] explicit dev profile set — skipping promotion check"`.

## Implementation

### New module: `src/theforge/escalation.py`

```python
@dataclass
class EscalationRecord:
    story: str
    sprint: str
    complexity: str       # LOW | MEDIUM | HIGH
    dev_model: str        # model name string
    outcome: str          # DONE | ESCALATE
    reason: str | None
    timestamp: datetime

@dataclass
class PromotionDecision:
    promoted: bool
    original_model: str
    promoted_model: str | None   # None if promoted=False
    escalation_count: int
    window_size: int             # actual number of matching records found
    reason: str                  # human-readable explanation for log

def load_history(path: Path) -> list[EscalationRecord]:
    """Load and parse assignment_history.yaml. Returns [] if file absent."""

def append_record(path: Path, record: EscalationRecord) -> None:
    """Append a single record to assignment_history.yaml atomically."""

def check_promotion(
    history: list[EscalationRecord],
    complexity: str,
    dev_model: str,
    agents: list[AgentDef],
    threshold: int = 2,
    window: int = 10,
) -> PromotionDecision:
    """Pure deterministic function. No LLM. No I/O."""
```

`check_promotion` is a pure function — all I/O is handled by the caller.
Tests exercise it directly without touching the filesystem.

### Coordinator integration

In the assignment path (after `assign_models()` returns, before the dev phase
starts):

1. Load `.forge/assignment_history.yaml` (absent file = empty history, not an
   error).
2. If `profiles.dev` is explicitly set, skip to step 5.
3. Call `check_promotion()` with the assigned dev model and complexity.
4. If `PromotionDecision.promoted`, replace the dev model with
   `PromotionDecision.promoted_model` and log the decision.
5. Proceed with the (possibly promoted) assignment.

After the story reaches DONE or ESCALATE, append a record to
`.forge/assignment_history.yaml`.

### Logging

All promotion decisions logged at INFO level (always visible, not just
verbose):

```
[escalation-learning] MEDIUM dev: sonnet → opus (2/8 recent MEDIUM stories escalated with sonnet)
[escalation-learning] No promotion needed for HIGH dev: opus (0/5 recent HIGH stories escalated with opus)
[escalation-learning] Cannot promote strong-tier dev: opus already at top tier (1/3 recent HIGH stories escalated — warning)
[escalation-learning] Skipping promotion check: explicit dev profile set in forge.yaml
```

The log line includes the window size actually found (not just the configured
window of 10), so operators can tell when history is sparse.

## Acceptance Criteria

- `.forge/assignment_history.yaml` records are written after every story
  completes (DONE or ESCALATE)
- `load_history()` returns empty list when the file does not exist
- `append_record()` creates the file if absent, appends otherwise
- `check_promotion()` is a pure function with no I/O or LLM calls
- Promotion fires when escalation count >= 2 in last 10 matching runs
- Promotion selects cheapest model in next tier from the configured agent pool
- Promotion is logged at INFO with escalation count and window size
- No promotion when escalation count < 2; logged at INFO
- Strong-tier model with >= 2 escalations logs a warning instead of promoting
- Explicit `profiles.dev` in forge.yaml bypasses all promotion logic
- Sprint stickiness: promotion derived from history, not stored separately
- History file persists across sprints; promotion decisions do not
- Tests cover:
  - Promotion fires at threshold (exactly 2 escalations in last 10)
  - Promotion does not fire below threshold (1 escalation in last 10)
  - Window boundary: only last 10 matching records counted, not all history
  - Sparse history: fewer than 10 matching records uses what is available
  - Strong-tier ceiling: no promotion, warning logged
  - No matching pool model for next tier: fallback behavior logged
  - Explicit override: promotion check skipped entirely
  - Empty history file: no promotion, no error
  - Absent history file: no promotion, no error
  - `append_record` creates file on first write
  - `append_record` preserves existing records on subsequent writes
