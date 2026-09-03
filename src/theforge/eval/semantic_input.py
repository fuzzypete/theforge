"""Semantic evaluation input construction and digesting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from theforge.shape_check.issue_spec import spec_for_labels


@dataclass(frozen=True)
class SemanticEvaluationInput:
    """Exact serialized input handed to the semantic evaluator."""

    title: str
    body: str
    canonical_type: str | None
    serialized_json: str
    input_digest: str


def canonical_type_for_labels(labels: tuple[str, ...] | list[str]) -> str | None:
    spec = spec_for_labels(labels)
    return None if spec is None else spec.label


def serialize_semantic_input(*, title: str, body: str, canonical_type: str | None) -> str:
    payload = {
        "body": body,
        "canonical_type": canonical_type,
        "title": title,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_semantic_evaluation_input(
    *,
    title: str,
    body: str,
    labels: tuple[str, ...] | list[str],
) -> SemanticEvaluationInput:
    canonical_type = canonical_type_for_labels(labels)
    serialized_json = serialize_semantic_input(
        title=title,
        body=body,
        canonical_type=canonical_type,
    )
    input_digest = hashlib.sha256(serialized_json.encode("utf-8")).hexdigest()
    return SemanticEvaluationInput(
        title=title,
        body=body,
        canonical_type=canonical_type,
        serialized_json=serialized_json,
        input_digest=input_digest,
    )
