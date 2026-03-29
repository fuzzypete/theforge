---
name: "Split large test files — max 1000 lines per file for parallel execution"
slug: split-tests
pytest_target: tests/
---

# Split Large Test Files

## Problem

pytest-xdist parallelises at the file level. When a handful of test files are
2000+ lines each, parallel execution is bottlenecked by those files — workers
sit idle while one worker grinds through a single giant file. The benefit of
parallel testing is negated.

The current state (files over 1000 lines):

| File | Lines |
|---|---|
| tests/test_runner_api.py | 2,119 |
| tests/test_coord_gate.py | 2,093 |
| tests/test_sprint.py | 2,008 |
| tests/test_coord_plan_flow.py | 1,952 |
| tests/test_coord_preflight.py | 1,920 |
| tests/test_config.py | 1,776 |
| tests/test_runner.py | 1,715 |
| tests/test_coord_review_phase.py | 1,554 |
| tests/test_task.py | 1,477 |
| tests/test_cli.py | 1,397 |
| tests/test_coord_notify.py | 1,318 |
| tests/test_coord_workspace.py | 1,244 |
| tests/test_coord_dev_phase.py | 1,213 |
| tests/test_ideate.py | 1,202 |

## Solution

Split every file over 1000 lines into focused sub-files grouped by test class
or logical concern. Each new file must be independently runnable.

**Target: no test file exceeds 1000 lines.**

### Rules

- Zero behavioural change — same tests, same assertions, new file locations
- Each new file is independently runnable: `pytest tests/test_foo_bar.py -v`
  passes in isolation
- Imports in new files point at the correct source modules directly
- Shared fixtures stay in `conftest.py` or in the source file where they are
  most used; do not duplicate fixtures across files
- Naming: `test_<module>_<concern>.py` — e.g. `test_runner_api_google.py`,
  `test_sprint_resume.py`
- `make test` passes with the same total test count before and after

### Split guidance per file

**test_runner_api.py** — split by provider adapter: openai, deepseek, google,
loop lifecycle, schema utils

**test_coord_gate.py** — split by: gate execution, dirty worktree/auto-commit,
parse files

**test_sprint.py** — split by: sprint lifecycle, resume/triage, parallel
execution, manifest parsing

**test_coord_plan_flow.py** — split by: plan generation, plan review, decompose
signal, plan injection

**test_coord_preflight.py** — split by: preflight verdict, complexity scoring,
persistent P1, escalation

**test_config.py** — split by: load_config, model registry, smart config,
profile validation

**test_runner.py** — split by: claude runner, codex runner, runner selection,
subprocess handling

**test_coord_review_phase.py** — split by: review pool execution, synthesis,
cycle management

**test_task.py** — split by: dev prompt, review prompt, plan prompt

**test_cli.py** — split by: run/sprint commands, version/init commands, daemon
commands

**test_coord_notify.py** — split by: ntfy, slack, human review, remote HITL

**test_coord_workspace.py** — split by: workspace creation, merge, stale
worktree

**test_coord_dev_phase.py** — split by: dev loop, fix iteration, handoff

**test_ideate.py** — split by: ideation flow, multi-LLM deliberation

## Acceptance Criteria

- No test file in `tests/` exceeds 1000 lines
- `make test` passes with the same total test count
- `make lint` passes
- Every new test file passes when run in isolation with
  `pytest tests/<filename> -v`
- No test logic is duplicated — each test exists in exactly one file
- Shared fixtures remain in `conftest.py` or a single canonical location
