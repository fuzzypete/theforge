"""Sprint budget enforcement decisions.

Pure, stdlib-only: given what the sprint has measurably spent and which spend it
could not measure, decide whether the sprint may keep spending. Extracted from
``runner.py`` so the policy is testable without a live sprint.

One decision point serves two enforcement moments (#2547). Dispatch asks it
before a story starts; the in-flight checkpoint asks it again while a story is
running, charging that story's measured spend so far on top of the ledger. The
second moment exists because the first one alone bounds nothing: a sprint whose
work is one long story never returns to a dispatch boundary, so a cap checked
only there is a number the run passes rather than a number that binds it.

The two moments differ in what they do with an *unverifiable* answer, and only
in that. Dispatch refuses to launch new work against a total it knows is a lower
bound. The in-flight checkpoint acts on ``exhausted`` alone: a running story is
still cancelled when its measured lower bound has definitely met the cap, but it
is not cancelled merely because some spend was unmeasured and the comparison is
therefore unverifiable. Killing a story that has already been paid for to
protect only that second case would destroy work for a comparison the sprint
will re-run at its next dispatch boundary anyway.

The load-bearing rule is that a cap can only be enforced against a number that
means what it says. ``accumulated_cost`` is a sum over measured spend; when a
transport reports no cost, that story contributes ``0.0`` to the sum while
having spent an unknown amount. Comparing that understated total against the
cap would certify a budget the sprint cannot actually show it is within, and
the understatement correlates with which transport served a story — so the
error is systematic, not noise (#1992). When any spend is unmeasured the check
therefore fails closed: it refuses to dispatch further work rather than
dispatch it against a number it knows is a lower bound.

Failing closed is not the same as failing forever. An unmeasured source whose
origin and ceiling are both recorded can be accepted by an operator, and the
accepted ceiling is then charged to the comparison in place of the unknown
(#2310) — the cap is still verified against a number that means what it says,
and the number says "at most this much". Only sources with no such resolution
keep the check closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: How many unmeasured sources to name in an operator-facing message before
#: eliding the rest. The full list lives in the sprint's structured records.
_MAX_NAMED_SOURCES = 5

#: A cap was configured and the run stayed inside it.
BUDGET_STATUS_WITHIN = "within"
#: A cap was configured and the run finished past it. Enforcement halts a run at
#: the first checkpoint after the cap is met, but a phase already in flight can
#: land beyond it, so "over" is a state an honest run can legitimately reach —
#: which is exactly why it has to be reported rather than inferred (#2547).
BUDGET_STATUS_OVER = "over"
#: No positive cap was configured, so there is nothing to be within or over.
BUDGET_STATUS_UNSET = "unset"

#: Spend has to pass the cap by more than rounding noise before a run is called
#: over budget: a story landing at exactly the cap met it, it did not breach it.
_OVERRUN_EPSILON_USD = 0.005


@dataclass(frozen=True)
class BudgetBlock:
    """A decision to stop dispatching, with the operator-facing wording.

    ``kind`` is ``"exhausted"`` (measured spend met or passed the cap) or
    ``"unverifiable"`` (spend is unknown, so the cap cannot be evaluated).
    """

    kind: str
    detail: str

    @property
    def story_reason(self) -> str:
        """Reason recorded on each story skipped by this decision."""
        label = "budget exhausted" if self.kind == "exhausted" else "budget unverifiable"
        return f"{label} ({self.detail})"

    @property
    def stopped_reason(self) -> str:
        """Sprint-level ``stopped_reason`` recorded in the summary and audit."""
        label = "Budget exhausted" if self.kind == "exhausted" else "Budget unverifiable"
        return f"{label} ({self.detail})"

    def notification_title(self, sprint_name: str) -> str:
        label = "budget exceeded" if self.kind == "exhausted" else "budget unverifiable"
        return f'TheForge: {label} — "{sprint_name}"'


def describe_unmeasured_spend(sources: Sequence[str]) -> str:
    """Render the unmeasured-spend sources for an operator message."""
    named = list(sources[:_MAX_NAMED_SOURCES])
    elided = len(sources) - len(named)
    rendered = ", ".join(named)
    if elided > 0:
        rendered = f"{rendered}, +{elided} more"
    return rendered


def budget_verification_spend(
    *,
    accumulated_cost: float,
    prior_cost: float,
    accepted_unmeasured_ceiling_usd: float = 0.0,
) -> float:
    """The figure the cap is actually verified against.

    Measured spend plus every accepted ceiling. It is deliberately not called a
    total: the ceilings in it stand in for spend nobody measured, so it is an
    upper bound on this sprint, not a statement of what it cost.
    """
    return round(prior_cost + accumulated_cost + max(0.0, accepted_unmeasured_ceiling_usd), 6)


def evaluate_budget(
    *,
    accumulated_cost: float,
    prior_cost: float,
    budget_usd: float,
    unmeasured_spend: Sequence[str],
    accepted_unmeasured_ceiling_usd: float = 0.0,
    source_details: Mapping[str, str] | None = None,
    acceptable_unmeasured_spend_sources: Sequence[str] | None = None,
) -> BudgetBlock | None:
    """Return the reason to stop dispatching, or ``None`` to proceed.

    ``unmeasured_spend`` names every source whose spend this sprint could not
    measure AND that no operator has resolved (completed stories, intake
    remediation passes). It being non-empty means ``accumulated_cost`` is a
    lower bound with no ceiling standing in for the remainder.

    ``accepted_unmeasured_ceiling_usd`` is the sum of the ceilings an operator
    deliberately accepted for sources that WERE resolvable. It is charged to the
    comparison exactly as if it had been spent, so accepting never buys
    headroom — it converts an unanswerable comparison into a conservative one.

    ``source_details`` maps a source id to a one-line description of its origin
    and bound, so a refusal names the amount and where it came from rather than
    reporting an opaque condition.

    ``acceptable_unmeasured_spend_sources`` lists the unresolved sources whose
    ceilings are derivable, so the refusal can name the exact flags that would
    let an operator accept those ceilings on the next launch.

    Exhaustion is checked first: when the verification figure already meets the
    cap, that is a definite answer, whether or not other spend is unresolved.
    Its wording is unchanged when nothing was accepted. Otherwise, unresolved
    unmeasured spend makes the comparison unanswerable and the sprint stops
    rather than launching more work against a total it knows is understated.
    """
    accepted = max(0.0, accepted_unmeasured_ceiling_usd)
    cumulative = prior_cost + accumulated_cost
    verification = budget_verification_spend(
        accumulated_cost=accumulated_cost,
        prior_cost=prior_cost,
        accepted_unmeasured_ceiling_usd=accepted,
    )
    if verification >= budget_usd:
        base = (
            f"sprint ${accumulated_cost:.2f} + carried ${prior_cost:.2f} = "
            f"${cumulative:.2f} >= ${budget_usd:.2f}"
        )
        if accepted > 0.0:
            base = (
                f"sprint ${accumulated_cost:.2f} + carried ${prior_cost:.2f} + "
                f"accepted unmeasured ceiling ${accepted:.2f} = "
                f"${verification:.2f} >= ${budget_usd:.2f}"
            )
        return BudgetBlock(kind="exhausted", detail=base)
    if unmeasured_spend:
        detail = (
            f"spend unmeasured for {len(unmeasured_spend)} source(s) "
            f"[{describe_unmeasured_spend(unmeasured_spend)}]; "
            f"measured ${cumulative:.2f} of ${budget_usd:.2f} cap is a lower bound, "
            "so the cap cannot be verified"
        )
        rendered = describe_source_origins(unmeasured_spend, source_details)
        if rendered:
            detail = f"{detail}; {rendered}"
        remedy = describe_unmeasured_spend_acceptance(acceptable_unmeasured_spend_sources)
        if remedy:
            detail = f"{detail}; {remedy}"
        return BudgetBlock(kind="unverifiable", detail=detail)
    return None


def budget_overrun_usd(*, budget_usd: float, spend_usd: float | None) -> float:
    """How far *spend_usd* passed the cap, or ``0.0`` when it did not.

    The reporting half of the same policy ``evaluate_budget`` enforces. It is
    deliberately a separate function: enforcement decides whether more work may
    start, reporting states what a finished or in-progress run actually did, and
    a run can be over budget without anything having been refused (the phase
    that crossed the cap was already running when it did).
    """
    if budget_usd <= 0.0 or spend_usd is None:
        return 0.0
    overrun = float(spend_usd) - float(budget_usd)
    if overrun <= _OVERRUN_EPSILON_USD:
        return 0.0
    return round(overrun, 6)


def budget_status(*, budget_usd: float, spend_usd: float | None) -> str:
    """Classify a run against its cap: within, over, or no cap configured."""
    if budget_usd <= 0.0:
        return BUDGET_STATUS_UNSET
    if budget_overrun_usd(budget_usd=budget_usd, spend_usd=spend_usd) > 0.0:
        return BUDGET_STATUS_OVER
    return BUDGET_STATUS_WITHIN


def describe_source_origins(
    sources: Sequence[str],
    source_details: Mapping[str, str] | None,
) -> str:
    """Render per-source origin/ceiling detail for a refusal message.

    A decision about accepting an unknown is only possible if the unknown is
    bounded and attributed, so the refusal carries both — for the same sources
    it names, under the same elision rule.
    """
    if not source_details:
        return ""
    lines = [
        source_details[src] for src in sources[:_MAX_NAMED_SOURCES] if source_details.get(src)
    ]
    if not lines:
        return ""
    return "unresolved: " + "; ".join(lines)


def describe_unmeasured_spend_acceptance(sources: Sequence[str] | None) -> str:
    """Render the concrete acceptance command for resolvable unknowns."""
    if not sources:
        return ""
    rendered = " ".join(f"--accept-unmeasured-spend {source}" for source in sources)
    return f"to proceed, rerun with {rendered}"
