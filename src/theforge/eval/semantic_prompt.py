"""Prompt construction for audit-only semantic evaluation."""

from __future__ import annotations

from theforge.eval.semantic_input import SemanticEvaluationInput

PROMPT_CONTRACT_VERSION = "semantic-review.v1"


def build_semantic_review_prompt(
    evaluation_input: SemanticEvaluationInput,
    *,
    prompt_contract_version: str = PROMPT_CONTRACT_VERSION,
) -> str:
    return f"""You are performing an audit-only semantic review of a GitHub issue.

Read only the structured issue input below. Do not assume repository state, and
do not rely on any information outside the provided JSON.

Your job is to identify semantically material contract defects that a structural
shape check could miss. This review has no readiness authority.

Return exactly one structured JSON or YAML mapping with this schema:

outcome: FINDINGS | NO_FINDINGS
findings:
  - summary: short defect statement
    rationale: why this is a defect relative to the issue's stated intent
    evidence: optional quoted or paraphrased supporting text from the issue
    severity: low | medium | high

Rules:
- Use outcome: NO_FINDINGS only when you found no semantically material defects.
- Use outcome: FINDINGS only when findings is a non-empty list.
- Do not emit prose before or after the structured payload.
- EVALUATION_FAILED is reserved for the caller when your output cannot be parsed.

Prompt contract version: {prompt_contract_version}

Input JSON:
```json
{evaluation_input.serialized_json}
```"""
