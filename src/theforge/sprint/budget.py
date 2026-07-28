"""Sprint budget enforcement decisions.

Pure, stdlib-only: given what the sprint has measurably spent and which spend it
could not measure, decide whether the next story may be dispatched. Extracted
from ``runner.py`` so the policy is testable without a live sprint.

The load-bearing rule is that a cap can only be enforced against a number that
means what it says. ``accumulated_cost`` is a sum over measured spend; when a
transport reports no cost, that story contributes ``0.0`` to the sum while
having spent an unknown amount. Comparing that understated total against the
cap would certify a budget the sprint cannot actually show it is within, and
the understatement correlates with which transport served a story — so the
error is systematic, not noise (#1992). When any spend is unmeasured the check
therefore fails closed: it refuses to dispatch further work rather than
dispatch it against a number it knows is a lower bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: How many unmeasured sources to name in an operator-facing message before
#: eliding the rest. The full list lives in the sprint's structured records.
_MAX_NAMED_SOURCES = 5


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


def evaluate_budget(
    *,
    accumulated_cost: float,
    prior_cost: float,
    budget_usd: float,
    unmeasured_spend: Sequence[str],
) -> BudgetBlock | None:
    """Return the reason to stop dispatching, or ``None`` to proceed.

    ``unmeasured_spend`` names every source whose spend this sprint could not
    measure (completed stories, intake remediation passes). It being non-empty
    means ``accumulated_cost`` is a lower bound.

    Exhaustion is checked first: when the measured lower bound alone already
    meets the cap, that is a definite answer and stays worded exactly as before,
    whether or not other spend was unmeasured. Otherwise, unmeasured spend makes
    the comparison unanswerable and the sprint stops rather than launching more
    work against a total it knows is understated.
    """
    cumulative = prior_cost + accumulated_cost
    if cumulative >= budget_usd:
        return BudgetBlock(
            kind="exhausted",
            detail=(
                f"sprint ${accumulated_cost:.2f} + carried ${prior_cost:.2f} = "
                f"${cumulative:.2f} >= ${budget_usd:.2f}"
            ),
        )
    if unmeasured_spend:
        return BudgetBlock(
            kind="unverifiable",
            detail=(
                f"spend unmeasured for {len(unmeasured_spend)} source(s) "
                f"[{describe_unmeasured_spend(unmeasured_spend)}]; "
                f"measured ${cumulative:.2f} of ${budget_usd:.2f} cap is a lower bound, "
                "so the cap cannot be verified"
            ),
        )
    return None
