"""Tests for per-AC verification table on review output.

Covers the silent-contract-swap guard introduced for #1224:
- Reviewers must emit an ac_verification table mapping each acceptance
  criterion to a status (VERIFIED / PARTIAL / NOT_VERIFIED) with concrete
  evidence pointers.
- APPROVE + any non-VERIFIED entry is a structural contradiction —
  validate_review_yaml records a parse error so the parse-retry loop fires
  and the reviewer's APPROVE is not silently accepted.
- The audit output emits the per-AC verification table so post-hoc readers
  can see which criteria were verified, by which evidence, at the time of
  approval.
- Bug-type issues (no ACs) record a single "Symptom resolution" entry.
- Merging across reviewers downgrades to the worst per-criterion status.
"""

from __future__ import annotations

from theforge.coordinator.audit_render import build_reviews
from theforge.coordinator.state import CoordinatorState, ReviewCycleMetadata
from theforge.review import (
    ACVerification,
    ReviewFinding,
    ReviewResult,
    _merge_ac_verification,
    parse_review_output,
)
from theforge.schemas import validate_review_yaml

# ── Schema cross-validation ──────────────────────────────────────────


def _approve_with_ac(entries: list[dict]) -> dict:
    return {
        "verdict": "APPROVE",
        "summary": "ok",
        "findings": [],
        "story_compliance": {"matches_spec": True, "mismatches": []},
        "test_coverage": {"adequate": True, "gaps": []},
        "ac_verification": entries,
    }


def test_approve_with_all_verified_passes() -> None:
    data = _approve_with_ac(
        [
            {
                "criterion": "Detection covers Codex 'usage limit' signature",
                "status": "VERIFIED",
                "evidence": (
                    "src/theforge/runners/cli.py:147-180 + "
                    "tests/test_cli.py::test_codex_usage_limit_falls_back"
                ),
            },
            {
                "criterion": "Resumed-retry path exercises fallback",
                "status": "VERIFIED",
                "evidence": (
                    "src/theforge/runners/cli.py:220-260 + "
                    "tests/test_cli.py::test_resumed_retry_fallback"
                ),
            },
        ]
    )
    assert validate_review_yaml(data) == []


def test_approve_with_partial_is_structural_error() -> None:
    """The exact failure mode this story was filed for.

    PR #402 review-style output: APPROVE with a generic implementation
    claim but no test for the production-observed signature. Schema
    validation must reject this so the parse-retry loop fires.
    """
    data = _approve_with_ac(
        [
            {
                "criterion": "Detection covers Codex 'usage limit' signature",
                "status": "PARTIAL",
                "evidence": (
                    "Generic CLI fallback exists at src/theforge/runners/cli.py:147 "
                    "but no test exercises the 'usage limit' string"
                ),
            },
        ]
    )
    errors = validate_review_yaml(data)
    assert any("APPROVE" in e and "PARTIAL" in e for e in errors)


def test_approve_with_not_verified_is_structural_error() -> None:
    data = _approve_with_ac(
        [
            {
                "criterion": "Resumed-retry path exercises fallback",
                "status": "NOT_VERIFIED",
                "evidence": "Could not locate runtime path that handles resumed retries",
            },
        ]
    )
    errors = validate_review_yaml(data)
    assert any("APPROVE" in e and "NOT_VERIFIED" in e for e in errors)


def test_request_changes_with_partial_ac_is_valid() -> None:
    data = {
        "verdict": "REQUEST_CHANGES",
        "summary": "Symptom not exercised",
        "findings": [
            {
                "severity": "P1",
                "file": "tests/test_cli.py",
                "line": 1,
                "description": (
                    "Missing test that exercises 'usage limit' string — "
                    "production failure mode is not covered"
                ),
                "suggestion": "Add a test fixture replaying the Codex usage limit error",
            },
        ],
        "story_compliance": {"matches_spec": False, "mismatches": ["test gap"]},
        "test_coverage": {"adequate": False, "gaps": ["usage limit not tested"]},
        "ac_verification": [
            {
                "criterion": "Detection covers Codex 'usage limit' signature",
                "status": "PARTIAL",
                "evidence": "Implementation present; concrete signature untested",
            },
        ],
    }
    assert validate_review_yaml(data) == []


def test_bug_symptom_resolution_entry_passes() -> None:
    """Bug issues use a single Symptom resolution entry instead of per-AC."""
    data = _approve_with_ac(
        [
            {
                "criterion": (
                    "Symptom resolution: Codex 'You've hit your usage limit' "
                    "now routes to API fallback"
                ),
                "status": "VERIFIED",
                "evidence": (
                    "src/theforge/runners/cli.py:147 (added 'usage limit' pattern) + "
                    "tests/test_cli.py::test_codex_usage_limit_falls_back"
                ),
            }
        ]
    )
    assert validate_review_yaml(data) == []


# ── Parser extraction ────────────────────────────────────────────────


def test_parse_review_output_extracts_ac_verification() -> None:
    output = """\
```yaml
verdict: APPROVE
summary: Good
findings: []
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
ac_verification:
  - criterion: It works
    status: VERIFIED
    evidence: src/foo.py:1 + tests/test_foo.py
```
"""
    result = parse_review_output(output)
    assert result.parse_errors == []
    assert len(result.ac_verification) == 1
    assert result.ac_verification[0].criterion == "It works"
    assert result.ac_verification[0].status == "VERIFIED"


# ── Merge across reviewers ───────────────────────────────────────────


def test_merge_ac_verification_takes_worst_status() -> None:
    """If any reviewer reports PARTIAL, the merged status is PARTIAL.

    Conservative rule: a single reviewer flagging a gap is enough to
    mark the criterion non-VERIFIED in the merged result.
    """
    a = (
        ACVerification(
            criterion="Detection covers Codex usage limit",
            status="VERIFIED",
            evidence="src/cli.py:147 + tests/test_cli.py::test_a",
        ),
    )
    b = (
        ACVerification(
            criterion="Detection covers Codex usage limit",
            status="PARTIAL",
            evidence="No test for the actual signature",
        ),
    )
    merged = _merge_ac_verification([a, b])
    assert len(merged) == 1
    assert merged[0].status == "PARTIAL"


def test_merge_ac_verification_preserves_unique_criteria() -> None:
    a = (ACVerification(criterion="Crit one", status="VERIFIED", evidence="e1"),)
    b = (ACVerification(criterion="Crit two", status="VERIFIED", evidence="e2"),)
    merged = _merge_ac_verification([a, b])
    assert {ac.criterion for ac in merged} == {"Crit one", "Crit two"}


# ── Audit emission ───────────────────────────────────────────────────


def test_build_reviews_emits_ac_verification_table() -> None:
    state = CoordinatorState()
    state.review_cycle_metadata.append(
        ReviewCycleMetadata(
            pool_models=["opus"],
            successful=["opus"],
            failed=[],
            synthesized=False,
        )
    )
    state.review_results.append(
        ReviewResult(
            verdict="APPROVE",
            summary="all good",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
            ac_verification=(
                ACVerification(
                    criterion="Detection covers Codex usage limit",
                    status="VERIFIED",
                    evidence="src/cli.py:147 + tests/test_cli.py::test_usage_limit",
                ),
            ),
        )
    )
    reviews = build_reviews(state)
    assert reviews[0]["ac_verification"] == [
        {
            "criterion": "Detection covers Codex usage limit",
            "status": "VERIFIED",
            "evidence": "src/cli.py:147 + tests/test_cli.py::test_usage_limit",
        }
    ]


def test_build_reviews_emits_empty_ac_verification_when_absent() -> None:
    """REQUEST_CHANGES results may carry no AC table — audit emits []."""
    state = CoordinatorState()
    state.review_cycle_metadata.append(
        ReviewCycleMetadata(
            pool_models=["opus"],
            successful=["opus"],
            failed=[],
            synthesized=False,
        )
    )
    state.review_results.append(
        ReviewResult(
            verdict="REQUEST_CHANGES",
            summary="bug",
            findings=[
                ReviewFinding(
                    severity="P1",
                    file="src/foo.py",
                    line=10,
                    description="bug",
                    suggestion="fix",
                )
            ],
            story_matches=False,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
    )
    reviews = build_reviews(state)
    assert reviews[0]["ac_verification"] == []
