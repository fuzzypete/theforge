---
name: "Session resume salvage on main"
slug: session-resume-mainline-salvage
file_scope:
  - src/theforge/runner.py
  - tests/test_runner.py
pytest_target: tests/test_runner.py
---

# Session Resume Salvage on `main`

## Problem

The old `feat/session-resume` branch contains real follow-up work, but it no
longer applies cleanly to current `main`.

Since that branch diverged, `main` has already absorbed most of the useful
session-resume behavior:

- dev timeout-resume routing
- reviewer session carry-forward
- coordinator-side timeout-resume tests

Attempting to merge or cherry-pick the branch now causes broad conflicts in
`coordinator.py`, `runner.py`, and their tests. The remaining useful behavior
should be re-applied directly on top of current `main` instead of merged from
the stale branch.

## Intent

Capture the remaining value from `feat/session-resume` and implement it cleanly
on `main`, with the smallest possible surface area.

This is a salvage spec, not a branch-integration spec.

## What Still Appears Missing on `main`

### P1: Codex resume path is still not wired

Current `main` accepts `session_id` in the runner API, but `_run_codex()`
still always launches a fresh:

```bash
npx @openai/codex exec --full-auto ...
```

That means resumed dev iterations can carry `state.dev_session_id` correctly in
the coordinator, but the Codex runner does not actually use it.

### P2: Test coverage for Codex resume is missing

Current tests cover Claude timeout/session extraction and coordinator resume
behavior, but they do not prove that Codex switches to a resume command when a
session id is present.

## Non-Goals

- Merging `feat/session-resume` wholesale
- Re-introducing coordinator changes already present on `main`
- Refactoring the session-resume architecture further
- Persisting sessions across separate CLI invocations
- Changing Gemini behavior

## Design

### 1. Implement Codex resume in `runner.py`

When `_run_codex()` receives `session_id`, it should resume the existing Codex
conversation instead of starting a fresh `exec` run.

Expected behavior:

- If `session_id is None`: keep existing `npx @openai/codex exec --full-auto ...`
- If `session_id` is set: invoke Codex resume mode instead

The salvage target from the old branch is:

```bash
codex resume <session_id>
```

The implementation should confirm the repo's current Codex invocation patterns
and preserve existing output/error handling as much as possible.

### 2. Keep the change narrowly scoped

Do not port the old coordinator diffs from `feat/session-resume`. Those ideas
already exist on `main` in a newer shape.

The only functional code change expected from this salvage spec is the missing
runner integration for Codex resume.

### 3. Add targeted tests

Add runner tests that verify:

- fresh Codex runs still use `npx @openai/codex exec ...`
- resumed Codex runs use the resume command when `session_id` is provided
- the resume path does not append the prompt as a positional argument

Tests should stay local to `tests/test_runner.py`.

## Acceptance Criteria

- [ ] `_run_codex()` uses resume mode when `session_id` is provided
- [ ] `_run_codex()` preserves existing fresh-run behavior when `session_id` is `None`
- [ ] No coordinator files are changed as part of this salvage
- [ ] `tests/test_runner.py` includes coverage for Codex resume behavior
- [ ] Existing runner tests still pass

## Test Expectations

In `tests/test_runner.py`:

- add `test_codex_resume_command_structure`
- verify the resume invocation uses the provided `session_id`
- verify the resume invocation does not pass the prompt as a trailing CLI arg
- keep existing fresh-run tests unchanged unless they need minor fixture updates

## Reference

The old branch `feat/session-resume` is useful as a source of intent, but not
as a merge target.

Relevant branch-only commit chain:

- `a01cec0` — initial session-resume implementation
- `64d5963` — review follow-up
- `41c440b` — review follow-up

Use those commits for comparison only. Re-implement the missing behavior
directly on top of current `main`.
