"""Tests for the report body, manifest, and evidence payload rendering."""

from __future__ import annotations

from theforge.reporting.evidence import EvidenceArtifact, MissingEvidence, RunEvidence
from theforge.reporting.render import (
    Diagnosis,
    Publication,
    build_evidence_chunks,
    default_title,
    dropped_as_missing,
    render_issue_body,
)
from theforge.shape_check.check import check
from theforge.shape_check.types import ShapeVerdict


def _evidence(
    *,
    artifacts: tuple[EvidenceArtifact, ...] = (),
    missing: tuple[MissingEvidence, ...] = (),
    config_summary: str = "resolved snapshot attached (12 recorded keys)",
) -> RunEvidence:
    return RunEvidence(
        run_id="f5aa21cf2d8d",
        run_kind="story",
        forge_version="0.14.2",
        observed_project="fuzzypete/hdp",
        sprint_name="issues-320,324,331",
        sprint_id="5ff0",
        story_slugs=("issue-320",),
        story_run_ids=("f5aa21cf2d8d",),
        config_summary=config_summary,
        artifacts=artifacts,
        missing=missing,
    )


def _artifact(kind: str, name: str, content: str = "body") -> EvidenceArtifact:
    return EvidenceArtifact(kind=kind, name=name, content=content, path=f".forge/{name}")


def _body(evidence: RunEvidence, publication: Publication) -> str:
    return render_issue_body(
        evidence,
        description="Sprint resume reported a story merged when no commit landed.",
        diagnosis=Diagnosis(symptom="resume false-skips zero-delta APPROVE stories"),
        publication=publication,
    )


def test_body_carries_the_recorded_runtime_facts_in_the_manifest():
    evidence = _evidence(artifacts=(_artifact("run_log", "logs/run.log"),))
    chunks, _ = build_evidence_chunks(evidence)

    body = _body(evidence, Publication(expected=tuple(c.label for c in chunks)))

    assert "forge version : 0.14.2" in body
    assert "observed in   : fuzzypete/hdp" in body
    assert "f5aa21cf2d8d" in body
    assert "config        : resolved snapshot attached (12 recorded keys)" in body
    assert "artifacts     : run log" in body


def test_missing_evidence_is_named_and_the_report_never_reads_complete():
    evidence = _evidence(
        artifacts=(_artifact("run_log", "logs/run.log"),),
        missing=(
            MissingEvidence(
                kind="intake_candidates",
                name="issue-320",
                reason="pruned before capture",
            ),
        ),
    )
    chunks, _ = build_evidence_chunks(evidence)

    body = _body(evidence, Publication(expected=tuple(c.label for c in chunks)))

    assert "missing       : intake candidate artifacts" in body
    assert "### Missing evidence" in body
    assert "pruned before capture" in body


def test_missing_artifact_produces_no_payload_section():
    evidence = _evidence(
        missing=(MissingEvidence(kind="run_log", reason="pruned before capture"),)
    )
    chunks, dropped = build_evidence_chunks(evidence)

    assert chunks == ()
    assert dropped == ()
    body = _body(evidence, Publication())
    assert "no payload" in body
    assert "Evidence — run log" not in body


def test_publication_state_starts_incomplete_and_only_completes_when_all_posted():
    evidence = _evidence(artifacts=(_artifact("run_log", "logs/run.log"),))
    chunks, _ = build_evidence_chunks(evidence)
    labels = tuple(c.label for c in chunks)

    pending = _body(evidence, Publication(expected=labels))
    assert "INCOMPLETE — 0 of 1" in pending
    assert "pending" in pending

    done = _body(evidence, Publication(expected=labels, posted=labels, started=True))
    assert "complete — 1 of 1 evidence comments attached" in done
    assert "attached" in done


def test_partial_publication_names_the_comments_that_never_landed():
    evidence = _evidence(
        artifacts=(
            _artifact("run_log", "logs/run.log"),
            _artifact("story_audit", "logs/audit.yaml"),
        )
    )
    chunks, _ = build_evidence_chunks(evidence)
    labels = tuple(c.label for c in chunks)

    body = _body(
        evidence,
        Publication(expected=labels, posted=(labels[0],), started=True),
    )

    assert "INCOMPLETE — 1 of 2" in body
    assert labels[1] in body
    assert "NOT ATTACHED" in body


def test_large_artifact_is_split_into_comment_sized_chunks():
    evidence = _evidence(artifacts=(_artifact("run_log", "logs/run.log", "y" * 250),))

    chunks, dropped = build_evidence_chunks(evidence, chunk_chars=100)

    assert len(chunks) == 3
    assert dropped == ()
    assert "part 1 of 3" in chunks[0].label
    assert all(len(c.body) < 65_536 for c in chunks)


def test_content_with_backticks_gets_a_longer_fence():
    evidence = _evidence(artifacts=(_artifact("run_log", "logs/run.log", "a ``` b"),))

    chunks, _ = build_evidence_chunks(evidence)

    assert chunks[0].body.count("````") >= 2


def test_payload_budget_drops_are_reported_as_missing_not_silently_skipped():
    evidence = _evidence(
        artifacts=(
            _artifact("run_log", "logs/a.log", "a" * 300),
            _artifact("story_audit", "logs/b.yaml", "b" * 300),
        )
    )

    chunks, dropped = build_evidence_chunks(evidence, chunk_chars=100, max_chunks=3)

    assert len(chunks) == 3
    assert len(dropped) == 1
    entries = dropped_as_missing(dropped, 3)
    assert entries[0].kind == "story_audit"
    assert "budget of 3 comments" in entries[0].reason
    body = _body(evidence.with_missing(entries), Publication())
    assert "logs/b.yaml" in body


def test_rendered_body_lands_in_a_known_shape_gate_state():
    evidence = _evidence(artifacts=(_artifact("run_log", "logs/run.log"),))
    chunks, _ = build_evidence_chunks(evidence)
    body = _body(evidence, Publication(expected=tuple(c.label for c in chunks)))

    result = check(default_title(evidence, "resume false-skips"), body, ["bug"])

    assert isinstance(result.verdict, ShapeVerdict)
    assert result.verdict is ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN


def test_operator_supplied_cause_makes_the_body_runnable():
    evidence = _evidence(artifacts=(_artifact("run_log", "logs/run.log"),))
    body = render_issue_body(
        evidence,
        description="Sprint resume reported a story merged when no commit landed.",
        diagnosis=Diagnosis(
            symptom="resume false-skips zero-delta APPROVE stories",
            cause="`_is_already_merged` requires a commit ahead, so zero-delta APPROVE misreads.",
            code_path="`sprint.runner._is_already_merged`.",
            fix_criterion="resume identifies a zero-delta APPROVE story as already merged.",
        ),
        publication=Publication(),
    )

    assert check(default_title(evidence, "resume false-skips"), body, ["bug"]).verdict is (
        ShapeVerdict.RUNNABLE
    )


def test_default_title_names_where_and_which_run():
    evidence = _evidence()

    title = default_title(evidence, "resume false-skips zero-delta stories")

    assert "fuzzypete/hdp" in title
    assert "f5aa21cf2d8d" in title
