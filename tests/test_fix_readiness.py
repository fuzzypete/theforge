"""Tests for the structured fix-readiness signal added in issue #1153.

Fix-readiness is orthogonal to story type (#1142): every bug-typed issue must
carry a complete Diagnosis section before sprinting; features are always
fix-ready since they are described by acceptance criteria, not diagnoses.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.shape_check import Shape, check
from theforge.shape_check.heuristics import (
    REQUIRED_DIAGNOSIS_TOKENS,
    cause_assertion_state,
    check_bug_missing_diagnosis,
    derive_fix_ready,
    diagnosis_completeness,
    diagnosis_completeness_score,
)
from theforge.sprint.manifest import _build_task_from_story
from theforge.sprint.shape_gate import apply_shape_gate
from theforge.sprint.sources import GitHubIssueSource

DIAGNOSIS_BODY = textwrap.dedent(
    """\
    ## Observed
    Sprint resume false-skips zero-delta APPROVE stories.

    ## Expected
    Resume identifies zero-delta APPROVE stories correctly.

    ## Diagnosis

    - **Observed symptom.** Sprint resume false-skips zero-delta APPROVE stories.
    - **Evidence.** Run id `1ff6b0bb7992`, story #1102.
    - **Confirmed cause.** `_is_already_merged` requires at least one commit ahead.
    - **Affected code path.** sprint.runner._is_already_merged.
    - **Fix-success criterion.** Resume identifies zero-delta APPROVE as merged.
    """
)


class TestDiagnosisCompleteness:
    def test_all_components_present(self) -> None:
        ok, missing = diagnosis_completeness(DIAGNOSIS_BODY)
        assert ok is True
        assert missing == []

    def test_missing_section(self) -> None:
        ok, missing = diagnosis_completeness("## What\nprose only")
        assert ok is False
        assert missing == ["missing Diagnosis section"]

    def test_partial_section(self) -> None:
        partial = textwrap.dedent(
            """\
            ## Diagnosis

            - Observed symptom: things happen.
            - Evidence: a log line.
            """
        )
        ok, missing = diagnosis_completeness(partial)
        assert ok is False
        # Partial section flags the specific missing tokens, not the whole section.
        expected_missing = (
            "confirmed cause",
            "affected code path",
            "fix-success criterion",
        )
        for token in expected_missing:
            assert token in missing


class TestCheckBugMissingDiagnosis:
    def test_bug_without_diagnosis_blocks(self) -> None:
        reason = check_bug_missing_diagnosis("T", "## What\nprose", ["bug"])
        assert reason is not None
        assert reason.code == "needs_diagnosis"
        assert "Diagnosis" in reason.detail

    def test_bug_with_complete_diagnosis_passes(self) -> None:
        assert check_bug_missing_diagnosis("T", DIAGNOSIS_BODY, ["bug"]) is None

    def test_non_bug_skipped(self) -> None:
        assert check_bug_missing_diagnosis("T", "## What\nprose", ["enhancement"]) is None

    def test_partial_diagnosis_lists_missing_components(self) -> None:
        partial = "## Diagnosis\n- Observed symptom: x.\n"
        reason = check_bug_missing_diagnosis("T", partial, ["bug"])
        assert reason is not None
        # The detail now names the exact literal labels a producer must hit,
        # each with its example, and links the full shape reference (#1629 AC3).
        assert "**Confirmed cause:**" in reason.detail
        assert "**Affected code path:**" in reason.detail
        assert "docs/reference/bug-shape.md" in reason.detail

    def test_check_pipeline_blocks_undiagnosed_bug(self) -> None:
        result = check("Bug: foo", "## What\nprose only", ["bug"])
        assert result.shape is Shape.NEEDS_GROOMING
        assert any(r.code == "needs_diagnosis" for r in result.reasons)

    def test_check_pipeline_passes_diagnosed_bug(self) -> None:
        result = check("Bug: foo", DIAGNOSIS_BODY, ["bug"])
        assert result.shape is Shape.RUNNABLE
        assert result.reasons == ()


class TestDeriveFixReady:
    def test_unknown_type_returns_none(self) -> None:
        signal, investigation_ready, warnings = derive_fix_ready(None, DIAGNOSIS_BODY)
        assert signal is None
        assert investigation_ready is False
        assert warnings and "type unknown" in warnings[0]

    def test_feature_always_fix_ready(self) -> None:
        # No diagnosis section needed for non-bug types per AC: "Features are
        # always fix-ready (no diagnosis required for new functionality)."
        signal, investigation_ready, warnings = derive_fix_ready(
            "enhancement", "## What\nDo a thing."
        )
        assert signal is True
        assert investigation_ready is False
        assert warnings == []

    def test_task_type_always_fix_ready(self) -> None:
        signal, investigation_ready, warnings = derive_fix_ready("task", "## What\nDo it.")
        assert signal is True
        assert investigation_ready is False
        assert warnings == []

    def test_bug_with_diagnosis_is_fix_ready(self) -> None:
        signal, investigation_ready, warnings = derive_fix_ready("bug", DIAGNOSIS_BODY)
        assert signal is True
        assert investigation_ready is False
        assert warnings == []

    def test_bug_without_diagnosis_is_not_fix_ready(self) -> None:
        signal, investigation_ready, warnings = derive_fix_ready("bug", "## What\nprose only")
        assert signal is False
        assert investigation_ready is False
        assert warnings and "not fix-ready" in warnings[0]

    def test_required_tokens_documented(self) -> None:
        # Guard against drift between docstring and implementation.
        assert "observed symptom" in REQUIRED_DIAGNOSIS_TOKENS
        assert "fix-success criterion" in REQUIRED_DIAGNOSIS_TOKENS


# Diagnosis body that's complete except confirmed-cause is a non-assertion phrase.
# This is the v0.10 admissible "investigation-ready" intake state — the operator
# documented every other slot honestly, including that the cause is not yet known.
def _investigation_ready_body(cause_value: str) -> str:
    return textwrap.dedent(
        f"""\
        ## Observed
        Sprint resume false-skips zero-delta APPROVE stories.

        ## Expected
        Resume identifies zero-delta APPROVE stories correctly.

        ## Diagnosis

        - **Observed symptom.** Sprint resume false-skips zero-delta APPROVE stories.
        - **Evidence.** Run id `1ff6b0bb7992`, story #1102.
        - **Confirmed cause.** {cause_value}
        - **Affected code path.** sprint.runner._is_already_merged.
        - **Fix-success criterion.** Resume identifies zero-delta APPROVE as merged.
        """
    )


class TestCauseAssertionState:
    @pytest.mark.parametrize(
        "value",
        [
            "unknown",
            "Unknown",
            "not yet identified",
            "not yet known",
            "not yet determined",
            "pending investigation",
            "still pending investigation",
            "under investigation",
            "TBD",
            "tbd",
            "to be determined",
            "to be investigated",
            "undetermined",
            "unconfirmed",
            "unidentified",
            "investigation ongoing",
            "investigation in progress",
            "?",
            "???",
            "cause is unknown",
            "cause remains unknown",
            "<fill in>",
            "<FILL IN>",
        ],
    )
    def test_non_assertion_values_recognized(self, value: str) -> None:
        body = _investigation_ready_body(value)
        assert cause_assertion_state(body) == "non_asserted"

    def test_specific_cause_is_asserted(self) -> None:
        assert cause_assertion_state(DIAGNOSIS_BODY) == "asserted"

    def test_no_diagnosis_section_is_missing(self) -> None:
        assert cause_assertion_state("## What\nprose only") == "missing"

    def test_section_without_cause_field_is_missing(self) -> None:
        body = "## Diagnosis\n- Observed symptom: x.\n- Evidence: y.\n"
        assert cause_assertion_state(body) == "missing"

    def test_non_assertion_prefix_with_trailing_prose(self) -> None:
        # Operators may add tailing context after the non-assertion phrase.
        body = _investigation_ready_body("unknown — first cycle's job is to find it")
        assert cause_assertion_state(body) == "non_asserted"


class TestDiagnosisCompletenessScore:
    """diagnosis_completeness_score ranks candidate Diagnosis sections when a
    body carries more than one — an unfilled `forge shape` scaffold must
    never outscore a genuine artifact, however many required labels it
    lists (#2263 review cycle 2)."""

    _UNFILLED_SCAFFOLD = (
        "Status: no diagnosis yet. Next step: run `forge diagnose`.\n\n"
        "- **Observed symptom:** <fill in>\n"
        "- **Evidence:** <fill in>\n"
        "- **Confirmed cause:** <fill in>\n"
        "- **Affected code path:** <fill in>\n"
        "- **Fix-success criterion:** <fill in>\n"
    )

    def test_unfilled_scaffold_scores_below_a_cause_unknown_artifact(self) -> None:
        cause_unknown_artifact = (
            "- **Observed symptom:** intermittent worktree writes\n"
            "- **Evidence:** git log shows phantom commits\n"
            "- **Confirmed cause:** not yet identified\n"
            "- **Affected code path:** TBD\n"
            "- **Fix-success criterion:** no review-phase worktree writes\n"
        )
        assert diagnosis_completeness_score(
            self._UNFILLED_SCAFFOLD
        ) < diagnosis_completeness_score(cause_unknown_artifact)

    def test_unfilled_scaffold_scores_below_empty_section(self) -> None:
        # An unfilled scaffold is worse than nothing: an empty candidate at
        # least does not masquerade as label-complete.
        scaffold_score = diagnosis_completeness_score(self._UNFILLED_SCAFFOLD)
        empty_score = diagnosis_completeness_score("")
        assert scaffold_score < empty_score


class TestInvestigationReadyDerivation:
    def test_non_assertion_cause_passes_gate_as_investigation_ready(self) -> None:
        body = _investigation_ready_body("not yet identified")
        signal, investigation_ready, warnings = derive_fix_ready("bug", body)
        assert signal is True
        assert investigation_ready is True
        assert warnings and "investigation-ready" in warnings[0]

    def test_specific_cause_remains_implementation_ready(self) -> None:
        signal, investigation_ready, warnings = derive_fix_ready("bug", DIAGNOSIS_BODY)
        assert signal is True
        assert investigation_ready is False
        assert warnings == []

    def test_check_pipeline_admits_investigation_ready_bug(self) -> None:
        # The discipline's spirit — refuse symptom-only bugs — must hold while
        # the new admissibility — accept honest non-assertion — also holds.
        body = _investigation_ready_body("unknown")
        result = check("Bug: foo", body, ["bug"])
        assert result.shape is Shape.RUNNABLE
        # Advisory-only reasons (e.g. diagnosis_cause_unknown) are compatible
        # with admission; only blocking reasons would contradict RUNNABLE.
        assert all(r.severity.value == "advisory" for r in result.reasons)

    def test_shape_gate_admits_investigation_ready_bug(self, tmp_path: Path) -> None:
        body = _investigation_ready_body("pending investigation")
        issues = [{"number": 101, "title": "Investigation-ready bug"}]

        def fetch(_number, _root):
            return {
                "title": "Investigation-ready bug",
                "body": body,
                "labels": ["bug"],
            }

        result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)
        assert [i["number"] for i in result.runnable] == [i["number"] for i in issues]
        assert result.skipped == []

    def test_github_source_propagates_investigation_ready(self, tmp_path: Path) -> None:
        body = _investigation_ready_body("unknown")
        issue_data = json.dumps(
            {
                "title": "Investigation-ready bug",
                "body": body,
                "state": "OPEN",
                "labels": [{"name": "bug"}],
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=0, stdout="[]", stderr=""),
            ]
            task = GitHubIssueSource().fetch("123", tmp_path)
        assert task.fix_ready is True
        assert task.investigation_ready is True
        assert task.readiness_warnings and any(
            "investigation-ready" in w for w in task.readiness_warnings
        )

    def test_file_source_propagates_investigation_ready(self, tmp_path: Path) -> None:
        body = _investigation_ready_body("not yet identified")
        story = tmp_path / "bug.md"
        story.write_text(
            "---\nname: B\nslug: b\ntype: bug\n---\n" + body,
            encoding="utf-8",
        )
        task = _build_task_from_story(story)
        assert task.fix_ready is True
        assert task.investigation_ready is True

    def test_implementation_ready_bug_does_not_set_investigation_ready(
        self, tmp_path: Path
    ) -> None:
        # Symmetric fix: a bug asserting a specific cause must continue to pass
        # the gate and route to implementation, with investigation_ready=False.
        story = tmp_path / "bug.md"
        story.write_text(
            "---\nname: B\nslug: b\ntype: bug\n---\n" + DIAGNOSIS_BODY,
            encoding="utf-8",
        )
        task = _build_task_from_story(story)
        assert task.fix_ready is True
        assert task.investigation_ready is False


class TestGitHubFixReadyPropagation:
    def _fetch_with(self, body: str, labels: list[str]):
        issue_data = json.dumps(
            {
                "title": "Some bug",
                "body": body,
                "state": "OPEN",
                "labels": [{"name": label} for label in labels],
            }
        )
        return MagicMock(returncode=0, stdout=issue_data, stderr=""), MagicMock(
            returncode=0, stdout="[]", stderr=""
        )

    def test_diagnosed_bug_propagates_fix_ready_true(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = self._fetch_with(DIAGNOSIS_BODY, ["bug"])
            task = GitHubIssueSource().fetch("123", tmp_path)
        assert task.fix_ready is True
        assert task.readiness_warnings == []

    def test_undiagnosed_bug_propagates_fix_ready_false(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = self._fetch_with("## What\nprose only", ["bug"])
            task = GitHubIssueSource().fetch("123", tmp_path)
        assert task.fix_ready is False
        assert task.readiness_warnings

    def test_enhancement_is_always_fix_ready(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = self._fetch_with("## What\nDo a thing.", ["enhancement"])
            task = GitHubIssueSource().fetch("123", tmp_path)
        assert task.fix_ready is True
        assert task.readiness_warnings == []

    def test_status_diagnosed_label_without_section_warns(self, tmp_path: Path) -> None:
        # Operator's label intent (status:diagnosed) is informational; absence
        # of the body section flips the issue to not-fix-ready and surfaces
        # the disagreement as a warning per AC.
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = self._fetch_with("## What\nprose", ["bug", "status:diagnosed"])
            task = GitHubIssueSource().fetch("123", tmp_path)
        assert task.fix_ready is False
        assert any("label claims fix-ready" in w for w in task.readiness_warnings)


class TestFileFrontmatterFixReady:
    def test_bug_frontmatter_with_diagnosis_section(self, tmp_path: Path) -> None:
        story = tmp_path / "bug.md"
        story.write_text(
            "---\nname: B\nslug: b\ntype: bug\n---\n" + DIAGNOSIS_BODY,
            encoding="utf-8",
        )
        task = _build_task_from_story(story)
        assert task.type == "bug"
        assert task.fix_ready is True
        assert task.readiness_warnings == []

    def test_bug_frontmatter_without_diagnosis(self, tmp_path: Path) -> None:
        story = tmp_path / "bug.md"
        story.write_text(
            "---\nname: B\nslug: b\ntype: bug\n---\n## What\nprose only\n",
            encoding="utf-8",
        )
        task = _build_task_from_story(story)
        assert task.fix_ready is False
        assert task.readiness_warnings

    def test_enhancement_frontmatter_always_fix_ready(self, tmp_path: Path) -> None:
        story = tmp_path / "f.md"
        story.write_text(
            "---\nname: F\nslug: f\ntype: enhancement\n---\n## What\nbuild it\n",
            encoding="utf-8",
        )
        task = _build_task_from_story(story)
        assert task.fix_ready is True

    def test_frontmatter_fix_ready_override_honored_with_warning(self, tmp_path: Path) -> None:
        # AC8: operators have a clear path to mark bugs fix-ready interactively.
        # Frontmatter `fix_ready: true` is the file-sourced equivalent of the
        # operator override. Body still drives derivation; operator override
        # wins but emits a warning so the disagreement is auditable.
        story = tmp_path / "bug.md"
        story.write_text(
            "---\nname: B\nslug: b\ntype: bug\nfix_ready: true\n---\n## What\nprose\n",
            encoding="utf-8",
        )
        task = _build_task_from_story(story)
        assert task.fix_ready is True
        assert any("frontmatter declares fix_ready" in w for w in task.readiness_warnings)

    def test_unknown_type_yields_none(self, tmp_path: Path) -> None:
        story = tmp_path / "legacy.md"
        story.write_text("---\nname: L\nslug: l\n---\nbody\n", encoding="utf-8")
        task = _build_task_from_story(story)
        assert task.fix_ready is None


class TestSprintShapeGateRefusesUndiagnosedBugs:
    """AC4 seam test: shape gate is the sprint-runner refusal point for
    GH-sourced bugs that lack a Diagnosis section."""

    def test_undiagnosed_bug_rejected_with_clear_message(self, tmp_path: Path) -> None:
        issues = [{"number": 99, "title": "Symptom-only bug"}]

        def fetch(_number, _root):
            return {"title": "Symptom-only bug", "body": "## What\nprose only", "labels": ["bug"]}

        result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)
        assert result.runnable == []
        assert len(result.skipped) == 1
        skipped = result.skipped[0]
        assert "needs_diagnosis" in skipped.reason_codes
        assert "Diagnosis" in skipped.detail

    def test_diagnosed_bug_passes_through(self, tmp_path: Path) -> None:
        issues = [{"number": 100, "title": "Diagnosed bug"}]

        def fetch(_number, _root):
            return {"title": "Diagnosed bug", "body": DIAGNOSIS_BODY, "labels": ["bug"]}

        result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)
        assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
        assert result.skipped == []


@pytest.mark.parametrize("label", ["enhancement", "task"])
def test_features_pass_shape_gate_without_diagnosis(label: str, tmp_path: Path) -> None:
    issues = [{"number": 5, "title": "Feature"}]

    def fetch(_number, _root):
        return {
            "title": "Feature",
            "body": textwrap.dedent(
                """\
                ## What
                Build a thing.

                ## Acceptance Criteria
                - The CLI returns 0 on success.

                ## Example
                ```
                forge thing --opt
                ```
                """
            ),
            "labels": [label],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)
    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []
