---
name: "Per-run verbose log capture"
slug: run-log-capture
pytest_target: tests/
---

# Per-Run Verbose Log Capture

## Problem

I want to see what agents actually did during a run — every tool call, every
edit, every bash command — without piping forge's output to tee. Piping is
fragile: if the terminal closes or the pipe dies, forge gets SIGPIPE and dies
silently mid-run.

The persistent-logging story landed `~/.forge/logs/<project>/forge.log` for
structured JSON events, but that only captures high-level events (phase
transitions, verdicts, costs). The full verbose output — `↳ Read: ...`,
`↳ Edit: ...`, `Starting dev (model=sonnet...)` — goes only to stderr and
disappears unless manually captured.

## Solution

When forge starts a run, automatically tee all stderr output (the `_log` /
`_log_verbose` stream) to a per-run log file alongside the JSON event log.
No flag required — always on.

### Log location

```
~/.forge/logs/<project>/<slug>-<run_id>.log
```

Example: `~/.forge/logs/theforge/deepseek-provider-5864dcc3a14e.log`

Same directory as `forge.log`. Created at run start, written append-style
during the run, closed cleanly on exit (including on SIGTERM).

### Implementation

In `cli.py`, after the run_id and log path are established, open the log
file and install a write-through stderr wrapper:

```python
class _TeeStderr:
    def __init__(self, original, log_fh):
        self._orig = original
        self._fh = log_fh
    def write(self, s):
        self._orig.write(s)
        self._fh.write(s)
        self._fh.flush()
    def flush(self):
        self._orig.flush()
        self._fh.flush()
```

`sys.stderr = _TeeStderr(sys.stderr, log_fh)` before calling the command.
Restore on exit. The file handle is closed in a `finally` block so it
survives exceptions and KeyboardInterrupt.

Also add a SIGTERM handler that closes the log file and emits a
`run_end: crashed` JSON event before dying — so incomplete runs are
identifiable in `forge.log`.

### No new flag needed

Always-on. The log file is cheap (text), doesn't affect run performance,
and solves the "why did it die" problem permanently.

## Acceptance Criteria

- [ ] Every `forge run` and `forge sprint` creates `~/.forge/logs/<project>/<slug>-<run_id>.log`
- [ ] The file contains the full stderr stream — both `_log` and `_log_verbose` lines
- [ ] The file is written incrementally (not buffered to end of run)
- [ ] File is closed cleanly on normal exit, KeyboardInterrupt, and SIGTERM
- [ ] SIGTERM handler emits `run_end` with `outcome: crashed` to `forge.log` before dying
- [ ] Log directory is created if it doesn't exist (same logic as forge.log)
- [ ] `forge.log` already captures run_id — the per-run filename uses the same run_id
- [ ] Existing tests pass unchanged
