---
name: "Agent trace artifacts in worktree"
slug: agent-trace-artifacts
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/runner.py
  - src/theforge/trace.py
  - src/theforge/cli.py
  - src/theforge/coord_gate.py
  - src/theforge/coord_phases.py
  - src/theforge/coord_workspace.py
  - src/theforge/coord_util.py
  - src/theforge/sprint.py
  - tests/test_coordinator.py
  - tests/test_runner.py
pytest_target: tests/
---

# Agent Trace Artifacts

## Problem

When a story doesn't finish review — REQUEST_CHANGES loops, persistent P1s,
budget exhaustion — there's no way to understand WHY after the fact. The audit
log records what happened (verdict, cost, findings summary) but not the full
context needed to diagnose root causes:

- What did the dev agent actually produce? (raw output, not just "Done.")
- What did each reviewer actually say? (full review text, not just parsed P1s)
- What was the synthesis reasoning? (why did it pick REQUEST_CHANGES?)
- What did the plan agent recommend? (was the plan wrong, or did dev ignore it?)
- What was the gate output? (did tests fail? which ones? what error?)

Today, debugging a failed run means: read the audit summary, guess what went
wrong, maybe re-run with `--verbose` and watch the terminal. This is the same
manual reconstruction problem TheForge was built to eliminate.

## Requirements

1. After each agent invocation (plan, dev, each reviewer, synthesis), write
   the full agent output to a trace file in the worktree
2. After each gate run, write the full gate output (stdout + stderr) to a
   trace file
3. Trace files are organized by phase and iteration so multiple dev passes
   and review cycles don't overwrite each other
4. The dev prompt sent to the agent is also preserved (so you can see exactly
   what the agent was asked to do)
5. Trace files survive in the worktree after the run completes — they're
   available for post-mortem even if the run escalated or failed
6. `forge audit` can optionally display trace content for a specific phase
   (e.g., "show me what reviewer-codex said in cycle 2")
7. Trace writing is best-effort — failures to write traces never block or
   crash the pipeline
8. Traces are not committed to git — they're local debugging artifacts in
   the worktree only

## Acceptance Criteria

- [ ] Dev agent output written to worktree trace file after each iteration
- [ ] Each reviewer's raw output written to separate trace files per cycle
- [ ] Synthesis output written to trace file per review cycle
- [ ] Plan agent output written to trace file (when PLAN runs)
- [ ] Gate stdout+stderr written to trace file after each gate run
- [ ] Dev prompt written to trace file before each dev invocation
- [ ] Trace files named to distinguish iterations and cycles (no overwrites)
- [ ] `forge audit <file> --trace <phase>` displays the relevant trace content
- [ ] Trace write failures are logged as warnings, never crash the run
- [ ] Traces are .gitignored or in a directory that's not committed
- [ ] Existing tests pass unchanged
