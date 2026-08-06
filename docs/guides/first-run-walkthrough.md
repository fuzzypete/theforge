# First-Run Walkthrough

A narrated, phase-by-phase walkthrough of a complete forge run using the
[hello-forge example](../../examples/hello-forge/). Follow along to understand
what normal looks like before running on your own project.

## Before you start

1. Complete the [Getting Started](getting-started.md) install steps
2. Clone and initialize hello-forge:
   ```bash
   cd examples/hello-forge
   git init && git add -A && git commit -m "initial"
   ```
3. Verify providers: `forge check-providers`

---

## The command

```bash
forge run specs/add-greeting.md --verbose --fg
```

`--verbose` shows tool activity in real time. Without it you see only phase
headers and the final result — useful for unattended runs.

`--fg` keeps the run in the foreground. Runs detach by default: without
`--fg`, `forge run` returns immediately and the run continues in the
background — watch it with `forge status` and `forge logs <run-id>`. The
transcript below assumes `--fg`.

---

## Phase 1: WORKSPACE

```
[forge] ═══════════════════════════════════════════════
[forge]   Story:   Add greeting endpoint
[forge]   Slug:    add-greeting
[forge]   Config:  forge.yaml
[forge] ═══════════════════════════════════════════════

[forge] ▸ WORKSPACE   add-greeting
[forge]   create_command: git worktree add .forge/worktrees/add-greeting -b forge/add-greeting main
[forge]   Created worktree at: .forge/worktrees/add-greeting
[forge]   setup_command: pip install pytest
[forge]   Workspace ready
```

**What happened:** The coordinator created a git worktree — a lightweight clone
of your repo on a new branch `forge/add-greeting`. All changes the dev agent
makes will land on this branch, leaving your main branch untouched.

**Files created:**
```
.forge/worktrees/add-greeting/     ← full working copy on the new branch
```

---

## Phase 2: PREFLIGHT

```
[forge] ▸ PREFLIGHT   sonnet
[forge]   Checking if story is already implemented...
[forge]   Verdict: PROCEED
[forge]   Reason: src/app.py exists but has no HTTP endpoint or JSON output
```

**What happened:** A fast model scans the codebase to determine whether the
story might already be implemented. If it returns `ALREADY_DONE`, the run stops here
(cost: ~$0.05). If it returns `PROCEED`, the full pipeline continues.

**If PREFLIGHT returns ALREADY_DONE:**
```
[forge] ▸ PREFLIGHT   sonnet
[forge]   Verdict: ALREADY_DONE
[forge]   Reason: greet_json() already exists in src/app.py with JSON output
[forge] ▸ DONE        add-greeting (already done)
```
The run ends cleanly. No dev, no validation, no review. This is correct behavior
if you re-run a story after it's already been implemented and merged.

**If PREFLIGHT returns BLOCKED:**
```
[forge] ▸ PREFLIGHT   sonnet
[forge]   Verdict: BLOCKED
[forge]   Reason: acceptance criteria contradict each other about the endpoint shape
[forge] ▸ ESCALATE    add-greeting
```

This is a spec problem, not a model failure. Fix the story first, then rerun.

---

## Phase 3: DEV

```
[forge] ▸ DEV         sonnet  iter 1/3
[forge]   Sending implementation prompt (2,847 tokens)
[forge]   ↳ Read: src/app.py
[forge]   ↳ Read: tests/test_app.py
[forge]   ↳ Edit: src/app.py
[forge]     + Added greet_json(name=None) returning dict
[forge]   ↳ Write: tests/test_greet.py
[forge]     + 3 tests for greet_json
[forge]   Dev complete (1m 43s, $0.48)
```

**What happened:** The dev agent received a prompt containing the story
(acceptance criteria, problem statement) and the current codebase state. It
read relevant files, edited `src/app.py`, and wrote new tests.

**The agent operates in the worktree** — it sees `.forge/worktrees/add-greeting/`,
not your main checkout. Every change is isolated on the feature branch.

**With `--verbose`**, you see each tool call as it happens. Without it, you
see only the phase header and final cost/timing line.

---

## Phase 4: VALIDATE

```
[forge] ▸ VALIDATE    pytest
[forge]   Running: python -m pytest tests/ -q
[forge]   ...
[forge]   5 passed in 0.4s
[forge]   Gate: PASS
```

**What happened:** The coordinator ran the `gate_command` from `forge.yaml`
(`python -m pytest tests/ -q`) inside the worktree. Exit code 0 = PASS.
Non-zero = FAIL, which triggers a DEV retry.

**What a failed validation looks like:**

```
[forge] ▸ VALIDATE    pytest
[forge]   Running: python -m pytest tests/ -q
[forge]   FAILED tests/test_greet.py::test_greet_with_name - AssertionError: assert 'hello alice' == 'Hello, Alice!'
[forge]   1 failed in 0.4s
[forge]   Gate: FAIL

[forge] ▸ DEV         sonnet  iter 2/3
[forge]   Sending fix prompt with gate failure details...
```

The coordinator feeds the failure output back to the dev agent. The agent gets
up to `max_dev_iterations` attempts (default: 3) before escalating.

---

## Phase 5: REVIEW

```
[forge] ▸ REVIEW      claude-reviewer (opus)
[forge]   Sending review prompt (4,112 tokens)
[forge]   Reading: src/app.py (22 lines)
[forge]   Reading: tests/test_greet.py (18 lines)
[forge]   Review complete (2m 29s, $0.62)
[forge]   ✓ REVIEW   APPROVE  0 P1  0 P2
```

**What happened:** The reviewer received a prompt containing the story, the diff
committed to the feature branch, and the test output. It evaluated the implementation
against the acceptance criteria and returned a structured verdict.

**What APPROVE looks like:**
```
[forge]   ✓ REVIEW   APPROVE  0 P1  0 P2
```

No P1 findings. P2 findings are advisory and don't block merge.

**What REQUEST_CHANGES looks like:**

```
[forge]   ✗ REVIEW   REQUEST_CHANGES  1 P1  0 P2

[forge]   P1 findings:
[forge]     src/app.py:8 — greet_json does not handle empty string name
[forge]       suggestion: treat empty string same as None

[forge] ▸ DEV         sonnet  iter 2/3  (review cycle 1/2)
[forge]   Sending fix prompt with P1 findings...
```

A single P1 from any reviewer triggers REQUEST_CHANGES and a DEV retry. P2 findings
are included in the audit but don't trigger retries.

---

## Phase 6: DONE

```
[forge] ▸ DONE        add-greeting
[forge]   Branch: forge/add-greeting
[forge]   Duration: 5m 14s
[forge]   Cost: $1.10 total ($0.48 dev, $0.62 review)
[forge]   Audit: .forge/audits/forge_audit.yaml

[forge] ═══════════════════════════════════════════════
[forge]   APPROVE — ready to merge
[forge]   Run: git merge forge/add-greeting
[forge]   Or:  forge run stories/add-greeting.md --auto-merge
[forge] ═══════════════════════════════════════════════
```

**What to do next:**
```bash
# Inspect the implementation
git diff main forge/add-greeting

# Merge
git merge forge/add-greeting

# Or auto-merge on the next run
forge run stories/add-greeting.md --auto-merge --fg
```

---

## Escalation

When all retries are exhausted, the run escalates instead of completing:

```
[forge] ▸ ESCALATE    add-greeting
[forge]   Max dev iterations (3) reached without a PASS gate.
[forge]   Branch forge/add-greeting left in place for inspection.
[forge]   Audit: .forge/worktrees/add-greeting/forge_audit.yaml
```

Escalation is not failure — it's the coordinator telling you that human
judgment is needed. The worktree is preserved for inspection.

**Recovery:**
```bash
# Inspect what the agent attempted
cat .forge/worktrees/add-greeting/forge_audit.yaml

# Make manual fixes in the worktree
cd .forge/worktrees/add-greeting
# ... edit files manually ...

# Re-review (skip plan+dev, just review)
forge review stories/add-greeting.md --verbose

# Or clean up and start fresh
git worktree remove .forge/worktrees/add-greeting --force
git branch -D forge/add-greeting
forge run stories/add-greeting.md --verbose --fg
```

---

## Resume after interruption

If the run is interrupted (Ctrl+C, crash, timeout):

```bash
forge run stories/add-greeting.md --resume --verbose --fg
```

The coordinator detects the existing worktree state and resumes from the last
confirmed phase:

```
[forge] ▸ RESUME      add-greeting
[forge]   Found existing worktree: .forge/worktrees/add-greeting
[forge]   Last completed phase: DEV (iter 1)
[forge]   Resuming from: VALIDATE
```

See [Troubleshooting — Resume Behavior](troubleshooting.md#resume-behavior) for
the full state recovery matrix.

---

## What the audit trail contains

After any run (successful or not), inspect the full trace:

```bash
forge audit .forge/audits/forge_audit.yaml
```

Per-run JSON records live under `.forge/audits/runs/<run_id>.json`, and
`forge explain` summarizes the last run's decisions. (On ESCALATE a copy is
also written to `.forge/worktrees/<slug>/forge_audit.yaml`.)

The audit contains:
- Story name, slug, branch
- Per-phase: start/end time, cost, outcome
- Preflight verdict and reasoning
- Gate decisions (PASS/FAIL) and test output
- Review verdict, all findings (P1 and P2), reviewer breakdown
- Total duration and cost

---

## See also

- [Getting Started](getting-started.md) — full install and config walkthrough
- [CLI Reference](cli-reference.md) — all commands and flags
- [Troubleshooting](troubleshooting.md) — what to do when something goes wrong
- [hello-forge README](../../examples/hello-forge/README.md) — the example used in this walkthrough
