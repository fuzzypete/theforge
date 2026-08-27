"""The shared producer-validation boundary: declaration, refusal, and CLI."""

from __future__ import annotations

import subprocess
import sys

import pytest

from theforge.shape_check.producer import (
    PRODUCERS,
    ProducerValidationError,
    compare_declaration,
    label_names,
    main,
    require_conforming_body,
    validate_issue_body,
)
from theforge.shape_check.types import Reason, Severity, ShapeVerdict

RUNNABLE_TASK_BODY = (
    "## Why\n\nOperators cannot see it.\n\n"
    "## Acceptance criteria\n\n- The command prints the resolved path.\n\n"
    "## Example\n\n```\n$ forge thing\n/tmp/thing\n```\n"
)

CITING_BODY = RUNNABLE_TASK_BODY + (
    "\n## Diagnosis\n\n- **Confirmed cause:** the loader in `src/a.py:42` drops it.\n"
    "- **Affected code path:** `src/b.py:17`\n- **Fix-success criterion:** it stops.\n"
    "- **Observed symptom:** it drops.\n- **Evidence:** `src/c.py:9`\n"
)


class TestDeclarationMatching:
    def test_matching_declaration_conforms(self):
        validation = validate_issue_body(
            producer="forge-advisory-finding",
            title="Advisory convention debt",
            body=RUNNABLE_TASK_BODY,
            labels=["task"],
            declared=ShapeVerdict.RUNNABLE,
        )
        assert validation.conforms
        assert validation.actual is ShapeVerdict.RUNNABLE

    def test_strict_mismatch_is_refused_and_names_what_failed(self):
        validation = validate_issue_body(
            producer="forge-todo-create",
            title="do a thing",
            body="",
            labels=["todo:draft"],
            declared=ShapeVerdict.RUNNABLE,
        )
        assert not validation.conforms
        report = validation.report()
        assert "forge-todo-create" in report
        assert "declared : runnable" in report
        assert "evaluated: needs_type" in report
        assert "missing_type" in report

    def test_todo_draft_declares_the_non_admissible_state_it_intends(self):
        """A producer that intends a draft may write a non-admissible object."""
        validation = validate_issue_body(
            producer="forge-todo-create",
            title="do a thing",
            body="",
            labels=["todo:draft"],
            declared=ShapeVerdict.NEEDS_TYPE,
        )
        assert validation.conforms

    def test_declared_may_name_more_than_one_admissible_state(self):
        validation = validate_issue_body(
            producer="forge-intake-autofix",
            title="t",
            body=RUNNABLE_TASK_BODY,
            labels=["task"],
            declared=(ShapeVerdict.RUNNABLE, ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN),
            previous_body=RUNNABLE_TASK_BODY,
        )
        assert validation.conforms

    def test_unknown_producer_cannot_validate(self):
        with pytest.raises(ValueError, match="unknown issue-body producer"):
            validate_issue_body(
                producer="mystery-writer",
                title="t",
                body="b",
                labels=[],
                declared=ShapeVerdict.RUNNABLE,
            )

    def test_declaring_nothing_requires_a_previous_body(self):
        with pytest.raises(ValueError, match="requires a previous body"):
            validate_issue_body(
                producer="forge-groom",
                title="t",
                body="b",
                labels=["task"],
                declared=None,
            )


class TestEditRules:
    def test_edit_that_introduces_a_refusal_is_refused(self):
        """The #2136 shape: citations landed into a non-bug body read as a plan."""
        validation = validate_issue_body(
            producer="forge-diagnose",
            title="add export",
            body=CITING_BODY,
            labels=["enhancement"],
            declared=ShapeVerdict.RUNNABLE,
            previous_body=RUNNABLE_TASK_BODY,
        )
        assert not validation.conforms
        assert "implementation_plan_in_body" in validation.new_blocking_codes
        assert validation.regressed_from_runnable
        assert "implementation_plan_in_body" in validation.report()

    def test_a_pre_existing_refusal_cannot_absorb_a_concrete_declaration(self):
        """The invariant this boundary exists for.

        An editing producer that declares runnable on an issue carrying an
        unrelated refusal it did not introduce is still writing a body whose
        evaluated state differs from the one it declared. That is refused —
        there is no "not my finding" carve-out, because a declaration a
        pre-existing refusal can absorb is not a declaration at all.
        """
        untyped = "## Observed\n\nrows vanish\n\n## Expected\n\nrows survive\n"
        validation = validate_issue_body(
            producer="forge-diagnose",
            title="export drops rows",
            body=untyped + "\n## Diagnosis\n\n- **Confirmed cause:** the loader drops it.\n",
            labels=[],  # no type label: needs_type, and nothing this edit caused
            declared=ShapeVerdict.RUNNABLE,
            previous_body=untyped,
        )
        assert validation.actual is ShapeVerdict.NEEDS_TYPE
        assert validation.new_blocking_codes == ()
        assert not validation.regressed_from_runnable
        assert not validation.conforms, (
            "declaring runnable and landing in needs_type must be refused even when "
            "the refusal was already on the issue"
        )
        report = validation.report()
        assert "declared : runnable" in report
        assert "evaluated: needs_type" in report

    def test_preserve_carries_a_pre_existing_refusal_forward(self):
        """PRESERVE is the honest declaration when the state is not the producer's to promise."""
        already_refused = "Some prose with no acceptance criteria at all.\n"
        validation = validate_issue_body(
            producer="forge-groom",
            title="t",
            body=already_refused + "\nA normalizing edit.\n",
            labels=["task"],
            declared=None,
            previous_body=already_refused,
        )
        assert validation.conforms
        assert validation.new_blocking_codes == ()
        assert "unchanged" in validation.declared_display

    def test_preserve_allows_an_improvement_that_falls_short_of_runnable(self):
        """Clearing a finding is an improvement even when the issue is still refused.

        A triage edit that supplies a Diagnosis section moves a bug from
        ``needs_diagnosis`` to ``diagnosis_cause_unknown``. That is progress the
        producer is entitled to make, so refusing it would make the boundary
        obstruct the operator rather than protect them.
        """
        before = "## Observed\n\nrows vanish\n\n## Expected\n\nrows survive\n"
        after = before + (
            "\n## Diagnosis\n\n- **Observed symptom:** rows vanish\n"
            "- **Evidence:** the run log\n"
            "- **Confirmed cause:** unknown\n"
            "- **Affected code path:** unknown\n"
            "- **Fix-success criterion:** rows survive\n"
        )
        validation = validate_issue_body(
            producer="forge-todo-triage",
            title="export drops rows",
            body=after,
            labels=["bug"],
            declared=None,
            previous_body=before,
        )
        assert validation.previous_verdict is ShapeVerdict.NEEDS_DIAGNOSIS
        assert validation.actual is ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN
        assert validation.new_blocking_codes == ()
        assert validation.conforms

    def test_preserve_refuses_a_new_refusal_hidden_under_a_higher_precedence_one(self):
        """A verdict that does not move is not proof that nothing was added."""
        untyped = "Some prose, no type label.\n"
        degraded = untyped + (
            "\n## Implementation plan\n\n"
            "1. Patch `src/a.py:42`\n2. Patch `src/b.py:17`\n3. Patch `src/c.py:9`\n"
        )
        validation = validate_issue_body(
            producer="forge-todo-triage",
            title="t",
            body=degraded,
            labels=[],
            declared=None,
            previous_body=untyped,
        )
        # missing_type outranks the new finding, so the verdict is identical...
        assert validation.actual is ShapeVerdict.NEEDS_TYPE
        assert validation.previous_verdict is ShapeVerdict.NEEDS_TYPE
        # ...but the edit still added a refusal, so PRESERVE is not satisfied.
        assert "implementation_plan_in_body" in validation.new_blocking_codes
        assert not validation.conforms

    def test_edit_may_improve_a_refused_body_to_runnable(self):
        validation = validate_issue_body(
            producer="forge-todo-triage",
            title="t",
            body=RUNNABLE_TASK_BODY,
            labels=["task"],
            declared=None,
            previous_body="Some prose with no acceptance criteria at all.\n",
        )
        assert validation.conforms
        assert validation.actual is ShapeVerdict.RUNNABLE

    def test_an_unchanged_body_does_not_satisfy_a_declaration_it_never_met(self):
        validation = validate_issue_body(
            producer="forge-intake-autofix",
            title="t",
            body="Prose only.\n",
            labels=["task"],
            declared=ShapeVerdict.RUNNABLE,
            previous_body="Prose only.\n",
        )
        assert not validation.conforms


class TestCompareDeclaration:
    def test_externally_evaluated_verdict_is_judged_by_the_same_rules(self):
        validation = compare_declaration(
            producer="forge-report-create",
            declared=ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN,
            actual=ShapeVerdict.NEEDS_DIAGNOSIS,
            reasons=(Reason(code="needs_diagnosis", severity=Severity.BLOCKING, detail="no dx"),),
        )
        assert not validation.conforms
        assert "needs_diagnosis" in validation.report()


class TestRequireConformingBody:
    def test_raises_on_mismatch(self):
        with pytest.raises(ProducerValidationError) as excinfo:
            require_conforming_body(
                producer="forge-diagnose",
                title="add export",
                body=CITING_BODY,
                labels=["enhancement"],
                declared=ShapeVerdict.RUNNABLE,
                previous_body=RUNNABLE_TASK_BODY,
            )
        assert "forge-diagnose" in str(excinfo.value)
        assert excinfo.value.validation.regressed_from_runnable

    def test_returns_the_validation_on_success(self):
        validation = require_conforming_body(
            producer="forge-advisory-finding",
            title="t",
            body=RUNNABLE_TASK_BODY,
            labels=["task"],
            declared=ShapeVerdict.RUNNABLE,
        )
        assert validation.conforms


class TestLabelNames:
    def test_accepts_both_gh_payload_shapes(self):
        assert label_names([{"name": "bug"}, "task"]) == ["bug", "task"]
        assert label_names(None) == []


class TestCli:
    def _write(self, tmp_path, text):
        path = tmp_path / "body.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_conforming_body_exits_zero(self, tmp_path):
        rc = main(
            [
                "--producer",
                "forge-advisory-finding",
                "--declared",
                "runnable",
                "--title",
                "t",
                "--body-file",
                self._write(tmp_path, RUNNABLE_TASK_BODY),
                "--label",
                "task",
            ]
        )
        assert rc == 0

    def test_mismatch_exits_two_and_reports(self, tmp_path, capsys):
        rc = main(
            [
                "--producer",
                "post-run-hook-finding",
                "--declared",
                "runnable",
                "--title",
                "t",
                "--body-file",
                self._write(tmp_path, "**Observed:** x\n"),
                "--label",
                "bug",
                "--label",
                "forge-finding",
                "--label",
                "needs-triage",
            ]
        )
        assert rc == 2
        assert "post-run-hook-finding" in capsys.readouterr().err

    def test_unknown_producer_exits_one(self, tmp_path, capsys):
        rc = main(
            [
                "--producer",
                "not-registered",
                "--declared",
                "runnable",
                "--body-file",
                self._write(tmp_path, RUNNABLE_TASK_BODY),
            ]
        )
        assert rc == 1
        assert "unknown issue-body producer" in capsys.readouterr().err

    def test_body_source_must_be_named_exactly_once(self, tmp_path, capsys):
        assert main(["--producer", "forge-todo-create", "--declared", "needs_type"]) == 1
        assert "exactly one of" in capsys.readouterr().err

    def test_module_entrypoint_reads_body_from_stdin(self):
        """The shape the shell hooks actually invoke."""
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "theforge.shape_check.producer",
                "--producer",
                "post-run-hook-finding",
                "--declared",
                "needs_operator_action",
                "--title",
                "[P1] slug: it drops",
                "--body-stdin",
                "--label",
                "bug",
                "--label",
                "forge-finding",
                "--label",
                "needs-triage",
                "--label",
                "p1",
            ],
            input="**Observed:** it drops\n\n**Expected:** it does not\n\n**Evidence:** a log\n",
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


class TestRegistry:
    def test_every_producer_has_a_description(self):
        assert PRODUCERS
        assert all(desc.strip() for desc in PRODUCERS.values())
