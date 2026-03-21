---
name: "Structured logging: progress view vs verbose mode"
slug: structured-logging
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/runner.py
  - src/theforge/campaign.py
  - src/theforge/cli.py
pytest_target: tests/test_coordinator.py
---

# Structured Logging

## Problem

TheForge's current log output mixes high-signal events (phase transitions, agent
completions, review verdicts) with high-volume tool activity lines (`↳ Read: ...`,
`↳ Edit: ...`). There is no campaign-level framing, no per-spec header, and no way
to suppress the noise.

Running a campaign produces hundreds of lines like:
```
[forge]   ↳ Read: file_path
[forge]   ↳ Edit: file_path
[forge]   ↳ Bash: make test
[forge]   ↳ Read: file_path
```

…interspersed with the actually-useful events, making it impossible to follow
progress at a glance.

**Desired at-a-glance output (default):**
```
[campaign] "TheForge hardening sprint"  4 specs  budget=$40.00

[1/4] gate-hardening ──────────────────────────────────
  ▸ WORKSPACE
  ▸ PREFLIGHT   claude/sonnet
  ▸ DEV         claude/sonnet  iter=1  (running...)
  ▸ DEV         claude/sonnet  iter=1  ✓ $1.23  145s
  ▸ VALIDATE    PASS
  ▸ REVIEW      opus+codex  cycle=1  (running...)
  ▸ REVIEW      opus+codex  cycle=1  ✓ APPROVE  $0.73  42s
  ✓ DONE        total=$1.96  188s

[2/4] reasoning-effort ────────────────────────────────
  ▸ WORKSPACE
  ...
```

**Verbose mode (`--verbose`)** adds tool activity, heartbeats, gate output,
and raw agent output snippets — the current default behavior.

## Design

### Log levels

Define two log levels:

```python
class LogLevel(IntEnum):
    PROGRESS = 0   # default: phase transitions, agent start/done, verdicts
    VERBOSE  = 1   # adds tool activity, heartbeats, raw output
```

A module-level `_LOG_LEVEL` variable (default `PROGRESS`) gates verbose output.
Functions `_log()` and `_log_verbose()`:

```python
def _log(msg: str) -> None:
    print(f"[forge] {msg}", file=sys.stderr, flush=True)

def _log_verbose(msg: str) -> None:
    if _LOG_LEVEL >= LogLevel.VERBOSE:
        print(f"[forge] {msg}", file=sys.stderr, flush=True)
```

### What moves to verbose-only

**`runner.py`** — tool activity and heartbeats:
- `↳ tool_name: preview` lines → `_log_verbose()`
- `↳ summary` lines → `_log_verbose()`
- `... label still running (Xs elapsed)` → `_log_verbose()`
- Agent start line `Starting label (model=..., timeout=...)` → keep at PROGRESS
  (user needs to know something is running), but simplify format

**`coordinator.py`** — raw detail:
- `Gate command failed: {output[:200]}` → `_log_verbose()`
- `Dirty worktree detected: {dirty_files}` → keep at PROGRESS (important signal)
- `Retrying dev (gate=..., iter=...)` → keep at PROGRESS
- `Review parse errors: ...` → `_log_verbose()`
- Overriding APPROVE → REQUEST_CHANGES detail → `_log_verbose()`

### Progress-level events (always shown)

In `coordinator.py`, upgrade these to structured progress lines:

```
▸ PHASE   detail
```

Examples:
```
▸ WORKSPACE   feat/gate-hardening
▸ PREFLIGHT   claude/sonnet
▸ DEV         claude/sonnet  iter=1
  ✓ DEV       $1.23  145s
▸ VALIDATE    running gate...
  ✓ VALIDATE  PASS
  ✗ VALIDATE  FAIL  (iter=1 → retrying)
▸ REVIEW      opus+codex  cycle=1
  ✓ REVIEW    APPROVE  0 P1  2 P2  $0.73  42s
  ✗ REVIEW    REQUEST_CHANGES  1 P1  $0.80  38s
✓ DONE        total=$1.96  188s
✗ ESCALATE    max dev iterations exhausted
```

### Campaign framing

In `campaign.py`, add a spec header line before each spec run:

```
[1/4] gate-hardening ──────────────────────────────────
```

The dashes fill to 60 chars. After spec completes:
```
[1/4] ✓ gate-hardening   $1.96  188s
```
or
```
[1/4] ✗ gate-hardening   ESCALATED  $3.10  420s
```

### CLI flag

Add `--verbose` / `-v` to both `forge run` and `forge campaign` subcommands.
When set, `_LOG_LEVEL = LogLevel.VERBOSE` before running.

The flag propagates via a module-level setter in `coordinator.py` and `runner.py`:

```python
def set_log_level(level: LogLevel) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level
```

`cli.py` calls `coordinator.set_log_level(LogLevel.VERBOSE)` and
`runner.set_log_level(LogLevel.VERBOSE)` when `--verbose` is passed.

### Timing for agents

Track elapsed time at PROGRESS level. Runner already returns `elapsed_seconds`
from `_run_with_heartbeat()`. Coordinator logs it on agent completion:

```
  ✓ DEV   $1.23  145s
```

## Acceptance Criteria

1. Default output shows only phase transitions, agent start/done (with cost+time),
   gate results, and review verdicts — no tool activity lines
2. `forge run --verbose` restores current verbosity (tool lines, heartbeats,
   raw output snippets)
3. `forge campaign --verbose` does the same for all specs in the campaign
4. Each spec in a campaign is headed by `[N/total] slug ───...` banner
5. Completed specs show one-line summary: `[N/total] ✓/✗ slug   $cost  time`
6. Agent start line still appears at PROGRESS level so user knows something is running
7. Heartbeats (`... still running`) are verbose-only (agent start line covers it)
8. All existing tests continue to pass

## Test Expectations

In `tests/test_coordinator.py`:

- `test_verbose_flag_enables_tool_lines` — with `LogLevel.VERBOSE`, tool activity
  is printed; with `LogLevel.PROGRESS`, it is not
- `test_progress_shows_phase_transitions` — phase transition lines always appear
  regardless of log level
- `test_campaign_spec_header_printed` — campaign emits `[N/total] slug` header
  before each spec (capture stderr)

## Out of Scope

- Color/ANSI formatting (separate enhancement)
- Log-to-file / structured JSON output
- Per-agent verbose toggle (all-or-nothing for now)
