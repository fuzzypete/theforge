"""Did a review finding restate something the run had already been told? (#2684)

This module compares two artifacts the run already recorded — the claims the
prior-run selector rendered into a prompt, and the findings the review
recorded — and reports where they correspond. It asks nothing of any model's
account of its own reasoning, and it decides nothing.

Three things it is deliberately *not*:

1. **Not an effectiveness verdict.** A correspondence does not show that usable
   guidance was ignored. The claim may have been irrelevant to the story,
   ambiguous, contradicted by newer evidence, or actionable only in part. What
   it yields is a population worth inspecting when asking whether selection,
   rendering or placement is working.
2. **Not a novelty detector.** A finding this matcher does not match is
   reported as *not matched to an eligible injected claim*, never as novel.
   Failing to find a correspondence establishes the absence of a
   correspondence and nothing more.
3. **Not a way to blame the reviewer for what only the reviewer saw.** Prior
   knowledge reaches the review phase too. A claim rendered into a reviewer's
   own prompt that the reviewer then raises is the claim *working*; counting it
   as missed uptake would charge the development agent for material it was
   never shown. Eligibility is therefore filtered by recipient role, not by
   run.

Every judgment is deterministic pure Python. The matcher is versioned
(``METHOD_NAME``/``METHOD_VERSION``) and reports its agreement against a stored
labelled set, so a figure recorded today stays interpretable after the method
changes — and a method whose agreement was never measured says so rather than
letting its numbers pass as validated.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

#: The matcher's identity, recorded with every result it produces. Bump the
#: version whenever the decision procedure changes; the labelled set declares
#: which version it was labelled against, so a stale set reads as unvalidated
#: rather than silently vouching for a different method.
METHOD_NAME = "rendered-claim-overlap"
METHOD_VERSION = "v1"

#: The role that authors the work a review finding is about. A review finding
#: concerns the code in the diff, which the development agent wrote — so a
#: claim is eligible to explain a finding only if it reached that role.
AUTHOR_ROLE = "dev"

# Run-level statuses. These are mutually exclusive and cover every run.
STATUS_COMPARED = "compared"
STATUS_NO_ELIGIBLE_CLAIMS = "no_eligible_claims"
STATUS_NO_REVIEW_FINDINGS = "no_review_findings"
STATUS_UNCOMPARABLE = "uncomparable_pre_capture"

# Per-finding outcomes.
OUTCOME_MATCHED = "matched"
OUTCOME_NOT_MATCHED = "not_matched_to_eligible_claim"
OUTCOME_INDETERMINATE = "indeterminate"

#: The one sentence every rendering of this report must carry.
INTERPRETATION_NOTE = "missed-uptake indicator only; contributes to no effectiveness verdict"

VALIDATION_MEASURED = "measured"
VALIDATION_UNVALIDATED = "unvalidated"

#: Where the stored labelled examples live, relative to the project root.
LABELLED_SET_RELPATH = ("docs", "knowledge-uptake-labels.yaml")

# ── Matcher parameters (v1) ──────────────────────────────────────────────────
#
# Overlap coefficient rather than Jaccard: a claim and a finding describing the
# same subject differ wildly in length, and Jaccard punishes that difference
# rather than the disagreement it is supposed to measure.
_MATCH_THRESHOLD = 0.60
_INDETERMINATE_THRESHOLD = 0.45
#: Below this many content tokens on either side there is not enough text to
#: decide either way — which is a third answer, not a "no".
_MIN_TOKENS = 4

_TOKEN_RE = re.compile(r"[^\w\s]")

# Tokens carried by nearly every claim and nearly every finding in this domain.
# Left in, they manufacture overlap between texts that share only the fact that
# both are about software review.
_NOISE_TOKENS = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "not",
        "was",
        "are",
        "but",
        "its",
        "from",
        "into",
        "than",
        "then",
        "when",
        "which",
        "should",
        "would",
        "could",
        "must",
        "can",
        "has",
        "have",
        "had",
        "one",
        "any",
        "all",
        "run",
        "runs",
        "code",
        "test",
        "tests",
        "file",
        "files",
        "line",
        "lines",
        "src",
        "theforge",
        "python",
        "add",
        "adds",
        "added",
        "use",
        "used",
        "uses",
    }
)


def _tokens(text: str) -> frozenset[str]:
    lowered = _TOKEN_RE.sub(" ", str(text or "").lower())
    return frozenset(
        token for token in lowered.split() if len(token) > 2 and token not in _NOISE_TOKENS
    )


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _parse_time(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


# ── Claim extraction ─────────────────────────────────────────────────────────


def _claim_records(context_manifests: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Every claim rendered into a prompt this run, in record order."""
    claims: list[dict] = []
    for entry in context_manifests:
        if not isinstance(entry, Mapping):
            continue
        prior = entry.get("prior_run_context")
        if not isinstance(prior, Mapping):
            continue
        for included in prior.get("included") or []:
            if not isinstance(included, Mapping):
                continue
            for claim in included.get("claims") or []:
                if isinstance(claim, Mapping) and str(claim.get("claim") or "").strip():
                    claims.append(dict(claim))
    return claims


def _capture_present(context_manifests: Sequence[Mapping[str, Any]]) -> bool:
    """False when any prior-run manifest predates claim-exposure capture.

    A single uncaptured manifest is enough: the run cannot say what that phase's
    agent was shown, so nothing computed over the rest can be reported as a
    complete comparison. A run with no prior-run manifests at all is comparable
    — it assembled no context, which is a fact about the run rather than a gap
    in the record.
    """
    for entry in context_manifests:
        if not isinstance(entry, Mapping):
            continue
        prior = entry.get("prior_run_context")
        if not isinstance(prior, Mapping):
            continue
        if not isinstance(prior.get("claim_exposure"), Mapping):
            return False
    return True


def _eligibility(claim: Mapping[str, Any]) -> str | None:
    """Why this claim can never reach the author at all, or None if it could.

    This is the *recipient* half of eligibility only. Whether the claim also
    arrived in time to explain any recorded finding is decided by
    :func:`_reached_in_time`, against the findings themselves — a claim's own
    timestamp says nothing about that on its own.
    """
    role = str(claim.get("agent_role") or "").strip()
    if not role:
        return "recipient_role_unrecorded"
    if role != AUTHOR_ROLE:
        return f"rendered_to_{role}_only_not_author"
    if _parse_time(claim.get("rendered_at")) is None:
        return "render_time_unrecorded"
    return None


def _reached_in_time(claim: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> bool:
    """True when this claim preceded at least one of the recorded findings.

    A claim rendered after *every* finding this run recorded could not have been
    acted on by any of them, so it is not merely unmatched — it was never
    eligible, and a run holding nothing else has no correspondence to compute.
    Counting it as eligible would put such a run in the compared path and report
    its findings as not matched to an eligible injected claim, which asserts a
    comparison that never happened (#2684 review cycle 1).

    A finding whose own recording time is unrecorded cannot rule the claim out:
    the order is unknown, not late, and unknown order is what the compared path
    reports as indeterminate.
    """
    rendered_at = _parse_time(claim.get("rendered_at"))
    if rendered_at is None:
        return False
    for finding in findings:
        recorded_at = _parse_time(finding.get("recorded_at"))
        if recorded_at is None or rendered_at <= recorded_at:
            return True
    return False


# ── Correspondence ───────────────────────────────────────────────────────────


def _finding_text(finding: Mapping[str, Any]) -> str:
    for key in ("description", "observed", "summary"):
        value = finding.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _classify_finding(finding: Mapping[str, Any], eligible_claims: Sequence[Mapping]) -> dict:
    """Decide one finding against the claims it could have acted on.

    Returns matched / not_matched / indeterminate. Indeterminate is a real
    answer here, not a shrug: it is what the record must say when the finding
    carries too little text to compare, or when a claim that *would* match
    cannot be ordered against it in time.
    """
    finding_id = finding.get("finding_id")
    text = _finding_text(finding)
    base = {
        "finding_id": finding_id,
        "description": text,
        "severity": finding.get("severity"),
        "recorded_at": finding.get("recorded_at"),
    }

    finding_tokens = _tokens(text)
    if len(finding_tokens) < _MIN_TOKENS:
        return {**base, "outcome": OUTCOME_INDETERMINATE, "reason": "finding_text_insufficient"}

    recorded_at = _parse_time(finding.get("recorded_at"))

    best_ordered: tuple[float, Mapping] | None = None
    best_unordered: tuple[float, Mapping] | None = None
    for claim in eligible_claims:
        claim_tokens = _tokens(claim.get("claim"))
        if len(claim_tokens) < _MIN_TOKENS:
            continue
        score = _overlap(claim_tokens, finding_tokens)
        rendered_at = _parse_time(claim.get("rendered_at"))
        if recorded_at is not None and rendered_at is not None:
            if rendered_at > recorded_at:
                # Rendered after the finding was recorded: it cannot have been
                # available to act on, so it is not a candidate at all.
                continue
            if best_ordered is None or score > best_ordered[0]:
                best_ordered = (score, claim)
        elif best_unordered is None or score > best_unordered[0]:
            best_unordered = (score, claim)

    if best_ordered is not None and best_ordered[0] >= _MATCH_THRESHOLD:
        score, claim = best_ordered
        return {
            **base,
            "outcome": OUTCOME_MATCHED,
            "score": round(score, 3),
            "claim_ref": claim.get("claim_ref"),
            "claim_index": claim.get("index"),
            "claim": claim.get("claim"),
            "claim_run_id": claim.get("run_id"),
            "claim_phase": claim.get("phase"),
            "claim_agent_role": claim.get("agent_role"),
            "claim_phase_iteration": claim.get("phase_iteration"),
        }

    ordered_score = best_ordered[0] if best_ordered else 0.0
    if ordered_score >= _INDETERMINATE_THRESHOLD:
        return {
            **base,
            "outcome": OUTCOME_INDETERMINATE,
            "reason": "overlap_below_match_above_noise",
            "score": round(ordered_score, 3),
            "claim_ref": best_ordered[1].get("claim_ref") if best_ordered else None,
        }

    # A claim that would have matched but whose order against this finding is
    # unknown cannot be counted either way (#2684).
    if best_unordered is not None and best_unordered[0] >= _INDETERMINATE_THRESHOLD:
        return {
            **base,
            "outcome": OUTCOME_INDETERMINATE,
            "reason": "claim_finding_order_unknown",
            "score": round(best_unordered[0], 3),
            "claim_ref": best_unordered[1].get("claim_ref"),
        }

    return {**base, "outcome": OUTCOME_NOT_MATCHED, "score": round(ordered_score, 3)}


# ── Report ───────────────────────────────────────────────────────────────────


def build_uptake_report(
    *,
    context_manifests: Sequence[Mapping[str, Any]] | None,
    findings: Sequence[Mapping[str, Any]] | None,
    validation: Mapping[str, Any] | None = None,
) -> dict:
    """Compare this run's injected claims against its recorded review findings.

    ``validation`` is the agreement measurement from :func:`measure_agreement`.
    Passing ``None`` does not omit the figures — it marks them unvalidated,
    because a number whose reliability was never checked is still a number a
    reader will act on.
    """
    manifests = list(context_manifests or [])
    finding_list = [f for f in (findings or []) if isinstance(f, Mapping)]
    method = {"name": METHOD_NAME, "version": METHOD_VERSION}
    validation_block = dict(validation) if validation else _unvalidated("not_measured")

    base = {
        "method": method,
        "author_role": AUTHOR_ROLE,
        "validation": validation_block,
        "interpretation": INTERPRETATION_NOTE,
    }

    if not _capture_present(manifests):
        return {
            **base,
            "status": STATUS_UNCOMPARABLE,
            "note": (
                "this run predates claim-exposure capture; what each agent was shown "
                "is not recorded, so its findings cannot be compared against injected claims"
            ),
            "claims_rendered": None,
            "claims_eligible": None,
            "review_findings": len(finding_list),
            "counts": None,
            "correspondences": None,
        }

    claims = _claim_records(manifests)
    exclusions: dict[str, int] = {}
    reached_author: list[dict] = []
    for claim in claims:
        reason = _eligibility(claim)
        if reason is None:
            reached_author.append(claim)
        else:
            exclusions[reason] = exclusions.get(reason, 0) + 1

    # Temporal eligibility is decided against the findings, not against the
    # claim alone — and only when there are findings to decide it against. With
    # no findings recorded there is nothing for a claim to be early or late
    # relative to, so the recipient filter is the whole answer and the run falls
    # to the no-findings path below.
    if finding_list:
        eligible = [claim for claim in reached_author if _reached_in_time(claim, finding_list)]
        late = len(reached_author) - len(eligible)
        if late:
            exclusions["rendered_after_every_recorded_finding"] = late
    else:
        eligible = reached_author

    common = {
        **base,
        "claims_rendered": len(claims),
        "claims_rendered_by_recipient": _recipient_breakdown(claims),
        "claims_eligible": len(eligible),
        "claims_excluded": [
            {"reason": reason, "count": count} for reason, count in sorted(exclusions.items())
        ],
        "review_findings": len(finding_list),
    }

    if not eligible:
        # Name which way eligibility failed. "Never reached the author" and
        # "reached the author only after every finding was recorded" are
        # different facts about the loop, and an operator inspecting selection
        # or placement needs to tell them apart.
        note = (
            "every injected claim that reached the author was rendered after all "
            "recorded findings; nothing to compare"
            if reached_author
            else "no injected claim reached the author of the reviewed work; nothing to compare"
        )
        return {
            **common,
            "status": STATUS_NO_ELIGIBLE_CLAIMS,
            "note": note,
            "counts": None,
            "correspondences": None,
        }
    if not finding_list:
        return {
            **common,
            "status": STATUS_NO_REVIEW_FINDINGS,
            "note": "the review recorded no findings; nothing to compare",
            "counts": None,
            "correspondences": None,
        }

    classified = [_classify_finding(finding, eligible) for finding in finding_list]
    return {
        **common,
        "status": STATUS_COMPARED,
        "counts": {
            OUTCOME_MATCHED: sum(1 for c in classified if c["outcome"] == OUTCOME_MATCHED),
            OUTCOME_NOT_MATCHED: sum(1 for c in classified if c["outcome"] == OUTCOME_NOT_MATCHED),
            OUTCOME_INDETERMINATE: sum(
                1 for c in classified if c["outcome"] == OUTCOME_INDETERMINATE
            ),
        },
        "correspondences": classified,
    }


def _recipient_breakdown(claims: Sequence[Mapping[str, Any]]) -> list[dict]:
    grouped: dict[tuple, int] = {}
    for claim in claims:
        key = (
            str(claim.get("agent_role") or ""),
            str(claim.get("phase") or ""),
            claim.get("phase_iteration"),
        )
        grouped[key] = grouped.get(key, 0) + 1
    return [
        {"agent_role": role, "phase": phase, "phase_iteration": iteration, "count": count}
        for (role, phase, iteration), count in sorted(grouped.items(), key=lambda kv: str(kv[0]))
    ]


# ── Labelled agreement ───────────────────────────────────────────────────────


def _unvalidated(reason: str, **extra: Any) -> dict:
    return {
        "status": VALIDATION_UNVALIDATED,
        "reason": reason,
        "agreement": None,
        "n": 0,
        **extra,
    }


def load_labelled_examples(project_root: Path) -> dict | None:
    """Read the stored labelled set, or None when it is absent/unreadable."""
    import yaml  # noqa: PLC0415

    path = Path(project_root).joinpath(*LABELLED_SET_RELPATH)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def measure_agreement(labelled: Mapping[str, Any] | None) -> dict:
    """Score this matcher against human labelling.

    A set labelled against a *different* method version is not evidence about
    this one, so it reports unvalidated rather than lending its agreement to a
    method nobody checked.
    """
    if not isinstance(labelled, Mapping):
        return _unvalidated("labelled_set_unavailable")

    declared_method = str(labelled.get("method") or "")
    declared_version = str(labelled.get("method_version") or "")
    if declared_method != METHOD_NAME or declared_version != METHOD_VERSION:
        return _unvalidated(
            "labelled_set_method_mismatch",
            labelled_method=declared_method or None,
            labelled_method_version=declared_version or None,
        )

    examples = labelled.get("examples")
    if not isinstance(examples, list) or not examples:
        return _unvalidated("labelled_set_empty")

    agreed = 0
    total = 0
    disagreements: list[dict] = []
    for example in examples:
        if not isinstance(example, Mapping):
            continue
        expected = str(example.get("label") or "")
        finding = example.get("finding")
        if expected not in (OUTCOME_MATCHED, OUTCOME_NOT_MATCHED, OUTCOME_INDETERMINATE):
            continue
        if not isinstance(finding, Mapping):
            continue
        total += 1
        actual = _label_example(example)
        if actual == expected:
            agreed += 1
        else:
            disagreements.append({"id": example.get("id"), "expected": expected, "actual": actual})

    if not total:
        return _unvalidated("labelled_set_empty")

    return {
        "status": VALIDATION_MEASURED,
        "agreement": round(agreed / total, 3),
        "n": total,
        "method": METHOD_NAME,
        "method_version": METHOD_VERSION,
        "disagreements": disagreements,
        "labelled_set": "/".join(LABELLED_SET_RELPATH),
    }


def _label_example(example: Mapping[str, Any]) -> str:
    """Run one labelled example through the full production path.

    Deliberately through :func:`build_uptake_report` and not the matcher alone:
    a labelled set that exercised only the text comparison could not cover the
    eligibility rules, and eligibility is where this indicator is most likely
    to be wrong in a way that blames the wrong agent.
    """
    claims = [dict(c) for c in example.get("claims") or [] if isinstance(c, Mapping)]
    manifest = [
        {
            "phase": "dev",
            "prior_run_context": {
                "enabled": True,
                "claim_exposure": {"capture_version": 1},
                "included": [{"run_id": "labelled", "claims": claims}],
            },
        }
    ]
    report = build_uptake_report(
        context_manifests=manifest,
        findings=[example.get("finding")],
        validation={"status": VALIDATION_MEASURED},
    )
    if report["status"] != STATUS_COMPARED:
        # No claim was eligible — it never reached the author, or reached it only
        # after the finding was recorded. Either way the finding is not matched
        # to an eligible claim, which is the same statement the compared path
        # would make, so the labelled set can score both shapes uniformly.
        return OUTCOME_NOT_MATCHED
    return str(report["correspondences"][0]["outcome"])


def build_uptake_report_for_project(
    project_root: Path,
    *,
    context_manifests: Sequence[Mapping[str, Any]] | None,
    findings: Sequence[Mapping[str, Any]] | None,
) -> dict:
    """``build_uptake_report`` with the project's labelled set applied."""
    return build_uptake_report(
        context_manifests=context_manifests,
        findings=findings,
        validation=measure_agreement(load_labelled_examples(project_root)),
    )
