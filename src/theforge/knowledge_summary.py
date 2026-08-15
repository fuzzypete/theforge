"""Schema, validation, and persistence for evidence-backed run summaries.

This is Layer 2 of ``docs/plans/knowledge-capture.md``: after a run reaches
DONE, forge can distil it into a structured artifact a later run could learn
from. The artifact is a *bounded agent output* — the model writes prose and
claims, the coordinator writes every countable fact — and this module is the
integrity boundary between the two.

The load-bearing rule is provenance. Every entry in ``what_was_learned`` must
cite at least one concrete reference **from this run**, and every citation must
*resolve* against the authoritative audit record: a finding id that exists in
the finding registry, a plan step id that exists in the plan, a review cycle
that ran, a file the diff actually touched, a commit/ref the run produced. A
reference that merely has the right shape is not evidence — an unresolvable
citation is the exact failure mode (hallucinated institutional memory) this
layer exists to prevent, so it rejects the whole summary rather than persisting
a claim nobody can check.

Deliberately low-dependency (stdlib + yaml). Prompt construction lives in
``theforge.task.summary_prompts``; the coordinator control flow that invokes the
agent and writes the artifact lives in
``theforge.coordinator.knowledge_summary_flow``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUMMARY_SCHEMA_VERSION = 1

# Where a summary lives, relative to the project root. Keyed by run_id (not
# slug) so reruns of the same story never collide.
SUMMARIES_DIR = Path(".forge") / "knowledge" / "summaries"

# The authoritative run record a summary is a summary *of*. The summary carries
# this backlink; the record deliberately carries no forward pointer to the
# summary, because it is written first and is immutable once written — the
# forward direction is resolved by this same path convention.
RUN_RECORD_DIR = Path(".forge") / "audits" / "runs"

# Evidence types and the key each one names its reference with. The doc's shape
# uses a type-specific key (``finding_id``, ``step_id``, …); a generic
# ``reference`` is accepted as an alias so a model that reaches for the obvious
# spelling is not rejected on a synonym.
EVIDENCE_REFERENCE_KEYS: dict[str, str] = {
    "review_finding": "finding_id",
    "plan_step": "step_id",
    "review_cycle": "cycle",
    "file": "path",
    "diff": "ref",
}

_GENERIC_REFERENCE_KEY = "reference"

# Caps so a runaway agent cannot balloon the artifact.
_MAX_CLAIMS = 20
_MAX_EVIDENCE_PER_CLAIM = 10
_MAX_PATTERNS = 20
_MAX_OBSERVATIONS = 20
_FIELD_MAX_LEN = 2000


class SummaryValidationError(ValueError):
    """Raised when agent-proposed summary content fails the schema boundary."""


def _text(value: Any, *, limit: int = _FIELD_MAX_LEN) -> str:
    """Coerce a scalar to a bounded, stripped string."""
    if value is None:
        return ""
    return str(value).strip()[:limit]


# ── Anchors: what a summary is allowed to cite ────────────────────────────────


@dataclass(frozen=True)
class RunAnchors:
    """The concrete references that actually exist in one run's audit record.

    Evidence is checked against these sets. Anything not in them did not happen
    in this run, whatever shape it has.
    """

    finding_ids: frozenset[str] = frozenset()
    plan_step_ids: frozenset[str] = frozenset()
    review_cycles: frozenset[str] = frozenset()
    file_paths: frozenset[str] = frozenset()
    diff_refs: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        """Report whether the run offers nothing citable at all."""
        return not (
            self.finding_ids
            or self.plan_step_ids
            or self.review_cycles
            or self.file_paths
            or self.diff_refs
        )

    def resolves(self, evidence_type: str, reference: str) -> bool:
        """Report whether ``reference`` names something this run produced."""
        if evidence_type == "review_finding":
            return reference in self.finding_ids
        if evidence_type == "plan_step":
            return reference in self.plan_step_ids
        if evidence_type == "review_cycle":
            return reference in self.review_cycles
        if evidence_type == "file":
            return reference in self.file_paths
        if evidence_type == "diff":
            return reference in self.diff_refs
        return False


def _iter_plan_steps(audit: dict) -> list[dict]:
    """Collect every plan step from the final plan and each regen attempt."""
    plan_block = ((audit.get("phases") or {}).get("plan")) or {}
    plans: list[Any] = [plan_block.get("plan_structured")]
    for attempt in plan_block.get("attempt_plans") or []:
        if isinstance(attempt, dict):
            plans.append(attempt.get("plan"))
    steps: list[dict] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        for step in plan.get("steps") or []:
            if isinstance(step, dict):
                steps.append(step)
    return steps


def extract_anchors(audit: dict) -> RunAnchors:
    """Build the citable-reference sets from an authoritative run audit."""
    if not isinstance(audit, dict):
        return RunAnchors()

    finding_ids = {
        _text(entry.get("finding_id"))
        for entry in (audit.get("finding_registry") or [])
        if isinstance(entry, dict) and entry.get("finding_id") is not None
    }
    step_ids = {
        _text(step.get("id")) for step in _iter_plan_steps(audit) if step.get("id") is not None
    }

    cycles: set[str] = set()
    for review in audit.get("reviews") or []:
        if isinstance(review, dict) and review.get("cycle") is not None:
            cycles.add(_text(review.get("cycle")))
    iterations = audit.get("iterations") or {}
    total_cycles = iterations.get("review_cycles_total")
    if isinstance(total_cycles, int):
        cycles.update(str(n) for n in range(1, total_cycles + 1))

    changed = audit.get("changed_files") or {}
    paths = {
        _text(entry.get("path"))
        for entry in (changed.get("files") or [])
        if isinstance(entry, dict) and entry.get("path")
    }

    diff_refs: set[str] = set()
    for key in ("base_ref", "head_ref"):
        if changed.get(key):
            diff_refs.add(_text(changed.get(key)))
    for review in audit.get("reviews") or []:
        if isinstance(review, dict) and review.get("commit"):
            diff_refs.add(_text(review.get("commit")))

    return RunAnchors(
        finding_ids=frozenset(v for v in finding_ids if v),
        plan_step_ids=frozenset(v for v in step_ids if v),
        review_cycles=frozenset(v for v in cycles if v),
        file_paths=frozenset(v for v in paths if v),
        diff_refs=frozenset(v for v in diff_refs if v),
    )


# ── Agent output: parse + validate ────────────────────────────────────────────


@dataclass(frozen=True)
class LearnedClaim:
    """One claim plus the run references that back it."""

    claim: str
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"claim": self.claim, "evidence": list(self.evidence)}


@dataclass(frozen=True)
class ProposedSummary:
    """The agent-authored half of a run summary, after validation."""

    run_id: str
    description: str
    approach: str
    learned: list[LearnedClaim] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    dominant_difficulty: str = ""


def parse_summary_output(text: str) -> dict:
    """Extract the ``run_summary:`` mapping from mixed agent output.

    Raises ``SummaryValidationError`` when no parseable rooted block is present
    — same guarded posture as the plan/review parsers: malformed output is
    surfaced, never coerced into a partial artifact.
    """
    stripped = (text or "").strip()
    if not stripped:
        raise SummaryValidationError("summary agent produced no output")

    lines = stripped.splitlines()
    start_index: int | None = None
    base_indent = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("run_summary:"):
            start_index = index
            base_indent = len(line) - len(line.lstrip())
            break
    if start_index is None:
        raise SummaryValidationError("no rooted 'run_summary:' block in summary agent output")

    block: list[str] = []
    for index, line in enumerate(lines[start_index:], start=start_index):
        indent = len(line) - len(line.lstrip())
        if index > start_index and line.strip() and indent <= base_indent:
            break
        block.append(line[base_indent:] if len(line) >= base_indent else line)

    try:
        data = yaml.safe_load("\n".join(block))
    except yaml.YAMLError as exc:
        raise SummaryValidationError(f"summary output is not valid YAML: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("run_summary"), dict):
        raise SummaryValidationError("summary output has no 'run_summary' mapping")
    return data["run_summary"]


def _string_list(proposed: dict, key: str, *, limit: int = _FIELD_MAX_LEN) -> list[str]:
    """Return the non-empty string entries of a list-valued optional field.

    A bare scalar is refused rather than iterated. ``learned_patterns:
    retry-decorator`` is valid YAML and iterating it yields *characters*, which
    would fill the pattern index with single letters — an index quietly made of
    garbage is worse than a summary that was rejected, and these fields exist
    precisely to be indexed.
    """
    raw = proposed.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SummaryValidationError(f"run_summary.{key} must be a list of strings, got {raw!r}")
    values: list[str] = []
    for item in raw:
        if isinstance(item, (dict, list)):
            raise SummaryValidationError(
                f"run_summary.{key} entries must be strings, got {item!r}"
            )
        text = _text(item, limit=limit)
        if text:
            values.append(text)
    return values


def _validate_evidence_item(raw: Any, anchors: RunAnchors, claim: str) -> dict:
    """Normalize one evidence item, or raise if it does not resolve."""
    if not isinstance(raw, dict):
        raise SummaryValidationError(
            f"evidence for claim {claim!r} must be a mapping, got {raw!r}"
        )
    evidence_type = _text(raw.get("type")).lower()
    if evidence_type not in EVIDENCE_REFERENCE_KEYS:
        raise SummaryValidationError(
            f"evidence for claim {claim!r} has unknown type {evidence_type!r}; "
            f"expected one of {sorted(EVIDENCE_REFERENCE_KEYS)}"
        )
    key = EVIDENCE_REFERENCE_KEYS[evidence_type]
    reference = _text(raw.get(key)) or _text(raw.get(_GENERIC_REFERENCE_KEY))
    if not reference:
        raise SummaryValidationError(
            f"evidence for claim {claim!r} of type {evidence_type!r} names no {key!r}"
        )
    if not anchors.resolves(evidence_type, reference):
        raise SummaryValidationError(
            f"evidence for claim {claim!r} cites {evidence_type} {reference!r}, "
            "which does not exist in this run's audit record"
        )
    return {
        "type": evidence_type,
        key: reference,
        "description": _text(raw.get("description")),
    }


def validate_proposed_summary(
    proposed: dict,
    *,
    run_id: str,
    anchors: RunAnchors,
) -> ProposedSummary:
    """Validate agent-proposed summary content against the run's own record.

    Raises ``SummaryValidationError`` on any of: a run_id that does not match
    the run being summarised, a learned claim with no ``evidence`` list, an
    empty one, or a citation that does not resolve against ``anchors``.
    """
    if not isinstance(proposed, dict):
        raise SummaryValidationError(f"run_summary must be a mapping, got {proposed!r}")

    claimed_run_id = _text(proposed.get("run_id"))
    if claimed_run_id and claimed_run_id != run_id:
        raise SummaryValidationError(
            f"summary run_id {claimed_run_id!r} does not match the run being "
            f"summarised ({run_id!r})"
        )

    what_changed = proposed.get("what_changed")
    if not isinstance(what_changed, dict):
        what_changed = {}
    description = _text(what_changed.get("description"))
    if not description:
        raise SummaryValidationError("run_summary.what_changed.description is required")

    raw_learned = proposed.get("what_was_learned") or []
    if not isinstance(raw_learned, list):
        raise SummaryValidationError(
            f"run_summary.what_was_learned must be a list, got {raw_learned!r}"
        )
    if len(raw_learned) > _MAX_CLAIMS:
        raise SummaryValidationError(
            f"run_summary.what_was_learned has {len(raw_learned)} entries; "
            f"the cap is {_MAX_CLAIMS}"
        )

    learned: list[LearnedClaim] = []
    for entry in raw_learned:
        if not isinstance(entry, dict):
            raise SummaryValidationError(
                f"run_summary.what_was_learned entries must be mappings, got {entry!r}"
            )
        claim = _text(entry.get("claim"))
        if not claim:
            raise SummaryValidationError("a what_was_learned entry has an empty claim")
        raw_evidence = entry.get("evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise SummaryValidationError(
                f"learned claim {claim!r} carries no evidence; every claim must cite "
                "at least one finding, plan step, review cycle, file, or diff ref "
                "from this run"
            )
        if len(raw_evidence) > _MAX_EVIDENCE_PER_CLAIM:
            raw_evidence = raw_evidence[:_MAX_EVIDENCE_PER_CLAIM]
        learned.append(
            LearnedClaim(
                claim=claim,
                evidence=[_validate_evidence_item(item, anchors, claim) for item in raw_evidence],
            )
        )

    patterns = _string_list(proposed, "learned_patterns", limit=200)[:_MAX_PATTERNS]
    observations = _string_list(proposed, "review_insights")[:_MAX_OBSERVATIONS]
    complexity = proposed.get("complexity_signal")
    dominant = _text(complexity.get("dominant_difficulty")) if isinstance(complexity, dict) else ""

    return ProposedSummary(
        run_id=run_id,
        description=description,
        approach=_text(what_changed.get("approach")),
        learned=learned,
        patterns=patterns,
        observations=observations,
        dominant_difficulty=dominant,
    )


# ── Mechanical fields: everything countable comes from the audit ──────────────


def _review_insight_blocks(audit: dict) -> tuple[list[dict], list[dict]]:
    """Split the finding registry into recurring and resolved finding views."""
    recurring: list[dict] = []
    resolved: list[dict] = []
    for entry in audit.get("finding_registry") or []:
        if not isinstance(entry, dict):
            continue
        first = entry.get("cycle_first_seen")
        last = entry.get("cycle_last_seen")
        cycles_seen = (
            last - first + 1 if isinstance(first, int) and isinstance(last, int) else None
        )
        view = {
            "finding_id": _text(entry.get("finding_id")),
            "severity": _text(entry.get("severity")),
            "file": _text(entry.get("file")),
            "description": _text(entry.get("description"), limit=500),
            "cycles_seen": cycles_seen,
            "disposition": _text(entry.get("disposition")),
        }
        if isinstance(cycles_seen, int) and cycles_seen > 1:
            recurring.append(view)
        if view["disposition"] in ("resolved", "fixed", "addressed"):
            resolved.append(view)
    return recurring, resolved


def build_summary_artifact(
    proposed: ProposedSummary,
    audit: dict,
    *,
    generation: dict | None = None,
) -> dict:
    """Compose the persisted artifact from validated claims + audited facts.

    Every countable field — changed files, domains, story shape, iteration and
    cost signals, the review finding views — is read from the audit record, not
    from the agent. The agent supplies the description, the claims, and the
    pattern tags; it never supplies a number or a file list, so no index built
    over this artifact can be poisoned by a model's recollection.
    """
    run_id = proposed.run_id
    preflight = audit.get("preflight") or {}
    iterations = audit.get("iterations") or {}
    cost = audit.get("cost") or {}
    task = audit.get("task") or {}
    changed = audit.get("changed_files") or {}
    files = [
        _text(entry.get("path"))
        for entry in (changed.get("files") or [])
        if isinstance(entry, dict) and entry.get("path")
    ]
    recurring, resolved = _review_insight_blocks(audit)

    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "run_id": run_id,
        # Backlink to the record this summary was distilled from. The summary is
        # advisory; that record is authoritative, and any consumer that wants a
        # fact rather than a claim reads it there.
        "authoritative_run_record": str(RUN_RECORD_DIR / f"{run_id}.json"),
        "generated_at": audit.get("timing", {}).get("finished_at"),
        "generation": generation or {},
        "story": {
            "slug": _text(task.get("slug")),
            "name": _text(task.get("name")),
            "github_issue": task.get("github_issue"),
        },
        "story_shape": {
            "work_type": _text(preflight.get("work_type")),
            "complexity": _text(preflight.get("complexity")),
            "complexity_score": preflight.get("complexity_score"),
            "contract_change": preflight.get("contract_change"),
        },
        "domains": [_text(d) for d in (preflight.get("domains") or []) if _text(d)],
        "changed_files": files,
        "what_changed": {
            "description": proposed.description,
            "approach": proposed.approach,
            "files_modified": files,
        },
        "what_was_learned": [claim.to_dict() for claim in proposed.learned],
        "learned_patterns": proposed.patterns,
        "review_insights": {
            "recurring_findings": recurring,
            "resolved_findings": resolved,
            "observations": proposed.observations,
        },
        "complexity_signal": {
            "actual_iterations": iterations.get("dev_iterations_productive"),
            "review_cycles": iterations.get("review_cycles_total"),
            "plan_regenerations": (audit.get("plan_review") or {}).get("regenerated"),
            "cost_usd": cost.get("total_usd"),
            "dominant_difficulty": proposed.dominant_difficulty,
        },
    }


# ── Persistence ───────────────────────────────────────────────────────────────


def summary_path(project_root: Path, run_id: str) -> Path:
    """Return the artifact path for one run's summary."""
    return Path(project_root) / SUMMARIES_DIR / f"{run_id}.yaml"


def summary_exists(project_root: Path, run_id: str) -> bool:
    """Report whether this run already has a persisted summary.

    The exactly-once guard: several terminal seams write a run's audit, and each
    would otherwise dispatch a fresh (billed) summary agent for the same run.
    """
    return summary_path(project_root, run_id).exists()


def write_summary(project_root: Path, run_id: str, artifact: dict) -> Path:
    """Write the artifact atomically and return its path."""
    path = summary_path(project_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(artifact, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return path
