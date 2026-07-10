"""Tests for the forge diagnose flow.

Covers:
- artifact dataclass and markdown rendering
- agent-output YAML parsing
- state machine phase transitions
- audit emission
- artifact landing modes (comment, body_section, pr_to_body)
- budget/timeout-driven partial-result handling
- interactive vs autonomous mode
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import _make_config

from theforge.diagnose_types import (
    DiagnosePhase,
    DiagnoseState,
    DiagnosisArtifact,
    Hypothesis,
    render_artifact_markdown,
    upsert_diagnosis_section,
)
from theforge.task.diagnose_prompts import (
    build_diagnose_prompt,
    parse_diagnose_output,
)


def _agent_yaml_output(*, confirmed: bool = True, partial: bool = False) -> str:
    payload = {
        "observed_symptom": "Sprint flow drops the third story silently",
        "reproduction_or_evidence": "Run forge sprint --issues 1,2,3 — story 3 never starts",
        "hypotheses": [
            {
                "statement": "DAG scheduler skips dependents when blocker fails",
                "status": "ruled_out",
                "evidence": "Logs show no failed blockers in this run",
            },
            {
                "statement": "Worker pool size off-by-one",
                "status": "confirmed" if confirmed else "inconclusive",
                "evidence": "scheduler.py:142 reserves N-1 slots when N requested",
            },
        ],
        "confirmed_cause": (
            "Worker pool reserves N-1 slots in scheduler.py:142" if confirmed else ""
        ),
        "affected_code_path": "src/theforge/sprint/scheduler.py:142",
        "fix_success_criterion": (
            "Running with --parallel 3 schedules and completes all 3 stories"
        ),
        "notes": "",
    }
    return f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"


# ── Artifact / rendering tests ────────────────────────────────────────


class TestArtifactRendering:
    def test_render_includes_all_required_sections(self):
        artifact = DiagnosisArtifact(
            issue_number=42,
            observed_symptom="It crashes",
            reproduction_or_evidence="Run X then Y",
            hypotheses=(
                Hypothesis("A", "ruled_out", "no log"),
                Hypothesis("B", "confirmed", "stack trace points here"),
            ),
            confirmed_cause="B was right",
            affected_code_path="foo.py:10",
            fix_success_criterion="X no longer crashes",
        )
        md = render_artifact_markdown(artifact)
        for required in (
            "## Diagnosis",
            "### Observed symptom",
            "### Reproduction / evidence",
            "### Hypotheses tested",
            "### Confirmed cause",
            "### Affected code path",
            "### Fix-success criterion",
        ):
            assert required in md, f"missing section: {required}"

    def test_partial_artifact_renders_warning(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "inconclusive", ""),),
            confirmed_cause="",
            affected_code_path="?",
            fix_success_criterion="?",
            partial=True,
        )
        md = render_artifact_markdown(artifact)
        assert "Partial diagnosis" in md

    def test_is_complete_requires_every_field(self):
        complete = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause="cause",
            affected_code_path="p",
            fix_success_criterion="c",
        )
        assert complete.is_complete()
        partial_no_cause = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "inconclusive", "e"),),
            confirmed_cause="",
            affected_code_path="p",
            fix_success_criterion="c",
        )
        assert not partial_no_cause.is_complete()

    def test_upsert_appends_when_section_absent(self):
        body = "# Title\n\nBody text\n"
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nDetails\n")
        assert new.startswith("# Title")
        assert "## Diagnosis" in new
        assert "Details" in new

    def test_upsert_replaces_existing_section(self):
        body = "# Title\n\nBody\n\n## Diagnosis\n\nOld content\n\n## Other\n\nKeep me\n"
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nNew content\n")
        assert "Old content" not in new
        assert "New content" in new
        assert "Keep me" in new
        assert new.count("## Diagnosis") == 1


# ── Output parser tests ───────────────────────────────────────────────


class TestParseDiagnoseOutput:
    def test_parses_well_formed_yaml(self):
        artifact = parse_diagnose_output(_agent_yaml_output(), issue_number=42)
        assert artifact is not None
        assert artifact.issue_number == 42
        assert artifact.is_complete()
        assert any(h.status == "confirmed" for h in artifact.hypotheses)

    def test_returns_none_for_unparseable_output(self):
        assert parse_diagnose_output("not yaml at all: just: : :", issue_number=1) is None

    def test_partial_flag_propagates(self):
        artifact = parse_diagnose_output(
            _agent_yaml_output(confirmed=False), issue_number=1, partial=True
        )
        assert artifact is not None
        assert artifact.partial is True
        assert artifact.confirmed_cause == ""
        assert not artifact.is_complete()

    def test_handles_yaml_without_fences(self):
        raw_yaml = (
            "observed_symptom: x\nreproduction_or_evidence: y\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: c\naffected_code_path: p\nfix_success_criterion: f\n"
        )
        artifact = parse_diagnose_output(raw_yaml, issue_number=7)
        assert artifact is not None
        assert artifact.confirmed_cause == "c"


# ── Prompt builder tests ──────────────────────────────────────────────


class TestPromptBuilder:
    def test_prompt_contains_issue_body_and_mode(self):
        prompt = build_diagnose_prompt(
            issue_number=99,
            title="It does not work",
            body="When I run X, Y crashes",
            mode="autonomous",
        )
        assert "ISSUE #99" in prompt
        assert "It does not work" in prompt
        assert "When I run X, Y crashes" in prompt
        assert "Mode: autonomous" in prompt

    def test_prompt_does_not_describe_implementation(self):
        prompt = build_diagnose_prompt(issue_number=1, title="t", body="b", mode="autonomous")
        assert "find" in prompt.lower() and "cause" in prompt.lower()
        # Diagnose != fix; the prompt must not instruct the agent to modify code.
        assert "Do not modify any files" in prompt


# ── State machine / flow tests ────────────────────────────────────────


def _fake_agent_result(output: str, *, success: bool = True, cost: float = 0.05):
    """Build a minimal AgentResult-shaped object usable as a runner stub."""
    from theforge.agent_types import AgentResult

    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=cost,
        exit_code=0 if success else 1,
        raw={},
    )


class TestDiagnoseFlow:
    def _setup_config(self, tmp_path: Path):
        config = _make_config(tmp_path)
        return config

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_happy_path_lands_comment_and_writes_audit(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 42,
            "title": "broken sprint",
            "body": "story 3 never starts",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://github.com/test/repo/issues/42#issuecomment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=42,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.landing_destination == "comment"
        assert "issuecomment" in result.state.landed_location
        # Audit YAML emitted
        audit = tmp_path / ".forge" / "audits" / f"diagnose-issue-42-{result.state.run_id}.yaml"
        assert audit.exists()
        loaded = yaml.safe_load(audit.read_text())
        assert loaded["kind"] == "diagnose"
        assert loaded["final_phase"] == "DONE"
        # phase transitions traceable for operator audit inspection
        names = [t["phase"] for t in loaded["phase_transitions"]]
        for required in ("INIT", "FETCH", "INVESTIGATE", "PARSE", "LAND", "DONE"):
            assert required in names, f"missing audit phase: {required}"
        assert "artifact" in loaded
        # Hypotheses preserved in audit
        assert len(loaded["artifact"]["hypotheses"]) == 2

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_unparseable_agent_output_marks_failed_with_audit(
        self, mock_agent, mock_fetch, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result("garbage output ::: !!!")

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        # Still writes audit so operator can see the raw agent output
        audit_files = list((tmp_path / ".forge" / "audits").glob("diagnose-*.yaml"))
        assert audit_files, "expected at least one audit file"

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_partial_artifact_lands_with_warning_and_returns_non_success(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 7,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output(confirmed=False))
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=7,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success  # partial
        assert result.state.phase == DiagnosePhase.TIMEOUT_PARTIAL
        # Artifact still landed so operator can review
        assert mock_post.called
        posted_body = mock_post.call_args[0][1]
        assert "Partial diagnosis" in posted_body

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_body_section_destination_replaces_or_appends(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 5,
            "title": "broken",
            "body": "# Original\n\nBody content\n",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=5,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )
        assert result.success
        assert mock_edit.called
        new_body = mock_edit.call_args[0][1]
        assert "## Diagnosis" in new_body
        assert "Original" in new_body  # original body preserved

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_default_destination_lands_in_body_and_satisfies_shape_gate(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        """Seam test: a default-config diagnose run must produce an issue body
        the sprint shape gate accepts as fix-ready. Diagnose and sprint share
        a contract on where the artifact lives — the default destination must
        match what the gate reads."""
        from theforge.coordinator.diagnose_flow import run_diagnose_flow
        from theforge.shape_check.heuristics import diagnosis_completeness

        config = self._setup_config(tmp_path)
        # Sanity: the configured default is the body-section destination so a
        # plain `forge diagnose` run lands where the shape gate looks.
        assert config.diagnose.output_destination == "body_section"
        original_body = "Bug report: sprint drops the third story.\n"
        mock_fetch.return_value = {
            "number": 42,
            "title": "broken sprint",
            "body": original_body,
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        # No explicit output_destination — exercise the default contract.
        result = run_diagnose_flow(
            issue_number=42,
            config=config,
            project_root=tmp_path,
        )

        assert result.success
        assert result.state.landing_destination == "body_section"
        assert mock_edit.called, "default destination must edit issue body, not post a comment"
        new_body = mock_edit.call_args[0][1]
        assert "## Diagnosis" in new_body
        # The body produced by diagnose must satisfy the same heuristic the
        # sprint shape gate uses to decide bug_missing_diagnosis. If this
        # assertion fails, diagnose and sprint disagree on what fix-ready means.
        is_complete, missing = diagnosis_completeness(new_body)
        assert is_complete, f"diagnose output does not satisfy shape gate; missing: {missing}"

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_pr_to_body_writes_markdown_file(self, mock_agent, mock_fetch, tmp_path):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 11,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=11,
            config=config,
            project_root=tmp_path,
            output_destination="pr_to_body",
        )
        assert result.success
        out = tmp_path / ".forge" / "diagnoses" / "issue-11.md"
        assert out.exists()
        text = out.read_text()
        assert "## Diagnosis" in text

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_interactive_mode_skips_landing_when_operator_declines(
        self, mock_agent, mock_fetch, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 9,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        decisions = []

        def decline(_artifact):
            decisions.append("asked")
            return False

        result = run_diagnose_flow(
            issue_number=9,
            config=config,
            project_root=tmp_path,
            output_destination="pr_to_body",
            interactive=True,
            confirm_landing=decline,
        )
        assert not result.success
        assert decisions == ["asked"]
        # No file was written
        assert not (tmp_path / ".forge" / "diagnoses" / "issue-9.md").exists()

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    def test_closed_issue_is_refused(self, mock_fetch, tmp_path):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 3,
            "title": "x",
            "body": "y",
            "state": "CLOSED",
        }

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=3,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert "closed" in result.message.lower()

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_budget_excess_marks_partial(self, mock_agent, mock_fetch, mock_post, tmp_path):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 8,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        # Agent succeeds but reports cost far above the configured budget.
        # Diagnose should mark the result as partial and not silently land
        # a "fix-ready" artifact built on a runaway investigation.
        big_cost = config.diagnose.budget_usd * 5
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output(), cost=big_cost)
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=8,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        # Budget guard: when the agent reports cost above the configured
        # budget, the flow must surface the run as partial — never as a
        # confident "fix-ready" landing — even when the YAML happens to be
        # structurally complete.
        assert not result.success
        assert result.state.phase == DiagnosePhase.BUDGET_EXCEEDED
        audit_files = list((tmp_path / ".forge" / "audits").glob("diagnose-issue-8-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[0].read_text())
        assert loaded["agent"]["cost_usd"] >= config.diagnose.budget_usd
        assert loaded["artifact"]["partial"] is True

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_empty_artifact_from_killed_agent_fails_without_mutating_body(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Seam test for the timeout/empty-output failure mode: an investigative
        agent killed mid-run emits output that still parses to a non-None but
        all-empty artifact. That is a failure to diagnose, not a partial
        diagnosis — the flow must exit FAILED, land nothing, and leave the issue
        body untouched. This exercises the PARSE→LAND boundary where the
        content-floor guard lives."""
        config = self._setup_config(tmp_path)
        original_body = "Bug report: diagnose lands empty scaffolding.\n"
        mock_fetch.return_value = {
            "number": 1575,
            "title": "broken diagnose",
            "body": original_body,
            "state": "OPEN",
        }
        # An empty-but-structurally-parseable YAML block — the shape a killed
        # agent's flushed output takes.
        empty_yaml = "```yaml\n{}\n```"
        mock_agent.return_value = _fake_agent_result(empty_yaml, success=False)

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1575,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        # (a) no landing occurred
        assert not mock_edit.called, "issue body must not be edited"
        assert not mock_post.called, "no comment must be posted"
        assert result.state.landed_location is None
        assert result.state.landing_destination is None
        # (b) final phase is FAILED (not TIMEOUT_PARTIAL)
        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert "Partial diagnosis landed" not in result.message
        # (c) audit still written so the operator can inspect the killed run
        audit_files = list((tmp_path / ".forge" / "audits").glob("diagnose-issue-1575-*.yaml"))
        assert audit_files, "expected an audit for the failed run"
        loaded = yaml.safe_load(audit_files[0].read_text())
        assert loaded["final_phase"] == "FAILED"


# ── DiagnoseState / phase transitions ─────────────────────────────────


class TestDiagnoseState:
    def test_transition_records_history(self):
        s = DiagnoseState(issue_number=1)
        s.transition(DiagnosePhase.FETCH, "t1")
        s.transition(DiagnosePhase.INVESTIGATE, "t2")
        assert s.phase == DiagnosePhase.INVESTIGATE
        assert s.phase_transitions == [("FETCH", "t1"), ("INVESTIGATE", "t2")]
