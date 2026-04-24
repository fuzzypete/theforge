"""Sprint manifest types and story-path helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..coordinator.state import CoordinatorResult
from ..task import TaskStory

if TYPE_CHECKING:
    from .sources import StorySource


@dataclass
class ResolvedSprint:
    """Common runtime shape produced by both manifest loading and GitHub query mode.

    Both ``resolve_from_manifest`` and GitHub query mode produce this object.
    The sprint runner (``run_sprint``) operates entirely on ``ResolvedSprint``
    with no assumptions about a manifest file existing on disk.
    """

    name: str
    budget_usd: float
    stories: list[tuple[TaskStory, StorySource, str]]  # (task, source, canonical_ref)
    max_parallel: int | None = None
    worker_timeout_seconds: int | None = None
    closed_dependency_slugs: set[str] = field(default_factory=set)
    baseline_gate: dict[str, Any] | None = None


@dataclass
class SprintManifest:
    """Parsed sprint.yaml manifest."""

    name: str
    budget_usd: float
    stories: list[str | dict]  # relative paths to story files or {issue: N} dicts
    max_parallel: int | None = None
    worker_timeout_seconds: int | None = None


@dataclass
class SprintResult:
    """Aggregate result from running a sprint."""

    name: str
    specs_total: int
    specs_succeeded: int
    specs_failed: int
    specs_skipped: int  # ALREADY_DONE or budget-stopped
    total_cost_usd: float
    budget_usd: float
    results: list[tuple[str, CoordinatorResult]] = field(default_factory=list)
    stopped_reason: str | None = None  # why sprint stopped early, if it did


def load_sprint_manifest(manifest_path: Path) -> SprintManifest:
    """Load and validate a sprint.yaml manifest.

    Raises ValueError if the manifest is invalid.
    """
    if not manifest_path.exists():
        raise ValueError(f"Sprint manifest not found: {manifest_path}")

    with open(manifest_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Sprint manifest must be a YAML mapping: {manifest_path}")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("Sprint manifest must have a non-empty 'name' field")

    budget_usd = raw.get("budget_usd")
    if budget_usd is None:
        raise ValueError("Sprint manifest must have a 'budget_usd' field")
    try:
        budget_usd = float(budget_usd)
    except (TypeError, ValueError):
        raise ValueError(f"Sprint 'budget_usd' must be a number, got {budget_usd!r}")
    if budget_usd <= 0:
        raise ValueError(f"Sprint 'budget_usd' must be > 0, got {budget_usd}")

    max_parallel_raw = raw.get("max_parallel")
    if max_parallel_raw is not None:
        if not isinstance(max_parallel_raw, int):
            raise ValueError(f"Sprint 'max_parallel' must be an integer, got {max_parallel_raw!r}")
        if max_parallel_raw < 1:
            raise ValueError(f"Sprint 'max_parallel' must be >= 1, got {max_parallel_raw}")
    max_parallel = max_parallel_raw

    worker_timeout_raw = raw.get("worker_timeout_seconds")
    if worker_timeout_raw is not None:
        if not isinstance(worker_timeout_raw, int):
            raise ValueError(
                f"Sprint 'worker_timeout_seconds' must be an integer, got {worker_timeout_raw!r}"
            )
        if worker_timeout_raw < 1:
            raise ValueError(
                f"Sprint 'worker_timeout_seconds' must be >= 1, got {worker_timeout_raw}"
            )
    worker_timeout_seconds = worker_timeout_raw

    # Accept both 'stories' (new) and 'specs' (deprecated) keys
    stories = raw.get("stories")
    if stories is None:
        specs_legacy = raw.get("specs")
        if specs_legacy is not None:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Sprint manifest uses deprecated 'specs:' key — rename to 'stories:'"
            )
            stories = specs_legacy
    if not stories or not isinstance(stories, list):
        raise ValueError("Sprint manifest must have a non-empty 'stories' list")
    for entry in stories:
        if isinstance(entry, str):
            continue
        if isinstance(entry, dict):
            if "issue" not in entry:
                raise ValueError(f"Dict entries in 'stories' must have an 'issue' key: {entry!r}")
            if not isinstance(entry["issue"], int):
                raise ValueError(
                    f"'issue' value must be an integer, got {type(entry['issue']).__name__}: "
                    f"{entry!r}"
                )
            continue
        raise ValueError(
            f"Entries in 'stories' must be strings or dicts, got {type(entry).__name__}: {entry!r}"
        )

    return SprintManifest(
        name=name,
        budget_usd=budget_usd,
        stories=stories,
        max_parallel=max_parallel,
        worker_timeout_seconds=worker_timeout_seconds,
    )


def _validate_story_paths(manifest: SprintManifest, project_root: Path) -> list[Path]:
    """Resolve and validate all story paths. Raises ValueError if any are missing.

    Only validates string entries (file paths). Dict entries (issue refs) are
    skipped since they are validated at runtime by GitHubIssueSource.fetch().
    """
    resolved: list[Path] = []
    missing: list[str] = []
    for entry in manifest.stories:
        if not isinstance(entry, str):
            continue  # skip issue entries
        path = (project_root / entry).resolve()
        if not path.exists():
            missing.append(entry)
        else:
            resolved.append(path)
    if missing:
        raise ValueError(
            f"Sprint manifest references {len(missing)} missing story/stories:\n"
            + "\n".join(f"  {s}" for s in missing)
        )
    return resolved


def _build_task_from_story(story_path: Path) -> TaskStory:
    """Build a TaskStory from a story file using frontmatter if available."""
    # Import here to avoid circular imports; cli._build_task is essentially the same logic
    text = story_path.read_text(encoding="utf-8")
    fm: dict = {}
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            try:
                parsed = yaml.safe_load(text[3:end].strip()) or {}
                if isinstance(parsed, dict):
                    fm = parsed
            except yaml.YAMLError:
                pass

    slug = fm.get("slug") or story_path.stem
    name = fm.get("name", story_path.stem.replace("_", " ").replace("-", " ").title())
    raw_deps = fm.get("depends_on", [])
    if isinstance(raw_deps, str):
        depends_on = [raw_deps]
    elif isinstance(raw_deps, list):
        depends_on = [str(d) for d in raw_deps]
    else:
        depends_on = []
    raw_issue = fm.get("github_issue")
    try:
        github_issue = int(raw_issue) if raw_issue is not None else None
    except (ValueError, TypeError):
        github_issue = None
    return TaskStory(
        name=name,
        story_path=story_path,
        slug=slug,
        test_target=fm.get("test_target"),
        gate_override=fm.get("gate"),
        depends_on=depends_on,
        github_issue=github_issue,
    )


def resolve_from_manifest(manifest_path: Path, project_root: Path) -> ResolvedSprint:
    """Load a sprint manifest and resolve all stories into a ResolvedSprint.

    Combines ``load_sprint_manifest``, ``_validate_story_paths``, and
    ``build_tasks_from_manifest`` into a single call that returns the common
    runtime object accepted by ``run_sprint``.
    """
    manifest = load_sprint_manifest(manifest_path)
    _validate_story_paths(manifest, project_root)
    _closed: set[str] = set()
    task_entries = build_tasks_from_manifest(manifest, project_root, closed_slugs=_closed)
    return ResolvedSprint(
        name=manifest.name,
        budget_usd=manifest.budget_usd,
        stories=task_entries,
        max_parallel=manifest.max_parallel,
        worker_timeout_seconds=manifest.worker_timeout_seconds,
        closed_dependency_slugs=_closed,
    )


def build_tasks_from_manifest(
    manifest: SprintManifest,
    project_root: Path,
    *,
    closed_slugs: set[str] | None = None,
) -> list[tuple]:
    """Build (task, source, canonical_ref) tuples from a manifest.

    For file entries, resolves the path and builds the task from frontmatter.
    For issue entries, fetches the issue via gh CLI and applies overrides
    (depends_on, slug) from the manifest dict.

    If ``closed_slugs`` is provided, issue slugs for any closed issues that are
    dropped will be added to it so callers can mark them as satisfied.
    """
    import sys  # noqa: PLC0415

    from .sources import GitHubIssueSource, IssueClosedError, resolve  # noqa: PLC0415

    results: list[tuple[TaskStory, StorySource, str]] = []
    for entry in manifest.stories:
        source, ref, canonical_ref = resolve(entry, project_root)
        try:
            task = source.fetch(ref, project_root)
        except IssueClosedError as exc:
            print(f"[sprint] WARNING: skipping {canonical_ref} — {exc}", file=sys.stderr)
            if (
                closed_slugs is not None
                and isinstance(source, GitHubIssueSource)
                and ref.isdigit()
            ):
                closed_slugs.add(f"issue-{ref}")
            continue

        # Apply overrides from dict entries
        if isinstance(entry, dict):
            overrides: dict = {}
            if "slug" in entry:
                overrides["slug"] = entry["slug"]
            if "depends_on" in entry:
                raw_deps = entry["depends_on"]
                if isinstance(raw_deps, str):
                    overrides["depends_on"] = [raw_deps]
                elif isinstance(raw_deps, list):
                    overrides["depends_on"] = [str(d) for d in raw_deps]
                else:
                    raise ValueError(
                        f"'depends_on' must be a string or list, got "
                        f"{type(raw_deps).__name__}: {entry!r}"
                    )
            if "test_target" in entry:
                overrides["test_target"] = entry["test_target"]
            if overrides:
                from dataclasses import replace

                if "depends_on" in overrides:
                    combined = list(task.depends_on)
                    for dep in overrides["depends_on"]:
                        if dep not in combined:
                            combined.append(dep)
                    overrides["depends_on"] = combined
                task = replace(task, **overrides)
                # Update canonical_ref if slug was overridden
                if "slug" in overrides:
                    canonical_ref = f"issue:{entry['issue']}"

        results.append((task, source, canonical_ref))
    return results
