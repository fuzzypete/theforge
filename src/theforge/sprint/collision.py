from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from ..config import ForgeConfig
from ..coordinator.engine import run_task
from ..coordinator.state import CoordinatorState, Phase
from ..task import TaskStory


def _log(msg: str) -> None:
    print(f"[sprint] {msg}", file=sys.stderr, flush=True)


def run_batch_preflight(
    tasks: list[TaskStory],
    config: ForgeConfig,
    *,
    sprint_name: str,
    no_pull: bool,
    max_parallel: int,
    notify: bool = False,
) -> dict[str, CoordinatorState]:
    states: dict[str, CoordinatorState] = {}

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(
                run_task,
                config,
                task,
                notify=notify,
                sprint_name=sprint_name,
                stop_phase=Phase.PREFLIGHT,
                no_pull=no_pull,
            ): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                _log(
                    "WARNING: batch preflight failed for "
                    f"{task.slug}: {type(exc).__name__}: {exc}; "
                    "excluding from collision detection"
                )
                states[task.slug] = CoordinatorState(preflight_likely_files=[])
                continue

            if not result.success:
                _log(
                    "WARNING: batch preflight returned failure for "
                    f"{task.slug}: {result.message}; excluding from collision detection"
                )
                states[task.slug] = CoordinatorState(preflight_likely_files=[])
                continue

            states[task.slug] = result.state

    return states


def compute_synthetic_edges(
    preflight_states: dict[str, CoordinatorState], tasks: list[TaskStory]
) -> dict[str, list[str]]:
    task_by_slug = {task.slug: task for task in tasks}
    file_to_slugs: dict[str, list[str]] = {}
    synthetic: dict[str, set[str]] = {}

    for slug, state in preflight_states.items():
        for path in state.preflight_likely_files:
            file_to_slugs.setdefault(path, []).append(slug)

    def _sort_key(slug: str) -> tuple[int, str]:
        issue = task_by_slug[slug].github_issue
        return (issue if issue is not None else sys.maxsize, slug)

    for path, slugs in sorted(file_to_slugs.items()):
        unique_slugs = sorted(set(slugs), key=_sort_key)
        if len(unique_slugs) <= 1:
            continue
        injected: list[str] = []
        for prev, curr in zip(unique_slugs, unique_slugs[1:], strict=False):
            synthetic.setdefault(curr, set()).add(prev)
            injected.append(f"{curr} depends_on {prev}")
        _log(f"Collision detected for {path}: stories={unique_slugs}; injected={injected}")

    return {slug: sorted(deps) for slug, deps in synthetic.items()}


def inject_synthetic_deps(
    tasks: list[TaskStory], synthetic: dict[str, list[str]]
) -> list[TaskStory]:
    augmented: list[TaskStory] = []
    for task in tasks:
        if task.slug not in synthetic:
            augmented.append(task)
            continue
        merged = sorted(set(task.depends_on) | set(synthetic[task.slug]))
        augmented.append(replace(task, depends_on=merged))
    return augmented
