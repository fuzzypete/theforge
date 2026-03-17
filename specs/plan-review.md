---
name: "Plan review — human gate between PLAN and DEV"
slug: plan-review
pytest_target: tests/
---

# Plan Review — Human Gate Between PLAN and DEV

## Problem

The PLAN phase produces `forge_plan.md` and DEV consumes it automatically.
No human ever sees the plan before code is written. A bad plan — wrong
approach, missing edge cases, misread spec — silently poisons the entire
DEV run and only surfaces as P1 findings after multiple expensive cycles.

In the manual HDP workflow, the plan is always reviewed before dev starts.
That review step is where course-corrections cost almost nothing. In
theforge, that gate doesn't exist.

## Solution

Add an optional `PLAN_REVIEW` state between `PLAN` and `DEV`. When enabled,
forge pauses after `forge_plan.md` is written, surfaces the plan to the
human, and waits for one of:

- **approve** — proceed to DEV with plan as-is
- **edit** — human edits `forge_plan.md` in-place, then approves
- **regenerate** — discard plan, re-run PLAN agent (once)
- **abandon** — cancel the run, leave worktree intact

### State machine

```
INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW → DONE/ESCALATE
```

`PLAN_REVIEW` only runs when:
1. `plan_review.enabled` is `true` in forge.yaml (default: `false`)
2. A PLAN agent actually ran (i.e. plan was not injected via `--plan`)

When `--plan` is used (plan injection), `PLAN_REVIEW` is skipped — the
human already reviewed the plan before injecting it.

## forge.yaml config

```yaml
plan_review:
  enabled: true          # default: false
  mode: blocking         # blocking | advisory (default: blocking)
  timeout_seconds: 14400 # 4h, only used in advisory mode
```

- **blocking** — forge waits indefinitely for a human decision (same
  behaviour as `HUMAN_REVIEW` in interactive mode). EOF or process kill
  → abandon.
- **advisory** — forge sends ntfy notification and auto-approves after
  `timeout_seconds` if no response arrives.

## Interactive mode (terminal)

When running in a terminal (no `--notify` or non-ntfy backend):

1. Print `forge_plan.md` contents to stdout
2. Print decision prompt:
   ```
   Plan ready. Review forge_plan.md and choose:
     [a] Approve — proceed to DEV
     [e] Edit    — edit forge_plan.md, then re-enter 'a' to approve
     [r] Regen   — discard and re-run PLAN agent
     [x] Abandon — cancel run, leave worktree intact
   Choice:
   ```
3. Read stdin. Loop on `e` (let human edit externally, re-prompt).
4. On EOF → abandon (consistent with HUMAN_REVIEW behaviour).

## Remote mode (ntfy)

When `--notify` is active and backend is ntfy, use the same async poll
pattern as `remote-hitl`:

1. Publish plan summary notification to ntfy topic:
   ```
   Title: TheForge: plan ready — <slug>
   Body:  <first 3 lines of forge_plan.md, truncated to 200 chars>
          Worktree: .forge/worktrees/<slug>

   Actions: http, Approve, <reply_url>, method=POST, body=approve;
            http, Regenerate, <reply_url>, method=POST, body=regenerate;
            http, Abandon, <reply_url>, method=POST, body=abandon
   ```
2. Long-poll reply topic every 10s until response or timeout.
3. On timeout in advisory mode → approve and log warning.
4. On timeout in blocking mode → keep polling (no auto-resolve).

Note: `edit` is not available in remote mode (human can't edit the file
from their phone). If the human wants to edit, they should abandon and
re-run with `--plan` after editing.

## Regenerate behaviour

On `regenerate`:
- Delete `forge_plan.md` from worktree
- Re-run PLAN agent with same prompt
- Return to `PLAN_REVIEW` with the new plan
- Regenerate is allowed **once only**. On second regenerate request,
  treat as abandon and log: `"Plan regenerated once already — abandoning."`

## Coordinator changes

### New `Phase.PLAN_REVIEW` state

Add to the phase enum. The coordinator enters `PLAN_REVIEW` after a
successful PLAN run when `plan_review.enabled` is true.

### `_run_plan_review(state, config, task, workspace_path) -> PlanReviewDecision`

Returns one of: `APPROVE | REGENERATE | ABANDON`

Handles both interactive and remote modes. Reads `forge_plan.md` from
`workspace_path`. On `edit`, re-reads the file after human confirms done.

### `CoordinatorState` additions

```python
plan_review_decision: str | None = None   # "approve" | "regenerate" | "abandon"
plan_regenerated: bool = False             # guard against infinite regen loop
```

### Audit additions

`forge_audit.yaml` gains a `plan_review` section:

```yaml
plan_review:
  decision: approve          # approve | regenerate | abandon
  regenerated: false         # true if plan was regenerated once
  waited_seconds: 42.3       # time from prompt to decision
  mode: blocking
```

## Log output

```
[forge] ▸ PLAN_REVIEW   waiting for human decision...
[forge]   Plan written to: .forge/worktrees/<slug>/forge_plan.md
[forge]   ✓ PLAN_REVIEW   approve  (42s)
```

On regenerate:
```
[forge]   ↺ PLAN_REVIEW   regenerate — re-running PLAN agent
```

On abandon:
```
[forge]   ✗ PLAN_REVIEW   abandoned — worktree preserved at .forge/worktrees/<slug>
```

## Config defaults

`plan_review` block is optional in forge.yaml. When absent:
- `enabled` defaults to `false` — no behaviour change for existing projects

## Acceptance Criteria

- [ ] `Phase.PLAN_REVIEW` added to state machine
- [ ] `plan_review.enabled` in forge.yaml (default false), no behaviour
      change when absent or false
- [ ] `PLAN_REVIEW` skipped when plan was injected via `--plan`
- [ ] Interactive mode: prints plan, prompts for decision, loops on edit
- [ ] Remote mode: ntfy notification with Approve/Regenerate/Abandon buttons
- [ ] Regenerate re-runs PLAN agent and returns to PLAN_REVIEW
- [ ] Regenerate only allowed once — second attempt → abandon
- [ ] Abandon leaves worktree intact, exits cleanly
- [ ] Advisory mode auto-approves after timeout with log warning
- [ ] `plan_review_decision` and `plan_regenerated` in `CoordinatorState`
- [ ] Audit YAML includes `plan_review` section when phase ran
- [ ] Log lines match spec format
- [ ] `forge.yaml` `theforge` project config updated with `plan_review.enabled: true`
- [ ] New tests: approve, edit→approve, regenerate, abandon, regen-twice→abandon
- [ ] New tests: advisory timeout → approve
- [ ] New tests: `--plan` injection skips PLAN_REVIEW
- [ ] Existing tests pass unchanged

## Future

When `decision-surface.md` lands, `_run_plan_review()` is refactored to
use `_await_human_decision()` with `decision_type: "plan_review"`. The
logic is identical — this spec intentionally mirrors the decision-surface
pattern so the refactor is mechanical.
