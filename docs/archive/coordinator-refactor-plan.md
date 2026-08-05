# Coordinator Refactor — Comprehensive Plan

Status: record (2026-03-15) — the coordinator split this plans has long shipped (`dev_phase.py`/`review_phase.py`/`validate_phase.py`); retained as history.

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
| 5 | **Complexity adaptation auditability** | Config swap after PREFLIGHT is invisible in audit trail |
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
coord_workspace ← coord_state, config, runner (or shell), coord_notify, coord_gate
coordinator     ← all of the above + task, review, schemas
```

**Why coord_workspace → coord_gate:** `_resolve_merge_conflicts()` calls
`_run_gate()` at line 746 to verify that conflict resolution didn't break
tests before completing the merge. This is a one-way dependency
(coord_gate does not import coord_workspace), so no circular dep risk.

### Backward-Compat Re-exports

Every symbol imported from `theforge.coordinator` by cli.py, sprint.py,
tests/test_coordinator.py, or tests/test_sprint.py must continue working
after the split. Derived from grepping the actual imports:

```python
# sprint.py imports (src/theforge/sprint.py:18–29)
from theforge.coordinator import (
    CoordinatorResult,   # → coord_state
    StructuredLogger,    # stays in coordinator
    _fmt_duration,       # stays in coordinator
    _generate_run_id,    # stays in coordinator
    _is_remote_mode,     # → coord_notify
    _notify,             # → coord_notify
    _ntfy_publish,       # → coord_notify
    _run_gate,           # → coord_gate
    generate_audit_log,  # stays in coordinator
    run_from_dev,        # stays in coordinator
    run_from_review,     # stays in coordinator
    run_task,            # stays in coordinator
)

# cli.py imports (src/theforge/cli.py:21–28)
from theforge.coordinator import (
    CoordinatorResult,   # → coord_state
    _fmt_duration,       # stays in coordinator
    generate_audit_log,  # stays in coordinator
    run_from_review,     # stays in coordinator
    run_task,            # stays in coordinator
    set_log_level,       # stays in coordinator (aliased)
)

# test_coordinator.py imports (tests/test_coordinator.py:29–47)
from theforge.coordinator import (
    Phase,                          # → coord_state
    StructuredLogger,               # stays in coordinator
    _apply_complexity_adaptation,   # → coord_preflight
    _escalate_dev_model,            # → coord_preflight
    _fmt_duration,                  # stays in coordinator
    _generate_run_id,               # stays in coordinator
    _has_persistent_p1,             # → coord_preflight
    _is_remote_mode,                # → coord_notify
    _is_stale_worktree,             # → coord_workspace
    _ntfy_poll_reply,               # → coord_notify
    _ntfy_reply_url,                # → coord_notify
    _parse_preflight_complexity,    # → coord_preflight
    _remove_worktree,               # → coord_workspace
    generate_audit_log,             # stays in coordinator
    run_from_review,                # stays in coordinator
    run_review_only,                # stays in coordinator
    run_task,                       # stays in coordinator
)
# Also inline imports of: _create_workspace (→ coord_workspace),
# CoordinatorResult/CoordinatorState/ReviewCycleMetadata (→ coord_state)

# test_sprint.py imports (tests/test_sprint.py:20–27)
from theforge.coordinator import (
    CoordinatorResult,   # → coord_state
    CoordinatorState,    # → coord_state
    Phase,               # → coord_state
    _notify,             # → coord_notify
    _osa_quote,          # → coord_notify
    run_task,            # stays in coordinator
)
```

**Strategy for test imports:** Once source is split, tests should import
from the canonical sub-module (e.g. `from theforge.coord_preflight import
_apply_complexity_adaptation`). The re-exports in coordinator.py exist for
backward compat of production code (cli.py, sprint.py) — tests should be
updated to import from the new homes as part of workstream 2.

**"What remains" in coordinator.py** must include `StructuredLogger` and
`_generate_run_id` — sprint.py depends on both. Everything that moves to
a sub-module gets a re-export line in coordinator.py.

### Execution Steps

Each step: implement → `make fmt` → `make test` → commit. Do NOT batch.

1. **`coord_state.py`** — Move Phase, ReviewCycleMetadata, CoordinatorState,
   CoordinatorResult. Add re-exports in coordinator.py. This is the lowest-risk
   move (pure data, no function deps).

2. **`coord_notify.py`** — Move notification functions: `_notify()`,
   `_ntfy_publish()`, `_ntfy_poll_reply()`, `_ntfy_reply_url()`,
   `_ntfy_terminal_link()`, `_remote_human_review()`, `_human_review()`,
   `_is_remote_mode()`, `_osa_quote()`. These form a natural cluster.

   **`_log()` and `_log_verbose()` stay in coordinator.py** — they're used
   by gate, workspace, merge, preflight, and the main loop. Moving them to
   coord_notify.py would create noisy cross-module imports from every other
   sub-module. They're logging utilities, not notification functions. If they
   become awkward in coordinator.py after the split, extract to a small
   `coord_logging.py` as a follow-up — but don't force them into notify.

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
   `generate_audit_log()`, `_fmt_duration()`, `set_log_level()`,
   `StructuredLogger`, `_generate_run_id()` (sprint.py depends on both).

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

Currently `_run_gate()` returns `tuple[str | None, str | None, str]` where the
three positions are `(decision, error, output)`. The coordinator uses `error`
(second element) to distinguish infrastructure failures that must escalate
immediately from ordinary gate FAILs that can retry. This distinction must be
preserved.

```python
@dataclasses.dataclass
class GateResult:
    decision: str | None   # "PASS", "FAIL", or None (unparseable)
    error: str | None       # infrastructure error (subprocess crash, timeout, missing handoff)
                            # non-None → immediate escalation, distinct from decision="FAIL"
    output: str             # full gate command stdout (for logging/audit)
```

Lives in `coord_state.py` (or `coord_gate.py` after split).

### 4b. `WorktreeInfo` dataclass

`_create_workspace()` returns `tuple[Path | None, str | None, str | None]`
where the three positions are `(path, branch, error)`. The caller (`run_task`)
treats any non-None third element as a fatal workspace failure and skips to
ESCALATE. This error channel must be preserved — it's not "setup output."

```python
@dataclasses.dataclass
class WorktreeInfo:
    path: Path | None       # worktree directory (None on failure)
    branch: str | None      # branch name (None on failure)
    error: str | None       # fatal workspace error; non-None → skip to ESCALATE
```

Lives in `coord_state.py` (or `coord_workspace.py` after split).

### 4c. `ReviewCycleResult` — evaluate need

Review cycle outcomes are currently intertwined with the coordinator loop
variables. This may not need a separate type if the loop refactor (workstream 1)
makes the flow clear enough. **Evaluate after workstream 1.**

---

## Workstream 5: Complexity Adaptation Auditability

`_apply_complexity_adaptation()` already returns a **new** `ForgeConfig` via
`_dc_replace()` rather than mutating in place — the caller rebinds the local
`config` name. So this is not a mutation bug. The problem is **auditability**:
the config swap is invisible in the audit trail, and there's no record of what
changed or why.

**Fix:** Return an explicit `ComplexityOverrides` alongside the new config.
The fields must reflect what `_apply_complexity_adaptation()` actually changes:

- **small:** trims `review_pool` to cheapest single reviewer, drops `synthesis_profile`
- **large:** upgrades `dev_profile` to strongest model/CLI, materializes `synthesis_profile`

```python
@dataclasses.dataclass
class ComplexityOverrides:
    complexity: str                          # "small", "medium", "large"
    review_pool_changed: bool = False        # True if pool was trimmed (small)
    review_pool_size: int | None = None      # new pool size after trim
    synthesis_dropped: bool = False           # True if synthesis was removed (small)
    synthesis_materialized: bool = False      # True if synthesis was created (large)
    dev_model_changed: bool = False           # True if dev CLI/model upgraded (large)
    dev_cli: str | None = None               # new dev CLI (if changed)
    dev_model: str | None = None             # new dev model (if changed)
```

The coordinator logs the overrides in the audit trail so the config delta is
visible. The new config is still produced by `_dc_replace()` — this doesn't
change the mechanics, just makes the decision surface auditable.

**When:** After workstream 1 (coord_preflight.py exists to own this).
**Priority:** Low — correctness is fine, this is a transparency improvement.

---

## Workstream 6: Notification Abstraction

Current state: osascript (macOS), ntfy.sh (HTTP), email (stub) — all
implemented as separate functions in coordinator.py with no shared interface.

**Fix:** The notification functions split into two concerns with different
contracts:

1. **Fire-and-forget notifications** (osascript, ntfy publish, email):
   simple `notify(title, body)` — a shared protocol works here.

2. **Interactive review polling** (ntfy poll): `_ntfy_poll_reply()` needs
   `reply_url` + `since_ts`, returns `(decision, feedback)` where feedback
   carries human rejection findings. This is ntfy-specific and cannot be
   generalized to a `poll_reply(topic, timeout) -> str | None` protocol
   without losing the rejection-feedback channel and replay protection.

**Approach:** Extract a `Notifier` protocol for concern 1 only:

```python
class Notifier(Protocol):
    def notify(self, title: str, body: str, *, priority: str = "default") -> None: ...
```

Keep `_ntfy_poll_reply()` and `_remote_human_review()` as standalone
functions in coord_notify.py — they're ntfy-specific interactive flows,
not generic notification. Don't force them into an abstraction that would
lose the `(decision, feedback)` return or `(reply_url, since_ts)` inputs.

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
  - 5 (complexity auditability): 4 hours
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
8. Complexity overrides logged in audit trail (workstream 5)
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
