"""Shape/action priority mapping. Shared between check() and classifier."""

from __future__ import annotations

from theforge.shape_check.types import Reason, Severity, Shape, SuggestedAction


def map_shape(reasons: tuple[Reason, ...]) -> tuple[Shape, SuggestedAction]:
    codes = {r.code for r in reasons}
    if "superseded" in codes:
        return Shape.SUPERSEDED, SuggestedAction.CLOSE
    if "epic_or_tracking" in codes:
        return Shape.TRACKING_ONLY, SuggestedAction.REMOVE_FROM_SPRINT
    if "untriaged_finding" in codes:
        return Shape.NEEDS_GROOMING, SuggestedAction.CLARIFY
    if "too_many_behavioral_clusters" in codes:
        return Shape.NEEDS_GROOMING, SuggestedAction.SPLIT
    blocking = [r for r in reasons if r.severity is Severity.BLOCKING]
    if blocking:
        return Shape.NEEDS_GROOMING, SuggestedAction.CLARIFY
    return Shape.RUNNABLE, SuggestedAction.PROCEED
