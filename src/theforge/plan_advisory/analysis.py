"""Pure extraction and aggregation for the plan-advisory resolution measure (#2112).

Two halves meet here and the seam between them is deliberate:

* **Mechanical** — which P1-level plan findings a run carried, and what the plan
  review cost against the story it guarded. Both come straight out of audit
  records; nothing here interprets them.
* **Judged** — what class a finding belongs to and whether the change that
  shipped addressed it. No audit field records either (``plan_finding_registry``
  entries carry only description, severity, cycle window and an in-run
  disposition), so these come from a checked-in corpus of hand-authored rows.

The join between them is by stable finding key, and it is validated in one
direction only. A judgment naming a finding that no audit record carries is a
hard error — the corpus has drifted from the substrate and every rate computed
from it is suspect. An audit finding with no judgment is *not* an error: the
substrate grows faster than findings can be judged, so unjudged findings are
counted, reported, and excluded from every rate denominator. A rate that silently
covered only part of its corpus would be worse than one that says so.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

RESOLVED = "resolved"
ESCAPED = "escaped"

#: Marker a judgment sets in ``evidence`` when nothing citable was found. Counted
#: and rendered separately so an under-evidenced corpus is visible rather than
#: averaged in alongside evidenced rows.
EVIDENCE_UNAVAILABLE = "evidence unavailable"

#: Controlled class vocabulary, seeded from the story's example. Kept closed so
#: classes stay comparable across judgments; a genuinely novel finding shape
#: warrants a new entry here rather than a free-text class on one row.
FINDING_CLASSES = (
    "module/placement",
    "scope / out-of-scope",
    "unspecified mechanism",
    "factual error in rationale",
    "already-implemented",
    "missing failure mode",
    "contract/interface mismatch",
    "test strategy gap",
)

#: Where an escaped finding was eventually caught. ``unshipped`` means it never
#: was — the defect is still latent as far as the corpus can tell.
DETECTION_POINTS = (
    "own code review",
    "own gate",
    "own later story",
    "adopter run",
    "operator",
    "unshipped",
)

_DONE = "DONE"
_WS = re.compile(r"\s+")

#: Registry disposition meaning the finding was gone by the last plan-review
#: cycle — the plan was regenerated and the finding did not survive. Such a
#: finding was never handed to dev as advisory context, so it cannot answer the
#: question this measure asks. Counted, reported, and kept out of the rate.
_FIXED_IN_PLAN = "fixed"


class CorpusMismatchError(RuntimeError):
    """The judgment corpus is unusable: unreadable, or at odds with the substrate.

    Both mean the same thing to whoever is looking at the report — no rate can be
    computed and the corpus file is what to inspect — so they are one signal
    rather than two an operator would have to tell apart.
    """


@dataclass(frozen=True)
class PlanFinding:
    """One P1-level advisory plan finding extracted from an audit record."""

    key: str
    run_id: str
    slug: str
    ordinal: int
    severity: str
    description: str
    disposition: str

    @property
    def carried_to_dev(self) -> bool:
        """True when the approved plan still held this finding at dev handoff."""
        return self.disposition != _FIXED_IN_PLAN


@dataclass
class RunCost:
    """Plan-review cost against total story cost for one run."""

    run_id: str
    slug: str
    plan_review_usd: float | None
    total_usd: float | None

    @property
    def fraction(self) -> float | None:
        if self.plan_review_usd is None or not self.total_usd:
            return None
        return self.plan_review_usd / self.total_usd


@dataclass
class Extraction:
    """Everything the substrate can say on its own, before any judgment."""

    findings: list[PlanFinding] = field(default_factory=list)
    costs: list[RunCost] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    excluded_runs: list[dict[str, Any]] = field(default_factory=list)
    records_scanned: int = 0
    p2_findings_skipped: int = 0


def normalize_description(text: str) -> str:
    """Collapse a finding description to its comparison form."""
    return _WS.sub(" ", str(text or "")).strip().lower()


def finding_key(run_id: str, ordinal: int, description: str) -> str:
    """Return the stable identity of one plan finding.

    Ordinal alone would shift if a registry were ever re-ordered, and the
    description hash alone collides when the same reviewer raises the same
    finding twice in one run. Together they survive both.
    """
    digest = hashlib.sha256(normalize_description(description).encode("utf-8")).hexdigest()[:8]
    return f"{run_id}:{ordinal}:{digest}"


def is_p1_level(finding: Mapping[str, Any]) -> bool:
    """True for P1 and its sub-severities (``P1-impl``, ``P1-spec``, ...)."""
    severity = finding.get("effective_severity") or finding.get("severity") or ""
    return str(severity).startswith("P1")


def _plan_registry(record: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, list]:
    plan_review = record.get("plan_review")
    if not isinstance(plan_review, dict) or not plan_review.get("decision"):
        return None, []
    registry = plan_review.get("plan_finding_registry")
    if not isinstance(registry, list):
        return plan_review, []
    return plan_review, registry


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def extract_plan_findings(records: Iterable[Mapping[str, Any]]) -> Extraction:
    """Pull P1-level advisory plan findings and plan-review cost from audit records.

    Only runs that reached ``DONE`` contribute to the rate corpus: "did the change
    that shipped address it" has no answer for a run that never shipped one. Runs
    that carried plan findings and ended otherwise are still counted and named in
    ``excluded_runs``, because selection bias that nobody can see is selection
    bias nobody can weigh.
    """
    out = Extraction()
    for record in records:
        out.records_scanned += 1
        plan_review, registry = _plan_registry(record)
        if plan_review is None or not registry:
            continue
        task = record.get("task") if isinstance(record.get("task"), dict) else {}
        outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
        run_id = str(record.get("run_id") or "")
        slug = str(task.get("slug") or task.get("issue_id") or "?")
        final_phase = str(outcome.get("final_phase") or "?")

        p1s = [f for f in registry if isinstance(f, dict) and is_p1_level(f)]
        if final_phase != _DONE:
            out.excluded_runs.append(
                {
                    "run_id": run_id,
                    "slug": slug,
                    "final_phase": final_phase,
                    "p1_findings": len(p1s),
                }
            )
            continue

        out.p2_findings_skipped += len(registry) - len(p1s)
        if not p1s:
            continue
        out.runs.append(run_id)
        for ordinal, finding in enumerate(p1s):
            description = str(finding.get("description") or "")
            out.findings.append(
                PlanFinding(
                    key=finding_key(run_id, ordinal, description),
                    run_id=run_id,
                    slug=slug,
                    ordinal=ordinal,
                    severity=str(
                        finding.get("effective_severity") or finding.get("severity") or "?"
                    ),
                    description=description,
                    disposition=str(finding.get("disposition") or "?"),
                )
            )
        cost = record.get("cost") if isinstance(record.get("cost"), dict) else {}
        out.costs.append(
            RunCost(
                run_id=run_id,
                slug=slug,
                plan_review_usd=_as_float(plan_review.get("cost_usd")),
                total_usd=_as_float(cost.get("total_usd")),
            )
        )
    return out


def _cost_summary(costs: Sequence[RunCost]) -> dict[str, Any]:
    fractions = [c.fraction for c in costs if c.fraction is not None]
    dollars = [c.plan_review_usd for c in costs if c.plan_review_usd is not None]
    return {
        "runs": len(costs),
        "runs_with_plan_review_cost": len(dollars),
        "runs_with_both": len(fractions),
        "median_plan_review_usd": round(statistics.median(dollars), 4) if dollars else None,
        "median_fraction_of_story": round(statistics.median(fractions), 4) if fractions else None,
        "omitted_missing_plan_review_cost": sum(1 for c in costs if c.plan_review_usd is None),
        "omitted_missing_total_cost": sum(1 for c in costs if not c.total_usd),
    }


def _validate(
    extraction: Extraction, judgments: Sequence[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    by_key = {f.key: f for f in extraction.findings if f.carried_to_dev}
    judged: dict[str, Mapping[str, Any]] = {}
    orphans: list[str] = []
    duplicates: list[str] = []
    for row in judgments:
        key = str(row.get("finding_key") or "")
        if key not in by_key:
            orphans.append(key)
            continue
        if key in judged:
            duplicates.append(key)
            continue
        judged[key] = row
    problems = []
    if orphans:
        problems.append(
            f"{len(orphans)} judgment(s) name no advisory finding carried into dev: "
            + ", ".join(sorted(orphans)[:5])
        )
    if duplicates:
        problems.append(
            f"{len(duplicates)} finding(s) judged more than once: " + ", ".join(sorted(duplicates))
        )
    bad_class = sorted(
        {
            str(row.get("class"))
            for row in judgments
            if str(row.get("class")) not in FINDING_CLASSES
        }
    )
    if bad_class:
        problems.append("class outside the controlled vocabulary: " + ", ".join(bad_class))
    bad_outcome = sorted(
        {
            str(row.get("advisory_outcome"))
            for row in judgments
            if str(row.get("advisory_outcome")) not in (RESOLVED, ESCAPED)
        }
    )
    if bad_outcome:
        problems.append("advisory_outcome must be resolved/escaped: " + ", ".join(bad_outcome))
    bad_point = sorted(
        {
            str(row.get("detection_point"))
            for row in judgments
            if row.get("advisory_outcome") == ESCAPED
            and str(row.get("detection_point")) not in DETECTION_POINTS
        }
    )
    if bad_point:
        problems.append("detection_point outside the vocabulary: " + ", ".join(bad_point))
    if problems:
        raise CorpusMismatchError("; ".join(problems))
    return judged


def analyze(extraction: Extraction, judgments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Join extracted findings to the judgment corpus and aggregate the decision report.

    Rates are computed over judged findings only. Both the advisory-resolution
    outcome and the final shipped-addressed status are carried through, because
    they are different questions: dev can leave an advisory finding untouched in
    the run it was raised against and the eventual shipped change can still carry
    the remedy — or not.
    """
    judged = _validate(extraction, judgments)
    carried = [f for f in extraction.findings if f.carried_to_dev]
    by_key = {f.key: f for f in carried}

    per_class: dict[str, Counter] = {}
    resolved_rows: list[dict[str, Any]] = []
    escaped_rows: list[dict[str, Any]] = []
    detection_points: Counter = Counter()
    shipped_addressed = 0
    shipped_unaddressed = 0
    evidence_unavailable = 0

    for key, row in sorted(judged.items(), key=lambda kv: (by_key[kv[0]].slug, kv[0])):
        finding = by_key[key]
        cls = str(row["class"])
        outcome = str(row["advisory_outcome"])
        counts = per_class.setdefault(cls, Counter())
        counts["findings"] += 1
        counts[outcome] += 1

        evidence = str(row.get("evidence") or "").strip()
        if not evidence or evidence == EVIDENCE_UNAVAILABLE:
            evidence_unavailable += 1
        if row.get("shipped_addressed"):
            shipped_addressed += 1
            counts["shipped_addressed"] += 1
        else:
            shipped_unaddressed += 1

        entry = {
            "finding_key": key,
            "run_id": finding.run_id,
            "slug": finding.slug,
            "class": cls,
            "severity": finding.severity,
            "description": finding.description,
            "shipped_addressed": bool(row.get("shipped_addressed")),
            "detection_point": row.get("detection_point"),
            "evidence": evidence or EVIDENCE_UNAVAILABLE,
        }
        if outcome == RESOLVED:
            resolved_rows.append(entry)
        else:
            escaped_rows.append(entry)
            detection_points[str(row.get("detection_point"))] += 1

    classes = []
    for cls in sorted(per_class, key=lambda c: (-per_class[c]["findings"], c)):
        counts = per_class[cls]
        total = counts["findings"]
        classes.append(
            {
                "class": cls,
                "findings": total,
                "resolved": counts[RESOLVED],
                "escaped": counts[ESCAPED],
                "shipped_addressed": counts["shipped_addressed"],
                "rate": round(counts[RESOLVED] / total, 4) if total else None,
            }
        )

    judged_count = len(judged)
    extracted = len(carried)
    total_resolved = sum(c["resolved"] for c in classes)
    return {
        "corpus": {
            "records_scanned": extraction.records_scanned,
            "runs": len(extraction.runs),
            "p1_findings_raised": len(extraction.findings),
            "findings_fixed_in_plan": len(extraction.findings) - extracted,
            "findings_extracted": extracted,
            "findings_judged": judged_count,
            "findings_unjudged": extracted - judged_count,
            "coverage": round(judged_count / extracted, 4) if extracted else None,
            "p2_findings_skipped": extraction.p2_findings_skipped,
            "excluded_runs": extraction.excluded_runs,
            "excluded_run_count": len(extraction.excluded_runs),
            "excluded_run_findings": sum(int(r["p1_findings"]) for r in extraction.excluded_runs),
            "evidence_unavailable": evidence_unavailable,
        },
        "overall": {
            "findings": judged_count,
            "resolved": total_resolved,
            "escaped": judged_count - total_resolved,
            "rate": round(total_resolved / judged_count, 4) if judged_count else None,
            "shipped_addressed": shipped_addressed,
            "shipped_unaddressed": shipped_unaddressed,
        },
        "classes": classes,
        "resolved_findings": resolved_rows,
        "escaped_findings": escaped_rows,
        "escapes_by_detection_point": dict(detection_points.most_common()),
        "cost": _cost_summary(extraction.costs),
        "unjudged_findings": [
            {"finding_key": f.key, "run_id": f.run_id, "slug": f.slug}
            for f in carried
            if f.key not in judged
        ],
    }
