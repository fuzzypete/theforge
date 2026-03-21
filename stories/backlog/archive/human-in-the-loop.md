---
name: "Human-in-the-loop: interactive review and escalation"
slug: human-in-the-loop
file_scope:
  - src/theforge/cli.py
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Human-in-the-Loop: Interactive Review and Escalation

## Problem

When theforge finishes a review cycle, the result is either DONE
(auto-merged, no human check) or ESCALATE (dead end). In practice,
the human operator always wants to review the output before it lands on
main. The current flow has no way to pause and ask the human.

## Requirements

### R1: New `--interactive` / `--auto` flags on `forge run`

- `forge run specs/foo.md` → default behavior: **interactive** (pauses
  for human review)
- `forge run specs/foo.md --auto` → current behavior (no pause, used
  for CI or unattended runs)
- Add the `--interactive` / `--auto` flag to the argparse `run` subcommand
- Pass a boolean `interactive: bool` through to `run_task()`

### R2: `HUMAN_REVIEW` phase in the coordinator

Add a new phase to the Phase enum:

```python
class Phase(Enum):
    INIT = auto()
    WORKSPACE = auto()
    DEV = auto()
    VALIDATE = auto()
    REVIEW = auto()
    HUMAN_REVIEW = auto()   # ← new
    DONE = auto()
    ESCALATE = auto()
```

After REVIEW returns APPROVE, transition to HUMAN_REVIEW instead of DONE
(when interactive=True). After REVIEW returns REQUEST_CHANGES and all
cycles are exhausted, also go to HUMAN_REVIEW instead of ESCALATE (when
interactive=True).

### R3: Human review prompt and input

When entering HUMAN_REVIEW, print to stderr:
1. The review verdict and summary
2. The P1/P2 finding count
3. The workspace path and branch name
4. The total cost so far

Then prompt on stderr and read from stdin:

```
[forge] ─── Human Review ───
[forge]   Verdict:   APPROVE (3 P2)
[forge]   Summary:   Solid implementation with minor style issues.
[forge]   Workspace: .forge/worktrees/multi-cli
[forge]   Branch:    feat/multi-cli
[forge]   Cost:      $1.234
[forge]
[forge] Options:
[forge]   [a]pprove  → DONE (ready to merge)
[forge]   [r]eject   → send findings back to dev
[forge]   [e]scalate → give up
[forge]
[forge] Choice [a/r/e]:
```

- `a` or `approve` → transition to DONE
- `r` or `reject` → prompt for findings text (multiline, end with
  empty line), then transition back to DEV with the findings as
  `human_feedback`
- `e` or `escalate` → transition to ESCALATE

### R4: Human findings input

When the human chooses `reject`, prompt for findings:

```
[forge] Enter your findings (empty line to finish):
>
```

Read lines from stdin until an empty line. Join them as the feedback
string and set `state.human_feedback` before looping back to DEV.

### R5: `run_task()` signature change

```python
def run_task(config: ForgeConfig, task: TaskSpec, *, interactive: bool = False) -> CoordinatorResult:
```

When `interactive=False`, the behavior is unchanged (current auto mode).
When `interactive=True`, HUMAN_REVIEW gates the transition.

### R6: Non-interactive escalation still works

In `--auto` mode, the current ESCALATE behavior is preserved exactly.
HUMAN_REVIEW is never entered.

### R7: Tests

Add to `tests/test_coordinator.py`:

- `TestCoordinatorHumanReview` class:
  - `test_interactive_approve`: mock stdin with "a\n", verify DONE
  - `test_interactive_reject_loops_back`: mock stdin with "r\nfix the
    bug\n\n", verify dev is called again with human_feedback set
  - `test_interactive_escalate`: mock stdin with "e\n", verify ESCALATE
  - `test_auto_mode_skips_human_review`: interactive=False, verify
    HUMAN_REVIEW phase is never entered
  - `test_interactive_on_exhausted_cycles`: review exhausts cycles,
    human gets to choose instead of auto-escalate

Tests should mock `sys.stdin` and `builtins.input` as needed. Do NOT
use `input()` — use `sys.stdin.readline()` so it can be mocked cleanly.

### R8: Audit log includes human review

When HUMAN_REVIEW occurs, record in the audit log:
- `human_review.decision`: "approve" | "reject" | "escalate"
- `human_review.feedback`: the rejection findings text (if any)

## Out of scope

- Web dashboard or UI for human review
- Timeout on human input (human can take as long as they want)
- Multiple human review rounds (one round per cycle is enough)

## Acceptance criteria

1. `forge run specs/foo.md` pauses after review for human input
2. `forge run specs/foo.md --auto` behaves exactly as before
3. Human can approve, reject with findings, or escalate
4. Rejection sends findings back to dev agent as context
5. All existing tests pass unchanged
6. New tests cover all human review paths
