---
name: "Trace artifact capture for post-mortem debugging"
slug: trace-capture
pytest_target: tests/
---

# Trace Artifact Capture

## Problem

When a forge run escalates or churns, there is no way to understand what
happened without re-running with `--verbose` and watching terminal output.
The audit log records outcomes (verdict, cost, findings) but not the raw
context: what the dev agent produced, what each reviewer actually said, what
prompt the dev received, what the gate printed.

The previous `agent-trace-artifacts` spec failed to ship because it was
scoped too broadly — it included a CLI viewer (`forge audit --trace`),
touched 11 files, and tried to solve display + capture + organization in
one pass.

This spec is capture only. Write the files. That's it.

## Requirements

1. After each agent invocation (plan, dev, reviewer, synthesis), write the
   raw agent output to a trace file in `.forge/traces/` under the worktree
2. Before each dev invocation, write the prompt that will be sent to the
   dev agent
3. After each gate run, write the gate output (stdout+stderr)
4. Trace files are named to distinguish phase, iteration, and cycle — no
   overwrites across multiple passes. Exception: `plan.txt` is always the
   most recent accepted plan (overwrite on regen is acceptable since only
   the approved plan matters downstream).
5. Trace writing is best-effort — failures log a warning, never crash or
   block the pipeline
6. Trace files are not committed to git
7. No CLI viewer, no config knob, no forge.yaml changes — just file writes

## Acceptance Criteria

- [ ] Dev prompt written to `.forge/traces/{iter}-dev-prompt.txt` before
      each dev invocation
- [ ] Dev agent output written to `.forge/traces/{iter}-dev-output.txt`
      after each dev invocation
- [ ] Each reviewer's raw output written to
      `.forge/traces/{cycle}-review-{profile}.txt` per review cycle
- [ ] Synthesis output written to `.forge/traces/{cycle}-synthesis.txt`
      per review cycle
- [ ] Plan output written to `.forge/traces/plan.txt` when PLAN phase runs
- [ ] Gate output written to `.forge/traces/{iter}-gate.txt` after each
      gate run
- [ ] Trace write failures are caught and logged as warnings
- [ ] `.forge/traces/` is gitignored (or traces are written outside the
      git tree)
- [ ] All existing tests pass
- [ ] No new CLI commands, no forge.yaml schema changes

## Out of Scope

- `forge audit --trace` CLI viewer — separate story after capture works
- Configurable trace verbosity (`audit.level`) — future story
- Trace rotation or cleanup — worktrees are ephemeral, traces die with them
- Changing the audit YAML format — audit and traces are independent
