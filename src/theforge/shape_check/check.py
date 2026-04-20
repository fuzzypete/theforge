"""Top-level ``check`` entry point and shape→action mapping."""

from __future__ import annotations

from collections.abc import Iterable

from theforge.shape_check._mapping import map_shape
from theforge.shape_check.classifier import LlmCaller, classify
from theforge.shape_check.heuristics import (
    DEFAULT_CLUSTER_THRESHOLD,
    SEED_VOCABULARY,
    check_epic_or_tracking,
    check_implementation_design_dump,
    check_missing_acceptance_criteria,
    check_no_observable_done_state,
    check_superseded,
    check_too_many_behavioral_clusters,
    check_untriaged_finding,
)
from theforge.shape_check.types import Reason, ShapeResult

DEFAULT_CLASSIFIER = "heuristic"

__all__ = [
    "DEFAULT_CLASSIFIER",
    "DEFAULT_CLUSTER_THRESHOLD",
    "SEED_VOCABULARY",
    "check",
]


def check(
    title: str,
    body: str,
    labels: Iterable[str],
    *,
    classifier_mode: str = DEFAULT_CLASSIFIER,
    cluster_threshold: int = DEFAULT_CLUSTER_THRESHOLD,
    cluster_vocabulary: Iterable[str] | None = None,
    llm_caller: LlmCaller | None = None,
) -> ShapeResult:
    """Return a ShapeResult describing whether this issue is safe to sprint.

    Pure function — no credentials, no filesystem, no network access unless
    the caller supplies ``llm_caller`` and selects ``classifier_mode='llm'``.
    """
    labels = list(labels)
    reasons: list[Reason] = []

    for fn in (
        check_epic_or_tracking,
        check_superseded,
        check_untriaged_finding,
        check_missing_acceptance_criteria,
        check_no_observable_done_state,
        check_implementation_design_dump,
    ):
        r = fn(title, body, labels)
        if r is not None:
            reasons.append(r)

    cluster_reason = check_too_many_behavioral_clusters(
        title,
        body,
        labels,
        threshold=cluster_threshold,
        vocabulary=cluster_vocabulary,
    )
    if cluster_reason is not None:
        reasons.append(cluster_reason)

    # Dedupe by code while preserving first occurrence.
    seen: set[str] = set()
    deduped: list[Reason] = []
    for r in reasons:
        if r.code in seen:
            continue
        seen.add(r.code)
        deduped.append(r)

    reasons_t = tuple(deduped)
    shape, action = map_shape(reasons_t)
    heuristic_result = ShapeResult(shape=shape, reasons=reasons_t, suggested_action=action)

    return classify(classifier_mode, body, heuristic_result, llm_caller=llm_caller)
