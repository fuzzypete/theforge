"""Tests for issue #1859: evidence-backed post-run knowledge summaries (Layer 2).

The load-bearing property is provenance, not shape. A learned claim is only
admissible if it cites something the run actually produced, so these tests pin
both halves of that: an unevidenced claim is rejected, and an evidenced claim
whose citation does not *resolve* against the audit record is rejected too — a
well-formed ``finding_id: f-999`` that no cycle ever raised is exactly the
hallucinated institutional memory this layer exists to keep out.

The remaining tests cover the rest of the AC surface: the indexable fields come
from the audit and never from the agent, and the artifact is keyed by run_id
with a backlink to the authoritative run record.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.knowledge_summary import (
    SummaryValidationError,
    build_summary_artifact,
    extract_anchors,
    parse_summary_output,
    summary_exists,
    summary_path,
    validate_proposed_summary,
    write_summary,
)

RUN_ID = "run-abc123"


def _audit() -> dict:
    """A representative terminal audit record for a completed story."""
    return {
        "run_id": RUN_ID,
        "task": {"name": "Retry the client", "slug": "retry-client", "github_issue": 42},
        "timing": {"finished_at": "2026-08-15T12:00:00+00:00"},
        "preflight": {
            "work_type": "feature",
            "complexity": "medium",
            "complexity_score": 5,
            "contract_change": False,
            "domains": ["backend", "testing"],
        },
        "iterations": {"dev_iterations_productive": 2, "review_cycles_total": 3},
        "cost": {"total_usd": 4.25},
        "plan_review": {"regenerated": True},
        "changed_files": {
            "base_ref": "aaa111",
            "head_ref": "bbb222",
            "files": [
                {"path": "src/client.py", "insertions": 40, "deletions": 3},
                {"path": "tests/test_client.py", "insertions": 60, "deletions": 0},
            ],
        },
        "reviews": [
            {"cycle": 1, "verdict": "REQUEST_CHANGES", "summary": "missing read timeout"},
            {"cycle": 2, "verdict": "REQUEST_CHANGES", "summary": "still flaky"},
            {"cycle": 3, "verdict": "APPROVE", "summary": "good"},
        ],
        "finding_registry": [
            {
                "finding_id": "f-003",
                "cycle_first_seen": 1,
                "cycle_last_seen": 2,
                "file": "src/client.py",
                "severity": "P1",
                "description": "missing read timeout on retry path",
                "disposition": "resolved",
            },
            {
                "finding_id": "f-007",
                "cycle_first_seen": 2,
                "cycle_last_seen": 2,
                "file": "src/client.py",
                "severity": "P2",
                "description": "missing type annotations",
                "disposition": "open",
            },
        ],
        "phases": {
            "plan": {
                "plan_structured": {
                    "approach": "wrap call sites",
                    "steps": [
                        {"id": 1, "description": "add retry decorator"},
                        {"id": 2, "description": "add configurable timeout pair"},
                    ],
                },
                "attempt_plans": [],
            }
        },
    }


def _proposed(evidence: list[dict] | None = None) -> dict:
    return {
        "run_id": RUN_ID,
        "what_changed": {
            "description": "Added retry logic with exponential backoff.",
            "approach": "Wrapped call sites in a retry decorator.",
        },
        "what_was_learned": [
            {
                "claim": "Timeout handling needs both a connect and a read timeout.",
                "evidence": (
                    evidence
                    if evidence is not None
                    else [
                        {
                            "type": "review_finding",
                            "finding_id": "f-003",
                            "description": "P1: missing read timeout",
                        }
                    ]
                ),
            }
        ],
        "learned_patterns": ["retry-decorator"],
        "review_insights": ["reviewers kept returning to the retry path"],
        "complexity_signal": {"dominant_difficulty": "edge case coverage"},
    }


class TestAnchorExtraction:
    """Only what the run actually produced is citable."""

    def test_anchors_come_from_every_reference_family(self) -> None:
        anchors = extract_anchors(_audit())

        assert anchors.finding_ids == frozenset({"f-003", "f-007"})
        assert anchors.plan_step_ids == frozenset({"1", "2"})
        assert anchors.review_cycles == frozenset({"1", "2", "3"})
        assert anchors.file_paths == frozenset({"src/client.py", "tests/test_client.py"})
        assert anchors.diff_refs == frozenset({"aaa111", "bbb222"})
        assert not anchors.is_empty()

    def test_a_run_with_nothing_citable_is_empty(self) -> None:
        assert extract_anchors({"run_id": RUN_ID}).is_empty()


class TestParsing:
    def test_rooted_block_is_extracted_from_surrounding_prose(self) -> None:
        text = (
            "Here is the summary you asked for.\n\n"
            "run_summary:\n"
            "  run_id: run-abc123\n"
            "  what_changed:\n"
            "    description: did a thing\n"
            "\nHope that helps!\n"
        )

        parsed = parse_summary_output(text)

        assert parsed["run_id"] == RUN_ID
        assert parsed["what_changed"]["description"] == "did a thing"

    @pytest.mark.parametrize(
        "text",
        ["", "I could not summarise this run.", "run_summary:\n  - not: a mapping\n"],
    )
    def test_output_without_a_usable_block_is_rejected(self, text: str) -> None:
        with pytest.raises(SummaryValidationError):
            parse_summary_output(text)


class TestEvidenceValidation:
    """AC: every learned claim carries at least one concrete run reference."""

    def test_a_resolvable_citation_is_accepted(self) -> None:
        anchors = extract_anchors(_audit())

        validated = validate_proposed_summary(_proposed(), run_id=RUN_ID, anchors=anchors)

        assert validated.learned[0].evidence == [
            {
                "type": "review_finding",
                "finding_id": "f-003",
                "description": "P1: missing read timeout",
            }
        ]

    @pytest.mark.parametrize("evidence", [[], None])
    def test_a_claim_with_no_evidence_is_rejected(self, evidence: list | None) -> None:
        anchors = extract_anchors(_audit())
        proposed = _proposed(evidence=[])
        if evidence is None:
            del proposed["what_was_learned"][0]["evidence"]

        with pytest.raises(SummaryValidationError, match="carries no evidence"):
            validate_proposed_summary(proposed, run_id=RUN_ID, anchors=anchors)

    @pytest.mark.parametrize(
        "evidence",
        [
            {"type": "review_finding", "finding_id": "f-999", "description": "invented"},
            {"type": "plan_step", "step_id": 9, "description": "invented"},
            {"type": "review_cycle", "cycle": 12, "description": "invented"},
            {"type": "file", "path": "src/not_touched.py", "description": "invented"},
            {"type": "diff", "ref": "cafe99", "description": "invented"},
        ],
    )
    def test_a_nonempty_but_unresolvable_citation_is_rejected(self, evidence: dict) -> None:
        """Anchor-shaped is not evidence: the reference must exist in the run."""
        anchors = extract_anchors(_audit())

        with pytest.raises(SummaryValidationError, match="does not exist in this run"):
            validate_proposed_summary(
                _proposed(evidence=[evidence]), run_id=RUN_ID, anchors=anchors
            )

    def test_an_unknown_evidence_type_is_rejected(self) -> None:
        anchors = extract_anchors(_audit())

        with pytest.raises(SummaryValidationError, match="unknown type"):
            validate_proposed_summary(
                _proposed(evidence=[{"type": "vibes", "reference": "f-003"}]),
                run_id=RUN_ID,
                anchors=anchors,
            )

    def test_a_summary_for_a_different_run_is_rejected(self) -> None:
        anchors = extract_anchors(_audit())
        proposed = _proposed()
        proposed["run_id"] = "run-somebody-else"

        with pytest.raises(SummaryValidationError, match="does not match"):
            validate_proposed_summary(proposed, run_id=RUN_ID, anchors=anchors)

    def test_every_reference_family_resolves_when_real(self) -> None:
        anchors = extract_anchors(_audit())

        validated = validate_proposed_summary(
            _proposed(
                evidence=[
                    {"type": "review_finding", "finding_id": "f-003"},
                    {"type": "plan_step", "step_id": 2},
                    {"type": "review_cycle", "cycle": 3},
                    {"type": "file", "path": "src/client.py"},
                    {"type": "diff", "ref": "bbb222"},
                ]
            ),
            run_id=RUN_ID,
            anchors=anchors,
        )

        assert len(validated.learned[0].evidence) == 5


class TestArtifactComposition:
    """AC: enough structured fields to index by, and all of them audit-derived."""

    def test_indexable_fields_are_read_from_the_audit(self) -> None:
        audit = _audit()
        validated = validate_proposed_summary(
            _proposed(), run_id=RUN_ID, anchors=extract_anchors(audit)
        )

        artifact = build_summary_artifact(validated, audit, generation={"cost_usd": 0.12})

        assert artifact["run_id"] == RUN_ID
        assert artifact["authoritative_run_record"] == f".forge/audits/runs/{RUN_ID}.json"
        assert artifact["changed_files"] == ["src/client.py", "tests/test_client.py"]
        assert artifact["domains"] == ["backend", "testing"]
        assert artifact["story_shape"]["work_type"] == "feature"
        assert artifact["learned_patterns"] == ["retry-decorator"]
        assert artifact["complexity_signal"] == {
            "actual_iterations": 2,
            "review_cycles": 3,
            "plan_regenerations": True,
            "cost_usd": 4.25,
            "dominant_difficulty": "edge case coverage",
        }
        assert artifact["generation"]["cost_usd"] == 0.12

    def test_review_insights_are_derived_from_the_finding_registry(self) -> None:
        audit = _audit()
        validated = validate_proposed_summary(
            _proposed(), run_id=RUN_ID, anchors=extract_anchors(audit)
        )

        insights = build_summary_artifact(validated, audit)["review_insights"]

        assert [f["finding_id"] for f in insights["recurring_findings"]] == ["f-003"]
        assert insights["recurring_findings"][0]["cycles_seen"] == 2
        assert [f["finding_id"] for f in insights["resolved_findings"]] == ["f-003"]
        assert insights["observations"] == ["reviewers kept returning to the retry path"]

    def test_an_agent_supplied_file_list_cannot_reach_the_artifact(self) -> None:
        """The agent writes prose; the coordinator writes every countable fact."""
        audit = _audit()
        proposed = _proposed()
        proposed["what_changed"]["files_modified"] = ["src/invented.py"]
        validated = validate_proposed_summary(
            proposed, run_id=RUN_ID, anchors=extract_anchors(audit)
        )

        artifact = build_summary_artifact(validated, audit)

        assert artifact["what_changed"]["files_modified"] == [
            "src/client.py",
            "tests/test_client.py",
        ]


class TestPersistence:
    def test_summary_is_written_atomically_keyed_by_run_id(self, tmp_path: Path) -> None:
        assert not summary_exists(tmp_path, RUN_ID)

        path = write_summary(tmp_path, RUN_ID, {"run_id": RUN_ID, "what_changed": {}})

        assert path == tmp_path / ".forge" / "knowledge" / "summaries" / f"{RUN_ID}.yaml"
        assert summary_exists(tmp_path, RUN_ID)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["run_id"] == RUN_ID
        assert not list(path.parent.glob("*.tmp"))

    def test_summary_path_is_stable(self, tmp_path: Path) -> None:
        assert summary_path(tmp_path, RUN_ID).name == f"{RUN_ID}.yaml"
