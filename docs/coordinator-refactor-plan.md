# Coordinator Refactor — Comprehensive Plan

Generated 2026-03-15. Covers the full refactoring scope: coordinator split,
code dedup, type safety, and structural cleanup.

---

## Scope

This isn't just "split coordinator.py into files." There are six workstreams:

| # | Workstream | Why |
|---|-----------|-----|
| 1 | **Coordinator module split** | 3,166-line file → merge conflicts on every sprint |
| 2 | **Test file split** | 7,555-line test file → same problem, blocks parallel test dev |
| 3 | **Code deduplication** | Merge block 3x, frontmatter 2x, manifest load 2x |
| 4 | **Missing type abstractions** | Raw tuples for GateResult, WorktreeInfo → unclear contracts |
| 5 | **Config mutation cleanup** | Complexity adaptation mutates config mid-run → invisible state |
| 6 | **Notification abstraction** | osascript/ntfy/email fragmented across coordinator |

Workstreams 1–2 are the critical path. 3–6 can be done incrementally.

---

## Workstream 1: Coordinator Module Split

### Verification Checklist (do before executing)

#### 1a. `_run_shell` Circular Dependency

Gemini flagged this. Verify before committing to the move:

```bash
grep -n "_run_shell" src/theforge/coordinator.py | head -20
```

Check which functions in gate/workspace groups call `_run_shell` directly.
If gate/workspace functions call `_run_shell` internally → it must move to
`runner.py` (or a new `shell.py`). Also verify:

- Does `runner.py` import from `coordinator.py`? (would create circular dep)
- What does `_run_shell` import? (subprocess, Path — both stdlib, safe)

**Decision:** If moving to `runner.py` creates a circular dep, create
`src/theforge/shell.py` instead (pure stdlib, no internal deps).

#### 1b. Module-Level Constants

```bash
grep -n "^_[A-Z_]*\s*=" src/theforge/coordinator.py
```

Any constant referenced by functions moving to a sub-module must either
move with those functions or live in `coord_state.py`.

### Target Module Structure

```
src/theforge/
├── coordinator.py       # < 800 lines: entry points + main loop + re-exports
├── coord_state.py       # Phase, ReviewCycleMetadata, CoordinatorState, CoordinatorResult
├── coord_notify.py      # _notify(), _ntfy_*(), _remote_human_review(), _human_review()
├── coord_gate.py        # _run_gate(), dirty-worktree detection, auto-commit logic
├── coord_preflight.py   # preflight parsing, complexity adaptation, model escalation
├── coord_workspace.py   # worktree lifecycle, _merge_branch(), conflict resolution
└── shell.py             # _run_shell() — if circular dep confirmed (else stays in runner.py)
```

### Target Import Graph (No Circular Deps)

```
coord_state     ← stdlib only
coord_notify    ← coord_state, config, runner (or shell)
coord_gate      ← coord_state, config, runner (or shell)
coord_preflight ← coord_state, config, runner, task
coord_workspace ← coord_state, config, runner (or shell), coord_notify
coordinator     ← all of the above + task, review, schemas
```

### Backward-Compat Re-exports

These imports must continue working from `theforge.coordinator`:

```python
from theforge.coordinator import (
    CoordinatorResult,   # → coord_state
    CoordinatorState,    # → coord_state
    Phase,               # → coord_state
    ReviewCycleMetadata, # → coord_state
    _fmt_duration,       # stays in coordinator
    _is_remote_mode,     # → coord_notify
    _notify,             # → coord_notify
    _ntfy_publish,       # → coord_notify
    _run_gate,           # → coord_gate
    generate_audit_log,  # stays in coordinator
    run_from_dev,        # stays in coordinator
    run_from_review,     # stays in coordinator
    run_task,            # stays in coordinator
    set_log_level,       # stays in coordinator
)
```

### Execution Steps

Each step: implement → `make fmt` → `make test` → commit. Do NOT batch.

1. **`coord_state.py`** — Move Phase, ReviewCycleMetadata, CoordinatorState,
   CoordinatorResult. Add re-exports in coordinator.py. This is the lowest-risk
   move (pure data, no function deps).

2. **`coord_notify.py`** — Move all notification functions: `_log()`,
   `_log_verbose()`, `_notify()`, `_ntfy_publish()`, `_ntfy_poll_reply()`,
   `_ntfy_terminal_link()`, `_remote_human_review()`, `_human_review()`,
   `_is_remote_mode()`. These form a natural cluster with minimal deps.

3. **Resolve `_run_shell` placement** — Based on verification 1a above,
   either move to `runner.py` or create `shell.py`.

4. **`coord_gate.py`** — Move `_run_gate()`, dirty-worktree detection,
   auto-commit logic, scope checking. Depends on `_run_shell` placement.

5. **`coord_preflight.py`** — Move preflight verdict parsing, complexity
   detection (`_parse_preflight_complexity()`), model escalation
   (`_escalate_dev_model()`, `_find_registry_key_for_profile()`),
   complexity adaptation (`_apply_complexity_adaptation()`).

6. **`coord_workspace.py`** — Move worktree lifecycle: stale detection,
   creation, removal, `_merge_branch()`, `_resolve_merge_conflicts()`.

7. **Slim `coordinator.py`** — Remove all moved code, add re-exports.
   Target: < 800 lines. What remains: `_coordinator_loop()`, entry points
   (`run_task`, `run_from_dev`, `run_from_review`, `run_review_only`),
   `generate_audit_log()`, `_fmt_duration()`, `set_log_level()`.

---

## Workstream 2: Test File Split

### Verification: Shared Fixtures

```bash
grep -n "^def \|^@pytest.fixture" tests/test_coordinator.py | head -30
```

Shared fixtures/helpers → move to `tests/conftest.py`.

### Test Class → File Mapping

Verify each class name exists before moving:

```bash
grep -n "^class " tests/test_coordinator.py
```

| Test Class | Destination |
|---|---|
| `TestCoordinatorReviewCycleMetadata` | `test_coord_state.py` |
| `TestCoordinatorHumanReview` | `test_coord_notify.py` |
| `TestRemoteHumanReview` | `test_coord_notify.py` |
| `TestNtfyPollReply` | `test_coord_notify.py` |
| `TestNtfyTerminalNotifications` | `test_coord_notify.py` |
| `TestCoordinatorDirtyWorktree` | `test_coord_gate.py` |
| `TestGateOverride` | `test_coord_gate.py` |
| `TestExitCodeGateMode` | `test_coord_gate.py` |
| `TestCoordinatorPreflight` | `test_coord_preflight.py` |
| `TestParsePreflightComplexity` | `test_coord_preflight.py` |
| `TestComplexityAdaptation` | `test_coord_preflight.py` |
| `TestComplexityIntegration` | `test_coord_preflight.py` |
| `TestLargeComplexitySynthesisP1` | `test_coord_preflight.py` |
| `TestHasPersistentP1` | `test_coord_preflight.py` |
| `TestEscalateDevModel` | `test_coord_preflight.py` |
| `TestDevModelEscalationIntegration` | `test_coord_preflight.py` |
| `TestCoordinatorStaleHandoff` | `test_coord_workspace.py` |
| `TestStaleWorktree` | `test_coord_workspace.py` |
| `TestCoordinatorWorkspaceFailure` | `test_coord_workspace.py` |
| `TestCoordinatorAutoMerge` | `test_coord_workspace.py` |
| `TestCoordinatorAutoPush` | `test_coord_workspace.py` |
| `TestConflictResolution` | `test_coord_workspace.py` |
| Everything else | `test_coordinator.py` (stays) |

### Execution

Do this AFTER workstream 1 is complete (source must be split first so
test imports point at the right modules).

8. **`tests/conftest.py`** — Extract shared fixtures and helpers.
9. **Split test files** — Move classes per mapping, update imports.
10. **Verify independence:** `pytest tests/test_coord_gate.py -v` etc.

---

## Workstream 3: Code Deduplication

Can be done before, during, or after workstreams 1–2.

### 3a. Merge Block 3x → `_finalize_with_merge()`

The same merge-then-log block appears at three points in `_coordinator_loop()`:
- Interactive approve (~line 2004)
- Non-interactive approve (~line 2088)
- Human approve after exhausted cycles (~line 2193)

Extract to:

```python
def _finalize_with_merge(state, cfg, auto_merge, auto_push, logger):
    """Attempt merge+push after approval, return status message."""
```

### 3b. Frontmatter Parsing 2x → Single Source

`cli.py:_parse_spec_frontmatter()` and `task.py:parse_spec_frontmatter()` are
independent implementations. The cli version lacks gate field validation.

**Fix:** Keep `task.py:parse_spec_frontmatter()` as the canonical implementation.
Import it in cli.py. Delete the cli.py copy.

### 3c. Double Manifest Load → Pass-Through

`cli.py:cmd_sprint()` loads the campaign manifest, then calls `run_sprint()`
which loads it again.

**Fix:** Have `cmd_sprint()` pass the parsed manifest to `run_sprint()`.

---

## Workstream 4: Missing Type Abstractions

### 4a. `GateResult` dataclass

Currently `_run_gate()` returns a raw tuple. Replace with:

```python
@dataclasses.dataclass
class GateResult:
    decision: str | None   # "PASS", "FAIL", or None
    summary: str | None     # human-readable gate output
    raw_output: str         # full gate command stdout
```

Lives in `coord_state.py` (or `coord_gate.py` after split).

### 4b. `WorktreeInfo` dataclass

Workspace creation returns `tuple[Path | None, str | None, str]`. Replace:

```python
@dataclasses.dataclass
class WorktreeInfo:
    path: Path | None       # worktree directory
    branch: str | None      # branch name
    setup_output: str       # workspace setup stdout
```

Lives in `coord_state.py` (or `coord_workspace.py` after split).

### 4c. `ReviewCycleResult` — evaluate need

Review cycle outcomes are currently intertwined with the coordinator loop
variables. This may not need a separate type if the loop refactor (workstream 1)
makes the flow clear enough. **Evaluate after workstream 1.**

---

## Workstream 5: Config Mutation Cleanup

`_apply_complexity_adaptation()` mutates the config object mid-run based on
PREFLIGHT complexity verdict. This makes config mutable state that's invisible
in the audit trail.

**Fix:** Instead of mutating config, return an `Overrides` dataclass:

```python
@dataclasses.dataclass
class ComplexityOverrides:
    dev_timeout: int | None = None
    dev_budget: float | None = None
    review_timeout: int | None = None
```

The coordinator loop applies overrides explicitly when computing timeouts/budgets,
leaving the original config immutable. Overrides are logged in the audit trail.

**When:** After workstream 1 (coord_preflight.py exists to own this).

---

## Workstream 6: Notification Abstraction

Current state: osascript (macOS), ntfy.sh (HTTP), email (stub) — all
implemented as separate functions in coordinator.py with no shared interface.

**Fix:** Define a `Notifier` protocol:

```python
class Notifier(Protocol):
    def notify(self, title: str, body: str, *, priority: str = "default") -> None: ...
    def poll_reply(self, topic: str, timeout: int) -> str | None: ...
```

Implementations: `OsascriptNotifier`, `NtfyNotifier`, `EmailNotifier`.
Coordinator receives a `list[Notifier]` and broadcasts.

**When:** During or after workstream 1 step 2 (coord_notify.py).
**Priority:** Low — current approach works, this is a cleanliness improvement.

---

## Execution Order

```
Phase 0: Quick wins (1–2 hours)
  - Remove DEBUG print (coordinator.py ~line 830)
  - Deduplicate frontmatter parsing (workstream 3b)
  - Deduplicate manifest loading (workstream 3c)

Phase 1: Coordinator split (workstream 1, steps 1–7)
  - Each step is one commit, each passes make fmt && make test
  - Critical path: ~1 week of focused work

Phase 2: Test split (workstream 2, steps 8–10)
  - Depends on Phase 1 completion
  - ~2–3 days

Phase 3: Cleanup (workstreams 3a, 4, 5, 6)
  - Can be done incrementally, independent of each other
  - 3a (merge dedup): 2 hours
  - 4 (type abstractions): 4 hours
  - 5 (config immutability): 4 hours
  - 6 (notification protocol): 8 hours (lowest priority)
```

---

## Acceptance Criteria

1. `make test` passes with same test count before and after
2. `make lint` passes
3. All backward-compat re-exports work without error
4. `coordinator.py` < 800 lines
5. `test_coordinator.py` < 2,000 lines
6. Each new `test_coord_*.py` runs independently
7. No raw tuple returns for gate/workspace results (workstream 4)
8. Config object is never mutated after initial load (workstream 5)
9. Zero code duplication for merge blocks, frontmatter, manifest loading

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Circular import from `_run_shell` move | Verify before executing; `shell.py` fallback |
| Test count changes during split | Count before, count after, assert equal |
| Re-export misses break cli.py/sprint.py | Grep all imports from `theforge.coordinator` across codebase |
| Merge conflicts during multi-step refactor | Each step is one commit; rebase frequently |
| Behavioral change sneaks in | Pure refactor — no logic changes. Diff should show only moves + imports |

---

## Gemini Plan Reference

Gemini 2.5 Pro produced the original split plan in 73s without reading code
(based on spec + function listing only). Used as structural reference; all
claims verified against actual source before inclusion here.
