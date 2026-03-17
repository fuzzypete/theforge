---
name: Universal session resume across all forge agents
slug: session-resume
---

# Spec: Universal Session Resume

## Problem

When forge agents run multiple times across a task (dev iterations, review
cycles), each invocation starts cold. The agent re-reads files, re-discovers
context, and repeats work. This is especially costly on timeout recovery where
the agent was killed mid-thought.

Claude Code supports `--resume <session-id>` to continue an existing
conversation with full context. Session resume should work for dev and
reviewer agents — the two roles that are invoked multiple times across a
forge run.

## Scope

| Agent | Session resume | Rationale |
|-------|---------------|-----------|
| Dev | Yes — across iterations and review cycles | Core use case; `build_fix_prompt` relies on resumed context |
| Reviewer | Yes — across review cycles | Reviewer knows what it flagged before, checks if fixed |
| Synthesis | **No** — one-shot per cycle | Fresh reconciliation by design; resuming contaminates with stale cycle context |
| Plan | **No** — single-shot, no retry loop | No multi-invocation scenario exists today |

## What Needs To Change

### 1. `src/theforge/runner.py` — Fix timeout session extraction

Add `_get_claude_session_id()` helper:
```python
def _get_claude_session_id(
    output: str,
    cwd: Path,
    *,
    fallback_to_file: bool = True,   # False for pool agents
    min_mtime: float | None = None,  # monotonic filter for dev fallback
) -> str | None:
```
- Primary: scan JSONL lines for any event containing `session_id` field
- Fallback (dev only, `fallback_to_file=True`): newest `.jsonl` under
  `~/.claude/projects/<project-slug>/` with `mtime > min_mtime`
  (Claude encodes cwd by replacing `/` with `-`)
- Pool agents call with `fallback_to_file=False` to avoid cross-contamination
  when multiple reviewers run in the same `working_dir` concurrently

Fix the timeout return block (currently lines 423-432):
- Join collected `lines` and pass through `_get_claude_session_id()`
- Change `session_id=None` → `session_id=_get_claude_session_id(partial, working_dir)`
- Change `exit_code=-1` → `exit_code=-9` to distinguish timeout from other failures

Fix the no-result-json fallback (currently lines 460-468):
- Also extract session_id via `_get_claude_session_id()` when `type=result`
  event is missing but other JSONL events may contain it

Add `session_ids` parameter to `run_agent_pool()`:
```python
def run_agent_pool(
    *,
    prompt: str | list[str],
    profiles: list[ModelProfile],
    working_dir: Path,
    session_ids: list[str | None] | None = None,  # NEW
) -> list[AgentResult]:
```
When provided, each agent gets its corresponding session_id via `run_agent()`.
Default `None` means no resume (backward compatible). When a list, must
satisfy `assert len(session_ids) == len(profiles)`.

### 2. `src/theforge/coord_state.py` — Reviewer session tracking

Add field to `CoordinatorState`:
```python
reviewer_session_ids: dict[str, str] = field(default_factory=dict)  # keyed by profile.name
```

Update `retry_reason` docstring to include `"timeout_resume"` as a valid value.

### 3. `src/theforge/coord_phases.py` — Dev session lifecycle

Replace unconditional session update (currently line 785):
```python
if dev_result.exit_code == -9:
    # Timeout: carry forward session_id for resume (prefer new, fallback to previous)
    state.dev_session_id = dev_result.session_id or state.dev_session_id
else:
    # Normal exit: use whatever agent returned (may be None)
    state.dev_session_id = dev_result.session_id
```

Add timeout_resume prompt routing (insert before existing retry_reason check
at line 749):
```python
if state.retry_reason == "timeout_resume":
    prompt = state.human_feedback or (
        "You were cut off by a timeout. Continue from where you left off."
    )
    state.retry_reason = None
    state.human_feedback = None
elif state.retry_reason in ("review_changes", "extend") and state.last_review_findings:
    # ... existing build_fix_prompt path
```

After `_run_validate_phase()` returns `RETRY_DEV`, conditionally override
retry_reason — **only when existing reason is `gate_fail`**, never
`dirty_worktree` (process violation guidance must not be erased):
```python
if (
    state.dev_results
    and state.dev_results[-1].exit_code == -9
    and state.dev_session_id
    and state.retry_reason == "gate_fail"  # only override gate_fail
):
    state.retry_reason = "timeout_resume"
    state.human_feedback = (
        "You were cut off by a timeout. Continue from where you left off. "
        f"Gate result: {gate_info}"
    )
```

### 4. `src/theforge/coordinator.py` — Review pool session tracking

In `_run_review_pool()`, pass session_ids from state to `run_agent_pool()`:
```python
pool_session_ids = [
    state.reviewer_session_ids.get(p.name)
    for p in config.review_pool
]
pool_results = run_agent_pool(
    prompt=review_prompts,
    profiles=config.review_pool,
    working_dir=workspace_path,
    session_ids=pool_session_ids,
)
```

After pool completes, update stored session_ids:
```python
for profile, result in zip(config.review_pool, pool_results):
    if result.session_id:
        state.reviewer_session_ids[profile.name] = result.session_id
```

### 5. Codex/Gemini — No changes

Codex and Gemini CLIs do not support session resume. Their `session_id`
parameters are accepted but unused. They return `session_id=None`. This is
fine — the coordinator handles None gracefully (no resume attempted).

## Session Lifecycle Rules

| Agent | Carry forward | Clear |
|-------|--------------|-------|
| Dev | Always when returned; on timeout (exit=-9), keep previous if new is None | Only when agent returns None (non-timeout) |
| Reviewer | Always (accumulate across cycles) | Never within a run |

## Acceptance Criteria

1. `_get_claude_session_id()` extracts session_id from partial JSONL output
2. `_get_claude_session_id()` has dev-only filesystem fallback filtered by run start time
3. `_get_claude_session_id()` with `fallback_to_file=False` skips filesystem (pool safety)
4. Timeout returns `exit_code=-9` and populated `session_id`
5. No-result-json fallback also extracts session_id
6. `run_agent_pool()` accepts and passes `session_ids` parameter
7. `run_agent_pool()` asserts `len(session_ids) == len(profiles)` when provided
8. `CoordinatorState` has `reviewer_session_ids` dict
9. Dev session carried on timeout (exit=-9), uses previous if new is None
10. Dev session uses agent's returned session_id on normal exit
11. Timeout retry uses `retry_reason="timeout_resume"` with short continuation prompt
12. Timeout-resume only overrides `gate_fail`, never `dirty_worktree`
13. Review pool passes stored session_ids per reviewer
14. Reviewer session_ids accumulate across review cycles
15. `make test` passes — all existing tests + new tests
16. `make lint` passes

## Test Requirements

### `tests/test_runner.py`
- `test_timeout_returns_session_id` — partial JSONL with session_id on timeout
- `test_timeout_exit_code_is_minus_9` — verify exit code distinction
- `test_get_claude_session_id_from_jsonl` — unit test the helper
- `test_get_claude_session_id_fallback_to_file` — filesystem fallback with mtime filter
- `test_get_claude_session_id_no_fallback_for_pool` — `fallback_to_file=False` skips disk
- `test_pool_passes_session_ids` — verify pool passes per-agent session_ids
- `test_pool_session_ids_length_mismatch` — assert on length mismatch

### `tests/test_coordinator.py`
- `test_dev_session_carried_on_timeout` — exit=-9 preserves session_id
- `test_dev_session_carried_across_review_cycles` — session persists
- `test_timeout_resume_uses_short_prompt` — continuation prompt, not full dev prompt
- `test_timeout_resume_only_overrides_gate_fail` — dirty_worktree not clobbered
- `test_reviewer_sessions_accumulate` — reviewer session_ids tracked across cycles
- `test_reviewer_sessions_passed_to_pool` — session_ids flow to run_agent_pool

## Reference: Prior Implementation

Commits a01cec0..41c440b on `feat/session-resume` contain a working
implementation against the pre-refactor monolithic coordinator.py. The logic
is correct but targets the wrong code structure. Key patterns to reuse:

- `_get_claude_session_id()` helper (runner.py) — port with added `fallback_to_file` and `min_mtime` params
- `_set_timeout_resume()` helper (coordinator.py) — adapt for coord_phases.py with gate_fail guard
- exit_code=-9 on timeout (runner.py) — port as-is
- Session lifecycle in dev loop (coordinator.py) — adapt for coord_phases.py

The worktree at `.forge/worktrees/session-resume/` contains this code but is
stale (pre-refactor). Do not rebase — implement fresh against current main.

## Review History

### Round 1 (2026-03-16)
Reviewed by Gemini (technical audit) and Codex (contract audit).

**Codex P1 — Synthesis contamination**: Synthesis is fresh reconciliation per
cycle. Resuming prior session bleeds earlier findings. → Dropped from scope.

**Codex P1 — Transcript fallback race**: Parallel reviewers in same working_dir
make "newest .jsonl" ambiguous. → `fallback_to_file=False` for pool agents.

**Codex P2 — Timeout override clobbering**: Blanket override erases dirty_worktree
guidance. → Only override when `retry_reason == "gate_fail"`.

**Codex P2 — Plan has no retry loop**: `plan_session_id` is dead weight. → Dropped.

**Gemini P2 — Temporal determinism**: Filesystem fallback could pick up sessions
from prior runs. → `min_mtime` filter based on run start time.

**Gemini suggestion — Pool validation**: → Assert length match in `run_agent_pool`.
