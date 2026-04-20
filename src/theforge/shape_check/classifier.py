"""Classifier-mode gate. Fail-open to heuristic result on any error."""

from __future__ import annotations

from collections.abc import Callable

from theforge.shape_check.types import Reason, ShapeResult

FUZZY_CODES: frozenset[str] = frozenset(
    {"too_many_behavioral_clusters", "no_observable_done_state"}
)

LlmCaller = Callable[[str, tuple[Reason, ...]], ShapeResult]


def _strip_fuzzy(result: ShapeResult) -> ShapeResult:
    kept = tuple(r for r in result.reasons if r.code not in FUZZY_CODES)
    if len(kept) == len(result.reasons):
        return result
    return ShapeResult(
        shape=result.shape,
        reasons=kept,
        suggested_action=result.suggested_action,
    )


def classify(
    mode: str,
    body: str,
    heuristic_result: ShapeResult,
    llm_caller: LlmCaller | None = None,
) -> ShapeResult:
    if mode == "off":
        return _strip_fuzzy(heuristic_result)
    if mode == "heuristic":
        return heuristic_result
    if mode == "llm":
        if llm_caller is None:
            return heuristic_result
        fuzzy = tuple(r for r in heuristic_result.reasons if r.code in FUZZY_CODES)
        try:
            refined = llm_caller(body, fuzzy)
        except Exception:
            return heuristic_result
        if not isinstance(refined, ShapeResult):
            return heuristic_result
        return refined
    return heuristic_result
