# Plan: Universal Session Resume Across Forge Agents

Generated 2026-03-16. Reviewed by Gemini (technical audit) and Codex (contract
audit). Updated with findings from both reviews.

---

## Context

Session resume (`--resume <session_id>`) lets Claude Code continue a previous
conversation with full context intact. This is valuable everywhere agents are
invoked multiple times across a forge run — dev iterations, dev across review
cycles, and reviewers across review cycles.

Currently session resume is partially implemented for the dev agent only, and
broken in three places. This plan broadens scope to include reviewers and fixes
the existing bugs.

## Scope (post-review)

| Agent | Resume? | Rationale |
|-------|---------|-----------|
| Dev | Yes | Core use case; `build_fix_prompt` relies on resumed context |
| Reviewer | Yes | Knows what it flagged before, checks if fixed |
| Synthesis | No | Fresh reconciliation per cycle; resume contaminates with stale context (Codex P1) |
| Plan | No | Single-shot, no retry loop exists (Codex P2) |

## Current State

**What works:**
- `AgentResult.session_id` field exists (runner.py:67)
- `_run_claude()` passes `--resume <session_id>` when set (runner.py:355-356)
- `_run_claude()` extracts session_id from `type=result` JSONL event (runner.py:478)
- `CoordinatorState.dev_session_id` persists across iterations (coord_state.py:59)
- `_run_dev_phase()` passes session_id to `run_agent()` (coord_phases.py:780)

**What's broken:**
1. Timeout returns `session_id=None` (runner.py:424) — discards partial JSONL
2. No conditional session update — stale session carried or lost on timeout
3. No timeout continuation prompt — resumed agent gets full dev prompt
4. Reviewers have no session tracking — cold start every review cycle
5. `run_agent_pool()` doesn't accept or pass session_ids (runner.py:221)

## Architecture Trace

### Dev session flow (current, broken)

```
coord_phases._run_dev_phase()
  → state.dev_session_id read
  → runner.run_agent(session_id=state.dev_session_id)
    → runner._run_claude(session_id=...)
      → cmd.extend(["--resume", session_id])
      → on timeout: returns session_id=None  ← BUG
      → on success: AgentResult(session_id=result_json.get("session_id"))
  → state.dev_session_id = dev_result.session_id  ← unconditional overwrite BUG
```

### Review pool flow (current, no sessions)

```
coordinator._run_review_pool()
  → runner.run_agent_pool(prompt=..., profiles=..., working_dir=...)
    → for each profile: run_agent(...)  ← no session_id passed
    → AgentResult.session_id captured but never stored
```

## Session Lifecycle Rules

| Agent | Carry forward | Clear |
|-------|--------------|-------|
| Dev | Always when returned; on timeout (exit=-9), keep previous if new is None | Only when agent returns None (non-timeout) |
| Reviewer | Always (accumulate across cycles) | Never within a run |

## Key Design Decisions

### Filesystem fallback is dev-only
Multiple Claude reviewers run concurrently in the same `working_dir`. "Newest
.jsonl" is ambiguous — could belong to a different reviewer. Pool agents use
`fallback_to_file=False`. Dev (single agent) uses fallback with `min_mtime`
filter to avoid picking up sessions from prior runs. (Codex P1, Gemini P2)

### Timeout-resume is conditional
After VALIDATE returns RETRY_DEV, timeout-resume only overrides when
`retry_reason == "gate_fail"`. Never overrides `dirty_worktree` — process
violation guidance must be preserved. (Codex P2)

### Synthesis stays one-shot
Resuming synthesis conversation keeps earlier cycle reviews in context, biasing
or duplicating findings. The synthesis prompt is written as fresh reconciliation
of only the current cycle's reviews. (Codex P1)

## Prior Implementation Reference

Commits a01cec0..41c440b on `feat/session-resume` implemented this against the
pre-refactor monolithic coordinator.py. The logic is sound:
- `_get_claude_session_id()` helper — port with `fallback_to_file` + `min_mtime`
- `_set_timeout_resume()` — adapt for coord_phases.py with gate_fail guard
- `exit_code=-9` on timeout — port as-is
- Session lifecycle — adapt for coord_phases.py

Worktree at `.forge/worktrees/session-resume/` is stale. Implement fresh.
