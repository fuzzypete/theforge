"""Unmeasured sprint spend: what it was, where it came from, and accepting it.

The budget check fails closed on spend it could not measure (#1992), and that
is right: a ceiling that cannot be checked is not a ceiling. What does not
follow is that the condition must be permanent. A single agent call that exits
without reporting cost belongs to one completed run, whose allocation, phase,
role and failure are all recorded — so the unknown is bounded and attributed,
and an operator can accept that bound deliberately instead of abandoning the
pipeline for the story (#2310).

This module is the sprint-owned representation of that: it normalizes source
ids, reads the origin and a derivable ceiling out of records the run already
wrote, and serializes the resolution. Accepting a source does NOT relabel its
cost as measured — ``cost_complete`` stays false and the measured total stays a
lower bound. What acceptance changes is only which number the cap is verified
against: the accepted ceiling is charged in place of an unknown.

An acceptance also resolves one OCCURRENCE, not a story. It stands in for a
specific recorded call, at a specific recorded ceiling, in a specific recorded
run. If the same story goes unmeasured again, that is a second unknown nobody
has bounded, and the guard closes on it again — see :func:`partition`.

Pure and stdlib-only apart from one narrow YAML read of a per-story audit, so
the policy is testable without a live sprint.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Prefix the runner stamps on spend inherited from an earlier generation of the
#: same sprint. ``carried:issue-2206`` and ``issue-2206`` name the same STORY —
#: the first is how a resume sees it, the second is how the generation that ran
#: it did — so acceptance is looked up on the normalized form. Which of that
#: story's unmeasured calls a given entry stands for is a separate question, and
#: is settled by occurrence identity rather than by the prefix (see
#: :func:`partition`).
CARRIED_PREFIX = "carried:"

#: The whole-generation marker the resume path adds when the prior sprint audit
#: recorded an incomplete cost. It is derived, not observed: it says only that
#: SOME source that generation named went unmeasured. So it has no story, no
#: allocation, no origin and no accept path, and it is never carried forward once
#: every source it stood for has been accepted — see
#: :func:`acceptable_prior_sources`.
PRIOR_GENERATION_SOURCE = "prior-generation"

#: Where a ceiling came from. Only ``derived`` bounds may be accepted.
CEILING_BASIS_ALLOCATION = "story_allocation"
CEILING_BASIS_CONFIGURED = "story_allocation_configured_fallback"
CEILING_BASIS_NONE = "no_recorded_bound"


def normalize_source_id(raw: object) -> str:
    """Return the canonical identity of an unmeasured-spend source."""
    text = str(raw or "").strip()
    while text.startswith(CARRIED_PREFIX):
        text = text[len(CARRIED_PREFIX) :].strip()
    return text


def source_slug(raw: object) -> str | None:
    """The story slug a source names, or ``None`` when it names no story.

    Story sources are recorded bare (``issue-2206``) or carried
    (``carried:issue-2206``). Every other source carries a kind prefix
    (``intake:``, ``entry-intake:``, ``dropped-with-work:``, …) naming work that
    is not a story run, and the whole-generation marker names no work at all.
    """
    normalized = normalize_source_id(raw)
    if not normalized or normalized == PRIOR_GENERATION_SOURCE:
        return None
    if ":" in normalized:
        return None
    return normalized


@dataclass(frozen=True)
class UnmeasuredSource:
    """One source of spend a sprint could not measure, with its origin.

    ``ceiling_usd`` is ``None`` when the recorded audit cannot support a bound.
    Such a source is never acceptable: fabricating a number to get past the
    check is exactly what measuring exists to prevent.
    """

    raw: str
    source: str
    origin: dict = field(default_factory=dict)
    measured_lower_bound_usd: float = 0.0
    ceiling_usd: float | None = None
    ceiling_basis: str = CEILING_BASIS_NONE
    ceiling_reason: str = "no per-story audit recorded a bound for this source"

    @property
    def acceptable(self) -> bool:
        """Whether an operator has a bounded number available to accept."""
        return self.ceiling_usd is not None

    def describe(self) -> str:
        """Operator-facing one-liner: what is unknown, how much, and from where."""
        origin_bits = [
            f"{key}={self.origin[key]}"
            for key in ("run_id", "phase", "role", "profile", "failure_code")
            if self.origin.get(key)
        ]
        origin_text = ", ".join(origin_bits) if origin_bits else "origin not recorded"
        if self.ceiling_usd is None:
            return f"{self.raw}: unbounded ({self.ceiling_reason}; {origin_text})"
        return (
            f"{self.raw}: measured ${self.measured_lower_bound_usd:.2f}, "
            f"at most ${self.ceiling_usd:.2f} more ({self.ceiling_basis}; {origin_text})"
        )

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "recorded_as": self.raw,
            "origin": dict(self.origin),
            "measured_lower_bound_usd": round(self.measured_lower_bound_usd, 4),
            "ceiling_usd": None if self.ceiling_usd is None else round(self.ceiling_usd, 4),
            "ceiling_basis": self.ceiling_basis,
            "ceiling_reason": self.ceiling_reason,
            "acceptable": self.acceptable,
        }


@dataclass(frozen=True)
class AcceptedUnmeasuredSpend:
    """An operator's deliberate acceptance of one bounded unmeasured source.

    The record is the whole point: the cap is afterwards verified against
    ``accepted_ceiling_usd`` rather than against an unknown, so a reader must be
    able to see which source that number stands in for, where it came from, and
    who decided it was acceptable.
    """

    source: str
    accepted_ceiling_usd: float
    measured_lower_bound_usd: float = 0.0
    ceiling_basis: str = CEILING_BASIS_NONE
    origin_run_id: str | None = None
    origin_phase: str | None = None
    origin_role: str | None = None
    origin_profile: str | None = None
    origin_failure_code: str | None = None
    accepted_at: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "accepted_ceiling_usd": round(self.accepted_ceiling_usd, 4),
            "measured_lower_bound_usd": round(self.measured_lower_bound_usd, 4),
            "ceiling_basis": self.ceiling_basis,
            "origin_run_id": self.origin_run_id,
            "origin_phase": self.origin_phase,
            "origin_role": self.origin_role,
            "origin_profile": self.origin_profile,
            "origin_failure_code": self.origin_failure_code,
            "accepted_at": self.accepted_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "AcceptedUnmeasuredSpend | None":
        """Rebuild a persisted record, or ``None`` when it is unusable.

        A record without a source or without a numeric ceiling resolves nothing
        — it is dropped rather than treated as an acceptance of an unknown.
        """
        if not isinstance(data, Mapping):
            return None
        source = normalize_source_id(data.get("source"))
        if not source:
            return None
        try:
            ceiling = float(data.get("accepted_ceiling_usd"))
        except (TypeError, ValueError):
            return None
        try:
            lower_bound = float(data.get("measured_lower_bound_usd") or 0.0)
        except (TypeError, ValueError):
            lower_bound = 0.0
        return cls(
            source=source,
            accepted_ceiling_usd=ceiling,
            measured_lower_bound_usd=lower_bound,
            ceiling_basis=str(data.get("ceiling_basis") or CEILING_BASIS_NONE),
            origin_run_id=_opt_str(data.get("origin_run_id")),
            origin_phase=_opt_str(data.get("origin_phase")),
            origin_role=_opt_str(data.get("origin_role")),
            origin_profile=_opt_str(data.get("origin_profile")),
            origin_failure_code=_opt_str(data.get("origin_failure_code")),
            accepted_at=_opt_str(data.get("accepted_at")),
            reason=_opt_str(data.get("reason")),
        )


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _phase_for_role(role: str | None) -> str | None:
    """Map an audit agent role onto the coordinator phase that invoked it."""
    if not role:
        return None
    lowered = str(role).lower()
    if lowered.startswith("review") or lowered == "synthesis":
        return "REVIEW"
    if lowered.startswith("dev"):
        return "DEV"
    if lowered.startswith("plan"):
        return "PLAN"
    if lowered.startswith("preflight"):
        return "PREFLIGHT"
    return lowered.upper()


def origin_from_story_audit(audit_data: Mapping | None) -> dict:
    """Extract the origin of a story's unmeasured spend from its audit record.

    The first agent invocation that reported no cost is the origin: that is the
    call whose spend is unknown. Everything reported here is read off the
    record, never inferred — an absent field stays absent.
    """
    origin: dict = {}
    if not isinstance(audit_data, Mapping):
        return origin
    run_id = _opt_str(audit_data.get("run_id"))
    if run_id:
        origin["run_id"] = run_id
    outcome = audit_data.get("outcome")
    if isinstance(outcome, Mapping):
        final_phase = _opt_str(outcome.get("final_phase"))
        if final_phase:
            origin["final_phase"] = final_phase
    failure_code = _opt_str(audit_data.get("error_type"))
    cost = audit_data.get("cost")
    agents = cost.get("agents") if isinstance(cost, Mapping) else None
    if isinstance(agents, list):
        for agent in agents:
            if not isinstance(agent, Mapping) or agent.get("cost_usd") is not None:
                continue
            role = _opt_str(agent.get("role"))
            if role:
                origin["role"] = role
                phase = _phase_for_role(role)
                if phase:
                    origin["phase"] = phase
            profile = _opt_str(agent.get("profile"))
            if profile:
                origin["profile"] = profile
            agent_failure = _opt_str(agent.get("failure_code"))
            if agent_failure:
                failure_code = agent_failure
            break
    if failure_code:
        origin["failure_code"] = failure_code
    if "phase" not in origin and origin.get("final_phase"):
        origin["phase"] = origin["final_phase"]
    return origin


def _measured_lower_bound(audit_data: Mapping | None) -> float:
    """Sum the agent invocations of a story that DID report a cost."""
    if not isinstance(audit_data, Mapping):
        return 0.0
    cost = audit_data.get("cost")
    if not isinstance(cost, Mapping):
        return 0.0
    total = cost.get("total_usd")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return max(0.0, float(total))
    agents = cost.get("agents")
    if not isinstance(agents, list):
        return 0.0
    measured = 0.0
    for agent in agents:
        if not isinstance(agent, Mapping):
            continue
        value = agent.get("cost_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            measured += float(value)
    return max(0.0, measured)


def _allocation_ceiling(audit_data: Mapping | None) -> tuple[float | None, str, str]:
    """Return ``(allocation_usd, basis, reason)`` for a story's recorded ceiling.

    The per-story allocation is the amount the run was permitted to spend on
    this story, so it bounds what the unmeasured call inside it could have cost.
    When no allocation was recorded there is no bound to offer, and the source
    stays unresolved rather than being given a guessed one.
    """
    if not isinstance(audit_data, Mapping):
        return None, CEILING_BASIS_NONE, "no per-story audit found for this source"
    cost = audit_data.get("cost")
    allocation = cost.get("story_allocation") if isinstance(cost, Mapping) else None
    if not isinstance(allocation, Mapping):
        return (
            None,
            CEILING_BASIS_NONE,
            "the per-story audit records no allocation to bound the unmeasured call",
        )
    for key, basis in (
        ("allocation_usd", CEILING_BASIS_ALLOCATION),
        ("fallback_configured_usd", CEILING_BASIS_CONFIGURED),
    ):
        value = allocation.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0.0:
            return (
                float(value),
                basis,
                f"bounded by the story's recorded {key} (${float(value):.2f})",
            )
    return (
        None,
        CEILING_BASIS_NONE,
        "the recorded allocation carries no usable dollar figure",
    )


def read_story_audit(project_root: Path, sprint_name: str, slug: str) -> dict | None:
    """Load ``.forge/logs/<sprint>/<slug>/audit.yaml``, or ``None``.

    The one I/O boundary in this module. An unreadable record is not an error
    here — it means the source has no derivable bound, which the caller reports
    as unresolvable rather than papering over.
    """
    import yaml  # noqa: PLC0415

    audit_path = Path(project_root) / ".forge" / "logs" / sprint_name / slug / "audit.yaml"
    if not audit_path.exists():
        return None
    try:
        with open(audit_path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def build_source(raw: str, story_audit: Mapping | None) -> UnmeasuredSource:
    """Describe one unmeasured source from the records that named it.

    Pure over ``story_audit`` so the derivation is testable without a sprint
    tree; :func:`read_story_audit` supplies the record. Deliberately describes
    ONE record rather than a whole list: a story has one audit path, and the
    caller — which knows whether it is reading a carried occurrence or one this
    run just produced — is the only thing that can say which reading of that path
    a description belongs to.
    """
    normalized = normalize_source_id(raw)
    origin = origin_from_story_audit(story_audit)
    lower_bound = _measured_lower_bound(story_audit)
    allocation, basis, reason = _allocation_ceiling(story_audit)
    if allocation is None:
        return UnmeasuredSource(
            raw=str(raw),
            source=normalized,
            origin=origin,
            measured_lower_bound_usd=lower_bound,
            ceiling_usd=None,
            ceiling_basis=basis,
            ceiling_reason=reason,
        )
    # The bound on what is UNKNOWN, not on the story: the measured part is
    # already in the sprint's accumulated total, and charging the whole
    # allocation on top of it would bill the same dollars twice.
    remainder = max(0.0, float(allocation) - lower_bound)
    return UnmeasuredSource(
        raw=str(raw),
        source=normalized,
        origin={**origin, "allocation_usd": round(float(allocation), 4)},
        measured_lower_bound_usd=lower_bound,
        ceiling_usd=round(remainder, 4),
        ceiling_basis=basis,
        ceiling_reason=reason,
    )


def accept(
    source: UnmeasuredSource,
    *,
    accepted_at: str,
    reason: str | None = None,
) -> AcceptedUnmeasuredSpend | None:
    """Turn a bounded source into an acceptance record, or ``None`` if unbounded."""
    if source.ceiling_usd is None:
        return None
    return AcceptedUnmeasuredSpend(
        source=source.source,
        accepted_ceiling_usd=float(source.ceiling_usd),
        measured_lower_bound_usd=source.measured_lower_bound_usd,
        ceiling_basis=source.ceiling_basis,
        origin_run_id=source.origin.get("run_id"),
        origin_phase=source.origin.get("phase"),
        origin_role=source.origin.get("role"),
        origin_profile=source.origin.get("profile"),
        origin_failure_code=source.origin.get("failure_code"),
        accepted_at=accepted_at,
        reason=reason,
    )


def partition(
    raw_sources: Sequence[str],
    accepted: Mapping[str, AcceptedUnmeasuredSpend],
    *,
    current_generation: Iterable[str] = (),
    occurrence_ids: Mapping[str, str | None] | None = None,
) -> tuple[list[str], list[AcceptedUnmeasuredSpend]]:
    """Split raw sources into the unresolved ones and the acceptances in force.

    Only acceptances whose source actually appears in ``raw_sources`` are
    returned: a ceiling is charged for spend this run is carrying, never for a
    stale record of work it is not.

    An acceptance resolves the OCCURRENCE it was made for, not the story: it
    stands in for one recorded call, at one recorded ceiling, in one recorded
    run. A second unmeasured call is a second unknown, of an amount nobody has
    bounded and nobody has accepted, so the guard closes on it exactly as it did
    the first time. Without this an operator's one-time acceptance would
    silently become a standing licence for that story to spend unmeasured
    forever. Two independent tests carry that, because the two occurrences look
    different depending on when you ask:

    ``current_generation`` names the raw sources THIS run produced itself — a
    story that completed unmeasured here, an intake pass that ran here. Nothing
    accepted before this run began can stand in for them.

    ``occurrence_ids`` maps a raw source to the run its unmeasured call happened
    in. Once the stopped run is resumed, its two occurrences are no longer
    distinguishable by *when* they were recorded — both are simply carried — so
    identity has to come from the record instead. An acceptance whose
    ``origin_run_id`` names a different run than the occurrence now in the ledger
    is an acceptance of some earlier call, and does not resolve this one. Either
    side being unrecorded falls back to matching on the source alone: an unknown
    identity is not evidence of a mismatch, and refusing there would strand an
    acceptance the operator legitimately made.
    """
    fresh = {str(s) for s in current_generation}
    ids = occurrence_ids or {}
    unresolved: list[str] = []
    applied: dict[str, AcceptedUnmeasuredSpend] = {}
    for raw in raw_sources:
        if str(raw) in fresh:
            unresolved.append(str(raw))
            continue
        normalized = normalize_source_id(raw)
        record = accepted.get(normalized)
        if record is None or not _same_occurrence(record, ids.get(str(raw))):
            unresolved.append(str(raw))
        else:
            applied.setdefault(normalized, record)
    return unresolved, list(applied.values())


def _same_occurrence(record: AcceptedUnmeasuredSpend, occurrence_id: str | None) -> bool:
    """Whether an acceptance was made for the occurrence now in the ledger."""
    if not record.origin_run_id or not occurrence_id:
        return True
    return str(record.origin_run_id) == str(occurrence_id)


def accepted_ceiling_total(records: Iterable[AcceptedUnmeasuredSpend]) -> float:
    """Sum of the ceilings a set of acceptances charges to budget verification."""
    return round(sum(max(0.0, float(r.accepted_ceiling_usd)) for r in records), 4)


def accepted_by_source(
    records: Iterable[Mapping | AcceptedUnmeasuredSpend],
) -> dict[str, AcceptedUnmeasuredSpend]:
    """Index acceptance records (persisted dicts or typed) by normalized source."""
    index: dict[str, AcceptedUnmeasuredSpend] = {}
    for record in records:
        typed = (
            record
            if isinstance(record, AcceptedUnmeasuredSpend)
            else AcceptedUnmeasuredSpend.from_dict(record)
        )
        if typed is not None:
            index[typed.source] = typed
    return index


def acceptable_prior_sources(recorded_sources: Sequence[str]) -> list[str]:
    """Normalized sources from a prior generation's record an operator can accept.

    The whole-generation marker is excluded, and this is the single place that
    exclusion is decided so no caller can drift from it. The marker names no work
    of its own: it is a DERIVED statement that some source in that generation was
    unmeasured, so it has no origin, no ceiling and no accept path by
    construction. Anything that treats it as a source — a completeness test, or a
    ledger re-surfacing pass — makes the generation permanently unresolvable,
    because the operator is then required to accept something nothing can bound.

    Order is preserved and duplicates collapse, since a source named twice by one
    record is one source as far as acceptance is concerned.
    """
    seen: set[str] = set()
    usable: list[str] = []
    for raw in recorded_sources:
        normalized = normalize_source_id(raw)
        if not normalized or normalized == PRIOR_GENERATION_SOURCE or normalized in seen:
            continue
        seen.add(normalized)
        usable.append(normalized)
    return usable


def all_sources_accepted(
    recorded_sources: Sequence[str],
    accepted: Mapping[str, AcceptedUnmeasuredSpend],
) -> bool:
    """Whether every source a prior generation named has been accepted.

    A record with nothing acceptable in it is False, not vacuously True: a
    generation that reported incomplete cost while naming only the derived
    marker (or nothing at all) carries a claim nothing here can resolve, so the
    guard must stay closed on it.
    """
    usable = acceptable_prior_sources(recorded_sources)
    if not usable:
        return False
    return all(s in accepted for s in usable)
