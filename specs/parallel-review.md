---
name: "parallel review pool execution"
slug: parallel-review
file_scope:
  - src/theforge/runner.py
  - src/theforge/coordinator.py
  - tests/test_runner.py
pytest_target: tests/
---

# Parallel Review Pool Execution

## Problem

Review pool agents run sequentially. With 3 reviewers:

```
opus:   52s
codex: 144s
gemini:  44s
────────────
total: 240s wall clock
```

Run in parallel, wall clock drops to 144s (bottleneck = codex). That's
**96 seconds saved per review cycle**. With 3-5 cycles per spec, that's
**5-8 minutes saved per spec**. In a 5-spec sprint, that's **25-40 minutes**.

The code already says `# sequentially for MVP` — time to graduate.

## Design

Replace the list comprehension in `run_agent_pool()` with
`ThreadPoolExecutor`. Each agent is a subprocess, so threads are
fine (no GIL contention — we're just waiting on I/O).

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def run_agent_pool(...) -> list[AgentResult]:
    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        futures = {
            pool.submit(run_agent, prompt=prompt, profile=p, working_dir=working_dir): i
            for i, p in enumerate(profiles)
        }
        results = [None] * len(profiles)
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
        return results
```

### Logging concern

Currently the stream logging (`_process_stream_event`) interleaves
tool activity lines with `[forge]   ↳` prefix. With parallel execution,
lines from different agents will interleave. Two options:

1. **Prefix with agent name**: `[forge]   ↳ [opus] Read: coordinator.py`
2. **Buffer and dump**: collect all output, print per-agent block after done

Option 1 is simpler and gives real-time visibility. The `label` parameter
already exists in `_process_stream_event` — just needs to be included in
the output prefix.

### Working directory isolation

All reviewers share the same worktree (read-only review). This is safe —
reviewers only use Read, Grep, Glob, Bash (read commands). They don't
write files. No filesystem contention.

---

## Requirements

### R1: `ThreadPoolExecutor` in `run_agent_pool`

Replace sequential list comprehension with concurrent execution.
All agents start simultaneously. Results returned in profile order
(not completion order).

### R2: Labeled log output

When running in parallel, prefix stream log lines with the agent name:

```
[forge]   ↳ [opus] Read: coordinator.py
[forge]   ↳ [codex] Grep: gate_override
[forge]   ↳ [gemini] Read: config.py
```

The `label` parameter already flows through `run_agent` → `_run_claude` /
`_run_codex` / `_run_gemini` → `_process_stream_event`. Just include it
in the output format.

### R3: Progress logging

Replace the sequential "Starting X... done" pattern with parallel-aware
progress:

```
[forge]   Starting review pool: opus, codex, gemini (parallel)
[forge]   ... gemini done (44s)
[forge]   ... opus done (52s)
[forge]   ... codex done (144s)
[forge]   Review pool complete: 144s wall clock (240s sequential)
```

Log completion as each agent finishes. Show wall clock vs sequential
comparison.

### R4: Error handling

If one agent fails (timeout, crash), the others continue. Failed agents
return their `AgentResult` with `exit_code != 0`. Same behavior as
sequential — a single reviewer failure doesn't abort the pool.

`ThreadPoolExecutor` handles this naturally — `future.result()` raises
the exception, which we catch and convert to a failed `AgentResult`.

### R5: Single-agent pool bypass

When `len(profiles) == 1`, skip the thread pool overhead and run directly
(same as current behavior). This avoids thread creation cost for
single-reviewer configurations.

### R6: Tests

- `test_pool_runs_parallel`: mock `run_agent` with sleep, verify wall
  clock < sum of individual durations (proves parallel)
- `test_pool_preserves_order`: results returned in profile order regardless
  of completion order
- `test_pool_single_agent_no_thread`: single profile → no ThreadPoolExecutor
- `test_pool_agent_failure_isolated`: one agent fails → others still return
- `test_pool_all_agents_receive_same_prompt`: verify prompt passed correctly

---

## Acceptance Criteria

1. Review pool agents run concurrently via ThreadPoolExecutor
2. Results returned in profile order (deterministic)
3. Log output prefixed with agent name for parallel runs
4. Single-agent pool runs without thread overhead
5. Agent failure is isolated — other agents complete normally
6. Wall clock time ≈ slowest agent (not sum of all agents)
7. All existing tests pass unchanged
