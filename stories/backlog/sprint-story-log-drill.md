---
name: "forge logs --story: drill into a specific story's log during a sprint"
slug: sprint-story-log-drill
pytest_target: tests/
---

# forge logs --story: per-story log access during a sprint

## Problem

During a running sprint, `forge logs <run_id>` tails the sprint-level log —
an interleaved stream across all parallel stories. There is no way to focus on
a single story. Finding the per-story `run-*.log` requires knowing the nested
path (`.forge/logs/<sprint-name>/<slug>/run-<id>.log`), and that path is not
shown anywhere in the sprint output.

## Expected behavior

- `forge logs <run_id> --story <slug>` tails the most recent `run-*.log` for
  that story under the sprint's log directory
- If the story has not started yet, print a clear message and wait (like `tail -f`)
- `forge logs <run_id> --story` (no slug) lists the stories in the sprint and
  their current phase, so the user can pick one

## Notes

Each story already writes `run-<id>.log` into `state.log_dir`. The sprint log
dir is `.forge/logs/<sprint-manifest-slug>/` for the top-level log, but story
artifacts land under `.forge/logs/<sprint-name>/<slug>/`. The slug-vs-name
discrepancy in the path is a separate issue.
