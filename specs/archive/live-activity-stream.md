---
name: "Live activity stream: real-time agent tool-use feed"
slug: live-activity
file_scope:
  - src/theforge/runner.py
  - src/theforge/cli.py
  - tests/test_runner.py
pytest_target: tests/
---

# Live Activity Stream: Real-Time Agent Tool-Use Feed

## Problem

TheForge dev runs take 5–10 minutes. The only feedback is a 30s heartbeat
saying "still running". We have no idea if the agent is making progress,
stuck in a loop, or spending 10 minutes reading files it doesn't need.

The Claude CLI supports `--output-format stream-json --verbose` which
emits JSONL events to stdout in real time — one line per tool call,
tool result, and assistant message. This is the visibility we need.

## What stream-json gives us

Each JSONL line has a `type` field. Relevant types:

```
type=assistant  content[].type=tool_use  → agent is calling a tool
type=tool_use_summary                    → tool completed (has summary)
type=result                              → final result with cost
```

Example assistant tool_use event:
```json
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"src/foo.py"}}]}}
```

Example tool_use_summary:
```json
{"type":"tool_use_summary","summary":"Read src/foo.py (120 lines)"}
```

## Requirements

### R1: Switch `_run_claude` to stream-json mode

Change the Claude subprocess invocation from:
```
claude -p --output-format json --model <model> ...
```
to:
```
claude -p --output-format stream-json --verbose --model <model> ...
```

The output is now a stream of JSONL lines instead of a single JSON object.
The final line with `"type": "result"` contains the same fields as before
(`result`, `session_id`, `total_cost_usd`, `modelUsage`).

### R2: Stream-reading subprocess pattern

Because we need to read stdout line-by-line as it arrives (not wait for
the whole output), switch from `subprocess.run` (which buffers all output)
to `subprocess.Popen` with line-by-line stdout reading in the background
thread:

```python
proc = subprocess.Popen(
    cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    cwd=str(working_dir),
    env=env,
)
proc.stdin.write(prompt)
proc.stdin.close()

lines = []
for line in proc.stdout:
    lines.append(line)
    _process_stream_event(line.strip(), label)

proc.wait(timeout=profile.timeout_seconds)
```

The heartbeat thread approach changes: instead of a separate thread for
heartbeats, the activity stream replaces the heartbeat. Print tool-use
events to stderr as they arrive.

### R3: Activity printing (replace heartbeat)

Print tool-use events to stderr as they arrive. Use the
`tool_use_summary` type for clean one-liners:

```
[forge]   ↳ Read: src/theforge/runner.py (240 lines)
[forge]   ↳ Edit: src/theforge/runner.py — replaced _run_claude
[forge]   ↳ Bash: PYTHONPATH=src python -m pytest tests/ -q
[forge]   ↳ Write: src/theforge/runner.py
```

Format: `[forge]   ↳ <tool_name>: <summary_or_input_preview>`

Fall back to the `assistant` tool_use event (with input preview) when
no `tool_use_summary` is available for a given tool call.

### R4: Timeout handling with Popen

The `subprocess.TimeoutExpired` approach changes with Popen. Use
`proc.wait(timeout=...)` and kill the process on timeout:

```python
try:
    proc.wait(timeout=profile.timeout_seconds)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()
    return AgentResult(success=False, output="TIMEOUT: ...", ...)
```

### R5: Result extraction unchanged

The final `type=result` line in the stream contains all the same fields
as the old single-JSON output:
- `result` → output text
- `session_id`
- `total_cost_usd`
- `modelUsage`

Parse these from the result line exactly as before.

### R6: Elapsed time still shown

Print elapsed time when the agent finishes (same as current behavior):
```
[forge]   ... dev done (477s)
```

### R7: Tests

Update `tests/test_runner.py`:

- The mocks for `_run_claude` currently mock `subprocess.run`. Switch
  them to mock `subprocess.Popen` with a mock that:
  - Returns a mock process object with `stdout` as an iterable of JSONL lines
  - Has a `wait()` method that returns 0
  - Has a `stdin` with `write()` and `close()` methods
  - Has a `returncode` of 0

- `test_happy_path`: mock stdout yields a result line with the full
  JSON including `total_cost_usd` and `modelUsage`
- `test_timeout`: mock `wait()` raises `TimeoutExpired`
- `test_activity_printed`: verify that tool_use_summary events are
  printed to stderr
- All existing test assertions about AgentResult fields remain valid

Helper fixture `_make_stream_mock(lines)` that returns a mock Popen
object given a list of JSONL strings.

## Out of scope

- Storing the activity stream in the audit log (future spec)
- Gemini/Codex activity streaming (those CLIs use different formats)
- Filtering or rate-limiting verbose output (print everything)

## Acceptance criteria

1. `forge run` shows tool-use activity in real time (Read/Edit/Bash/etc.)
2. The `result`, `session_id`, `cost_usd`, `model_usage` fields still
   populate correctly from the stream's final result line
3. Timeout still works and kills the process
4. All 136 existing tests pass (updated for Popen mocking)
5. New tests cover activity printing and stream parsing
