# Flake Register

A flaky gate is a direct trust violation for a project whose thesis is
trustworthy gates. A red-when-it-should-be-green (or green-when-it-should-be-red)
gate is the gate lying, and it has teeth: on 2026-07-17 a flaky `gate (3.13)`
failure blocked a PR, a re-run passed, and armed auto-merge then landed
out-of-scope code (#1717) — flake + auto-merge turned "blocked pending decision"
into "merged on the second coin flip."

This register is the visible ledger of every known-flaky test. **The point is
burndown, not tolerance.** A flake that isn't tracked rots; a flake that is
retried into invisibility trains everyone to ignore red. See
[the flake discipline in CONVENTIONS.md](../CONVENTIONS.md#flake-discipline)
for the rules that govern entries here.

## How this register works

- **A flake is fixed by removing nondeterminism, not by retrying.** Retrying a
  flaky test masks it and is worse than the flake itself.
- **Every known flake lives here** with a test id, symptom, suspected cause, and
  an owning issue, until it has a determinism fix or is deliberately retired.
- **A green-on-re-run after a same-SHA red is a flake signal, not a pass.** When
  a gate goes red and a plain re-run of the same commit goes green, the test that
  flipped is a flake candidate — add it here rather than merging on the green.

## Status legend

- **fixed** — nondeterminism removed; kept in the register for provenance until
  a clean burndown window confirms it stays green.
- **open** — reproduces or can reproduce; has an owning issue and awaits a
  determinism fix.
- **absent** — the flaky code is not currently in the tree (e.g. reverted); the
  entry is a forward-looking guard so the flake is re-registered, not
  rediscovered, if the code returns.

## Register

### 1. `test_process_group.py::test_kill_process_group_reaps_the_whole_tree`

- **Status:** absent
- **Symptom:** Process-reap timing race — the test asserts the whole process
  tree is reaped, but the reap has not always completed when the assertion runs,
  producing intermittent `gate (3.13)` failures.
- **Suspected cause:** The test synchronizes on a schedule/timing assumption
  rather than on the actual "all descendants are gone" condition, so under load
  (notably 3.13 in the CI matrix) the reap lags the assertion.
- **Owning issue:** #1649 (process-group isolation, held for v0.12).
- **Notes:** The `process_group.py` implementation and this test were reverted
  by #1719 as out-of-scope for rc1, so neither is in the tree today. When #1649
  reintroduces subprocess isolation, the reaper test **must synchronize on the
  observed absence of every child PID** (poll until no descendant remains, with
  a bounded timeout) instead of sleeping or assuming the reap has finished.

### 2. `test_apply_branch_protection.py` — fake-`gh` SIGPIPE race

- **Status:** open
- **Symptom:** Intermittent failure of the branch-protection tests when the
  script under test pipes the protection body into the PATH-mocked `gh`.
- **Suspected cause:** SIGPIPE race between the writer (`echo "$BODY" | gh`) and
  the fake `gh` — exit paths in the fake that do not drain stdin can leave the
  writing side of the pipe receiving SIGPIPE, and `set -o pipefail` then fails
  the pipeline nondeterministically.
- **Owning issue:** #1722 (this register); burndown tracked here.
- **Determinism fix applied:** The fake `gh` now drains stdin on **every** exit
  path (`tests/test_apply_branch_protection.py`), so the writer never races a
  reader that exits without reading. This removes the SIGPIPE window rather than
  papering over it with a gate-timeout bump.

### 3. Gate-timeout contention under `--parallel 3`

- **Status:** open
- **Symptom:** `make gate` occasionally times out under `forge sprint --parallel 3`
  on a 10-core host, then passes instantly when the same commit is resumed solo
  (observed for #1243, twice; worked around by bumping `gate_timeout` 45 → 60 in
  `forge.yaml`).
- **Suspected cause:** Aggregate gate CPU demand exceeds host capacity when three
  gates run concurrently on ~10 cores, so wall-clock gate time crosses the
  timeout even though the tests themselves are green. This is a scheduling
  race, not a per-test race — the "flake" is the timeout, not an assertion.
- **Owning issue:** #1722 (this register); burndown tracked here.
- **Notes:** The current mitigation (`gate_timeout: 60`) is a timeout bump, not a
  determinism fix. A real fix removes the contention rather than widening the
  window: cap concurrent gate CPU (e.g. bound `-n auto` worker count per gate, or
  scale `--parallel` to `cores / gate_worker_budget`) so N parallel gates cannot
  oversubscribe the host. Until then, do **not** raise `gate_timeout` further to
  chase this — a wider timeout hides the contention instead of removing it.
