---
name: "LLM-assisted merge conflict resolution"
slug: llm-merge-resolution
github_issue: 145
pytest_target: tests/
---

# LLM-Assisted Merge Conflict Resolution

## Problem

When parallel stories or sequential auto-merges produce git conflicts, the
coordinator fails and the story is marked as "merge failed." Human must
manually resolve. For parallel sprints this is the primary bottleneck — stories
complete independently but can't land because they touched overlapping files.

## Solution

When auto-merge fails with conflicts, send the conflict to an LLM for
resolution:

1. Extract conflict hunks from the failed merge
2. Gather context: both stories' specs, both branches' commit messages
3. Send to a strong model: "Resolve this merge conflict preserving both
   changes' intent"
4. Apply the resolution
5. Run the gate (tests) to verify the resolution is correct
6. If gate passes → commit and continue
7. If gate fails → escalate to human with the LLM's attempt and explanation

### Cost controls

- Only attempt LLM resolution for conflicts under N hunks (configurable)
- Use a budget cap per resolution attempt
- Track resolution success rate in telemetry

### Safety

- Gate MUST pass after LLM resolution — no blind merges
- If the LLM can't resolve (gives up, produces invalid output), fall through
  to human escalation
- Conflict context + resolution stored in audit for post-mortem

## Acceptance criteria

- Merge conflicts auto-detected after failed git merge
- Conflict hunks + story context sent to configurable model
- LLM resolution applied and gate-verified before committing
- Failed gate after resolution → escalate to human
- Resolution cost tracked in telemetry
- Configurable max conflict size (skip LLM for massive conflicts)
- All existing tests pass
- New test: simulated conflict → LLM resolution → gate pass → merge
