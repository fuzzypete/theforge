from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace

from ..config import ForgeConfig
from ..coordinator.engine import run_task
from ..coordinator.state import CoordinatorState, Phase
from ..log_util import _log_line
from ..task import TaskStory


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


@dataclass(frozen=True)
class BundleHint:
    """Deterministic bundling inputs derived from a task and its preflight state."""

    slug: str
    work_type: str | None
    complexity: str | None
    likely_files: tuple[str, ...] | None
    bundle_candidate: bool
    area: str | None


def _normalize_area_label(area: str | None) -> str | None:
    if area is None:
        return None
    normalized = area.strip().lower()
    return normalized or None


def _extract_area_label(task: TaskStory) -> str | None:
    story_text = task.story_text or ""
    for line in story_text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("area:"):
            continue
        return _normalize_area_label(stripped.split(":", 1)[1])
    return None


def build_bundle_hint(task: TaskStory, state: CoordinatorState) -> BundleHint:
    work_type = state.preflight_work_type
    complexity = state.preflight_complexity
    bundle_candidate = bool(
        state.preflight_bundle_candidate
        and work_type in {"bug", "mechanical"}
        and complexity == "small"
    )
    return BundleHint(
        slug=task.slug,
        work_type=work_type,
        complexity=complexity,
        likely_files=(
            None
            if state.preflight_likely_files is None
            else tuple(sorted(set(state.preflight_likely_files)))
        ),
        bundle_candidate=bundle_candidate,
        area=_extract_area_label(task),
    )


def _bundle_sort_key(task: TaskStory) -> tuple[int, str]:
    issue = task.github_issue
    return (issue if issue is not None else sys.maxsize, task.slug)


def _tasks_overlap_by_signal(left: BundleHint, right: BundleHint) -> bool:
    if left.area is not None and left.area == right.area:
        return True
    if left.likely_files is None or right.likely_files is None:
        return True
    return bool(set(left.likely_files) & set(right.likely_files))


def _complexity_weight(complexity: str | None) -> int | None:
    if complexity == "small":
        return 1
    if complexity == "medium":
        return 2
    if complexity == "large":
        return 3
    return None


def compute_bundle_assignments(
    preflight_states: dict[str, CoordinatorState],
    tasks: list[TaskStory],
    *,
    complexity_ceiling: int = 5,
) -> list[list[str]]:
    task_by_slug = {task.slug: task for task in tasks}
    hints = {
        slug: build_bundle_hint(task_by_slug[slug], state)
        for slug, state in preflight_states.items()
        if slug in task_by_slug
    }

    eligible = [
        task
        for task in sorted(tasks, key=_bundle_sort_key)
        if hints.get(task.slug) is not None and hints[task.slug].bundle_candidate
    ]

    bundles: list[list[str]] = []
    used: set[str] = set()

    for task in eligible:
        if task.slug in used:
            continue
        hint = hints[task.slug]
        bundle = [task.slug]
        used.add(task.slug)
        total_complexity = _complexity_weight(hint.complexity) or 0

        for candidate in eligible:
            if candidate.slug in used:
                continue
            candidate_hint = hints[candidate.slug]
            if candidate_hint.work_type != hint.work_type:
                continue
            candidate_complexity = _complexity_weight(candidate_hint.complexity)
            if candidate_complexity is None:
                continue
            if total_complexity + candidate_complexity > complexity_ceiling:
                continue
            if not _tasks_overlap_by_signal(hint, candidate_hint):
                continue
            if task.slug in candidate.depends_on or candidate.slug in task.depends_on:
                continue
            if any(
                existing in candidate.depends_on
                or candidate.slug in task_by_slug[existing].depends_on
                for existing in bundle
            ):
                continue
            bundle.append(candidate.slug)
            used.add(candidate.slug)
            total_complexity += candidate_complexity

        if len(bundle) > 1:
            bundles.append(bundle)

    return bundles


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
                states[task.slug] = CoordinatorState(preflight_likely_files=None)
                continue

            if not result.success:
                _log(
                    "WARNING: batch preflight returned failure for "
                    f"{task.slug}: {result.message}; excluding from collision detection"
                )
                states[task.slug] = CoordinatorState(preflight_likely_files=None)
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
        if state.preflight_likely_files is None:
            continue
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
