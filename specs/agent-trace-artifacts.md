---
name: "Agent trace artifacts"
slug: agent-trace-artifacts
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/runner.py
  - src/theforge/sprint.py
  - .gitignore
pytest_target: tests/
---

# Agent Trace Artifacts

## Problem

When a sprint stalls — a story cycling through 3+ review iterations without
converging — there is no way to understand why after the fact. The audit YAML
records verdicts and costs. The event log records transitions. Neither records
what actually flowed between agents.

To diagnose convergence failure you need to answer:
- Did the dev receive the reviewer's feedback in the next iteration's prompt?
- Did two reviewers contradict each other, leaving the dev with no clear fix?
- Did the dev acknowledge the P1 finding but implement something different?
- Did the gate handoff describe a different problem than the reviewer flagged?

Without the actual content that passed through each handoff, these questions
are unanswerable. You're left re-running the story and watching it fail again.

## Requirements

1. For each story run, the worktree contains a trace directory with one file
   per agent invocation capturing the prompt sent and the raw output received
2. Gate handoff content is captured after each gate execution
3. The feedback text passed back to the dev at the start of each repair
   iteration is captured (this is what the dev actually sees, not the raw
   review output)
4. Trace files are written incrementally — each file appears as soon as that
   agent invocation completes, not only at the end of the run
5. Trace artifacts are ignored by git and do not appear as worktree dirt
6. Trace capture is unconditional — no config flag required, no verbose mode
   required. Visibility into data flow should not be opt-in.
7. Sprint runs produce traces for every story, in each story's own worktree

## Acceptance Criteria

- [ ] After `forge run`, the worktree contains `.forge/traces/` with at least
      one file per agent invocation that occurred
- [ ] The dev prompt file for iteration N contains the full prompt text sent
      to the dev agent, including any repair feedback from the prior review cycle
- [ ] The dev output file for iteration N contains the raw text the dev agent
      produced
- [ ] The gate handoff file for iteration N contains the handoff.yaml content
      written after that iteration's gate ran
- [ ] A reviewer output file exists for each reviewer that produced output in
      each review cycle (named to identify both the reviewer and the cycle)
- [ ] The repair feedback file for each dev iteration (after cycle 1) contains
      the exact text passed back to the dev as "here is what to fix"
- [ ] All trace files are present even when the story escalates or fails
- [ ] `.forge/traces/` does not appear in `git status` output in the worktree
- [ ] After `forge sprint`, each story's worktree has its own `.forge/traces/`
- [ ] A story that was ALREADY_DONE produces no trace directory (no agents ran)

## Out of Scope

- Surfacing trace content in `forge audit` display (separate story)
- Compressing or rotating old traces
- Capturing plan agent output (plan traces can follow in a later story)
- Capturing preflight agent output
