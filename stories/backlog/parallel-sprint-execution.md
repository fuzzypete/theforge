---
name: "Parallel sprint execution — concurrent story workers with dependency gating"
slug: parallel-sprint-execution
pytest_target: tests/
depends_on: [forge-daemon, stage-aware-pipeline]
---

# Parallel Sprint Execution

## Problem

Sprints run stories sequentially. A 10-story sprint takes 10× the wall-clock
time of a single story. Stories with no dependency relationship wait in line
behind each other for no reason.

The daemon (#41) provides process management. Stage-aware pipeline (#27) provides
per-run config overrides. This story adds the missing piece: running independent
stories concurrently.

## Solution

### Sprint manifest gets `max_parallel`

```yaml
name: "M3 hardening"
budget_usd: 50
max_parallel: 3          # run up to 3 stories concurrently (default: 1)
stories:
  - stories/backlog/foo.md
  - stories/backlog/bar.md
  - stories/backlog/baz.md
```

### Dependency-aware scheduling

Stories declare dependencies via `depends_on` in frontmatter. The sprint
runner builds a DAG and schedules stories whose dependencies have completed
(DONE or ALREADY_DONE). Stories whose dependencies failed or escalated are
skipped with reason logged.

```
depends_on: [foo, bar]   # wait for foo and bar to reach DONE before starting
```

When `max_parallel > 1`:
1. Build DAG from `depends_on` across all stories in manifest
2. Find ready set: stories with no unmet dependencies
3. Launch up to `max_parallel` from ready set concurrently
4. As each completes, re-evaluate ready set and launch next batch
5. Budget check before each launch — skip if aggregate cost exceeds budget

Stories with no `depends_on` are all eligible immediately.

### Each story gets its own worktree

This already happens — `forge run` creates `.forge/worktrees/<slug>/`. Parallel
execution just means N worktrees exist simultaneously. Each runs an independent
coordinator state machine. No shared state between concurrent stories.

### Merge ordering

When `on_approve: merge`, stories merge in dependency order (not completion
order). A story that finishes first waits for its predecessors to merge.
This prevents merge conflicts from concurrent branches.

When `on_approve: pr`, all PRs are created immediately on completion.
Merge ordering is the human's problem.

### Budget pooling

Aggregate budget is shared across all concurrent workers. Before launching a
story, check `accumulated_cost < budget_usd`. The sprint runner holds a lock
on the cost accumulator. Individual story budgets (from forge.yaml profiles)
still apply per-story.

### Implementation

In `sprint.py`:

```python
def run_sprint(...):
    dag = build_dag(stories)
    active: dict[str, Future] = {}
    completed: set[str] = set()

    with ThreadPoolExecutor(max_workers=manifest.max_parallel) as pool:
        while not dag.all_done():
            ready = dag.ready(completed)
            for story in ready:
                if over_budget():
                    skip(story)
                    continue
                active[story.slug] = pool.submit(run_single_story, story)

            # Wait for at least one to complete
            done_futures = wait(active.values(), return_when=FIRST_COMPLETED)
            for slug, future in list(active.items()):
                if future in done_futures.done:
                    result = future.result()
                    completed.add(slug)
                    del active[slug]
                    accumulate_cost(result)
```

Each `run_single_story` call is a full `run_task()` — its own worktree,
state machine, and audit trail. The sprint runner just manages scheduling.

### Status reporting

Sprint status shows all active workers:

```
[sprint] [1/10] foo ────────── DEV iter=2  ($1.23)
[sprint] [2/10] bar ────────── REVIEW cycle=1  ($0.87)
[sprint] [3/10] baz ────────── PLAN  ($0.12)
[sprint] waiting: qux (depends on foo), quux (depends on bar)
```

### Daemon integration

When a daemon is running, `forge sprint manifest.yaml` submits to the daemon.
The daemon's event loop manages the thread pool. Without a daemon, the sprint
runner manages the pool directly in the foreground process.

## Acceptance criteria

- `max_parallel` in sprint manifest controls concurrency (default 1 = sequential)
- Stories with no `depends_on` launch immediately up to max_parallel
- Stories with `depends_on` wait for dependencies to complete
- Failed/escalated dependencies cause dependents to skip
- Budget check before each story launch
- Merge ordering respects dependency order when on_approve: merge
- Sprint status shows all active workers with current phase
- Sprint audit includes per-story start/end times and parallel batch info
- Fallback: max_parallel=1 produces identical behavior to current sequential runner
- All existing tests pass
- New tests for DAG scheduling, dependency gating, budget pooling, merge ordering
