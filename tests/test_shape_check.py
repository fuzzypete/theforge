"""Unit tests for the shape_check subpackage."""

from __future__ import annotations

import subprocess
import sys
import textwrap

from theforge.shape_check import (
    DEFAULT_CLUSTER_THRESHOLD,
    Reason,
    Severity,
    Shape,
    ShapeResult,
    SuggestedAction,
    check,
)
from theforge.shape_check.classifier import classify
from theforge.shape_check.heuristics import (
    check_epic_or_tracking,
    check_implementation_design_dump,
    check_missing_acceptance_criteria,
    check_missing_example,
    check_no_observable_done_state,
    check_superseded,
    check_too_many_behavioral_clusters,
    check_untriaged_finding,
)

WELL_FORMED_AC = textwrap.dedent(
    """\
    ## What
    Do a thing.

    ## Acceptance Criteria
    - The command returns 0 on success.
    - On failure, the tool writes a diagnostic to stderr.
    """
)


# ----- per-reason unit tests -----------------------------------------------


class TestEpicOrTracking:
    def test_title_prefix(self):
        r = check_epic_or_tracking("Epic: triage lifecycle", WELL_FORMED_AC, [])
        assert r is not None and r.code == "epic_or_tracking"

    def test_label(self):
        r = check_epic_or_tracking("Plain title", WELL_FORMED_AC, ["epic"])
        assert r is not None and r.code == "epic_or_tracking"

    def test_body_phrase_umbrella(self):
        body = "This is an umbrella for many things.\n" + WELL_FORMED_AC
        r = check_epic_or_tracking("Plain title", body, [])
        assert r is not None

    def test_benign(self):
        assert check_epic_or_tracking("Fix bug", WELL_FORMED_AC, ["bug"]) is None


class TestMissingAcceptanceCriteria:
    def test_missing(self):
        r = check_missing_acceptance_criteria("T", "## What\nDo stuff.", [])
        assert r is not None and r.code == "missing_acceptance_criteria"

    def test_bug_label_exempt(self):
        assert check_missing_acceptance_criteria("T", "## What\nDo stuff.", ["bug"]) is None

    def test_bug_report_headings_exempt(self):
        body = textwrap.dedent(
            """\
            ## What happened
            The command returned a success status despite invalid input.

            ## What was expected
            The command should fail and report the invalid input.
            """
        )
        assert check_missing_acceptance_criteria("T", body, []) is None

    def test_heading_but_no_bullets(self):
        r = check_missing_acceptance_criteria(
            "T", "## Acceptance Criteria\n\nSome prose only.", []
        )
        assert r is not None

    def test_present(self):
        assert check_missing_acceptance_criteria("T", WELL_FORMED_AC, []) is None


class TestMissingExample:
    def test_present_and_substantive(self):
        body = textwrap.dedent(
            """\
            ## Proposed solution
            Add a shape-check heuristic.

            ## What it should look like
            - Running `forge shape-check` on a feature issue with no example emits an advisory.
            - Adding the example section clears that advisory in the output.

            ## Acceptance Criteria
            - The CLI emits an advisory when the section is missing.
            """
        )
        assert check_missing_example("T", body, ["enhancement"]) is None

    def test_present_but_empty(self):
        body = textwrap.dedent(
            """\
            ## What it should look like
            Soon.

            ## Acceptance Criteria
            - The CLI emits an advisory when the section is missing.
            """
        )
        r = check_missing_example("T", body, ["enhancement"])
        assert r is not None and r.code == "missing_example"

    def test_present_via_alternate_heading(self):
        body = textwrap.dedent(
            """\
            ## Target
            ```text
            $ forge shape-check
            advisory: missing_example
            ```

            ## Acceptance Criteria
            - The CLI emits an advisory when the section is missing.
            """
        )
        assert check_missing_example("T", body, ["enhancement"]) is None

    def test_fully_absent(self):
        r = check_missing_example("T", WELL_FORMED_AC, ["enhancement"])
        assert r is not None and r.code == "missing_example"

    def test_bug_format_issue_is_exempt(self):
        body = textwrap.dedent(
            """\
            ## What happened
            The shape-check emitted a warning.

            ## What was expected
            Bug reports should not require example sections.
            """
        )
        assert check_missing_example("T", body, ["bug"]) is None


class TestSuperseded:
    def test_body(self):
        r = check_superseded("T", "This is superseded by #42.", [])
        assert r is not None and r.code == "superseded"

    def test_title(self):
        r = check_superseded("Old thing (superseded by #7)", "body", [])
        assert r is not None

    def test_benign(self):
        assert check_superseded("T", "body", []) is None


class TestUntriagedFinding:
    def test_untriaged(self):
        r = check_untriaged_finding("T", "body", ["forge-finding", "needs-triage"])
        assert r is not None and r.code == "untriaged_finding"
        assert "needs-triage" in r.detail

    def test_triaged(self):
        assert check_untriaged_finding("T", "body", ["forge-finding"]) is None

    def test_other_labels_do_not_block_triaged_finding(self):
        assert check_untriaged_finding("T", "body", ["forge-finding", "p2"]) is None

    def test_not_a_finding(self):
        assert check_untriaged_finding("T", "body", ["bug", "needs-triage"]) is None


class TestImplementationDesignDump:
    def test_dump(self):
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - Implement:

            ```python
            def foo():
                return 1

            class Bar:
                pass
            ```

            ```yaml
            config:
              x: 1
            ```
            """
        )
        r = check_implementation_design_dump("T", body, [])
        assert r is not None and r.code == "implementation_design_dump"

    def test_clean_ac(self):
        assert check_implementation_design_dump("T", WELL_FORMED_AC, []) is None


class TestNoObservableDoneState:
    def test_no_ac(self):
        r = check_no_observable_done_state("T", "## What\nprose", [])
        assert r is not None

    def test_bug_label_exempt(self):
        assert check_no_observable_done_state("T", "## What\nprose", ["bug"]) is None

    def test_bug_report_headings_exempt(self):
        body = textwrap.dedent(
            """\
            ## What happened
            The sprint gate blocked the issue.

            ## What was expected
            The sprint gate should allow bug reports to proceed.
            """
        )
        assert check_no_observable_done_state("T", body, []) is None

    def test_no_verb(self):
        body = "## Acceptance Criteria\n- Something vague.\n- Another thing.\n"
        r = check_no_observable_done_state("T", body, [])
        assert r is not None and r.code == "no_observable_done_state"

    def test_with_verb(self):
        assert check_no_observable_done_state("T", WELL_FORMED_AC, []) is None


class TestTooManyBehavioralClusters:
    def test_fires(self):
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - Apply release labels at triage time.
            - Retire old report schema and config.
            - Add workflow phase hooks for audit milestone.
            - Emit a prompt summary for each finding.
            - Block release cut when triage label is missing.
            """
        )
        r = check_too_many_behavioral_clusters("T", body, [])
        assert r is not None and r.code == "too_many_behavioral_clusters"

    def test_below_threshold(self):
        assert check_too_many_behavioral_clusters("T", WELL_FORMED_AC, []) is None

    def test_custom_threshold(self):
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - Release and triage.
            """
        )
        # lower the threshold to zero so even one hit fires
        r = check_too_many_behavioral_clusters("T", body, [], threshold=0)
        assert r is not None

    def test_custom_vocabulary(self):
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - Touches alpha, beta, gamma, delta, and epsilon.
            """
        )
        r = check_too_many_behavioral_clusters(
            "T",
            body,
            [],
            threshold=3,
            vocabulary={"alpha", "beta", "gamma", "delta", "epsilon"},
        )
        assert r is not None


# ----- top-level check() aggregation and mapping ---------------------------


class TestCheckAggregation:
    def test_runnable(self):
        result = check("Fix thing", WELL_FORMED_AC, ["bug"])
        assert result.shape is Shape.RUNNABLE
        assert result.suggested_action is SuggestedAction.PROCEED
        assert result.reasons == ()

    def test_tracking_only(self):
        result = check("Epic: big thing", WELL_FORMED_AC, ["epic"])
        assert result.shape is Shape.TRACKING_ONLY
        assert result.suggested_action is SuggestedAction.REMOVE_FROM_SPRINT

    def test_superseded_beats_other(self):
        body = "Superseded by #99.\n" + WELL_FORMED_AC
        result = check("Old", body, ["epic"])
        assert result.shape is Shape.SUPERSEDED
        assert result.suggested_action is SuggestedAction.CLOSE

    def test_untriaged_finding(self):
        result = check("P2 something", WELL_FORMED_AC, ["forge-finding", "needs-triage"])
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.CLARIFY
        assert any(r.code == "untriaged_finding" for r in result.reasons)

    def test_too_many_clusters_suggests_split(self):
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - Apply release labels at triage time.
            - Retire old report schema and config.
            - Add workflow phase hooks for audit milestone.
            - Emit a prompt summary for each finding.
            - Block release cut when triage label is missing.
            """
        )
        result = check("Do all the things", body, [])
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.SPLIT

    def test_missing_ac_clarify(self):
        result = check("Something", "## What\nprose only", [])
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.CLARIFY

    def test_missing_example_is_advisory_only(self):
        result = check("Feature", WELL_FORMED_AC, ["enhancement"])
        assert result.shape is Shape.RUNNABLE
        assert result.suggested_action is SuggestedAction.PROCEED
        assert any(
            r.code == "missing_example" and r.severity is Severity.ADVISORY for r in result.reasons
        )

    def test_bug_label_without_ac_is_runnable(self):
        result = check("Bug: command exits incorrectly", "## What\nprose only", ["bug"])
        assert result.shape is Shape.RUNNABLE
        assert result.suggested_action is SuggestedAction.PROCEED
        assert result.reasons == ()

    def test_bug_report_headings_without_ac_are_runnable(self):
        body = textwrap.dedent(
            """\
            ## What happened
            The sprint gate blocked a bug report for missing acceptance criteria.

            ## What was expected
            Bug reports should proceed with observed and expected behavior.
            """
        )
        result = check("Shape gate blocks bugs", body, [])
        assert result.shape is Shape.RUNNABLE
        assert result.suggested_action is SuggestedAction.PROCEED
        assert result.reasons == ()


# ----- classifier mode tests ------------------------------------------------


class TestClassifierModes:
    def _heur_with_fuzzy(self) -> ShapeResult:
        return ShapeResult(
            shape=Shape.NEEDS_GROOMING,
            reasons=(
                Reason(
                    code="too_many_behavioral_clusters",
                    severity=Severity.BLOCKING,
                    detail="",
                ),
                Reason(
                    code="missing_acceptance_criteria",
                    severity=Severity.BLOCKING,
                    detail="",
                ),
            ),
            suggested_action=SuggestedAction.SPLIT,
        )

    def test_mode_off_drops_fuzzy(self):
        base = self._heur_with_fuzzy()
        result = classify("off", "body", base)
        codes = {r.code for r in result.reasons}
        assert "too_many_behavioral_clusters" not in codes
        assert "missing_acceptance_criteria" in codes

    def test_mode_off_recomputes_shape_and_action(self):
        # fuzzy-only input: stripping fuzzy must recompute shape to RUNNABLE
        fuzzy_only = ShapeResult(
            shape=Shape.NEEDS_GROOMING,
            reasons=(
                Reason(
                    code="too_many_behavioral_clusters",
                    severity=Severity.BLOCKING,
                    detail="",
                ),
            ),
            suggested_action=SuggestedAction.SPLIT,
        )
        result = classify("off", "body", fuzzy_only)
        assert result.reasons == ()
        assert result.shape is Shape.RUNNABLE
        assert result.suggested_action is SuggestedAction.PROCEED

    def test_mode_heuristic_unchanged(self):
        base = self._heur_with_fuzzy()
        assert classify("heuristic", "body", base) is base

    def test_mode_llm_fail_open_on_exception(self):
        base = self._heur_with_fuzzy()

        def boom(_body, _fuzzy):
            raise RuntimeError("no api key")

        assert classify("llm", "body", base, llm_caller=boom) is base

    def test_mode_llm_fail_open_on_none(self):
        base = self._heur_with_fuzzy()
        assert classify("llm", "body", base, llm_caller=None) is base

    def test_mode_llm_fuzzy_cleared_preserves_non_fuzzy(self):
        # Caller says fuzzy reasons are false positives — non-fuzzy must survive.
        base = self._heur_with_fuzzy()
        refined_clear = ShapeResult(
            shape=Shape.RUNNABLE, reasons=(), suggested_action=SuggestedAction.PROCEED
        )

        def ok(_body, _fuzzy):
            return refined_clear

        result = classify("llm", "body", base, llm_caller=ok)
        codes = {r.code for r in result.reasons}
        assert "missing_acceptance_criteria" in codes, (
            "deterministic non-fuzzy reason must be preserved"
        )
        assert "too_many_behavioral_clusters" not in codes
        # non-fuzzy missing_acceptance_criteria is blocking → needs_grooming/clarify
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.CLARIFY

    def test_mode_llm_cannot_drop_non_fuzzy_even_if_tried(self):
        # Malicious / buggy caller returns RUNNABLE with zero reasons trying to
        # override a deterministic untriaged_finding. That must be ignored.
        base = ShapeResult(
            shape=Shape.NEEDS_GROOMING,
            reasons=(
                Reason(
                    code="untriaged_finding",
                    severity=Severity.BLOCKING,
                    detail="",
                ),
                Reason(
                    code="no_observable_done_state",
                    severity=Severity.ADVISORY,
                    detail="",
                ),
            ),
            suggested_action=SuggestedAction.CLARIFY,
        )

        def override(_body, _fuzzy):
            return ShapeResult(
                shape=Shape.RUNNABLE,
                reasons=(),
                suggested_action=SuggestedAction.PROCEED,
            )

        result = classify("llm", "body", base, llm_caller=override)
        codes = {r.code for r in result.reasons}
        assert "untriaged_finding" in codes
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.CLARIFY

    def test_mode_llm_refined_fuzzy_kept_non_fuzzy_preserved(self):
        # LLM refines with updated fuzzy detail; both fuzzy and non-fuzzy end up in output.
        base = self._heur_with_fuzzy()
        refined_detail = Reason(
            code="too_many_behavioral_clusters",
            severity=Severity.BLOCKING,
            detail="LLM confirmed split",
        )

        def ok(_body, _fuzzy):
            return ShapeResult(
                shape=Shape.NEEDS_GROOMING,
                reasons=(refined_detail,),
                suggested_action=SuggestedAction.SPLIT,
            )

        result = classify("llm", "body", base, llm_caller=ok)
        codes = {r.code for r in result.reasons}
        assert codes == {"missing_acceptance_criteria", "too_many_behavioral_clusters"}
        # Both blocking codes present. missing_ac wins priority → clarify, not split.
        assert result.shape is Shape.NEEDS_GROOMING

    def test_mode_llm_ignores_non_fuzzy_reasons_from_caller(self):
        # Caller tries to inject an unrelated non-fuzzy reason; it must be filtered out.
        base = ShapeResult(
            shape=Shape.NEEDS_GROOMING,
            reasons=(
                Reason(
                    code="too_many_behavioral_clusters",
                    severity=Severity.BLOCKING,
                    detail="",
                ),
            ),
            suggested_action=SuggestedAction.SPLIT,
        )

        def inject(_body, _fuzzy):
            return ShapeResult(
                shape=Shape.SUPERSEDED,
                reasons=(Reason(code="superseded", severity=Severity.BLOCKING, detail=""),),
                suggested_action=SuggestedAction.CLOSE,
            )

        result = classify("llm", "body", base, llm_caller=inject)
        codes = {r.code for r in result.reasons}
        assert "superseded" not in codes
        assert "too_many_behavioral_clusters" not in codes  # caller cleared it
        # No reasons remain → runnable
        assert result.shape is Shape.RUNNABLE


# ----- import isolation -----------------------------------------------------


def test_shape_check_import_does_not_pull_in_coordinator():
    """Critical: shape_check must be importable without theforge.config/runners."""
    code = textwrap.dedent(
        """\
        import sys
        # pre-seed: block heavy deps via import hook simulation (just observation).
        import theforge.shape_check as sc  # noqa
        forbidden = [
            'theforge.config',
            'theforge.coordinator',
            'theforge.runners',
            'theforge.sprint',
            'theforge.task',
            'theforge.schemas',
        ]
        bad = [m for m in forbidden if m in sys.modules]
        if bad:
            print('FAIL ' + ','.join(bad))
            sys.exit(1)
        print('OK')
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK"


def test_default_cluster_threshold_is_reasonable():
    assert DEFAULT_CLUSTER_THRESHOLD >= 3
