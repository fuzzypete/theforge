---
name: Handoff integrity — verify and supplement dev self-reports
slug: handoff-integrity
pytest_target: tests/
---

# Handoff Integrity — Verify and Supplement Dev Self-Reports

## Problem

The dev agent writes handoff.yaml as a self-report: "here's what I did, here
are my commits, here's how I met each AC." The coordinator passes this to
reviewers as-is with no verification. Reviewers treat it as ground truth.

In a recent sprint, Sonnet wrote a handoff describing work from a completely
different story — four bug fixes to coord_state.py/task.py/coord_audit.py —
while the actual commits on the branch were terminology consistency edits
across 10 doc files. All four reviewers read the hallucinated handoff, concluded
"dev worked on the wrong story," and gave P1s. The actual implementation was
correct.

The handoff is the dev agent's voice in the review. When it lies, the entire
review cycle is poisoned. The agent did the work; it just described the wrong
work.

## Acceptance criteria

- After the dev agent finishes, the coordinator captures ground-truth metadata
  from git before passing anything to reviewers:
  - `git log --oneline main..HEAD` (actual commits)
  - `git diff main --stat` (actual files changed)
  - `git diff main` (actual diff content, or a summary if too large)
- The coordinator cross-checks the handoff's `commits` list against the actual
  `git log` output. If the handoff claims commits that don't exist on the
  branch, or omits commits that do exist, a warning is logged
- The review prompt includes the ground-truth git metadata (commit log +
  diff stat) as a separate section from the dev's self-reported handoff, so
  reviewers can distinguish verified facts from agent claims
- If the handoff is entirely missing or unparseable, the review still
  proceeds using the ground-truth git metadata alone — a bad handoff does
  not block the pipeline
- Existing tests pass
- The dev prompt is unchanged — the agent still writes handoff.yaml as before
