"""Sprint manifest types and story-path helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..coordinator.state import CoordinatorResult
from ..task import TaskStory


@dataclass
class SprintManifest:
    """Parsed sprint.yaml manifest."""

    name: str
    budget_usd: float
    stories: list[str]  # relative paths to story files
    max_parallel: int | None = None


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
    if not all(isinstance(s, str) for s in stories):
        raise ValueError("All entries in 'stories' must be strings (file paths)")

    return SprintManifest(
        name=name, budget_usd=budget_usd, stories=stories, max_parallel=max_parallel
    )


def _validate_story_paths(manifest: SprintManifest, project_root: Path) -> list[Path]:
    """Resolve and validate all story paths. Raises ValueError if any are missing."""
    resolved: list[Path] = []
    missing: list[str] = []
    for spec_str in manifest.stories:
        path = (project_root / spec_str).resolve()
        if not path.exists():
            missing.append(spec_str)
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
    return TaskStory(
        name=name,
        story_path=story_path,
        slug=slug,
        pytest_target=fm.get("pytest_target"),
        gate_override=fm.get("gate"),
        depends_on=depends_on,
    )
