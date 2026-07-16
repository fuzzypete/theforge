---
description: TheForge dev-phase execution via GitHub Agentic Workflows (ADR-0004 spike)
on:
  workflow_dispatch:
    inputs:
      dispatch_id:
        description: "Correlation id issued by the forge coordinator"
        required: true
      story_ref:
        description: "Issue number or story reference (informational)"
        required: false
        default: ""
      prompt:
        description: "Dev-phase prompt built by the forge coordinator"
        required: true
run-name: "forge-dev-ghaw ${{ inputs.dispatch_id }}"
engine: copilot
timeout-minutes: 20
permissions:
  contents: read
  issues: read
  pull-requests: read
safe-outputs:
  upload-artifact:
    allowed-paths:
      - "forge-output/**"
  create-pull-request:
    title-prefix: "[ghaw-spike] "
    labels: [enhancement]
    draft: true
---

# TheForge dev phase (gh-aw execution substrate spike)

You are a development agent executing one dev phase of a TheForge story.
The coordinator dispatched this run; it will collect your output from the
run artifacts. Story reference: `${{ inputs.story_ref }}`.

## Task

${{ inputs.prompt }}

## Output contract

1. Implement the requested change in the repository working copy.
2. Propose the change as a pull request (the safe-outputs pipeline will
   create it as a draft).
3. Write your final handoff to `forge-output/handoff.md` and upload it as
   an artifact. The handoff must contain: what you changed and why, files
   touched, how you verified the change, and any concerns a reviewer
   should weigh. This artifact is the coordinator's capture surface —
   without it the run counts as a capture-fidelity failure.
