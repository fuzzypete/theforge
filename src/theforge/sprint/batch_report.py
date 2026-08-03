"""Post-sprint batchability analytics — *would* batching have been cheaper?

This module answers a measurement question, not a scheduling one. Cost-aware
batch groups (``sprint/collision.py``) decide, live, which small independent
stories share one dev pass. This report runs *after* a sprint and asks the
question that should precede enabling that feature: of the stories that actually
ran, which ones would have qualified, what did they really cost, and would a
shared dev pass have cost less?

Like the RCA engine it is a **pure function of on-disk artifacts** — no runtime
state, no coordinator, no LLM::

    sprint-summary.yaml
      + <slug>/audit.yaml        (phase costs, outcome, dev/review counts,
      + <slug>/preflight.yaml     sufficiency + likely_files footprint)
      + sprint-rca.yaml (optional, enrichment only)
            ──▶  build_batch_report()  ──▶  BatchabilityReport
                                             ├─▶ render_terminal()
                                             └─▶ report_payload()  (YAML/JSON)

Both renderers derive from the same :class:`BatchabilityReport`, so the
human-readable and machine-readable views cannot drift.

**Eligibility** is deliberately stricter than the live batcher's. The live rules
are all *preflight* facts (small / bug-or-mechanical / implementation_ready /
known, bounded footprint) because that is all a scheduler knows before the work
runs. After the fact we also know how the story actually went, so a story that
conflicted, retried, or escalated is disqualified too: batching such a story
would have dragged its whole group through the same trouble.

**The batched-cost figure is an estimate, and is labelled as one everywhere it
appears.** See :data:`METHODOLOGY` for the model and what it assumes away.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .rca import DONE_OUTCOMES, RCA_FILENAME

SCHEMA_VERSION = 1

#: Phase cost breakdown reported per story, in execution order.
PHASES: tuple[str, ...] = (
    "preflight",
    "plan",
    "plan_review",
    "dev",
    "validate",
    "review",
)

#: Phases a batch group would run *once* for the whole group. ``preflight`` is
#: absent on purpose: batch eligibility is decided from per-story preflight
#: facts, so preflight still runs per story and its cost is never amortised.
SHARED_PHASES: tuple[str, ...] = tuple(p for p in PHASES if p != "preflight")

#: Outcomes that mean the story hit a merge conflict rather than landing clean.
CONFLICT_OUTCOMES = frozenset({"MERGE_FAILED", "MERGE_ARMING_FAILED"})
ESCALATION_OUTCOMES = frozenset({"ESCALATE", "ESCALATED"})

_COMPLEXITY_WEIGHT = {"small": 1, "medium": 2, "large": 3}

# Analysis parameters. Deliberately independent of the project's ``sprint.batch``
# config: batching is off by default, and a report that inherited the off switch
# would answer "no opportunity" for every sprint — which is the very question it
# exists to measure. Callers may vary these to run a sensitivity analysis.
DEFAULT_MAX_STORIES = 3
DEFAULT_MAX_COMPLEXITY_BUDGET = 3
DEFAULT_MAX_TOUCHED_FILES = 6

METHODOLOGY = (
    "actual_combined_cost_usd is measured: the sum of each member's recorded "
    "phase costs. hypothetical_batched_cost_usd is an ESTIMATE, not a "
    "measurement: per-story preflight cost is kept (eligibility is decided from "
    "per-story preflight facts, so preflight does not amortise), and each shared "
    "downstream phase (plan, plan_review, dev, validate, review) is charged at "
    "the MAX measured cost across the group's members — the optimistic bound "
    "where one pass over the combined work costs no more than the most expensive "
    "member's solo pass. It assumes away prompt growth, broader test surface and "
    "harder review on a multi-subject diff, so treat it as a ceiling on savings, "
    "not a forecast."
)


# ── Report data ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoryBatchReport:
    """One story's batchability evidence and measured cost breakdown."""

    slug: str
    path: str
    outcome: str
    work_type: str | None
    complexity: str | None
    sufficiency: str | None
    likely_files: tuple[str, ...]
    files_touched: tuple[str, ...]
    depends_on: tuple[str, ...]
    #: Slugs of sibling stories that declared a ``depends_on`` edge *to* this
    #: story. Recorded separately from ``depends_on`` because a dependency
    #: disqualifies *both* endpoints, and only this side is invisible from the
    #: story's own summary row.
    dependents: tuple[str, ...]
    dev_iterations: int | None
    review_cycles: int | None
    #: Per-phase cost. ``0.0`` = phase did not run; ``None`` = it ran but its
    #: spend was never measured, which must never render as free (#1992).
    phase_costs: dict[str, float | None]
    #: Sum of the *measured* phase costs only.
    total_cost_usd: float
    #: False when at least one phase that ran has an unmeasured cost.
    cost_complete: bool
    conflicted: bool
    retried: bool
    escalated: bool
    eligible: bool
    disqualifiers: tuple[str, ...]
    rca_class: str | None = None


@dataclass(frozen=True)
class BatchGroupReport:
    """A hypothetical batch group, with measured vs estimated cost."""

    group_id: str
    members: tuple[str, ...]
    combined_files: tuple[str, ...]
    actual_combined_cost_usd: float
    hypothetical_batched_cost_usd: float
    estimated_savings_usd: float
    cheaper_if_batched: bool
    cost_complete: bool


@dataclass(frozen=True)
class BatchabilityReport:
    """Everything the terminal and structured renderers need."""

    run_id: str
    sprint_name: str
    sprint_total_cost_usd: float | None
    rules: dict[str, int]
    stories: tuple[StoryBatchReport, ...]
    groups: tuple[BatchGroupReport, ...]

    @property
    def qualified(self) -> tuple[StoryBatchReport, ...]:
        return tuple(s for s in self.stories if s.eligible)

    @property
    def disqualified(self) -> tuple[StoryBatchReport, ...]:
        return tuple(s for s in self.stories if not s.eligible)

    @property
    def totals(self) -> dict:
        grouped = {slug for group in self.groups for slug in group.members}
        actual = sum(g.actual_combined_cost_usd for g in self.groups)
        batched = sum(g.hypothetical_batched_cost_usd for g in self.groups)
        return {
            "story_count": len(self.stories),
            "qualified_count": len(self.qualified),
            "disqualified_count": len(self.disqualified),
            "grouped_story_count": len(grouped),
            "group_count": len(self.groups),
            "actual_grouped_cost_usd": _round(actual),
            "hypothetical_batched_cost_usd": _round(batched),
            "estimated_savings_usd": _round(actual - batched),
            "cheaper_if_batched": actual - batched > 0,
            "cost_complete": all(g.cost_complete for g in self.groups),
        }


# ── Builder ───────────────────────────────────────────────────────────────────


def build_batch_report(
    summary_path: Path,
    *,
    run_id: str | None = None,
    max_stories: int = DEFAULT_MAX_STORIES,
    max_complexity_budget: int = DEFAULT_MAX_COMPLEXITY_BUDGET,
    max_touched_files: int = DEFAULT_MAX_TOUCHED_FILES,
) -> BatchabilityReport | None:
    """Build the batchability report for a completed sprint summary.

    Returns ``None`` when ``summary_path`` is missing or unreadable — the caller
    decides how to surface that, rather than this module printing or guessing.
    """
    summary = _load_yaml(summary_path)
    if not isinstance(summary, dict):
        return None

    sprint_block = summary.get("sprint") if isinstance(summary.get("sprint"), dict) else {}
    resolved_run_id = str(sprint_block.get("run_id") or run_id or "")
    sprint_log_dir = summary_path.parent
    rca_classes = _rca_classes(sprint_log_dir, resolved_run_id)

    rows = [
        raw
        for raw in (summary.get("stories") or [])
        if isinstance(raw, dict) and str(raw.get("slug") or "")
    ]
    dependents = _dependents_by_slug(rows)

    stories: list[StoryBatchReport] = []
    for raw in rows:
        slug = str(raw.get("slug"))
        stories.append(
            _build_story_report(
                raw,
                sprint_log_dir / slug,
                dependents=dependents.get(slug, ()),
                rca_class=rca_classes.get(slug),
                max_touched_files=max_touched_files,
            )
        )

    groups = _compute_groups(
        stories,
        max_stories=max_stories,
        max_complexity_budget=max_complexity_budget,
        max_touched_files=max_touched_files,
    )

    total = sprint_block.get("total_cost_usd")
    return BatchabilityReport(
        run_id=resolved_run_id or str(run_id or ""),
        sprint_name=str(sprint_block.get("name") or resolved_run_id or "sprint"),
        sprint_total_cost_usd=float(total) if isinstance(total, (int, float)) else None,
        rules={
            "max_stories": max_stories,
            "max_complexity_budget": max_complexity_budget,
            "max_touched_files": max_touched_files,
        },
        stories=tuple(stories),
        groups=tuple(groups),
    )


def _dependents_by_slug(rows: list[dict]) -> dict[str, tuple[str, ...]]:
    """Reverse the sprint's ``depends_on`` edges: slug ─▶ slugs depending on it.

    A dependency disqualifies *both* endpoints, matching
    ``collision.compute_batch_groups``: batch members are dispatched as one unit
    and only the group leader lands a branch, so an edge crossing a group
    boundary is a scheduling constraint the batch cannot honour — in either
    direction. The child endpoint is visible on its own summary row; the parent
    endpoint is only knowable sprint-wide, which is why it is computed here.

    Only declared ``depends_on`` edges are recoverable: the sprint summary does
    not persist the ``collision_deps`` the collision detector injects, so a
    story serialized purely by a synthetic edge is not excluded on that basis.
    Its overlapping ``likely_files`` — the same signal that produced the
    synthetic edge — still keeps it out of any group at the grouping step.
    """
    dependents: dict[str, list[str]] = {}
    for raw in rows:
        slug = str(raw.get("slug"))
        for target in _slug_tuple(raw.get("depends_on")):
            dependents.setdefault(target, []).append(slug)
    return {target: tuple(sorted(set(slugs))) for target, slugs in dependents.items()}


def _build_story_report(
    raw: dict,
    story_log_dir: Path,
    *,
    dependents: tuple[str, ...],
    rca_class: str | None,
    max_touched_files: int,
) -> StoryBatchReport:
    slug = str(raw.get("slug") or "")
    audit = _load_yaml(story_log_dir / "audit.yaml")
    audit = audit if isinstance(audit, dict) else {}
    preflight_artifact = _load_yaml(story_log_dir / "preflight.yaml")
    preflight_artifact = preflight_artifact if isinstance(preflight_artifact, dict) else {}
    audit_preflight = audit.get("preflight") if isinstance(audit.get("preflight"), dict) else {}

    outcome = str(raw.get("outcome") or "").upper()
    depends_on = _slug_tuple(raw.get("depends_on"))

    # audit.yaml is the coordinator's canonical record; preflight.yaml carries
    # the two fields the audit block omits (sufficiency, likely_files) and backs
    # up the rest for older/partial audits.
    work_type = _first_str(audit_preflight.get("work_type"), preflight_artifact.get("work_type"))
    complexity = _first_str(
        audit_preflight.get("complexity"), preflight_artifact.get("complexity")
    )
    sufficiency = _first_str(
        preflight_artifact.get("sufficiency"), audit_preflight.get("sufficiency")
    )
    likely_files = _file_tuple(
        preflight_artifact.get("likely_files") or audit_preflight.get("likely_files")
    )

    iterations = audit.get("iterations") if isinstance(audit.get("iterations"), dict) else {}
    files_touched = _files_touched(iterations)
    dev_iterations = _iteration_count(
        iterations.get("dev_attempts_total"), raw.get("iteration_usage"), "dev"
    )
    review_cycles = _iteration_count(
        iterations.get("review_cycles_total"), raw.get("iteration_usage"), "review"
    )

    phase_costs = _phase_costs(audit)
    measured = [c for c in phase_costs.values() if c is not None]
    total_cost = _round(sum(measured))
    cost_complete = all(c is not None for c in phase_costs.values())

    conflict_detail = _conflict_detail(outcome, raw, audit)
    conflicted = conflict_detail is not None
    retried = bool((dev_iterations or 0) > 1 or (review_cycles or 0) > 1)
    escalated = outcome in ESCALATION_OUTCOMES or bool(audit.get("escalation"))

    disqualifiers = _disqualifiers(
        outcome=outcome,
        depends_on=depends_on,
        dependents=dependents,
        conflict_detail=conflict_detail,
        retried=retried,
        dev_iterations=dev_iterations,
        review_cycles=review_cycles,
        escalated=escalated,
        work_type=work_type,
        complexity=complexity,
        sufficiency=sufficiency,
        likely_files=likely_files,
        max_touched_files=max_touched_files,
    )

    return StoryBatchReport(
        slug=slug,
        path=str(raw.get("path") or slug),
        outcome=outcome,
        work_type=work_type,
        complexity=complexity,
        sufficiency=sufficiency,
        likely_files=likely_files,
        files_touched=files_touched,
        depends_on=depends_on,
        dependents=dependents,
        dev_iterations=dev_iterations,
        review_cycles=review_cycles,
        phase_costs=phase_costs,
        total_cost_usd=total_cost,
        cost_complete=cost_complete,
        conflicted=conflicted,
        retried=retried,
        escalated=escalated,
        eligible=not disqualifiers,
        disqualifiers=tuple(disqualifiers),
        rca_class=rca_class,
    )


def _disqualifiers(
    *,
    outcome: str,
    depends_on: tuple[str, ...],
    dependents: tuple[str, ...],
    conflict_detail: str | None,
    retried: bool,
    dev_iterations: int | None,
    review_cycles: int | None,
    escalated: bool,
    work_type: str | None,
    complexity: str | None,
    sufficiency: str | None,
    likely_files: tuple[str, ...],
    max_touched_files: int,
) -> list[str]:
    """Every reason this story would not have joined a batch — not just the first.

    A report exists to show evidence, so all failing gates are listed. The
    preflight gates mirror ``collision.build_batch_hint``; the outcome/conflict/
    retry/escalation gates are the post-sprint additions only hindsight affords.
    """
    reasons: list[str] = []

    if outcome not in DONE_OUTCOMES:
        reasons.append(f"outcome={outcome or 'unknown'} (batching requires a completed story)")
    # Both endpoints of a dependency edge are excluded, not just the child.
    if depends_on:
        reasons.append(f"dependency edge (depends_on: {', '.join(depends_on)})")
    if dependents:
        reasons.append(f"dependency edge (depended on by: {', '.join(dependents)})")
    if conflict_detail:
        reasons.append(f"conflicted — {conflict_detail}")
    if retried:
        reasons.append(
            f"retried ({dev_iterations or 0} dev iterations, {review_cycles or 0} review cycles)"
        )
    if escalated:
        reasons.append("escalated")

    if complexity != "small":
        reasons.append(f"complexity={complexity or 'unknown'} (batching requires small)")
    if work_type not in _batch_work_types():
        reasons.append(f"work_type={work_type or 'unknown'} (batching requires bug/mechanical)")
    if sufficiency != "implementation_ready":
        reasons.append(
            f"sufficiency={sufficiency or 'unknown'} (batching requires implementation_ready)"
        )
    if not likely_files:
        reasons.append("unknown touched-file footprint")
    elif len(likely_files) > max_touched_files:
        reasons.append(f"touches {len(likely_files)} files (limit {max_touched_files})")

    return reasons


def _batch_work_types() -> frozenset[str]:
    """The live batcher's work-type gate, read from its definition.

    Imported lazily: ``sprint.collision`` pulls in the coordinator engine, and
    this module is otherwise a pure artifact reader. Importing the constant
    rather than restating it keeps the report from drifting away from the
    scheduling rule it is meant to be measuring.
    """
    from .collision import BATCH_WORK_TYPES  # noqa: PLC0415

    return BATCH_WORK_TYPES


def _compute_groups(
    stories: list[StoryBatchReport],
    *,
    max_stories: int,
    max_complexity_budget: int,
    max_touched_files: int,
) -> list[BatchGroupReport]:
    """Greedily pack eligible stories into independent groups.

    Mirrors ``collision.compute_batch_groups``: deterministic issue/slug order,
    pairwise *non*-overlapping footprints (overlap makes it a conflict-bundle
    question, not a cost question), and combined footprint/complexity ceilings.
    The ``area:`` label the live predicate also consults is not recoverable from
    sprint artifacts, so independence here rests on file footprints alone.
    """
    eligible = sorted((s for s in stories if s.eligible), key=_sort_key)
    by_slug = {s.slug: s for s in eligible}
    groups: list[BatchGroupReport] = []
    used: set[str] = set()

    for story in eligible:
        if story.slug in used:
            continue
        members = [story.slug]
        used.add(story.slug)
        budget = _COMPLEXITY_WEIGHT.get(story.complexity or "", 0)
        footprint = set(story.likely_files)

        for candidate in eligible:
            if candidate.slug in used or len(members) >= max_stories:
                continue
            weight = _COMPLEXITY_WEIGHT.get(candidate.complexity or "")
            if weight is None:
                continue
            if budget + weight > max_complexity_budget:
                continue
            candidate_files = set(candidate.likely_files)
            if len(footprint | candidate_files) > max_touched_files:
                continue
            if footprint & candidate_files:
                continue
            members.append(candidate.slug)
            used.add(candidate.slug)
            budget += weight
            footprint |= candidate_files

        # A group of one is not a batch.
        if len(members) < 2:
            continue
        groups.append(_group_report(members, [by_slug[m] for m in members]))

    return groups


def _group_report(members: list[str], reports: list[StoryBatchReport]) -> BatchGroupReport:
    """Measured combined cost vs the estimated cost of one shared dev pass.

    See :data:`METHODOLOGY`. Unmeasured phase costs are simply absent from both
    sides rather than coerced to zero; ``cost_complete`` flags that so neither
    figure is read as exact.
    """
    actual = sum(r.total_cost_usd for r in reports)

    batched = sum(_measured(r.phase_costs.get("preflight")) for r in reports)
    for phase in SHARED_PHASES:
        costs = [_measured(r.phase_costs.get(phase)) for r in reports]
        batched += max(costs) if costs else 0.0

    combined_files: set[str] = set()
    for report in reports:
        combined_files |= set(report.likely_files)

    savings = actual - batched
    return BatchGroupReport(
        group_id=f"batch-{members[0]}",
        members=tuple(members),
        combined_files=tuple(sorted(combined_files)),
        actual_combined_cost_usd=_round(actual),
        hypothetical_batched_cost_usd=_round(batched),
        estimated_savings_usd=_round(savings),
        cheaper_if_batched=savings > 0,
        cost_complete=all(r.cost_complete for r in reports),
    )


# ── Artifact field helpers ────────────────────────────────────────────────────


def _phase_costs(audit: dict) -> dict[str, float | None]:
    """Per-phase cost from ``audit.yaml``'s phases block.

    A missing phase block means the phase never ran, which really is $0. A block
    whose ``cost_usd`` is null means it ran and its spend was never measured —
    that stays ``None``, because rendering unpriced work as $0.00 is the failure
    mode #1992 exists to prevent.
    """
    phases = audit.get("phases") if isinstance(audit.get("phases"), dict) else {}
    costs: dict[str, float | None] = {}
    for phase in PHASES:
        block = phases.get(phase)
        if not isinstance(block, dict):
            costs[phase] = 0.0
            continue
        raw = block.get("cost_usd")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            costs[phase] = float(raw)
        else:
            costs[phase] = None
    return costs


def _files_touched(iterations: dict) -> tuple[str, ...]:
    """Union of ``files_changed`` across the story's dev iterations.

    This is the observed footprint, and it is what validates (or refutes) the
    independence assumption the ``likely_files`` prediction rests on.
    """
    touched: set[str] = set()
    for item in iterations.get("dev_loop") or []:
        if not isinstance(item, dict):
            continue
        for path in item.get("files_changed") or []:
            if isinstance(path, str) and path.strip():
                touched.add(path.strip())
    return tuple(sorted(touched))


def _iteration_count(audit_value: object, iteration_usage: object, key: str) -> int | None:
    if isinstance(audit_value, int) and not isinstance(audit_value, bool):
        return audit_value
    if isinstance(iteration_usage, dict):
        block = iteration_usage.get(key)
        if isinstance(block, dict):
            used = block.get("used")
            if isinstance(used, int) and not isinstance(used, bool):
                return used
    return None


def _conflict_detail(outcome: str, raw: dict, audit: dict) -> str | None:
    """Detail string when the story hit a conflict, else None.

    Merge-failure outcomes are conclusive. Beyond those, an ``error`` mentioning
    a conflict catches integration failures the outcome code alone flattens.
    """
    if outcome in CONFLICT_OUTCOMES:
        return f"outcome={outcome}"
    for source in (raw.get("error"), audit.get("error")):
        if isinstance(source, str) and "conflict" in source.lower():
            return source.strip()
    return None


def _rca_classes(sprint_log_dir: Path, run_id: str) -> dict[str, str]:
    """Per-slug ``primary_failure_class`` from the RCA artifact, if one exists.

    Enrichment only — never a gate. Prefers the durable run-keyed artifact, and
    accepts the ``sprint-rca.yaml`` pointer only when it belongs to this run, so
    a later same-name run's analysis is never misattributed here.
    """
    data: object = None
    if run_id:
        data = _load_yaml(sprint_log_dir / f"run-{run_id}-sprint-rca.yaml")
    if not isinstance(data, dict):
        pointer = _load_yaml(sprint_log_dir / RCA_FILENAME)
        if isinstance(pointer, dict):
            pointer_run_id = pointer.get("sprint_run_id")
            if not pointer_run_id or not run_id or str(pointer_run_id) == run_id:
                data = pointer
    if not isinstance(data, dict):
        return {}

    stories = data.get("stories")
    if not isinstance(stories, dict):
        return {}
    classes: dict[str, str] = {}
    for slug, entry in stories.items():
        if isinstance(entry, dict):
            primary = entry.get("primary_failure_class")
            if isinstance(primary, str) and primary:
                classes[str(slug)] = primary
    return classes


def _sort_key(story: StoryBatchReport) -> tuple[int, str]:
    tail = story.slug.rsplit("-", 1)[-1]
    return (int(tail) if tail.isdigit() else sys.maxsize, story.slug)


def _first_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _slug_tuple(value: object) -> tuple[str, ...]:
    """Normalise a summary row's slug list (``depends_on``), dropping blanks."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _file_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    paths = {item.strip() for item in value if isinstance(item, str) and item.strip()}
    return tuple(sorted(paths))


def _measured(value: float | None) -> float:
    return value if isinstance(value, (int, float)) else 0.0


def _round(value: float) -> float:
    return round(float(value), 6)


def _load_yaml(path: Path) -> object:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


# ── Structured payload ────────────────────────────────────────────────────────


def report_payload(report: BatchabilityReport) -> dict:
    """Machine-readable payload for ``--format yaml|json``.

    The terminal renderer reads the same :class:`BatchabilityReport`, so the two
    views describe identical facts by construction.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "sprint": {
            "run_id": report.run_id,
            "name": report.sprint_name,
            "total_cost_usd": report.sprint_total_cost_usd,
        },
        "batch_rules": dict(report.rules),
        "methodology": METHODOLOGY,
        "totals": report.totals,
        "stories": [
            {
                "slug": s.slug,
                "path": s.path,
                "outcome": s.outcome,
                "eligible": s.eligible,
                "disqualifiers": list(s.disqualifiers),
                "work_type": s.work_type,
                "complexity": s.complexity,
                "sufficiency": s.sufficiency,
                "likely_files": list(s.likely_files),
                "files_touched": list(s.files_touched),
                "depends_on": list(s.depends_on),
                "dependents": list(s.dependents),
                "dev_iterations": s.dev_iterations,
                "review_cycles": s.review_cycles,
                "conflicted": s.conflicted,
                "retried": s.retried,
                "escalated": s.escalated,
                "rca_primary_failure_class": s.rca_class,
                "phase_costs_usd": {p: s.phase_costs.get(p) for p in PHASES},
                "total_cost_usd": s.total_cost_usd,
                "cost_complete": s.cost_complete,
            }
            for s in report.stories
        ],
        "groups": [
            {
                "group_id": g.group_id,
                "members": list(g.members),
                "combined_files": list(g.combined_files),
                "actual_combined_cost_usd": g.actual_combined_cost_usd,
                "hypothetical_batched_cost_usd": g.hypothetical_batched_cost_usd,
                "estimated_savings_usd": g.estimated_savings_usd,
                "cheaper_if_batched": g.cheaper_if_batched,
                "cost_complete": g.cost_complete,
            }
            for g in report.groups
        ],
    }


# ── Terminal renderer ─────────────────────────────────────────────────────────


def render_terminal(report: BatchabilityReport) -> str:
    lines: list[str] = []
    _render_header(report, lines)
    _render_qualified(report, lines)
    _render_disqualified(report, lines)
    _render_groups(report, lines)
    _render_phase_costs(report, lines)
    _render_methodology(lines)
    return "\n".join(lines) + "\n"


def _render_header(report: BatchabilityReport, lines: list[str]) -> None:
    parts = [f"BATCH ANALYTICS {report.sprint_name}"]
    if report.run_id:
        parts.append(f"run {report.run_id}")
    parts.append(f"{len(report.stories)} stories")
    if report.sprint_total_cost_usd is not None:
        parts.append(_money(report.sprint_total_cost_usd))
    lines.append("  ·  ".join(parts))
    rules = report.rules
    lines.append(
        f"  rules: max_stories={rules['max_stories']}  "
        f"max_complexity_budget={rules['max_complexity_budget']}  "
        f"max_touched_files={rules['max_touched_files']}"
    )


def _render_qualified(report: BatchabilityReport, lines: list[str]) -> None:
    qualified = report.qualified
    lines.append("")
    lines.append(f"QUALIFIED FOR BATCHING ({len(qualified)} of {len(report.stories)})")
    if not qualified:
        lines.append("  (none)")
        return
    for story in qualified:
        lines.append(f"  ✓ {story.slug}  {_story_cost(story)}  {_traits(story)}")
        lines.append(f"       predicted: {_files(story.likely_files)}")
        lines.append(f"       touched:   {_files(story.files_touched)}")


def _render_disqualified(report: BatchabilityReport, lines: list[str]) -> None:
    disqualified = report.disqualified
    if not disqualified:
        return
    lines.append("")
    lines.append(f"DISQUALIFIED ({len(disqualified)})")
    for story in disqualified:
        lines.append(f"  ✗ {story.slug}  {_story_cost(story)}  {story.outcome or '—'}")
        for reason in story.disqualifiers:
            lines.append(f"       {reason}")
        if story.rca_class:
            lines.append(f"       rca: {story.rca_class}")
        lines.append(f"       touched:   {_files(story.files_touched)}")


def _render_groups(report: BatchabilityReport, lines: list[str]) -> None:
    lines.append("")
    lines.append(f"BATCH GROUPS ({len(report.groups)})")
    if not report.groups:
        lines.append("  (none — no two qualified stories were independent enough to share a pass)")
        return
    for group in report.groups:
        lines.append(f"  {group.group_id}  [{', '.join(group.members)}]")
        lines.append(f"       actual combined:       {_money(group.actual_combined_cost_usd)}")
        lines.append(
            f"       estimated if batched:  {_money(group.hypothetical_batched_cost_usd)}"
            "  (estimate)"
        )
        verdict = "cheaper batched" if group.cheaper_if_batched else "NOT cheaper batched"
        lines.append(
            f"       estimated savings:     {_money(group.estimated_savings_usd)}  ({verdict})"
        )
        if not group.cost_complete:
            lines.append("       ⚠ incomplete cost data — figures are lower bounds")
        lines.append(f"       combined files: {_files(group.combined_files)}")

    totals = report.totals
    lines.append("")
    lines.append(
        f"  TOTAL across {totals['group_count']} group(s): "
        f"actual {_money(totals['actual_grouped_cost_usd'])} vs "
        f"estimated {_money(totals['hypothetical_batched_cost_usd'])} → "
        f"savings {_money(totals['estimated_savings_usd'])}"
    )


def _render_phase_costs(report: BatchabilityReport, lines: list[str]) -> None:
    lines.append("")
    lines.append("PER-STORY PHASE COSTS")
    header = f"  {'story':<22}" + "".join(f"{p:>13}" for p in PHASES) + f"{'total':>13}"
    lines.append(header)
    for story in report.stories:
        row = f"  {story.slug[:22]:<22}"
        for phase in PHASES:
            row += f"{_phase_cell(story.phase_costs.get(phase)):>13}"
        total = _money(story.total_cost_usd) + ("+" if not story.cost_complete else "")
        row += f"{total:>13}"
        lines.append(row)
    if any(not s.cost_complete for s in report.stories):
        lines.append("  (+ = at least one phase ran with unmeasured cost; total is a lower bound)")


def _render_methodology(lines: list[str]) -> None:
    lines.append("")
    lines.append("METHODOLOGY")
    for chunk in _wrap(METHODOLOGY, 88):
        lines.append(f"  {chunk}")


def _traits(story: StoryBatchReport) -> str:
    return "/".join(
        [
            story.work_type or "unknown",
            story.complexity or "unknown",
            story.sufficiency or "unknown",
        ]
    )


def _story_cost(story: StoryBatchReport) -> str:
    return _money(story.total_cost_usd) + ("+" if not story.cost_complete else "")


def _phase_cell(value: float | None) -> str:
    if value is None:
        return "?"
    if value == 0:
        return "—"
    return _money(value)


def _files(paths: tuple[str, ...]) -> str:
    return ", ".join(paths) if paths else "—"


def _money(value: float) -> str:
    """Format a cost. Sub-cent amounts keep four places rather than reading $0.00."""
    if value and abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines
