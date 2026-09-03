from __future__ import annotations

from theforge.intake.author_flow import (
    AuthoringStatus,
    AuthorPrompt,
    available_type_labels,
    run_author_flow,
)


def _answer_map(mapping: dict[str, str | None]):
    def answer(prompt: AuthorPrompt) -> str | None:
        return mapping.get(prompt.key)

    return answer


def test_complete_enhancement_body_passes_without_fixed_observable_verb() -> None:
    result = run_author_flow(
        title="",
        selected_type_label="enhancement",
        answer_source=_answer_map(
            {
                "title": "Expose the readiness summary in the dashboard header",
                "acceptance_criteria": (
                    "- A reviewer can see the readiness summary in the dashboard header.\n"
                    "- The summary stays visible after a page refresh."
                ),
            }
        ),
    )

    assert result.status is AuthoringStatus.RUNNABLE
    assert result.labels == ("enhancement",)
    assert "## Acceptance criteria" in result.body
    assert not any(reason.code == "no_observable_done_state" for reason in result.reasons)


def test_declined_required_part_returns_honest_todo_draft() -> None:
    result = run_author_flow(
        title="Document the new authoring path",
        selected_type_label="task",
        answer_source=_answer_map({"acceptance_criteria": None}),
    )

    assert result.status is AuthoringStatus.DRAFT
    assert "todo:draft" in result.labels
    assert result.missing_parts[0].label == "Acceptance criteria"
    assert result.body_for_storage().startswith("> Status: incomplete draft")


def test_existing_draft_round_trips_and_replaces_placeholder_acceptance_criteria() -> None:
    body = (
        "## Why\n\nAuthors keep discovering the rules one refusal at a time.\n\n"
        "## Acceptance criteria\n\nTODO: replace with real criteria.\n\n"
        "## Notes\n\nPreserve this prose.\n"
    )

    result = run_author_flow(
        title="Surface issue-body requirements before submission",
        selected_type_label="enhancement",
        existing_body=body,
        existing_labels=("enhancement", "todo:draft"),
        answer_source=_answer_map(
            {
                "acceptance_criteria": (
                    "- A reviewer can tell which body part is still missing before submission.\n"
                    "- The authoring path never asks the author to choose from a verb list."
                ),
            }
        ),
    )

    assert result.status is AuthoringStatus.RUNNABLE
    assert result.body.count("## Acceptance criteria") == 1
    assert "TODO: replace with real criteria." not in result.body
    assert "Authors keep discovering the rules one refusal at a time." in result.body
    assert "## Notes\n\nPreserve this prose.\n" in result.body


def test_partial_diagnosis_collects_only_missing_fields_and_preserves_existing_values() -> None:
    body = (
        "## Observed\n\nResume reports a story as merged when no commit landed.\n\n"
        "## Expected\n\nResume only marks a story merged when the merge already happened.\n\n"
        "## Diagnosis\n\n"
        "- **Observed symptom:** zero-delta APPROVE stories are falsely reported merged.\n"
        "- **Evidence:** run id `abc123` shows the false positive.\n"
    )

    result = run_author_flow(
        title="Resume falsely treats zero-delta APPROVE stories as merged",
        selected_type_label="bug",
        existing_body=body,
        existing_labels=("bug", "todo:draft"),
        answer_source=_answer_map(
            {
                "diagnosis.confirmed_cause": (
                    "`_is_already_merged` treats zero ahead commits as an automatic merge."
                ),
                "diagnosis.affected_code_path": "`sprint.runner._is_already_merged`.",
                "diagnosis.fix_success_criterion": (
                    "resume leaves zero-delta APPROVE stories out of the rerun set."
                ),
            }
        ),
    )

    assert result.status is AuthoringStatus.RUNNABLE
    assert result.body.count("## Diagnosis") == 1
    assert "zero-delta APPROVE stories are falsely reported merged." in result.body
    assert "run id `abc123` shows the false positive." in result.body
    assert "- **Confirmed cause:**" in result.body
    assert "- **Affected code path:**" in result.body
    assert "- **Fix-success criterion:**" in result.body


def test_partial_diagnosis_decline_preserves_answers_and_reports_remaining_missing() -> None:
    body = (
        "## Observed\n\nResume reports merged work without a landed commit.\n\n"
        "## Expected\n\nResume leaves unmerged work in the rerun set.\n\n"
        "## Diagnosis\n\n"
        "- **Observed symptom:** resume reports merged work without a landed commit.\n"
        "- **Evidence:** run id `abc123` shows the false positive.\n"
    )

    result = run_author_flow(
        title="Resume falsely treats zero-delta APPROVE stories as merged",
        selected_type_label="bug",
        existing_body=body,
        existing_labels=("bug", "todo:draft"),
        answer_source=_answer_map(
            {
                "diagnosis.confirmed_cause": "zero ahead commits are treated as already merged.",
                "diagnosis.affected_code_path": None,
            }
        ),
    )

    assert result.status is AuthoringStatus.DRAFT
    assert "zero ahead commits are treated as already merged." in result.body
    assert "- **Affected code path:**" in result.body
    assert "- **Fix-success criterion:**" in result.body
    assert [part.label for part in result.missing_parts] == [
        "Affected code path",
        "Fix-success criterion",
    ]


def test_unknown_confirmed_cause_surfaces_reason_detail_in_missing_parts() -> None:
    body = (
        "## Observed\n\nReady queue and sprint entry disagree.\n\n"
        "## Expected\n\nReady queue and sprint entry agree on ready issues.\n\n"
        "## Diagnosis\n\n"
        "- **Observed symptom:** ready queue and sprint entry disagree.\n"
        "- **Evidence:** run id `abc123` reproduces the mismatch.\n"
        "- **Confirmed cause:** unknown\n"
        "- **Affected code path:** `ready_queue.build_ready_queue`.\n"
        "- **Fix-success criterion:** queue and sprint entry agree on ready issues.\n"
    )

    result = run_author_flow(
        title="Ready queue disagrees with sprint entry",
        selected_type_label="bug",
        existing_body=body,
        existing_labels=("bug", "todo:draft"),
        answer_source=_answer_map({}),
    )

    assert result.status is AuthoringStatus.DRAFT
    assert result.missing_parts[0].label == "Confirmed cause"
    assert "investigation-ready but not implementation-runnable" in result.missing_parts[0].detail


def test_advisory_shape_reasons_do_not_become_missing_parts() -> None:
    body = (
        "## Why\n\nAuthors need the CLI to state the contract before submit.\n\n"
        "## Acceptance criteria\n\n"
        "- A reviewer can see the issue-body contract before any GitHub mutation.\n\n"
        "## Design\n\n"
        "Implement this in `src/theforge/cli/author.py` and "
        "`src/theforge/intake/author_flow.py`.\n"
    )

    result = run_author_flow(
        title="Surface issue-body requirements before submission",
        selected_type_label="enhancement",
        existing_body=body,
        existing_labels=("enhancement",),
        answer_source=_answer_map({}),
    )

    assert result.status is AuthoringStatus.DRAFT
    assert any(reason.code == "missing_example" for reason in result.reasons)
    assert any(reason.code == "implementation_plan_in_body" for reason in result.reasons)
    assert [part.key for part in result.missing_parts] == ["implementation_plan_in_body"]


def test_available_type_labels_exclude_non_dispatchable_types() -> None:
    assert available_type_labels() == ("bug", "enhancement", "task", "spike")
