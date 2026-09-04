"""Sprint manifest types and story-path helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..coordinator.state import CoordinatorResult
from ..shape_check.heuristics import derive_fix_ready
from ..task import (
    TaskStory,
    frontmatter_allows_forge_yaml_mutation,
    inspect_story_frontmatter,
)
from .budget import budget_overrun_usd, budget_status

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
    # Slugs whose GitHub issue was already closed at fetch time but which this
    # sprint's own earlier generation ran and paid for, so they were kept as
    # stories rather than reclassified as external closed dependencies (#2847).
    # The runner reads this to keep them out of every dispatch/spend path: their
    # bodies could not be fetched, so they are records, never runnable work.
    reconciled_prior_slugs: set[str] = field(default_factory=set)
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
    # False when at least one story's cost could not be measured. ``total_cost_usd``
    # is then only a measured lower bound, not the sprint's cost (#1992).
    cost_complete: bool = True
    # Every spend source this sprint could not measure (story slugs, intake
    # passes). Recorded so an operator can trace WHICH work is unpriced rather
    # than only that the total is incomplete.
    unmeasured_spend_sources: tuple[str, ...] = ()
    # The subset of ``unmeasured_spend_sources`` no operator has resolved. These
    # are what keeps the budget check closed; the rest are accounted for by an
    # accepted ceiling below (#2310).
    unresolved_unmeasured_spend_sources: tuple[str, ...] = ()
    # Serialized ``AcceptedUnmeasuredSpend`` records in force for this run: the
    # source, its origin, and the ceiling charged in place of the unknown.
    # Acceptance never relabels cost as measured — ``cost_complete`` stays False.
    accepted_unmeasured_spend: tuple[dict, ...] = ()
    # Measured spend plus every accepted ceiling: the figure the cap was
    # verified against. Not a total — part of it stands in for unmeasured spend.
    budget_verification_spend_usd: float = 0.0
    # Measured spend this sprint recorded that no per-story row carries, because
    # it belongs to no story of this sprint — entry-level and mid-sprint intake
    # remediation on issues the sprint never scheduled. Declared here so the
    # sprint-total-versus-story-rows cross-check (#2847) treats it as accounted
    # for rather than as spend nothing explains.
    non_story_spend_usd: float = 0.0
    results: list[tuple[str, CoordinatorResult]] = field(default_factory=list)
    stopped_reason: str | None = None  # why sprint stopped early, if it did

    @property
    def budget_spend_against_cap(self) -> float:
        """The figure the cap is reported against.

        The verification figure where one was computed (it charges accepted
        ceilings for unmeasured spend, so it is never below the measured total),
        falling back to the measured total for results built without it.
        """
        return max(float(self.budget_verification_spend_usd or 0.0), float(self.total_cost_usd))

    @property
    def budget_overrun_usd(self) -> float:
        """How far this run passed its cap, or ``0.0`` when it did not (#2547)."""
        return budget_overrun_usd(
            budget_usd=self.budget_usd, spend_usd=self.budget_spend_against_cap
        )

    @property
    def budget_status(self) -> str:
        """``within``, ``over``, or ``unset`` — see ``sprint.budget``."""
        return budget_status(budget_usd=self.budget_usd, spend_usd=self.budget_spend_against_cap)


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
    parsed = inspect_story_frontmatter(story_path)
    fm = parsed.data

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
    story_type = fm.get("type")
    dependency_warnings: list[str] = []
    type_warnings: list[str] = []
    if parsed.warning is not None:
        dependency_warnings.append(parsed.warning)
        import logging as _logging  # noqa: PLC0415

        _logging.getLogger(__name__).warning(parsed.warning)
    if story_type is None:
        warning = (
            f"file-sourced story {story_path.name!r} has no 'type:' frontmatter field — "
            "treat as migration concern (downstream phases cannot read structured type)"
        )
        type_warnings.append(warning)
        import logging as _logging  # noqa: PLC0415

        _logging.getLogger(__name__).warning(warning)

    body = story_path.read_text(encoding="utf-8")
    fix_ready, investigation_ready, readiness_warnings = derive_fix_ready(story_type, body)
    fm_override = fm.get("fix_ready")
    if isinstance(fm_override, bool):
        if fm_override is True and fix_ready is False:
            readiness_warnings = [
                *readiness_warnings,
                "frontmatter declares fix_ready: true but body lacks a complete "
                "Diagnosis section — honoring frontmatter override",
            ]
        elif fm_override is False and fix_ready is True:
            readiness_warnings = [
                *readiness_warnings,
                "frontmatter explicitly marks fix_ready: false despite a complete body",
            ]
        fix_ready = fm_override

    return TaskStory(
        name=name,
        story_path=story_path,
        slug=slug,
        test_target=fm.get("test_target"),
        gate_override=fm.get("gate"),
        depends_on=depends_on,
        dependency_warnings=dependency_warnings,
        github_issue=github_issue,
        allow_mutate_forge_yaml=frontmatter_allows_forge_yaml_mutation(fm),
        type=story_type,
        type_warnings=type_warnings,
        fix_ready=fix_ready,
        investigation_ready=investigation_ready,
        readiness_warnings=readiness_warnings,
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


def semantic_manifest_admission(issue_number: int, project_root: Path):
    """Return the semantic readiness withholding a manifest issue entry, if any.

    Manifest mode reaches the same admission boundary query mode does
    (``eval.semantic_readiness``), so a document the ready queue and the sprint
    gate refuse for lack of a ratified review is not admitted just because an
    operator named it in a manifest.

    The structural verdict is the authority it always was and is not re-decided
    here: when ``classify_admissibility`` does not admit the document, the
    semantic requirement does not apply and the entry proceeds exactly as
    before. Best-effort — an unreachable ``gh`` or store leaves the entry
    runnable rather than becoming a new silent drop.
    """
    from ..admissibility import classify_admissibility  # noqa: PLC0415
    from ..eval.semantic_readiness import (  # noqa: PLC0415
        SEMANTIC_REVIEW_REQUIRED_STATE,
        semantic_readiness_for_issue,
    )
    from ..eval.semantic_runner import load_semantic_issue  # noqa: PLC0415

    try:
        issue = load_semantic_issue(issue_number=issue_number, project_root=project_root)
        structural = classify_admissibility(issue.title, issue.body, list(issue.labels))
        if not structural.admissible:
            return None
        readiness = semantic_readiness_for_issue(
            issue_number=issue_number,
            title=issue.title,
            body=issue.body,
            labels=issue.labels,
            project_root=project_root,
            lifecycle_state=SEMANTIC_REVIEW_REQUIRED_STATE,
        )
    except Exception:  # noqa: BLE001
        return None
    return readiness if readiness.withholds_admission else None


def build_tasks_from_manifest(
    manifest: SprintManifest,
    project_root: Path,
    *,
    closed_slugs: set[str] | None = None,
    semantic_admission=None,
) -> list[tuple]:
    """Build (task, source, canonical_ref) tuples from a manifest.

    For file entries, resolves the path and builds the task from frontmatter.
    For issue entries, fetches the issue via gh CLI and applies overrides
    (depends_on, slug) from the manifest dict.

    Issue entries also pass the semantic admission boundary before they are
    fetched; one whose policy-required semantic review is not ratified for its
    current revision is dropped with a warning. File stories carry no issue
    type or lifecycle state to apply the policy to, so they are
    ``not_required`` and unaffected.

    If ``closed_slugs`` is provided, issue slugs for any closed issues that are
    dropped will be added to it so callers can mark them as satisfied.
    """
    import sys  # noqa: PLC0415

    from .sources import GitHubIssueSource, IssueClosedError, resolve  # noqa: PLC0415

    # Resolved at call time (not as a default argument) so the boundary stays a
    # patchable seam for callers exercising manifest resolution alone.
    admit_semantically = semantic_admission or semantic_manifest_admission

    results: list[tuple[TaskStory, StorySource, str]] = []
    for entry in manifest.stories:
        if isinstance(entry, dict) and "issue" in entry and admit_semantically is not None:
            withheld = admit_semantically(int(entry["issue"]), project_root)
            if withheld is not None:
                print(
                    f"[sprint] WARNING: skipping issue:{entry['issue']} — "
                    f"{withheld.reason_code}: {withheld.detail}",
                    file=sys.stderr,
                )
                continue
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
