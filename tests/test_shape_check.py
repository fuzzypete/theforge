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
    ShapeVerdict,
    SuggestedAction,
    check,
)
from theforge.shape_check.classifier import classify
from theforge.shape_check.heuristics import (
    check_criterion_needs_live_evidence,
    check_epic_or_tracking,
    check_implementation_design_dump,
    check_implementation_plan_in_body,
    check_missing_acceptance_criteria,
    check_missing_example,
    check_no_observable_done_state,
    check_superseded,
    check_too_many_behavioral_clusters,
    check_untriaged_finding,
)
from theforge.shape_check.parsing import (
    extract_contextual_bullets,
    extract_contextual_fenced_code_blocks,
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

# A bug body that satisfies the fix-readiness contract (issue #1153): every
# required Diagnosis component must be present for the issue to pass shape check.
DIAGNOSED_BUG_BODY = textwrap.dedent(
    """\
    ## Diagnosis

    - **Observed symptom.** Command exits 1 instead of 0 on success.
    - **Evidence.** Reproduction in run id `abc123`.
    - **Ruled out.** Config drift; verified config is loaded correctly.
    - **Confirmed cause.** Off-by-one in exit-code computation at runner.py:42.
    - **Affected code path.** runner.exit_code, cli.main return.
    - **Fix-success criterion.** Command returns 0 on success path; existing tests pass.
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
        body = "This is an umbrella issue for many things.\n" + WELL_FORMED_AC
        r = check_epic_or_tracking("Plain title", body, [])
        assert r is not None
        assert "umbrella" in r.detail
        assert "This is an umbrella issue for many things." in r.detail

    def test_body_sentence_start_tracking_issue(self):
        body = "Tracking issue for several follow-up tasks.\n" + WELL_FORMED_AC
        r = check_epic_or_tracking("Plain title", body, [])
        assert r is not None
        assert "tracking issue" in r.detail
        assert "Tracking issue for several follow-up tasks." in r.detail

    def test_body_heading_tracking_issue(self):
        body = textwrap.dedent(
            """\
            ## Tracking issue
            Coordinate related work here.

            ## Acceptance Criteria
            - This should not run as a normal story.
            """
        )
        r = check_epic_or_tracking("Plain title", body, [])
        assert r is not None
        assert "Tracking issue" in r.detail

    def test_benign_umbrella_vocabulary_does_not_fire(self):
        body = textwrap.dedent(
            """\
            ## What
            Teach story shapes organized by use-case labels such as umbrella, bug, and docs.

            ## Acceptance Criteria
            - The guide explains when each label applies.
            """
        )
        assert check_epic_or_tracking("Plain title", body, []) is None

    def test_benign_embedded_tracking_phrase_does_not_fire(self):
        body = textwrap.dedent(
            """\
            ## What
            The authoring guide should explain what the phrase "tracking issue" means in practice.

            ## Acceptance Criteria
            - The glossary includes the term and a definition.
            """
        )
        assert check_epic_or_tracking("Plain title", body, []) is None

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


class TestImplementationPlanInBody:
    def test_clean_what_only_body_passes(self):
        body = textwrap.dedent(
            """\
            ## What
            The command must succeed for valid inputs.

            ## Acceptance Criteria
            - The command returns 0 on success.
            - Failure writes a diagnostic to stderr.
            """
        )
        assert check_implementation_plan_in_body("T", body, []) is None

    def test_design_section_is_flagged(self):
        body = textwrap.dedent(
            """\
            ## What
            We need a hygiene gate before DEV.

            ## Acceptance Criteria
            - Phase boundaries are enforced before DEV iterations begin.

            ## Design
            The hygiene gate uses a snapshot of git status taken at phase entry,
            compared against a snapshot at phase exit. Mutations are flagged.
            """
        )
        r = check_implementation_plan_in_body("T", body, [])
        assert r is not None
        assert r.code == "implementation_plan_in_body"
        assert r.severity is Severity.BLOCKING
        assert "Design" in r.detail

    def test_file_paths_and_function_names_flagged(self):
        body = textwrap.dedent(
            """\
            ## What
            Add a hygiene gate.

            ## Acceptance Criteria
            - The gate fires after `normalize_dependency_plan` and before
              `run_batch_preflight` in `src/theforge/coordinator/runner.py`.
            - `src/theforge/coordinator/workspace_hygiene.py` is called for
              each phase transition.
            - Tests added at `tests/test_coord_workspace_hygiene.py` cover
              the three boundaries.
            """
        )
        r = check_implementation_plan_in_body("T", body, [])
        assert r is not None
        assert r.code == "implementation_plan_in_body"
        assert "file path reference" in r.detail

    def test_file_line_reference_flagged(self):
        body = textwrap.dedent(
            """\
            ## What
            Refactor.

            ## Acceptance Criteria
            - The fix is applied at src/theforge/runner.py:109.
            """
        )
        r = check_implementation_plan_in_body("T", body, [])
        assert r is not None
        assert "file:line" in r.detail

    def test_refactor_exception_path_permits_file_paths(self):
        body = textwrap.dedent(
            """\
            ## What
            Split the monolithic runner.py into per-phase modules.

            Implementation target: src/theforge/coordinator/runner.py

            ## Acceptance Criteria
            - The src/theforge/coordinator/runner.py module is split into
              dev_phase.py, review_phase.py, and validate_phase.py.
            - Public entry points remain importable from runner.py.
            """
        )
        assert check_implementation_plan_in_body("T", body, []) is None

    def test_single_file_path_does_not_fire(self):
        # A single config-file mention in prose is not an implementation plan.
        body = textwrap.dedent(
            """\
            ## What
            Honor the threshold value declared in forge.yaml.

            ## Acceptance Criteria
            - The runtime reads the configured threshold and applies it.
            """
        )
        assert check_implementation_plan_in_body("T", body, []) is None

    def test_bug_format_issue_is_exempt(self):
        # Bugs legitimately name affected code paths in their Diagnosis section
        # — the WHAT-not-HOW rule applies to features, not symptom reports.
        body = textwrap.dedent(
            """\
            ## What happened
            Command exits 1 instead of 0.

            ## What was expected
            Command exits 0 on success.

            ## Diagnosis
            - Observed symptom: wrong exit code.
            - Evidence: reproduction in run abc123.
            - Ruled out: config drift.
            - Confirmed cause: off-by-one at src/theforge/runner.py:42.
            - Affected code path: runner.exit_code.
            - Fix-success criterion: command returns 0.
            """
        )
        assert check_implementation_plan_in_body("T", body, ["bug"]) is None

    def test_fenced_code_blocks_do_not_trip_check(self):
        # Diagnostic samples inside fenced blocks shouldn't count — they're
        # often illustrative output, not implementation guidance.
        body = textwrap.dedent(
            """\
            ## What
            Improve diagnostics.

            ## Acceptance Criteria
            - The diagnostic emits the offending file path.

            ```
            error in src/foo/bar.py:42
            error in src/foo/baz.py:10
            ```
            """
        )
        assert check_implementation_plan_in_body("T", body, []) is None

    def test_example_section_is_excluded(self):
        # An Example section may legitimately illustrate a "bad" body shape
        # (typically wrapped in a fenced markdown block); the check itself
        # should not flag the host issue for that demonstration content.
        body = textwrap.dedent(
            """\
            ## What
            The shape gate must flag bodies with implementation-plan content.

            ## Acceptance Criteria
            - Bodies containing structural plan signals are flagged.

            ## Example
            Bad body that should be flagged:

            ```markdown
            ## Design
            See src/theforge/runner.py:42 and src/theforge/cli.py:10.
            ```
            """
        )
        assert check_implementation_plan_in_body("T", body, []) is None


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


class TestExampleParsingContext:
    def test_extract_contextual_bullets_marks_example_subsections(self):
        section = textwrap.dedent(
            """\
            - The artifact is written.

            ### Example
            - function_signature: str

            ### Notes
            - The command returns 0.
            """
        )

        bullets = extract_contextual_bullets(section)
        assert [bullet.text for bullet in bullets] == [
            "The artifact is written.",
            "function_signature: str",
            "The command returns 0.",
        ]
        assert [bullet.in_example_section for bullet in bullets] == [False, True, False]

    def test_extract_contextual_bullets_keeps_non_example_fenced_bullets(self):
        section = textwrap.dedent(
            """\
            - The artifact is written.

            ### Notes
            ```markdown
            - Refactor the export pipeline into an ExportService class
            ```
            """
        )

        bullets = extract_contextual_bullets(section)
        assert [bullet.text for bullet in bullets] == [
            "The artifact is written.",
            "Refactor the export pipeline into an ExportService class",
        ]
        assert [bullet.in_example_section for bullet in bullets] == [False, False]

    def test_extract_contextual_fenced_blocks_marks_schema_heading(self):
        body = textwrap.dedent(
            """\
            ## Schema
            ```yaml
            primary_failure_class: flaky-tests
            ```

            ## Notes
            ```yaml
            function_signature: str
            ```
            """
        )

        blocks = extract_contextual_fenced_code_blocks(body)
        assert [block.in_example_section for block in blocks] == [True, False]


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
        # Bugs require a complete Diagnosis section to pass shape check (#1153).
        result = check("Fix thing", DIAGNOSED_BUG_BODY, ["bug"])
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

    def test_bug_label_without_diagnosis_is_blocked(self):
        # Replaces the prior "bug without AC is runnable" assertion; #1153 flips
        # bugs to not-fix-ready when the Diagnosis section is missing.
        result = check("Bug: command exits incorrectly", "## What\nprose only", ["bug"])
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.CLARIFY
        assert any(r.code == "needs_diagnosis" for r in result.reasons)

    def test_bug_report_headings_without_diagnosis_are_blocked(self):
        body = textwrap.dedent(
            """\
            ## What happened
            The sprint gate blocked a bug report for missing acceptance criteria.

            ## What was expected
            Bug reports should proceed with observed and expected behavior.
            """
        )
        result = check("Shape gate blocks bugs", body, ["bug"])
        assert result.shape is Shape.NEEDS_GROOMING
        assert any(r.code == "needs_diagnosis" for r in result.reasons)

    def test_implementation_plan_in_body_flags_as_needs_grooming(self):
        body = textwrap.dedent(
            """\
            ## What
            Add a hygiene gate.

            ## Acceptance Criteria
            - The gate runs in `src/theforge/coordinator/runner.py`.
            - Tests are added at `tests/test_coord_workspace_hygiene.py`.

            ## Design
            Snapshot git status at phase entry and exit; diff to detect mutations.
            """
        )
        result = check("Add hygiene gate", body, ["enhancement"])
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.CLARIFY
        assert any(r.code == "implementation_plan_in_body" for r in result.reasons)

    def test_bug_with_complete_diagnosis_is_runnable(self):
        result = check("Bug: command exits incorrectly", DIAGNOSED_BUG_BODY, ["bug"])
        assert result.shape is Shape.RUNNABLE
        assert result.suggested_action is SuggestedAction.PROCEED
        assert result.reasons == ()


# ----- live-run evidence criterion (#1735) ----------------------------------


# The three acceptance criteria #1425 carried, verbatim in shape. Only the
# third depends on the recorded outcome of a live run.
ISSUE_1425_AC = textwrap.dedent(
    """\
    ## What
    Enrich the diagnose prompt with environment context.

    ## Acceptance criteria
    - The diagnose prompt includes an environment briefing section describing
      the runtime, package layout, and available tooling.
    - The briefing is templated from project structure, not hardcoded.
    - A diagnose run on an issue with a sparse body (only observed/expected, no
      evidence pointers) produces a complete artifact within budget on a
      representative landing-failure bug.
    """
)


class TestCriterionNeedsLiveEvidence:
    def _reason(self, body: str, labels=("enhancement",)):
        return check_criterion_needs_live_evidence("Some feature", body, list(labels))

    def test_flags_live_run_outcome_criterion(self):
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - A diagnose run produces a complete artifact within budget on a
              representative landing-failure bug.
            """
        )
        r = self._reason(body)
        assert r is not None
        assert r.code == "criterion_needs_live_evidence"
        assert r.severity is Severity.BLOCKING

    def test_does_not_fire_on_mere_mention_of_runs_budgets_agents_artifacts(self):
        # Each bullet mentions a trigger noun but describes an inspectable
        # source feature, not the recorded outcome of a live run (AC3).
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - `forge run` prints config warnings at startup.
            - The command respects the `--budget` flag and stops when exceeded.
            - The report lists each agent's cost and duration.
            - The dev agent produces a diff and a gate exit code.
            - Running the test suite passes.
            """
        )
        assert self._reason(body) is None

    def test_1425_replay_flags_only_the_live_run_criterion(self):
        r = self._reason(ISSUE_1425_AC)
        assert r is not None
        # The flagged criterion is named; the two satisfiable ones are not.
        assert "produces a complete artifact within budget" in r.detail
        assert "environment briefing section" not in r.detail
        assert "templated from project structure" not in r.detail
        assert "1 of 3" in r.detail
        # A mix stays distinguishable: the satisfiable remainder is called out.
        assert "remain dispatchable" in r.detail

    def test_wholly_undispatchable_is_distinguishable_from_mix(self):
        body = textwrap.dedent(
            """\
            ## Acceptance Criteria
            - A diagnose run produces a complete artifact within budget.
            - A sprint run stays within its iteration budget and costs under $5.
            """
        )
        r = self._reason(body)
        assert r is not None
        assert "2 of 2" in r.detail
        assert "wholly undispatchable" in r.detail
        assert "remain dispatchable" not in r.detail

    def test_bug_format_issue_is_exempt(self):
        body = textwrap.dedent(
            """\
            ## What happened
            A diagnose run produced an incomplete artifact within budget.

            ## What was expected
            The run should complete within budget.
            """
        )
        assert self._reason(body, labels=["bug"]) is None

    def test_check_aggregation_routes_to_split_and_operator_action(self):
        result = check("Enrich diagnose prompt", ISSUE_1425_AC, ["enhancement"])
        assert result.shape is Shape.NEEDS_GROOMING
        assert result.suggested_action is SuggestedAction.SPLIT
        assert any(r.code == "criterion_needs_live_evidence" for r in result.reasons)
        assert result.verdict is ShapeVerdict.NEEDS_OPERATOR_ACTION


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
