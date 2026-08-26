"""Shape check — standalone primitive for validating GitHub issue drafts.

Pure function API: ``check(title, body, labels) -> ShapeResult``.

The rules ``check`` enforces are stated once, as data, in
:mod:`theforge.shape_check.issue_spec`; :mod:`theforge.shape_check.document`
holds the parse and render halves of that same contract, so a body can be read
into a typed document and written back canonically without losing what the
specification does not model.

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
from theforge.shape_check.document import (
    DocumentSection,
    IssueDocument,
    parse_issue_document,
    render_issue_document,
)
from theforge.shape_check.issue_spec import (
    ISSUE_TYPES,
    RECOGNIZED_TYPE_LABELS,
    IssueTypeSpec,
    Presence,
    SectionSpec,
    spec_for_labels,
)
from theforge.shape_check.skip_taxonomy import (
    DEFAULT_STUCK_ISSUE_THRESHOLD,
    FourQuestionAxis,
    RemediationOutcome,
    SkipCategory,
    SkipClassification,
    SkipSeverity,
    classify_skip,
    group_by_category,
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
    "DocumentSection",
    "ISSUE_TYPES",
    "IssueDocument",
    "IssueTypeSpec",
    "Presence",
    "RECOGNIZED_TYPE_LABELS",
    "SectionSpec",
    "parse_issue_document",
    "render_issue_document",
    "spec_for_labels",
    "DEFAULT_CLUSTER_THRESHOLD",
    "DEFAULT_STUCK_ISSUE_THRESHOLD",
    "FourQuestionAxis",
    "Reason",
    "RemediationOutcome",
    "SEED_VOCABULARY",
    "Severity",
    "Shape",
    "ShapeResult",
    "ShapeVerdict",
    "SkipCategory",
    "SkipClassification",
    "SkipSeverity",
    "SuggestedAction",
    "VERDICT_DESCRIPTIONS",
    "check",
    "classify_skip",
    "group_by_category",
]
