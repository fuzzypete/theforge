"""Classifier-mode gate. Fail-open to heuristic result on any error.

LLM mode is strictly limited to refining fuzzy reasons; deterministic
non-fuzzy reasons from the heuristic result are always preserved so that
an injected classifier cannot drop blocking reasons like
``missing_acceptance_criteria`` or ``untriaged_finding``.
"""

from __future__ import annotations

from collections.abc import Callable

from theforge.shape_check._mapping import map_shape
from theforge.shape_check.types import Reason, ShapeResult
from theforge.shape_check.verdict import derive_verdict

FUZZY_CODES: frozenset[str] = frozenset(
    {"too_many_behavioral_clusters", "no_observable_done_state"}
)

LlmCaller = Callable[[str, tuple[Reason, ...]], ShapeResult]


def _rebuild(reasons: tuple[Reason, ...]) -> ShapeResult:
    shape, action = map_shape(reasons)
    return ShapeResult(
        shape=shape,
        reasons=reasons,
        suggested_action=action,
        verdict=derive_verdict(reasons),
    )


def _strip_fuzzy(result: ShapeResult) -> ShapeResult:
    kept = tuple(r for r in result.reasons if r.code not in FUZZY_CODES)
    if len(kept) == len(result.reasons):
        return result
    return _rebuild(kept)


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
        # The LLM is only allowed to refine FUZZY_CODES. Non-fuzzy heuristic
        # reasons are preserved verbatim; from the refined result we keep only
        # reasons whose code is in FUZZY_CODES. Shape/action are recomputed.
        non_fuzzy = tuple(r for r in heuristic_result.reasons if r.code not in FUZZY_CODES)
        refined_fuzzy = tuple(r for r in refined.reasons if r.code in FUZZY_CODES)
        # Dedupe by code, preserving first occurrence (non-fuzzy first).
        seen: set[str] = set()
        merged: list[Reason] = []
        for r in (*non_fuzzy, *refined_fuzzy):
            if r.code in seen:
                continue
            seen.add(r.code)
            merged.append(r)
        return _rebuild(tuple(merged))
    return heuristic_result
