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
    # Eligibility is per-story prerequisite only — work_type + complexity. The
    # cross-story relational decision (overlap with a sibling) lives in
    # compute_bundle_assignments. The legacy LLM-emitted preflight_bundle_candidate
    # flag is no longer consulted here; the field is now scheduler-written audit
    # output meaning "the scheduler placed this story in a bundle".
    work_type = state.preflight_work_type
    complexity = state.preflight_complexity
    bundle_candidate = bool(work_type in {"bug", "mechanical"} and complexity == "small")
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


def _bundle_overlap(left: BundleHint, right: BundleHint) -> bool:
    """Bundling overlap predicate — fail-closed on unknown footprint.

    Returning True means the two stories may safely share a single dev pass.
    Unknown likely_files on either side is NOT enough to glue stories together
    (would risk merging unrelated work into one PR), so we require an explicit
    overlap signal: matching area: labels, OR a known-files intersection.
    """
    if left.area is not None and left.area == right.area:
        return True
    if left.likely_files is None or right.likely_files is None:
        return False
    return bool(set(left.likely_files) & set(right.likely_files))


def _collision_overlap(left: BundleHint, right: BundleHint) -> bool:
    """Collision-DAG overlap predicate — fail-closed on unknown footprint.

    Returning True means the two stories must serialize. Unknown likely_files
    forces serialization because letting an undetected conflict run in parallel
    is a worse failure than over-serializing safe parallel work.
    """
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
            if not _bundle_overlap(hint, candidate_hint):
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
    deps: dict[str, set[str]] = {task.slug: set(task.depends_on) for task in tasks}
    synthetic: dict[str, set[str]] = {}

    # A story whose preflight_likely_files is None (preflight failed/excluded) or
    # empty ([]) has an *unknown* footprint. Absence of evidence is not evidence
    # of no collision: such a story could touch any file, so it must serialize
    # conservatively against every other story rather than silently race them to
    # a merge-time conflict. Stories with a known, non-empty footprint continue
    # to serialize only on an actual file intersection, so genuinely disjoint
    # work still runs in parallel.
    unknown_slugs: list[str] = []
    for slug, state in preflight_states.items():
        if slug not in task_by_slug:
            continue
        if not state.preflight_likely_files:
            unknown_slugs.append(slug)
            continue
        for path in state.preflight_likely_files:
            file_to_slugs.setdefault(path, []).append(slug)

    def _sort_key(slug: str) -> tuple[int, str]:
        issue = task_by_slug[slug].github_issue
        return (issue if issue is not None else sys.maxsize, slug)

    def _has_dependency_path(start: str, target: str) -> bool:
        stack = list(deps.get(start, set()))
        seen: set[str] = set()
        while stack:
            slug = stack.pop()
            if slug == target:
                return True
            if slug in seen:
                continue
            seen.add(slug)
            stack.extend(deps.get(slug, set()) - seen)
        return False

    def _serialize(prev: str, curr: str) -> str | None:
        """Inject a serialize edge (curr depends_on prev). Returns a skip reason
        if the edge already exists or would cycle, else None (edge injected)."""
        if _has_dependency_path(curr, prev):
            return f"{curr} already depends_on {prev}"
        if _has_dependency_path(prev, curr):
            return f"{curr} depends_on {prev} would cycle"
        synthetic.setdefault(curr, set()).add(prev)
        deps.setdefault(curr, set()).add(prev)
        return None

    for path, slugs in sorted(file_to_slugs.items()):
        unique_slugs = sorted(set(slugs), key=_sort_key)
        if len(unique_slugs) <= 1:
            continue
        injected: list[str] = []
        skipped: list[str] = []
        for prev, curr in zip(unique_slugs, unique_slugs[1:], strict=False):
            reason = _serialize(prev, curr)
            if reason is None:
                injected.append(f"{curr} depends_on {prev}")
            else:
                skipped.append(reason)
        detail = f"Collision detected for {path}: stories={unique_slugs}; injected={injected}"
        if skipped:
            detail += f"; skipped={skipped}"
        _log(detail)

    # Conservative fallback for unknown footprints. A story with a known,
    # non-empty likely_files set makes a concrete claim about which files it
    # touches; an unknown-footprint sibling cannot be proven disjoint from that
    # claim, so it is serialized against it rather than left to race to a
    # merge-time conflict. Edges are pairwise (not a chain), so known, disjoint
    # siblings are not serialized against each other as a side effect — only the
    # unknown story is pinned relative to each concrete claim.
    #
    # Two prediction-less stories are deliberately NOT serialized against each
    # other: there is no concrete file claim on either side and no evidence of
    # overlap, so chaining every prediction-less story would collapse all
    # parallelism on zero signal (e.g. a fully offline sprint where preflight
    # could not run at all). That residual overlap is left to the integration
    # step to detect and recover.
    known_slugs = sorted(
        (
            slug
            for slug, state in preflight_states.items()
            if state.preflight_likely_files and slug in task_by_slug
        ),
        key=_sort_key,
    )
    for unknown in sorted(set(unknown_slugs), key=_sort_key):
        injected = []
        skipped = []
        for known in known_slugs:
            if known == unknown:
                continue
            prev, curr = sorted((unknown, known), key=_sort_key)
            reason = _serialize(prev, curr)
            if reason is None:
                injected.append(f"{curr} depends_on {prev}")
            else:
                skipped.append(reason)
        if not injected and not skipped:
            continue
        detail = (
            f"Unknown footprint for {unknown} (empty/None likely_files): "
            f"serializing conservatively against known-footprint siblings; "
            f"injected={injected}"
        )
        if skipped:
            detail += f"; skipped={skipped}"
        _log(detail)

    return {slug: sorted(deps) for slug, deps in synthetic.items()}


def inject_synthetic_deps(
    tasks: list[TaskStory], synthetic: dict[str, list[str]]
) -> list[TaskStory]:
    augmented: list[TaskStory] = []
    for task in tasks:
        if task.slug not in synthetic:
            augmented.append(task)
            continue
        merged = sorted(set(task.collision_deps) | set(synthetic[task.slug]))
        augmented.append(replace(task, collision_deps=merged))
    return augmented
