"""Shape check — standalone primitive for validating GitHub issue drafts.

Pure function API: ``check(title, body, labels) -> ShapeResult``.

No dependency on ``forge.yaml``, coordinator state, provider adapters, or
any optional provider SDK. Stdlib-only imports so this subpackage can be
imported from a GitHub Action runtime that has none of TheForge's runtime
dependencies.
"""

from theforge.shape_check.check import (
    DEFAULT_CLASSIFIER,
    DEFAULT_CLUSTER_THRESHOLD,
    SEED_VOCABULARY,
    check,
)
from theforge.shape_check.types import (
    VERDICT_DESCRIPTIONS,
    Reason,
    Severity,
    Shape,
    ShapeResult,
    ShapeVerdict,
    SuggestedAction,
)

__all__ = [
    "DEFAULT_CLASSIFIER",
    "DEFAULT_CLUSTER_THRESHOLD",
    "Reason",
    "SEED_VOCABULARY",
    "Severity",
    "Shape",
    "ShapeResult",
    "ShapeVerdict",
    "SuggestedAction",
    "VERDICT_DESCRIPTIONS",
    "check",
]
