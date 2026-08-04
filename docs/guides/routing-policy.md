# Routing policy: what the complexity score controls

TheForge's preflight assigns each story a **complexity score** from 1 to 10.
That score is granular *as input*, but the routing *output* on each axis (which
model tier, how many reviewers, how hard each phase thinks) is deliberately coarse — models and reviewers
come in discrete units, not a continuous gradient, and aggressive bucketing
keeps cost variance predictable. This guide is the operator-facing answer to the
fair question "I see `complexity_score=4` in the audit — what would `8` do
differently?" (issue #1019).

For the full adaptive-assignment system — eligibility vs. preference, signal
admissibility, taint exclusion, recency recovery, exploration, and
`routing_decision` explainability — see
[Adaptive Assignment](adaptive-assignment.md).

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
| **reasoning_effort** | 3, **per phase** | plan: 1–3 → `medium`, 4–10 → `high`; dev and review: 1–3 → `low`, 4–6 → `medium`, 7–10 → `high` | Override the bands and the effort→token-budget map, sprint-wide or per provider, under `assignment.reasoning_effort` in `forge.yaml`. Edit `REASONING_EFFORT_PHASE_BUCKETS` to move the defaults. |

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
## Reasoning effort (per phase)

> **Retraction.** Earlier versions of this guide (and of `routing.py`) described
> `reasoning_effort` as intentionally *not* score-controlled, on the rationale
> that it is a per-model capability knob rather than a per-story routing
> decision. That is no longer the policy: as of #1108 it is a score-controlled
> axis, resolved per phase and applied at assignment time.

Model tier and reasoning effort are orthogonal levers — changing tier changes
*which model* runs; changing effort changes *how hard the same model thinks*.
The default table is deliberately asymmetric:

| Score band | plan | dev | review |
|---|---|---|---|
| 1–3 | `medium` | `low` | `low` |
| 4–6 | `high` | `medium` | `medium` |
| 7–10 | `high` | `high` | `high` |

Plan starts a band above dev because it runs once and its output constrains
every downstream phase. `review` covers both plan-review and code-review;
preflight is excluded (it is the cheap classification pass the score comes
*from*). The only valid effort values are `low`, `medium`, and `high`.

**Token-budget providers.** Some transports express thinking as a token count
rather than an effort level. Each level resolves to a configurable budget —
default `low` → 2048, `medium` → 8192, `high` → 24576.

**Provider support** is a *transport* capability, not a provider-family one,
and lives in `REASONING_EFFORT_TRANSPORT_SUPPORT` in `routing.py`:

| Transport | Knob | Field set on the profile |
|---|---|---|
| Codex CLI | effort level | `reasoning_effort` |
| Google API | token budget | `thinking_budget` |
| Claude CLI, Gemini CLI, gh-aw, Anthropic/OpenAI/DeepSeek API | none | — |

Where a transport has no passthrough the resolved level is recorded but **not**
applied, and the model's entry records `provider_unsupported`. Where it is
applied, the entry records one of:

- `supported_metered` — the transport folds thinking tokens into the run's
  priced total *and* the selected model has a pricing entry.
- `supported_unmetered` — the knob applies but the spend does not reach the
  run's measured cost (no pricing entry for the model). Recorded explicitly so
  score-driven spend on a surface the audit reports as $0.00 stays visible
  rather than silent.

An explicit `reasoning_effort` / `thinking_budget` on a profile wins over score
routing (ADR-0006 clause 1); the entry then records `static_profile_override`.

**Configuring it.** All of the above is overridable in `forge.yaml`, sprint-wide
or per provider (keys are transport runner names: `codex`, `google`, `claude`,
`gemini`, `anthropic`, `openai`, `deepseek`, `ghaw`):

```yaml
assignment:
  reasoning_effort:
    enabled: true                # false leaves the axis flat (still recorded)
    phases:
      dev:
        - {max_score: 5, effort: low}
        - {max_score: 10, effort: high}
    token_budgets: {low: 2048, medium: 8192, high: 24576}
    providers:
      google:
        token_budgets: {high: 32768}
      codex:
        phases:
          dev:
            - {max_score: 10, effort: high}
```

Bands are validated at load: efforts must be `low`/`medium`/`high`, `max_score`
values must ascend within 1–10 and reach 10, and token budgets must be
non-negative integers. A bad override is a load error, never a silent drop.

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
recorded once at the top level of the block, keyed by phase, with one entry per
seated model (a reviewer pool can span providers, so a single phase-level
support status would be under-specified):

```jsonc
"reasoning_effort": {
  "axis": "reasoning_effort",
  "score": 8,
  "score_controlled": true,
  "enabled": true,
  "applied": true,
  "phases": {
    "dev": {
      "phase": "dev",
      "bucket": "high",
      "range": [7, 10],          // the covering score range
      "thresholds": [3, 6, 10],  // the bucket ceilings actually applied
      "output": "high",          // the resolved effort level
      "applied": true,
      "provider_support": "supported_metered",
      "models": [
        {
          "model": "gpt-5.4",
          "transport": "codex",
          "knob": "effort",
          "output": "high",
          "provider_support": "supported_metered",
          "applied": true,
          "field": "reasoning_effort",
          "value": "high"
        }
      ]
    }
  }
}
```

This block is the only operator-facing surface for the axis — the feature adds
no separate rationale or audit structure, and `forge explain` renders straight
from it.

`forge explain --story <id>` renders this block for a given run.

## Changing the policy

1. Edit the relevant `*_BUCKETS` tuple (or `ROUTING_POLICY` rationale) in
   `src/theforge/routing.py`. That is the only place a threshold lives.
2. Update the table above if a boundary moved.
3. Run the routing tests: `tests/test_routing.py`,
   `tests/test_assignment_routing_decision.py`,
   `tests/test_assignment_reasoning_effort.py`,
   `tests/test_reasoning_effort_config.py`.

Do **not** hardcode a score threshold anywhere else — `assignment.py` and the
coordinator route through `routing.py` precisely so the policy stays in one
auditable place.
