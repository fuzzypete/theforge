# Routing policy: what the complexity score controls

TheForge's preflight assigns each story a **complexity score** from 1 to 10.
That score is granular *as input*, but the routing *output* on each axis (which
model tier, how many reviewers) is deliberately coarse — models and reviewers
come in discrete units, not a continuous gradient, and aggressive bucketing
keeps cost variance predictable. This guide is the operator-facing answer to the
fair question "I see `complexity_score=4` in the audit — what would `8` do
differently?" (issue #1019).

The thresholds below are **not** duplicated prose: they are read directly from
the single source of truth, `src/theforge/routing.py`
(`ROUTING_POLICY` / the `*_BUCKETS` tables). If the code and this table ever
disagree, the code wins — but the intent is that they never drift, because
`routing.py` is the only place a threshold may live and this guide describes it.

## The intended design (per axis)

Every axis takes **option (a)**: the coarse buckets are the intended design.
The score's extra resolution is retained as a signal for possible future
finer-grained routing, not as a defect to be "fixed." No axis is rewired to
finer granularity in this pass.

| Axis | Buckets | Score → output | What an operator can tune |
|---|---|---|---|
| **Dev model tier** | 3 | 1–3 → `cheap`, 4–6 → `mid`, 7–10 → `strong` | Raise/lower the tier ceiling by editing `DEV_SCORE_TIER_BUCKETS`; the tier floor is never crossed by budget downgrades. |
| **Plan model tier** (planner *and* reviewer models) | 2 | 1–5 → `mid`, 6–10 → `strong` | Edit `PLAN_SCORE_TIER_BUCKETS`. Planning quality is bimodal, so 2 buckets is deliberate. |
| **Reviewer count** (plan-review + code-review) | 3 | 1–4 → `min`, 5–7 → midpoint, 8–10 → `max` | Set `assignment.min_reviewers` / `assignment.max_reviewers` in `forge.yaml`; the score selects which of {min, midpoint, max} applies. Edit `REVIEWER_SCORE_COUNT_BUCKETS` to move the boundaries. |
| **reasoning_effort** | — | *not score-controlled* | Set per model via config/overrides (`reasoning_effort` / `thinking_budget` on a `ModelRef`); never derived from the score. |

Notes:

- **Reviewer count is bounded, not absolute.** The score picks a *target*
  (`min` / midpoint / `max`); the actual number seated can be lower if the
  candidate pool is exhausted (self-exclusion, cross-provider preference) or a
  budget downgrade drops a reviewer. The audit records both the policy target
  (`resolved_count`) and the number actually seated (`seated_count`).
- **Static / band routing.** When adaptive routing is disabled, or preflight
  produced no numeric score, the score axes fall back to the legacy
  complexity-band tables (`LOW`/`MEDIUM`/`HIGH`). The `routing_decision` block
  still records each axis with `applied: false` and the reason, so the fallback
  is never silent.
- **Why `reasoning_effort` is excluded.** It is a per-model capability knob, not
  a per-story routing decision. Wiring it to the score would couple a model's
  thinking budget to story complexity in a way operators cannot reason about
  per model. It is listed in the axis table with `score_controlled: false` so
  its exclusion is explicit, not an oversight.

## Reading it in the audit

Every run records a `routing_decision` block (see ADR-0006 clause 7). Each axis
appears under the relevant role with the full policy view:

```jsonc
"dev": {
  "score": 8,
  "score_policy": {
    "dev_tier": {
      "axis": "dev_tier",
      "score": 8,
      "bucket": "strong",
      "range": [7, 10],          // the covering score range
      "thresholds": [3, 6, 10],  // the bucket ceilings actually applied
      "output": "strong",        // the selected tier
      "rationale": "3 buckets — splits the legacy MEDIUM band …"
    }
  }
}
```

Reviewer roles (`plan_review`, `code_review`) carry two axes — `reviewer_tier`
(the plan-tier axis) and `reviewer_count`. The `reasoning_effort` axis is
recorded once at the top level of the block.

`forge explain --story <id>` renders this block for a given run.

## Changing the policy

1. Edit the relevant `*_BUCKETS` tuple (or `ROUTING_POLICY` rationale) in
   `src/theforge/routing.py`. That is the only place a threshold lives.
2. Update the table above if a boundary moved.
3. Run the routing tests: `tests/test_routing.py`,
   `tests/test_assignment_routing_decision.py`.

Do **not** hardcode a score threshold anywhere else — `assignment.py` and the
coordinator route through `routing.py` precisely so the policy stays in one
auditable place.
