# Plan: Plan Review — Human Gate Between PLAN and DEV

Generated 2026-03-16. For injection via `forge run specs/plan-review.md --plan docs/plans/plan-review.md`.

---

## Context

PLAN produces `forge_plan.md` and DEV consumes it automatically. No human sees
the plan before code is written. A bad plan silently poisons the entire run and
only surfaces as P1 findings after expensive cycles. This adds a `PLAN_REVIEW`
gate between PLAN and DEV.

**v1 scope: blocking interactive only.** Remote/advisory mode deferred — it
requires extending `_ntfy_poll_reply` vocabulary, which should be a follow-up.

**Critical constraint:** PLAN_REVIEW only fires when `interactive=True`. In
non-interactive mode (the default), it is skipped with a log warning. This
prevents unattended `forge run` or sprint calls from blocking on stdin.

## Implementation Order

### Step 1: Config (`src/theforge/config.py`)

Add `PlanReviewConfig` dataclass after `PlanConfig` (line ~151):

```python
@dataclass(frozen=True)
class PlanReviewConfig:
    enabled: bool = False
```

Add to `ForgeConfig` (after `plan` field, line ~176):

```python
plan_review: PlanReviewConfig = field(default_factory=PlanReviewConfig)
```

Parse `plan_review` from forge.yaml in `load_config()`. Pattern: follow
existing `plan` parsing — read `plan_review` dict if present, construct
`PlanReviewConfig`, default to disabled.

No `mode` or `timeout_seconds` fields in v1. Those come with remote mode.

### Step 2: State (`src/theforge/coord_state.py`)

Add `Phase.PLAN_REVIEW` to the enum (after `PLAN`, before `DEV`).

Change `plan_result` from single to list (for regen cost tracking):
```python
# Change from:
plan_result: AgentResult | None = None
# To:
plan_results: list[AgentResult] = field(default_factory=list)
```

Update `total_plan_cost` property:
```python
@property
def total_plan_cost(self) -> float:
    return sum(r.cost_usd for r in self.plan_results)
```

Add fields:
```python
plan_review_decision: str | None = None   # "approve" | "regenerate" | "abandon"
plan_regenerated: bool = False             # guard: regen allowed once only
plan_review_waited_seconds: float | None = None
```

**Migration note:** All existing references to `state.plan_result` must change
to `state.plan_results`. In coordinator.py, `state.plan_result = plan_result`
becomes `state.plan_results.append(plan_result)`. The `plan_result.success`
check uses `state.plan_results[-1].success`. Audit code that reads
`state.plan_result` updates to `state.plan_results[-1]` (or iterates for full
cost). Grep for `plan_result` across the codebase to catch all references.

**Known references outside coordinator.py:**
- `tests/test_coord_preflight.py` lines ~1180 and ~1316 assert on
  `state.plan_result` — must update to `state.plan_results[-1]`
- `coord_audit.py` reads `state.plan_result` for audit output

### Step 3: Interactive decision handler (`src/theforge/coord_notify.py`)

Add `_plan_review_interactive()` — new function, follows `_human_review` pattern
(lines 281-331). Signature:

```python
def _plan_review_interactive(
    state: "_cs.CoordinatorState",
    plan_text: str,
    workspace_path: "Path",
    task: "TaskSpec",
) -> str:
    """Interactive plan review. Returns 'approve' | 'regenerate' | 'abandon'."""
```

Implementation:
1. Print full `plan_text` to **stdout** (matches spec: human needs the complete plan)
2. Print path to stderr: `Plan at: {workspace_path}/forge_plan.md`
3. Print decision prompt:
   ```
   Plan ready. Review forge_plan.md and choose:
     [a] Approve   — proceed to DEV
     [e] Edit      — edit forge_plan.md externally, then re-enter 'a'
     [r] Regenerate — discard and re-run PLAN agent (once)
     [x] Abandon   — cancel run, leave worktree intact
   Choice [a/e/r/x]:
   ```
4. Read stdin. On `e`, print "Edit the plan, then enter 'a' to approve:" and
   re-read loop. On EOF → abandon. Invalid input → re-prompt.
5. Return decision string.

### Step 4: Coordinator integration (`src/theforge/coordinator.py`)

Insert `PLAN_REVIEW` between PLAN success (line 885) and DEV loop (line 903).

After `state.plan_output = plan_text` and the success log:

```python
# ── PLAN_REVIEW ─────────────────────────────────────────────
if config.plan_review.enabled and interactive:
    state.phase = Phase.PLAN_REVIEW
    _log_phase(state.phase, "waiting for human decision...")
    _log(f"  Plan written to: {workspace_path / 'forge_plan.md'}")

    _pr_start = time.monotonic()
    plan_review_decision = _plan_review_interactive(
        state, plan_text, workspace_path, task
    )
    state.plan_review_waited_seconds = time.monotonic() - _pr_start
    state.plan_review_decision = plan_review_decision

    if plan_review_decision == "approve":
        # On edit, re-read the file (human may have modified it)
        try:
            updated = (workspace_path / "forge_plan.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log(f"  ✗ PLAN_REVIEW   forge_plan.md unreadable after edit: {exc}")
            state.phase = Phase.PLAN_REVIEW
            return CoordinatorResult(
                success=False, phase=Phase.PLAN_REVIEW, state=state,
                message=f"forge_plan.md unreadable after edit: {exc}",
            )
        state.plan_output = updated
        _log(f"  ✓ PLAN_REVIEW   approve  ({_fmt_duration(state.plan_review_waited_seconds)})")

    elif plan_review_decision == "regenerate":
        if state.plan_regenerated:
            _log("  ✗ PLAN_REVIEW   already regenerated once — abandoning")
            return CoordinatorResult(
                success=False, phase=Phase.PLAN_REVIEW, state=state,
                message="Plan regenerated once already — abandoning.",
            )
        state.plan_regenerated = True
        _log("  ↺ PLAN_REVIEW   regenerate — re-running PLAN agent")

        # Delete old plan, re-run PLAN agent
        (workspace_path / "forge_plan.md").unlink(missing_ok=True)
        plan_result = run_agent(
            prompt=plan_prompt, profile=plan_profile, working_dir=workspace_path,
        )
        state.plan_results.append(plan_result)  # append, not overwrite
        if not plan_result.success:
            state.phase = Phase.ESCALATE
            state.error = "PLAN regeneration failed"
            _log(f"  ✗ PLAN regen failed — escalating")
            return CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error,
            )
        plan_text = plan_result.output
        (workspace_path / "forge_plan.md").write_text(plan_text, encoding="utf-8")
        state.plan_output = plan_text
        _log(f"  ✓ PLAN (regenerated)  ${plan_result.cost_usd:.2f}")

        # Loop back to PLAN_REVIEW with new plan
        # (recursive call or goto — simplest: repeat the decision block)
        # To avoid recursion, use a loop:
        # ... see note below

    elif plan_review_decision == "abandon":
        _log(f"  ✗ PLAN_REVIEW   abandoned — worktree preserved at {workspace_path}")
        state.phase = Phase.PLAN_REVIEW  # stay at PLAN_REVIEW, not ESCALATE
        return CoordinatorResult(
            success=False, phase=Phase.PLAN_REVIEW, state=state,
            message="Plan review abandoned by human.",
        )
```

**Non-interactive skip:** After the `if config.plan_review.enabled and interactive:` block:
```python
elif config.plan_review.enabled and not interactive:
    _log("  ⚠ PLAN_REVIEW   skipped (non-interactive mode)")
```

**Regenerate loop structure:** Wrap the PLAN_REVIEW block in a `for _ in range(2):`
loop (max 2 iterations: original + one regen). On `approve` or `abandon`, break.
On `regenerate`, continue. After loop exhaustion (shouldn't happen due to guard),
abandon.

**`--plan` injection skip:** The `PLAN_REVIEW` block is inside `if should_plan:`,
which already checks `plan_path is None`. So injected plans naturally skip it.
Add a log for clarity when plan_review is enabled but plan was injected:

```python
if plan_path is not None:
    # ... existing injection code ...
    if config.plan_review.enabled:
        _log("  ℹ PLAN_REVIEW   skipped (plan injected)")
```

### Step 5: Audit (`src/theforge/coord_audit.py`)

Add `plan_review` section to audit output when the phase ran:

```python
if state.plan_review_decision:
    audit["plan_review"] = {
        "decision": state.plan_review_decision,
        "regenerated": state.plan_regenerated,
        "waited_seconds": round(state.plan_review_waited_seconds or 0, 2),
    }
```

Find where audit dict is built and add this block.

### Step 6: Log output format

Match spec exactly:
```
[forge] ▸ PLAN_REVIEW   waiting for human decision...
[forge]   Plan written to: .forge/worktrees/<slug>/forge_plan.md
[forge]   ✓ PLAN_REVIEW   approve  (42s)
```

### Step 7: forge.yaml update

Add to project config:
```yaml
plan_review:
  enabled: true
```

### Step 8: Tests (`tests/test_coordinator.py`)

New test class `TestPlanReview`:

1. **test_plan_review_approve** — PLAN runs, human approves, DEV proceeds.
   Mock `_plan_review_interactive` returning `"approve"`. Verify
   `state.plan_review_decision == "approve"` and result is success.

2. **test_plan_review_edit_approve** — Human edits plan file then approves.
   Mock interactive returning `"approve"`. Write modified plan to worktree
   before the mock returns. Verify `state.plan_output` contains the edit.

3. **test_plan_review_regenerate** — Human requests regen. Mock interactive
   returning `"regenerate"` first, then `"approve"`. Verify PLAN agent called
   twice, `state.plan_regenerated == True`.

4. **test_plan_review_abandon** — Human abandons. Verify result is failure
   with "abandoned" message, worktree not cleaned up.

5. **test_plan_review_regen_twice_abandons** — Mock interactive returning
   `"regenerate"` twice. Verify auto-abandon on second regen.

6. **test_plan_review_skipped_on_injection** — Use `plan_path` parameter.
   Verify `PLAN_REVIEW` never entered, `plan_review_decision` is None.

7. **test_plan_review_skipped_when_disabled** — `plan_review.enabled = False`.
   Verify PLAN runs but no PLAN_REVIEW.

8. **test_plan_review_eof_abandons** — Mock stdin EOF. Verify abandon.

9. **test_plan_review_skipped_non_interactive** — `plan_review.enabled = True`
   but `interactive=False`. Verify PLAN_REVIEW never entered, log warning emitted.

10. **test_plan_review_abandon_phase_not_escalate** — Verify abandon returns
    `phase=Phase.PLAN_REVIEW`, not `Phase.ESCALATE`.

11. **test_plan_review_reread_error** — Delete `forge_plan.md` during edit.
    Verify deterministic failure, not exception.

12. **test_plan_regen_tracks_both_costs** — Verify `total_plan_cost` sums
    both plan invocations after a regen.

## Files to Modify

| File | Change |
|------|--------|
| `src/theforge/config.py` | `PlanReviewConfig` dataclass, `ForgeConfig.plan_review`, `load_config()` |
| `src/theforge/coord_state.py` | `Phase.PLAN_REVIEW`, state fields |
| `src/theforge/coord_notify.py` | `_plan_review_interactive()` |
| `src/theforge/coordinator.py` | PLAN_REVIEW gate between PLAN and DEV loop |
| `src/theforge/coord_audit.py` | `plan_review` audit section |
| `forge.yaml` | `plan_review.enabled: true` |
| `tests/test_coordinator.py` | 12 new tests |
| `tests/test_coord_preflight.py` | Update `state.plan_result` → `state.plan_results` references |

## What's NOT in v1

- Remote/advisory mode (ntfy polling with plan-specific vocabulary)
- `mode: advisory` with auto-approve timeout
- Review pool reviewing the plan (multi-model plan critique)
- `plan_review.timeout_seconds`

These all come in v2 after v1 is stable and dogfooded.

## Review History

### Round 1 (2026-03-16)
Reviewed by Codex.

**P1 — Non-interactive blocking**: `plan_review.enabled` without `interactive`
guard would block unattended runs on stdin. → Added `and interactive` guard,
skip with warning in non-interactive mode.

**P2 — Regen cost loss**: Overwriting `plan_result` loses first PLAN cost.
→ Changed to `plan_results: list[AgentResult]` with append, summed in
`total_plan_cost`.

**P2 — Abandon as ESCALATE**: User cancellation recorded as escalation pollutes
audit/dashboards. → Abandon returns `phase=Phase.PLAN_REVIEW` not `ESCALATE`.

**P2 — Reread error**: `forge_plan.md` deleted during edit raises exception.
→ Added try/except for `OSError`/`UnicodeDecodeError` with deterministic failure.

### Round 2 (2026-03-16)
Reviewed by Codex.

**P2 — Plan output channel**: Plan proposed printing first 50 lines to stderr;
spec says full contents to stdout. → Fixed: print full plan_text to stdout.

**P2 — Migration scope understated**: `plan_result → plan_results` migration
also affects `tests/test_coord_preflight.py` (lines ~1180, ~1316). → Added
to files list and migration note.

## Verification

1. `make test` — all existing + 12 new tests pass
2. `make lint` — clean
3. Manual test: `forge run specs/<small-spec>.md --interactive` with
   `plan_review.enabled: true` — verify prompt appears, each decision works
