"""Pure-data result types for shape_check. Stdlib only."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Shape(str, Enum):
    RUNNABLE = "runnable"
    NEEDS_GROOMING = "needs_grooming"
    TRACKING_ONLY = "tracking_only"
    SUPERSEDED = "superseded"


class Severity(str, Enum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


class SuggestedAction(str, Enum):
    SPLIT = "split"
    CLARIFY = "clarify"
    REMOVE_FROM_SPRINT = "remove_from_sprint"
    CLOSE = "close"
    PROCEED = "proceed"


@dataclass(frozen=True)
class Reason:
    code: str
    severity: Severity
    detail: str


@dataclass(frozen=True)
class ShapeResult:
    shape: Shape
    reasons: tuple[Reason, ...] = field(default_factory=tuple)
    suggested_action: SuggestedAction = SuggestedAction.PROCEED
