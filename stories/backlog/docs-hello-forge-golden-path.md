---
name: Upgrade hello-forge example to a golden-path confidence builder
slug: docs-hello-forge-golden-path
pytest_target: tests/
---

# Upgrade hello-forge to a Golden-Path Confidence Builder

## Problem

The hello-forge example is directionally good but not self-contained enough to
be a true confidence builder. A user should be able to run one command and
compare their result against a known-good transcript. That's a huge trust
accelerant that the current example doesn't deliver.

## Acceptance criteria

- The hello-forge directory is fully self-contained:
  - Includes a tiny app (src/app.py or equivalent) already committed
  - Includes a minimal test suite that passes before forge runs
  - Includes a minimal forge.yaml that works with a single Claude CLI
  - Includes one story (specs/add-greeting.md) as the canonical first run
- hello-forge/README.md documents:
  - Prerequisites (Python version, Claude CLI authenticated, TheForge installed)
  - Exact command to run
  - What phase messages the user should see (with example console output)
  - What files should change after a successful run
  - How to verify success
  - How to clean/reset and rerun
- An EXPECTED_OUTPUT.md file shows a realistic terminal transcript of a
  successful run, phase by phase
- The example does not require the user to run git init or scaffold files —
  it's ready to go after cloning the repo (minus provider auth)
