---
name: Session resume for dev iterations
slug: session-resume
---

# Spec: Session Resume for Dev Iterations

## Problem

When a dev agent times out (exit=-9, SIGKILL), forge discards all session
context and starts the next iteration cold. The agent re-reads files it already
read, re-discovers what it was doing, and often repeats work it already completed.
This is the primary cause of repeated 30-minute timeouts on large tasks.

Claude Code (`claude`) supports `--resume <session-id>` to continue an existing
conversation. Codex supports `codex resume <session-id>`. Session resume means
the next iteration picks up mid-thought with full context intact — dramatically
faster convergence and no repeated file reads.

## What Needs To Change

### `src/theforge/runner.py`

1. **Capture session ID from agent output.**

   After a successful or timed-out Claude run, parse the session ID from the
   agent's output. Claude Code writes its session ID to the JSONL transcript
   at `~/.claude/projects/<project-slug>/<session-id>.jsonl`. The session ID
   is also available in the agent's stdout in some invocation modes.

   Add `_get_claude_session_id(output: str, cwd: Path) -> str | None` helper
   that:
   - Checks stdout/stderr for a session ID pattern
   - Falls back to finding the most recently modified JSONL under
     `~/.claude/projects/` matching the worktree path

2. **Pass `--resume` on subsequent iterations.**

   `_run_claude()` gains an optional `session_id: str | None = None` parameter.
   When set, append `--resume {session_id}` to the claude CLI command.

3. **Codex resume support.**

   `_run_codex()` gains `session_id: str | None = None`. When set, use
   `codex resume {session_id}` as the command instead of `codex -p <prompt>`.
   Codex resume re-enters the previous session with an appended message.

4. **`RunResult` gains `session_id` field.**

   ```python
   @dataclass
   class RunResult:
       output: str
       exit_code: int
       cost_usd: float
       session_id: str | None = None  # NEW
   ```

5. **`run_agent()` passes session_id through.**

   Signature gains `session_id: str | None = None`, passes to the CLI runner,
   returns `RunResult` with `session_id` populated.

### `src/theforge/coordinator.py`

1. **Store session ID in `CoordinatorState`.**

   ```python
   dev_session_id: str | None = None  # last dev agent session ID
   ```

2. **Pass session ID into dev invocation.**

   In the dev iteration loop, pass `state.dev_session_id` to `run_agent()`.
   After each dev run, store `result.session_id` back to `state.dev_session_id`.

3. **Resume semantics on timeout.**

   If `result.exit_code == -9` (timeout) AND `result.session_id` is not None,
   the next iteration resumes the same session. The dev prompt for a resumed
   session is a short continuation message: "You were cut off by a timeout.
   Continue from where you left off. Gate result: {gate_result}."

4. **Clear session ID on clean iteration start.**

   Session ID is only carried forward on timeout (exit=-9). On a clean
   completion (exit=0), the session ID is cleared so the next review cycle
   starts fresh.

## Acceptance Criteria

1. `RunResult` has a `session_id: str | None` field.
2. `_run_claude()` accepts `session_id` and appends `--resume <id>` when set.
3. `_run_codex()` accepts `session_id` and uses `codex resume <id>` when set.
4. `CoordinatorState` has `dev_session_id: str | None`.
5. After a timeout (exit=-9), the next dev iteration passes `session_id` to
   `run_agent()`.
6. After a clean exit (exit=0), `dev_session_id` is cleared.
7. `make test` passes. `make lint` passes.
8. A coordinator test verifies that session_id is passed on timeout retry and
   cleared on clean exit.

## File Scope

(no restriction)
