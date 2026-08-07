"""Comparing a project model declaration against the shipped entry it duplicates.

When the same canonical identity is defined in both the shipped catalog and
``forge.yaml``, the duplicate is *not* automatically inert. A project
declaration is resolved on its own terms, so it re-derives its own pricing
attribution: figures the operator wrote in ``forge.yaml`` are attributed to
``forge.yaml``, while the shipped entry's figures carry whatever the catalog
attributed them to — which for a vendor shorthand (``anthropic/haiku/cli``) is
*nothing at all*. Attribution is a routing input, not a label: it gates
``effective_*_cost_per_mtok``, which is what the cost band and the price
tie-break read (see :mod:`theforge.config.pricing`). So an entry that looks
character-for-character redundant can still route differently depending on which
half of the configuration defined it.

This module makes that difference computable, and therefore reportable. It
separates two kinds of divergence:

- **routing** differences — values something actually selects on. These change
  dispatch, so a duplicate that has them is a redefinition rather than a
  restatement.
- **attribution** differences — what a figure is attributed to and what a cost
  band is explained by. These are recorded and reported, but nothing selects on
  them directly; they matter because they *produce* the routing differences
  above, which is why both are reported together.

Stdlib-only apart from the identity leaf, so it stays importable from anywhere
in the config package (CONVENTIONS: pure-data types live in low-dependency
modules).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .model_identity import AgentSpec

# Values routing genuinely selects on.
#
# The two ``effective_*`` entries are the reason this list is not just
# ``RoutingPolicy``'s fields: they are attribution-gated views of the stored
# price (``None`` whenever the figures cannot be tied to the billed identity), so
# they are where a difference in *attribution* becomes a difference in
# *behaviour*. Comparing the raw price literals here instead would report a
# routing change for two entries whose prices routing is not allowed to read.
_ROUTING_READS: tuple[tuple[str, Callable[[AgentSpec], Any]], ...] = (
    ("tier", lambda s: s.routing.tier),
    ("capability", lambda s: s.routing.capability),
    ("cost_rank", lambda s: s.routing.cost_rank),
    ("dev_capable", lambda s: s.routing.dev_capable),
    ("phase_eligibility", lambda s: tuple(sorted(s.routing.phase_eligibility))),
    ("base_url", lambda s: s.base_url),
    ("effective_input_cost_per_mtok", lambda s: s.effective_input_cost_per_mtok),
    ("effective_output_cost_per_mtok", lambda s: s.effective_output_cost_per_mtok),
)

# Recorded and reported, but nothing selects on them directly. The stored price
# literals sit here rather than under routing on purpose: an unattributable
# literal is carried for reference and is invisible to routing, so two entries
# may disagree about it while dispatching identically.
_ATTRIBUTION_READS: tuple[tuple[str, Callable[[AgentSpec], Any]], ...] = (
    ("pricing_provenance", lambda s: s.pricing_provenance),
    ("cost_rank_basis", lambda s: s.routing.cost_rank_basis),
    ("input_cost_per_mtok", lambda s: s.input_cost_per_mtok),
    ("output_cost_per_mtok", lambda s: s.output_cost_per_mtok),
)


@dataclass(frozen=True)
class FieldDifference:
    """One field on which the two definitions of an identity disagree.

    Values are held as ``repr`` strings so the record stays JSON-able and
    digest-stable — it is carried on :class:`~theforge.config.types.ForgeConfig`
    and therefore participates in the resolved-configuration digest.
    """

    field: str
    project: str
    builtin: str

    def describe(self) -> str:
        return f"{self.field}: forge.yaml={self.project} builtin={self.builtin}"


@dataclass(frozen=True)
class DuplicateDeclaration:
    """How a project declaration compares to the shipped entry it duplicates."""

    canonical_id: str
    routing_differences: tuple[FieldDifference, ...] = ()
    attribution_differences: tuple[FieldDifference, ...] = ()

    @property
    def routing_differs(self) -> bool:
        """True when removing or keeping the declaration changes dispatch."""
        return bool(self.routing_differences)

    @property
    def is_redundant(self) -> bool:
        """True when the declaration restates the shipped entry exactly.

        Redundant in the strong sense: nothing about the resolved entry —
        including what its figures are attributed to — depends on which half of
        the configuration defined it, so removing it is a no-op.
        """
        return not self.routing_differences and not self.attribution_differences

    def describe(self) -> tuple[str, ...]:
        """Return one human-readable line per difference, routing first."""
        return tuple(
            difference.describe()
            for difference in (*self.routing_differences, *self.attribution_differences)
        )


def compare_duplicate_declaration(
    canonical_id: str,
    project: AgentSpec,
    builtin: AgentSpec,
) -> DuplicateDeclaration:
    """Compare a project-declared entry against the shipped entry of one identity.

    Answers the two questions an operator holding a duplicate declaration has:
    would routing behave differently without it, and where did the resolved
    entry's figures come from.
    """
    routing: list[FieldDifference] = []
    attribution: list[FieldDifference] = []
    for bucket, reads in ((routing, _ROUTING_READS), (attribution, _ATTRIBUTION_READS)):
        for field_name, read in reads:
            project_value, builtin_value = read(project), read(builtin)
            if project_value != builtin_value:
                bucket.append(
                    FieldDifference(
                        field=field_name,
                        project=repr(project_value),
                        builtin=repr(builtin_value),
                    )
                )
    return DuplicateDeclaration(
        canonical_id=canonical_id,
        routing_differences=tuple(routing),
        attribution_differences=tuple(attribution),
    )
