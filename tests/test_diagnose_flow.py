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

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from coord_test_helpers import _make_config

from theforge.diagnose_types import (
    ClaimVerification,
    DiagnosePartialReason,
    DiagnosePhase,
    DiagnoseState,
    DiagnosisArtifact,
    Hypothesis,
    SupportProvenance,
    SymptomScopeCoverage,
    UncheckedPremise,
    render_artifact_markdown,
    upsert_diagnosis_section,
)
from theforge.task.diagnose_prompts import (
    build_diagnose_prompt,
    build_environment_briefing,
    derive_issue_scope_requirement,
    parse_diagnose_output,
)


def _agent_yaml_output(
    *,
    hypothesis_statuses: tuple[str, ...] = ("ruled_out", "confirmed"),
    symptom_scope_coverage: dict | None = None,
    advisory_repair_proposal: str = "",
) -> str:
    confirmed = "confirmed" in hypothesis_statuses
    hypotheses = []
    template = (
        (
            "DAG scheduler skips dependents when blocker fails",
            "Logs show no failed blockers in this run",
        ),
        (
            "Worker pool size off-by-one",
            "scheduler.py:142 reserves N-1 slots when N requested",
        ),
    )
    for idx, status in enumerate(hypothesis_statuses):
        statement, evidence = (
            template[idx]
            if idx < len(template)
            else (
                f"Hypothesis {idx + 1}",
                f"evidence {idx + 1}",
            )
        )
        hypotheses.append(
            {
                "statement": statement,
                "status": status,
                "evidence": evidence,
                "claim_verification": {
                    "verification_type": "source",
                    "detail": "Checked against the target repository source.",
                },
            }
        )
    payload = {
        "observed_symptom": "Sprint flow drops the third story silently",
        "reproduction_or_evidence": "Run forge sprint --issues 1,2,3 — story 3 never starts",
        "hypotheses": hypotheses,
        "confirmed_cause": (
            "Worker pool reserves N-1 slots in scheduler.py:142" if confirmed else ""
        ),
        "affected_code_path": "src/theforge/sprint/scheduler.py:142",
        "fix_success_criterion": (
            "Running with --parallel 3 schedules and completes all 3 stories"
        ),
        "advisory_repair_proposal": advisory_repair_proposal,
        "notes": "",
        "confirmed_cause_verification": {
            "verification_type": "source",
            "detail": "Checked against the target repository source.",
        },
    }
    if symptom_scope_coverage is not None:
        payload["symptom_scope_coverage"] = symptom_scope_coverage
    return f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"


def _load_audit_artifact(tmp_path: Path) -> dict:
    audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-*.yaml"))
    assert audit_files, "expected at least one audit file"
    audit = yaml.safe_load(audit_files[-1].read_text())
    assert isinstance(audit, dict)
    artifact = audit.get("artifact")
    assert isinstance(artifact, dict), "expected artifact in audit payload"
    return artifact


def _categorical_bug_body() -> str:
    return (
        "## What happened\n"
        "The CLI status surface drops the branch name.\n\n"
        "## What was expected\n"
        "Every sibling renderer should include the branch name regardless of output mode.\n"
    )


def _observed_section_categorical_bug_body() -> str:
    return (
        "## What happened\n"
        "Every user-facing surface fails to include the run id.\n\n"
        "## What was expected\n"
        "The CLI status view should include the run id.\n"
    )


def _categorical_bug_body_with_modified_scope_noun() -> str:
    return (
        "## What happened\n"
        "One story lost its notes after rerun.\n\n"
        "## What was expected\n"
        "Any story with notes should preserve them.\n"
    )


def _categorical_bug_body_with_relative_clause() -> str:
    return (
        "## What happened\n"
        "One story that failed review was not retried.\n\n"
        "## What was expected\n"
        "Any story that fails review is retried.\n"
    )


def _categorical_bug_body_with_domain_scope_noun(expected_sentence: str) -> str:
    return (
        "## What happened\n"
        "One concrete reproduction failed in the current run.\n\n"
        "## What was expected\n"
        f"{expected_sentence}\n"
    )


def _categorical_bug_body_with_nested_scope(expected_sentence: str) -> str:
    return (
        "## What happened\n"
        "One concrete reproduction failed in sprint 12.\n\n"
        "## What was expected\n"
        f"{expected_sentence}\n"
    )


def _single_instance_bug_body_with_quantifier() -> str:
    return (
        "## What happened\n"
        "The run summary omits one of the cost fields.\n\n"
        "## What was expected\n"
        "The summary should print all three cost fields for this run.\n"
    )


def _single_instance_bug_body_with_single_word_scope_noun() -> str:
    return (
        "## What happened\n"
        "All rows are duplicated in the exported CSV for job 42.\n\n"
        "## What was expected\n"
        "Each retry is logged twice for this story.\n"
    )


def _single_instance_bug_body_with_scope_modifier_in_symptom() -> str:
    return (
        "## What happened\n"
        "All rows in the run summary are duplicated.\n\n"
        "## What was expected\n"
        "The exported CSV should contain each row once.\n"
    )


def _single_instance_bug_body_with_scope_modifier_in_expected() -> str:
    return (
        "## What happened\n"
        "One retry is logged twice for story 12.\n\n"
        "## What was expected\n"
        "Each retry of the run is logged twice for story 12.\n"
    )


def _single_instance_bug_body_with_domain_scope_narrowed_by_run() -> str:
    return (
        "## What happened\n"
        "One phase is shown twice in run 42.\n\n"
        "## What was expected\n"
        "Each phase of the run is shown once for run 42.\n"
    )


def _single_instance_bug_body_with_domain_scope_narrowed_by_sprint() -> str:
    return (
        "## What happened\n"
        "One sprint duplicates tasks in a single incident.\n\n"
        "## What was expected\n"
        "All tasks in this one sprint are duplicated.\n"
    )


def _single_instance_bug_body_with_domain_scope_narrowed_by_dependency() -> str:
    return (
        "## What happened\n"
        "One task is skipped in this dependency.\n\n"
        "## What was expected\n"
        "Each task of this dependency should run once.\n"
    )


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
            hypotheses=(
                Hypothesis(
                    "z",
                    "confirmed",
                    "e",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="cause",
            affected_code_path="p",
            fix_success_criterion="c",
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
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
            _agent_yaml_output(hypothesis_statuses=("ruled_out", "inconclusive")),
            issue_number=1,
            partial=True,
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

    def test_prompt_instructs_scope_boundary_discipline(self):
        # The prompt must tell the agent to scope confirmed_cause to the stated
        # symptom and surface adjacent defects as separate related_findings —
        # the #1672 scope-creep guard.
        prompt = build_diagnose_prompt(issue_number=1, title="t", body="b", mode="autonomous")
        lower = prompt.lower()
        assert "related_findings" in prompt
        assert "boundary" in lower
        assert "scope" in lower

    def test_prompt_distinguishes_analogous_scope_from_adjacent_defects(self):
        prompt = build_diagnose_prompt(issue_number=1, title="t", body="b", mode="autonomous")
        lower = prompt.lower()
        assert "structurally analogous sibling locations" in lower
        assert "adjacent-but-different" in lower
        assert "same" in lower and "construct" in lower
        assert "the same omission or behavior" in lower
        assert "do not broaden" in lower
        assert "concrete-instance symptom" in lower

    def test_prompt_marks_prior_assertions_as_non_independent_support(self):
        prompt = build_diagnose_prompt(issue_number=1, title="t", body="b", mode="autonomous")
        lower = prompt.lower()
        assert "prior assertions" in lower
        assert "commit" in lower and "messages" in lower
        assert "issue comments" in lower
        assert "memory files" in lower
        assert "not independent corroboration" in lower
        assert "status: unverifiable" in prompt
        assert "claim_verification" in prompt
        assert "confirmed_cause_verification" in prompt
        assert "evidence_provenance" in prompt
        assert "confirmed_cause_support_provenance" in prompt
        assert "src/theforge/example.py" not in prompt
        assert "src/theforge/routing.py" not in prompt

    def test_scope_coverage_example_stays_stack_neutral(self):
        prompt = build_diagnose_prompt(issue_number=1, title="t", body="b", mode="autonomous")
        assert 'location: "path/to/sibling_surface_a.ext:render_output"' in prompt
        assert 'location: "path/to/sibling_surface_b.ext:serialize_output"' in prompt
        assert "src/theforge/ui/status_cli.py:render_status" not in prompt
        assert "src/theforge/ui/status_web.py:serialize_status" not in prompt

    def test_issue_scope_requirement_prefers_expected_section(self):
        categorical, scope_text = derive_issue_scope_requirement("x", _categorical_bug_body())
        assert categorical is True
        assert scope_text.startswith("Every sibling renderer")

    def test_issue_scope_requirement_uses_observed_section_when_expected_is_concrete(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _observed_section_categorical_bug_body()
        )
        assert categorical is True
        assert scope_text == "Every user-facing surface fails to include the run id."

    def test_issue_scope_requirement_uses_title_when_body_is_concrete(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "Any landing path should preserve merge evidence.",
            "## What happened\nOne landing run lost merge evidence.\n\n"
            "## What was expected\nThe merge evidence should be preserved.\n",
        )
        assert categorical is True
        assert scope_text == "Any landing path should preserve merge evidence."

    def test_issue_scope_requirement_ignores_incidental_quantifiers(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _single_instance_bug_body_with_quantifier()
        )
        assert categorical is False
        assert scope_text == "The summary should print all three cost fields for this run."

    def test_issue_scope_requirement_detects_plural_scope_terms(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x",
            "## What happened\nOne output path omitted the run id.\n\n"
            "## What was expected\nAll surfaces should include the run id.\n",
        )
        assert categorical is True
        assert scope_text == "All surfaces should include the run id."

    def test_issue_scope_requirement_detects_unlisted_singular_scope_terms(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x",
            "## What happened\nOne sprint resumed dirty after restart.\n\n"
            "## What was expected\nEvery sprint must resume cleanly.\n",
        )
        assert categorical is True
        assert scope_text == "Every sprint must resume cleanly."

    def test_issue_scope_requirement_detects_run_scope_term(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x",
            "## What happened\nOne run lost its landing evidence.\n\n"
            "## What was expected\nEvery run should preserve landing evidence.\n",
        )
        assert categorical is True
        assert scope_text == "Every run should preserve landing evidence."

    def test_issue_scope_requirement_detects_scope_noun_before_with_modifier(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _categorical_bug_body_with_modified_scope_noun()
        )
        assert categorical is True
        assert scope_text == "Any story with notes should preserve them."

    def test_issue_scope_requirement_detects_scope_noun_before_relative_clause(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _categorical_bug_body_with_relative_clause()
        )
        assert categorical is True
        assert scope_text == "Any story that fails review is retried."

    def test_issue_scope_requirement_detects_domain_scope_nouns(self):
        cases = (
            "Every phase should emit an audit record.",
            "Every worktree should be cleaned up.",
            "Every dependency must be resolved before scheduling.",
            "Every agent should preserve the sprint lease.",
            "Every task should record its diagnosis artifact.",
        )
        for expected_sentence in cases:
            categorical, scope_text = derive_issue_scope_requirement(
                "x", _categorical_bug_body_with_domain_scope_noun(expected_sentence)
            )
            assert categorical is True
            assert scope_text == expected_sentence

    def test_issue_scope_requirement_detects_nested_categorical_scope(self):
        cases = (
            "Every story in every sprint should preserve notes.",
            "Every path in every run must be logged.",
            "Every surface for each run should show the id.",
            "Any story in the sprint should be retried.",
            "Every renderer for the sprint should include the branch.",
        )
        for expected_sentence in cases:
            categorical, scope_text = derive_issue_scope_requirement(
                "x", _categorical_bug_body_with_nested_scope(expected_sentence)
            )
            assert categorical is True
            assert scope_text == expected_sentence

    def test_issue_scope_requirement_ignores_domain_scope_nouns_narrowed_to_one_run(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _single_instance_bug_body_with_domain_scope_narrowed_by_run()
        )
        assert categorical is False
        assert scope_text == "Each phase of the run is shown once for run 42."

    def test_issue_scope_requirement_ignores_domain_scope_nouns_narrowed_to_one_sprint(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _single_instance_bug_body_with_domain_scope_narrowed_by_sprint()
        )
        assert categorical is False
        assert scope_text == "All tasks in this one sprint are duplicated."

    def test_issue_scope_requirement_ignores_domain_scope_nouns_narrowed_to_one_dependency(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _single_instance_bug_body_with_domain_scope_narrowed_by_dependency()
        )
        assert categorical is False
        assert scope_text == "Each task of this dependency should run once."

    def test_issue_scope_requirement_ignores_single_word_concrete_nouns(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _single_instance_bug_body_with_single_word_scope_noun()
        )
        assert categorical is False
        assert scope_text == "Each retry is logged twice for this story."

    def test_issue_scope_requirement_ignores_scope_hint_modifiers_in_symptom(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _single_instance_bug_body_with_scope_modifier_in_symptom()
        )
        assert categorical is False
        assert scope_text == "The exported CSV should contain each row once."

    def test_issue_scope_requirement_ignores_scope_hint_modifiers_in_expected(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "x", _single_instance_bug_body_with_scope_modifier_in_expected()
        )
        assert categorical is False
        assert scope_text == "Each retry of the run is logged twice for story 12."

    def test_issue_scope_requirement_falls_back_to_full_body(self):
        categorical, scope_text = derive_issue_scope_requirement(
            "", "Any landing path should preserve merge evidence."
        )
        assert categorical is True
        assert scope_text == "Any landing path should preserve merge evidence."


class TestEnvironmentBriefing:
    """The prompt must brief the agent on TheForge's audit/log layout, field
    semantics, and landing/merge queries (issue #1425).  The briefing is
    templated from project structure, not hardcoded (AC2)."""

    def test_prompt_embeds_environment_section(self):
        prompt = build_diagnose_prompt(issue_number=1, title="t", body="b", mode="autonomous")
        assert "== ENVIRONMENT ==" in prompt

    def test_briefing_names_canonical_audit_and_log_paths(self):
        briefing = build_environment_briefing()
        # Sprint run logs and per-story audit paths from the story example.
        assert ".forge/logs/<sprint-name>/run-<run-id>.log" in briefing
        assert ".forge/logs/<sprint-name>/<slug>/audit.yaml" in briefing
        assert ".forge/sprints/<sprint-id>/state.yaml" in briefing

    def test_briefing_names_landing_merge_queries(self):
        briefing = build_environment_briefing()
        # The one-line checks that most often crack landing-failure bugs.
        assert "gh pr list --head <branch> --state all" in briefing
        assert "gh issue view <N> --comments" in briefing

    def test_briefing_explains_common_audit_field_semantics(self):
        briefing = build_environment_briefing()
        assert "merge: true" in briefing
        assert "landing_status" in briefing
        assert "outcome_code" in briefing

    def test_audit_paths_render_from_the_owning_registry(self):
        # AC2: every path in the registry owned by audit_substrate appears in
        # the briefing, with its human label — the briefing iterates the
        # registry rather than repeating a parallel hand-maintained list.
        from theforge.coordinator import audit_substrate

        briefing = build_environment_briefing()
        assert audit_substrate.AUDIT_PATH_REGISTRY  # guard: registry is non-empty
        for info in audit_substrate.AUDIT_PATH_REGISTRY:
            assert info.display in briefing
            assert info.label in briefing

    def test_new_registry_entry_surfaces_without_editing_the_prompt(self, monkeypatch):
        # AC2 (the crux): appending a new audit path to the registry makes it
        # appear in the diagnose briefing with no edit to the prompt builder.
        from theforge.coordinator import audit_substrate

        extra = audit_substrate.AuditPathInfo(
            "Brand new audit surface", (".forge", "audits", "newly_added.db")
        )
        monkeypatch.setattr(
            audit_substrate,
            "AUDIT_PATH_REGISTRY",
            (*audit_substrate.AUDIT_PATH_REGISTRY, extra),
        )
        briefing = build_environment_briefing()
        assert ".forge/audits/newly_added.db" in briefing
        assert "Brand new audit surface" in briefing

    def test_landing_semantics_are_templated_from_landing_record(self):
        # AC2: adding a new landing_path -> outcome mapping in landing_record
        # surfaces in the briefing without editing the prompt.
        from theforge.coordinator.landing_record import LANDING_OUTCOME_BY_PATH

        briefing = build_environment_briefing()
        for path, outcome in LANDING_OUTCOME_BY_PATH.items():
            assert path in briefing
            assert outcome in briefing


# ── State machine / flow tests ────────────────────────────────────────


def _fake_agent_result(
    output: str,
    *,
    success: bool = True,
    cost: float | None = 0.05,
    failure_code: str | None = None,
):
    """Build a minimal AgentResult-shaped object usable as a runner stub."""
    from theforge.agent_types import AgentResult

    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=cost,
        exit_code=0 if success else 1,
        raw={},
        failure_code=failure_code,
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

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_audit_preserves_categorical_scope_coverage(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 43,
            "title": "broken sprint",
            "body": "every sibling renderer drops the same field",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(
                symptom_scope_coverage={
                    "symptom_is_categorical": True,
                    "stated_scope": "every sibling renderer",
                    "examined_locations": [
                        {
                            "location": "src/theforge/ui/status_cli.py:render_status",
                            "status": "covered",
                            "rationale": "Same renderer helper omitted the field.",
                        },
                        {
                            "location": "src/theforge/ui/status_web.py:serialize_status",
                            "status": "excluded",
                            "rationale": (
                                "Checked sibling path; different serializer already includes it."
                            ),
                        },
                    ],
                }
            )
        )
        mock_post.return_value = "https://github.com/test/repo/issues/43#issuecomment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=43,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        artifact = _load_audit_artifact(tmp_path)
        assert artifact["symptom_scope_coverage"]["symptom_is_categorical"] is True
        assert artifact["symptom_scope_coverage"]["stated_scope"] == "every sibling renderer"
        assert artifact["symptom_scope_coverage"]["examined_locations"] == [
            {
                "location": "src/theforge/ui/status_cli.py:render_status",
                "status": "covered",
                "rationale": "Same renderer helper omitted the field.",
            },
            {
                "location": "src/theforge/ui/status_web.py:serialize_status",
                "status": "excluded",
                "rationale": "Checked sibling path; different serializer already includes it.",
            },
        ]

    def test_audit_persists_support_provenance(self):
        from theforge.coordinator.diagnose_flow import _artifact_to_dict

        artifact = DiagnosisArtifact(
            issue_number=43,
            observed_symptom="symptom",
            reproduction_or_evidence="evidence",
            hypotheses=(
                Hypothesis(
                    "hypothesis",
                    "confirmed",
                    "commit message already states the same mechanism",
                    SupportProvenance(
                        "prior_assertion",
                        "Earlier fix branch already asserted it.",
                    ),
                    ClaimVerification(
                        "attached_evidence",
                        "Only the attached commit message was available.",
                    ),
                ),
            ),
            confirmed_cause="root cause",
            confirmed_cause_support="Earlier diagnosis already states the same cause",
            confirmed_cause_verification=ClaimVerification(
                "source_and_attached_evidence",
                "Confirmed in source and attached packet.",
            ),
            confirmed_cause_support_provenance=SupportProvenance(
                "prior_assertion",
                "Diagnosis hdp#342 already asserted the cause.",
            ),
            affected_code_path="src/theforge/task/diagnose_prompts.py",
            fix_success_criterion="render prior assertions as restatements",
        )

        payload = _artifact_to_dict(artifact)
        assert payload["hypotheses"][0]["evidence_provenance"] == {
            "source_type": "prior_assertion",
            "detail": "Earlier fix branch already asserted it.",
        }
        assert payload["hypotheses"][0]["claim_verification"] == {
            "verification_type": "attached_evidence",
            "detail": "Only the attached commit message was available.",
        }
        assert payload["confirmed_cause_support"] == (
            "Earlier diagnosis already states the same cause"
        )
        assert payload["confirmed_cause_verification"] == {
            "verification_type": "source_and_attached_evidence",
            "detail": "Confirmed in source and attached packet.",
        }
        assert payload["confirmed_cause_support_provenance"] == {
            "source_type": "prior_assertion",
            "detail": "Diagnosis hdp#342 already asserted the cause.",
        }

    def test_audit_persists_advisory_repair_proposal(self):
        from theforge.coordinator.diagnose_flow import _artifact_to_dict

        artifact = DiagnosisArtifact(
            issue_number=43,
            observed_symptom="symptom",
            reproduction_or_evidence="evidence",
            hypotheses=(
                Hypothesis(
                    "hypothesis",
                    "confirmed",
                    "support",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="root cause",
            affected_code_path="src/theforge/task/diagnose_prompts.py",
            fix_success_criterion="confirmed content stays unchanged",
            advisory_repair_proposal="Likely belongs in the renderer, not the body field.",
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
        )

        payload = _artifact_to_dict(artifact)
        assert payload["advisory_repair_proposal"] == (
            "Likely belongs in the renderer, not the body field."
        )

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
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(hypothesis_statuses=("ruled_out", "inconclusive"))
        )
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=7,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success  # partial
        assert result.state.phase == DiagnosePhase.NO_CAUSE_FOUND
        # Artifact still landed so operator can review
        assert mock_post.called
        posted_body = mock_post.call_args[0][1]
        assert "Partial diagnosis" in posted_body
        assert "did not reach a confirmed cause" in posted_body
        assert "budget or timeout" not in posted_body
        assert result.state.artifact is not None
        assert result.state.artifact.partial_reason is DiagnosePartialReason.NO_CAUSE_FOUND
        assert _load_audit_artifact(tmp_path)["partial_reason"] == "no_cause_found"

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_interactive_decline_of_partial_artifact_is_discarded_not_landed(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 8,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(hypothesis_statuses=("ruled_out", "inconclusive"))
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=8,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
            interactive=True,
            confirm_landing=lambda _artifact: False,
        )
        assert not result.success
        assert not mock_post.called, "declined partial artifact must not land"
        assert result.state.phase == DiagnosePhase.DISCARDED
        assert result.state.artifact is not None
        assert result.state.artifact.partial_reason is DiagnosePartialReason.DISCARDED
        assert _load_audit_artifact(tmp_path)["partial_reason"] == "discarded"

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_missing_claim_verification_lands_runnable_and_records_metadata_gap(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 2672,
            "title": "verification metadata missing",
            "body": "diagnosis omitted claim provenance",
            "state": "OPEN",
        }
        payload = {
            "observed_symptom": "Diagnosis renders without claim-verification lines",
            "reproduction_or_evidence": "Rendered markdown has no verification annotations",
            "hypotheses": [
                {
                    "statement": "Renderer omitted the verification block",
                    "status": "confirmed",
                    "evidence": "The artifact lands as a complete diagnosis.",
                }
            ],
            "confirmed_cause": "Completeness ignores missing claim verification metadata",
            "affected_code_path": "src/theforge/diagnose_types.py:321",
            "fix_success_criterion": "Diagnoses with omitted verification metadata land runnable",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2672,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        # The verification metadata is unrecorded, but the diagnosis itself is
        # substantive: it lands runnable rather than declaring needs_diagnosis
        # against a body the shape gate reads as runnable (#2797).
        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert mock_post.called
        posted_body = mock_post.call_args[0][1]
        assert "Partial diagnosis" not in posted_body
        assert result.state.artifact is not None
        assert not result.state.artifact.partial
        # The strict schema signal is unchanged — only its lifecycle weight is.
        assert result.state.artifact.missing_required_fields() == (
            "hypotheses[0].claim_verification",
            "confirmed_cause_verification",
        )
        assert result.state.missing_metadata_fields == (
            "hypotheses[0].claim_verification",
            "confirmed_cause_verification",
        )
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-2672-*.yaml"))
        assert audit_files
        audit = yaml.safe_load(audit_files[-1].read_text())
        assert audit["missing_metadata_fields"] == [
            "hypotheses[0].claim_verification",
            "confirmed_cause_verification",
        ]

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_missing_verification_metadata_body_landing_is_not_refused(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        """Seam test for the LAND boundary: the state the producer declares must
        match what the shared shape gate evaluates off the same rendered body.

        A substantive confirmed cause with unrecorded verification metadata used
        to declare ``needs_diagnosis`` while ``check_bug_missing_diagnosis`` read
        the same body as ``runnable``, so ``require_conforming_body`` refused the
        write before ``_gh_edit_body`` and the whole paid investigation was
        discarded (#2797).
        """
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 419,
            "title": "two exercises collapse onto one identity",
            "body": (
                "## Observed\n\n"
                "Two distinct exercises share one base key.\n\n"
                "## Expected\n\n"
                "Distinct exercises keep distinct identities.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        payload = {
            "observed_symptom": "Two distinct exercises collapse onto one base key",
            "reproduction_or_evidence": (
                "infer_base_exercise_key strips a fixed equipment-prefix list."
            ),
            "hypotheses": [
                {
                    "statement": "The prefix strip is lossy",
                    "status": "confirmed",
                    "evidence": "Both names reduce to the same key after stripping.",
                }
            ],
            "confirmed_cause": (
                "infer_base_exercise_key strips a fixed equipment-prefix list, "
                "collapsing two distinct exercises onto one identity."
            ),
            "affected_code_path": "src/hdp/exercises.py:infer_base_exercise_key",
            "fix_success_criterion": "The two exercises retain distinct base keys.",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=419,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert result.success, result.message
        assert mock_edit.called, "the conforming-body gate must not refuse this write"
        new_body = mock_edit.call_args[0][1]
        assert "## Diagnosis" in new_body
        assert "Partial diagnosis" not in new_body
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.artifact is not None
        assert not result.state.artifact.partial
        assert result.state.missing_metadata_fields == (
            "hypotheses[0].claim_verification",
            "confirmed_cause_verification",
        )

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_empty_hypotheses_body_landing_preserves_confirmed_cause(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 420,
            "title": "confirmed cause should not be discarded",
            "body": (
                "## Observed\n\nA landed diagnosis used to be discarded.\n\n"
                "## Expected\n\nA confirmed cause should be recorded on the issue.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        payload = {
            "observed_symptom": "A landed diagnosis used to be discarded",
            "reproduction_or_evidence": "The coordinator reached LAND and refused the write.",
            "hypotheses": [],
            "confirmed_cause": "The declaration path required hypotheses the gate never reads.",
            "affected_code_path": (
                "src/theforge/coordinator/diagnose_flow.py:_declared_diagnosis_verdict"
            ),
            "fix_success_criterion": (
                "The issue body records the confirmed cause instead of discarding it."
            ),
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=420,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert result.success, result.message
        assert mock_edit.called
        new_body = mock_edit.call_args[0][1]
        assert "## Diagnosis" in new_body
        assert "Partial diagnosis" not in new_body
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.missing_metadata_fields == ("hypotheses",)

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_scope_gap_body_landing_preserves_confirmed_cause(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 421,
            "title": "every sibling surface omits the same field",
            "body": _categorical_bug_body(),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=421,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert result.success, result.message
        assert mock_edit.called
        new_body = mock_edit.call_args[0][1]
        assert "## Diagnosis" in new_body
        assert "Partial diagnosis" not in new_body
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.missing_metadata_fields == ("symptom_scope_coverage",)

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_partial_body_landing_with_empty_observed_symptom_is_not_refused(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 422,
            "title": "rendered diagnosis headings must not cause refusal",
            "body": (
                "## Observed\n\nThe diagnosis write was refused.\n\n"
                "## Expected\n\nThe confirmed cause should still be recorded.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        payload = {
            "observed_symptom": "",
            "reproduction_or_evidence": (
                "A rendered section still includes the Observed symptom heading."
            ),
            "hypotheses": [
                {
                    "statement": "The declaration was derived from stricter artifact fields",
                    "status": "confirmed",
                    "evidence": "The body validator only reads the rendered diagnosis section.",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": (
                "The declaration and the rendered-body verdict were computed "
                "from different signals."
            ),
            "affected_code_path": (
                "src/theforge/coordinator/diagnose_flow.py:_declared_diagnosis_verdict"
            ),
            "fix_success_criterion": (
                "LAND writes the diagnosis instead of refusing it at the producer boundary."
            ),
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=422,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.CAUSE_FOUND_PARTIAL
        assert mock_edit.called, "the conforming-body gate must not refuse this partial write"
        new_body = mock_edit.call_args[0][1]
        assert "## Diagnosis" in new_body
        assert "Partial diagnosis" in new_body
        assert result.message == (
            "Partial diagnosis landed (cause found, diagnosis otherwise incomplete) "
            "— operator review required"
        )

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_complete_confirmed_cause_body_lands_when_runner_reports_unsuccessful(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 2848,
            "title": "diagnose discards completed investigation",
            "body": (
                "## What happened\n"
                "One diagnose run recorded nothing on the issue.\n\n"
                "## What was expected\n"
                "A completed investigation should land its diagnosis.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        payload = {
            "observed_symptom": "The diagnose run recorded nothing on the issue.",
            "reproduction_or_evidence": (
                "The audit shows a parsed confirmed-cause artifact with no issue-body update."
            ),
            "hypotheses": [
                {
                    "statement": "Landing failed before the issue body write.",
                    "status": "confirmed",
                    "evidence": "The audit records a LAND failure after PARSE.",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": (
                "The declared lifecycle verdict followed artifact.partial instead of "
                "lifecycle-blocking completeness, so shape validation refused a runnable body."
            ),
            "confirmed_cause_support": (
                "The run reached a confirmed cause and source-verified it against the checkout."
            ),
            "confirmed_cause_support_provenance": {
                "source_type": "observed",
                "detail": "Observed directly in the persisted diagnose audit.",
            },
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": "src/theforge/coordinator/diagnose_flow.py:1849",
            "fix_success_criterion": (
                "A lifecycle-complete diagnosis lands on the issue even if the runner "
                "reported unsuccessful completion."
            ),
            "symptom_scope_coverage": {
                "symptom_is_categorical": False,
                "stated_scope": "",
                "examined_locations": [],
            },
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```",
            success=False,
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2848,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert result.success, result.message
        assert result.state.phase == DiagnosePhase.DONE
        assert result.message.startswith("Diagnosis landed at ")
        assert "runner reported unsuccessful completion" in result.message
        assert mock_edit.called, "a lifecycle-complete diagnosis must still land"
        new_body = mock_edit.call_args.args[1]
        assert "## Diagnosis" in new_body
        assert "Partial diagnosis" not in new_body
        assert result.state.artifact is not None
        assert not result.state.artifact.partial
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-2848-*.yaml"))
        assert audit_files
        audit = yaml.safe_load(audit_files[-1].read_text())
        assert audit["agent"]["reported_success"] is False
        assert audit["artifact"]["partial"] is False
        assert audit["landing"]["destination"] == "body_section"

    def test_declared_verdict_follows_rendered_section_for_categorical_scope_gap(self):
        from theforge.coordinator.diagnose_flow import _declared_diagnosis_verdict
        from theforge.shape_check.types import ShapeVerdict

        artifact = DiagnosisArtifact(
            issue_number=2849,
            observed_symptom="A sibling renderer omits the branch name.",
            reproduction_or_evidence="CLI output shows the branch name missing.",
            hypotheses=(
                Hypothesis(
                    "The shared renderer omits the field.",
                    "confirmed",
                    "The same helper is used by the affected surface.",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="The shared renderer omits the branch name field.",
            affected_code_path="src/theforge/ui/status_cli.py:render_status",
            fix_success_criterion="Every sibling renderer includes the branch name.",
            partial=True,
            partial_reason=DiagnosePartialReason.CAUSE_FOUND_INCOMPLETE,
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
            symptom_scope_coverage=SymptomScopeCoverage(),
        )
        section = render_artifact_markdown(
            artifact,
            issue_requires_categorical_scope=True,
        )

        assert (
            artifact.lifecycle_blocking_missing_fields(issue_requires_categorical_scope=True) == ()
        )
        assert (
            _declared_diagnosis_verdict(
                artifact,
                section,
                issue_requires_categorical_scope=True,
            )
            is ShapeVerdict.RUNNABLE
        )

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_categorical_issue_without_scope_coverage_lands_and_audits_gap(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 44,
            "title": "broken renderer coverage",
            "body": _categorical_bug_body(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=44,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert result.message == (
            "Diagnosis landed at https://example/comment — non-blocking diagnosis "
            "gaps: symptom_scope_coverage"
        )
        assert result.state.artifact is not None
        assert result.state.artifact.partial is False
        assert result.state.artifact.symptom_scope_coverage == SymptomScopeCoverage()
        assert result.state.missing_metadata_fields == ("symptom_scope_coverage",)
        assert mock_post.call_count == 1
        landed_markdown = mock_post.call_args.args[1]
        assert "Partial diagnosis" not in landed_markdown
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-44-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["missing_metadata_fields"] == ["symptom_scope_coverage"]
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": True,
            "stated_scope": (
                "Every sibling renderer should include the branch name regardless of output mode."
            ),
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_title_only_categorical_scope_requirement_lands_and_audits_gap(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 46,
            "title": "Any landing path should preserve merge evidence",
            "body": (
                "## What happened\nOne landing path dropped the merge evidence.\n\n"
                "## What was expected\nThe merge evidence should be preserved.\n"
            ),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=46,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.missing_metadata_fields == ("symptom_scope_coverage",)
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-46-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["missing_metadata_fields"] == ["symptom_scope_coverage"]
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": True,
            "stated_scope": "Any landing path should preserve merge evidence",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_observed_section_categorical_scope_requirement_lands_and_audits_gap(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 48,
            "title": "run id missing from CLI status view",
            "body": _observed_section_categorical_bug_body(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=48,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.missing_metadata_fields == ("symptom_scope_coverage",)
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-48-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["missing_metadata_fields"] == ["symptom_scope_coverage"]
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": True,
            "stated_scope": "Every user-facing surface fails to include the run id.",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_modified_scope_noun_categorical_requirement_lands_and_audits_gap(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 49,
            "title": "story notes disappear on rerun",
            "body": _categorical_bug_body_with_modified_scope_noun(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=49,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.missing_metadata_fields == ("symptom_scope_coverage",)
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-49-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["missing_metadata_fields"] == ["symptom_scope_coverage"]
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": True,
            "stated_scope": "Any story with notes should preserve them.",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_nested_categorical_scope_requirement_lands_and_audits_gap(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 52,
            "title": "story notes disappear in sibling sprints",
            "body": _categorical_bug_body_with_nested_scope(
                "Every story in every sprint should preserve notes."
            ),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=52,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.missing_metadata_fields == ("symptom_scope_coverage",)
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-52-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["missing_metadata_fields"] == ["symptom_scope_coverage"]
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": True,
            "stated_scope": "Every story in every sprint should preserve notes.",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_categorical_issue_with_false_scope_flag_lands_and_audits_gap(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 45,
            "title": "broken renderer coverage",
            "body": _categorical_bug_body(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(
                symptom_scope_coverage={
                    "symptom_is_categorical": False,
                    "stated_scope": "",
                    "examined_locations": [],
                }
            )
        )
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=45,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.artifact is not None
        assert result.state.artifact.partial is False
        assert result.state.artifact.symptom_scope_coverage.symptom_is_categorical is False
        assert result.state.missing_metadata_fields == ("symptom_scope_coverage",)

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_single_instance_quantifier_issue_does_not_require_scope_coverage(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 47,
            "title": "summary omits a field",
            "body": _single_instance_bug_body_with_quantifier(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=47,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.issue_scope_is_categorical is False
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-47-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": False,
            "stated_scope": "The summary should print all three cost fields for this run.",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_single_word_concrete_noun_issue_does_not_require_scope_coverage(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 49,
            "title": "duplicate rows in one export",
            "body": _single_instance_bug_body_with_single_word_scope_noun(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=49,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.issue_scope_is_categorical is False
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-49-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": False,
            "stated_scope": "Each retry is logged twice for this story.",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_scope_modifier_concrete_issue_does_not_require_scope_coverage(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 50,
            "title": "duplicate retry in one run",
            "body": _single_instance_bug_body_with_scope_modifier_in_expected(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=50,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.issue_scope_is_categorical is False
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-50-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": False,
            "stated_scope": "Each retry of the run is logged twice for story 12.",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_domain_scope_phrase_narrowed_to_one_run_does_not_require_scope_coverage(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 51,
            "title": "duplicate phase display in one run",
            "body": _single_instance_bug_body_with_domain_scope_narrowed_by_run(),
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=51,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        assert result.state.issue_scope_is_categorical is False
        audit_files = sorted((tmp_path / ".forge" / "audits").glob("diagnose-issue-51-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[-1].read_text())
        assert loaded["issue_scope_requirement"] == {
            "symptom_is_categorical": False,
            "stated_scope": "Each phase of the run is shown once for run 42.",
        }

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
            "body": (
                "# Original\n\n"
                "Body content\n\n"
                "## Observed\n\n"
                "The command exits 1.\n\n"
                "## Expected\n\n"
                "The command should exit 0.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
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

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_bug_missing_capture_sections_refuses_before_investigate(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """Seam test for the FETCH→INVESTIGATE boundary: a bug issue missing
        substantive Observed/Expected capture must fail closed before any scope
        inference or agent dispatch."""
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 2665,
            "title": "diagnose scans unrelated prose",
            "body": (
                "## Summary\n"
                "Diagnose falls back to unrelated prose.\n\n"
                "## Impact\n"
                "The paid investigation starts from a guessed scope.\n\n"
                "## Scope\n"
                "Affects direct diagnose on bug issues.\n\n"
                "## Notes\n"
                "Observed and Expected were never captured.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2665,
            config=config,
            project_root=tmp_path,
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert not mock_agent.called, "investigative agent must not run"
        assert "missing_observed" in (result.state.error or "")
        assert "missing_expected" in (result.state.error or "")
        audit = yaml.safe_load(
            (
                tmp_path / ".forge" / "audits" / f"diagnose-issue-2665-{result.state.run_id}.yaml"
            ).read_text()
        )
        phases = [entry["phase"] for entry in audit["phase_transitions"]]
        assert "INVESTIGATE" not in phases

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
        original_body = (
            "## What happened\n"
            "The sprint drops the third story.\n\n"
            "## What was expected\n"
            "The sprint should run every selected story.\n"
        )
        mock_fetch.return_value = {
            "number": 42,
            "title": "broken sprint",
            "body": original_body,
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(hypothesis_statuses=("confirmed",))
        )

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

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    def test_body_section_landing_replaces_placeholder_only_bug_capture_sections(
        self, mock_edit, tmp_path
    ):
        """Seam test for the LAND boundary: placeholder-only Observed/Expected
        headings must be normalized before producer validation so the diagnosis
        can land into a bug issue body."""
        from theforge.coordinator.diagnose_flow import _land_artifact
        from theforge.shape_check.heuristics import (
            check_bug_missing_expected,
            check_bug_missing_observed,
        )

        state = DiagnoseState(
            issue_number=2136,
            issue_title="landing fails after paid diagnosis",
            issue_body=(
                "## Observed\n\n"
                "<insert observation here>\n\n"
                "## Expected\n\n"
                "TODO: describe the expected behavior.\n"
            ),
        )
        artifact = DiagnosisArtifact(
            issue_number=2136,
            observed_symptom="Sprint flow drops the third story silently",
            reproduction_or_evidence="Run forge sprint --issues 1,2,3.",
            hypotheses=(Hypothesis("worker pool off by one", "confirmed", "scheduler.py:142"),),
            confirmed_cause="Worker pool reserves N-1 slots in scheduler.py:142",
            affected_code_path="src/theforge/sprint/scheduler.py:142",
            fix_success_criterion=(
                "Running with --parallel 3 schedules and completes all 3 stories"
            ),
        )

        location = _land_artifact(
            state,
            artifact,
            "body_section",
            tmp_path,
            issue_labels=["bug"],
        )

        assert location == "issue #2136 body updated"
        assert mock_edit.called
        new_body = mock_edit.call_args[0][1]
        assert "<insert observation here>" not in new_body
        assert "TODO: describe the expected behavior." not in new_body
        assert "Sprint flow drops the third story silently" in new_body
        assert "Running with --parallel 3 schedules and completes all 3 stories" in new_body
        assert (
            check_bug_missing_observed("landing fails after paid diagnosis", new_body, ["bug"])
            is None
        )
        assert (
            check_bug_missing_expected("landing fails after paid diagnosis", new_body, ["bug"])
            is None
        )

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_default_destination_uses_comment_for_non_bug_issue(
        self, mock_agent, mock_fetch, mock_edit, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 43,
            "title": "add export",
            "body": (
                "## Acceptance criteria\n"
                "- Export the report as CSV.\n\n"
                "## Example\n"
                "`forge report --format csv` writes a CSV file.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "enhancement"}],
        }
        mock_post.return_value = "https://github.com/test/repo/issues/43#issuecomment-1"
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(hypothesis_statuses=("confirmed",))
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=43,
            config=config,
            project_root=tmp_path,
        )

        assert result.success
        assert result.state.landing_destination == "comment"
        assert mock_post.called
        assert not mock_edit.called

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_explicit_body_section_override_reports_rather_than_writing_non_bug_body(
        self, mock_agent, mock_fetch, mock_edit, mock_post, tmp_path
    ):
        """An explicit override does not license writing a body the gate refuses.

        Landing a Diagnosis section with file:line citations into an enhancement
        makes an admissible body inadmissible — the citations read as an
        implementation plan (#2136). Diagnose declares the state it intends the
        landing to produce, and when the rendered body would not occupy it,
        reports rather than writes.
        """
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 44,
            "title": "add export",
            "body": (
                "## Acceptance criteria\n"
                "- Export the report as CSV.\n\n"
                "## Example\n"
                "`forge report --format csv` writes a CSV file.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "enhancement"}],
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(hypothesis_statuses=("confirmed",))
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=44,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert not mock_edit.called
        assert not mock_post.called
        assert "forge-diagnose" in (result.state.error or "")
        assert "implementation_plan_in_body" in (result.state.error or "")

    def test_ensure_bug_capture_sections_noops_for_non_bug_body(self):
        from theforge.coordinator.diagnose_flow import _ensure_bug_capture_sections

        body = (
            "## Acceptance criteria\n"
            "- Export the report as CSV.\n\n"
            "## Example\n"
            "`forge report --format csv` writes a CSV file.\n"
        )
        artifact = DiagnosisArtifact(
            issue_number=44,
            observed_symptom="Observed symptom that should not be inserted.",
            reproduction_or_evidence="Run forge report --format csv.",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="csv writer is not wired up",
            affected_code_path="src/theforge/report.py:12",
            fix_success_criterion="CSV export writes a file.",
        )

        assert _ensure_bug_capture_sections(body, artifact, ["enhancement"]) == body

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
        assert result.state.phase == DiagnosePhase.DISCARDED
        assert result.state.artifact is not None
        assert result.state.artifact.partial_reason is DiagnosePartialReason.DISCARDED

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_interactive_mode_does_not_retry_confirmer_internal_type_error(
        self, mock_agent, mock_fetch, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 10,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        calls = []

        def boom(_artifact):
            calls.append("asked")
            raise TypeError("callback body failed")

        with pytest.raises(TypeError, match="callback body failed"):
            run_diagnose_flow(
                issue_number=10,
                config=config,
                project_root=tmp_path,
                output_destination="pr_to_body",
                interactive=True,
                confirm_landing=boom,
            )

        assert calls == ["asked"]

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
        assert result.message == (
            f"Partial diagnosis landed (budget exceeded (${config.diagnose.budget_usd})) "
            "— operator review required"
        )
        assert mock_post.called, "budget-exceeded partial diagnoses should still land for review"
        posted_body = mock_post.call_args.args[1]
        assert "## Diagnosis" in posted_body
        assert "Partial diagnosis" in posted_body
        assert "exceeded its budget" in posted_body
        assert "Worker pool reserves N-1 slots in scheduler.py:142" in posted_body
        audit_files = list((tmp_path / ".forge" / "audits").glob("diagnose-issue-8-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[0].read_text())
        assert loaded["agent"]["cost_usd"] >= config.diagnose.budget_usd
        assert loaded["artifact"]["partial"] is True

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_timeout_partial_still_reports_timeout_when_agent_timed_out(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = self._setup_config(tmp_path)
        mock_fetch.return_value = {
            "number": 81,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(hypothesis_statuses=("ruled_out", "inconclusive")),
            success=False,
            failure_code="timeout",
        )
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=81,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.agent_failure_code == "timeout"
        assert result.state.phase == DiagnosePhase.TIMEOUT_PARTIAL

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

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_blank_hypothesis_scaffold_fails_without_mutating_body(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Seam test for the empty-scaffold variant: a killed agent can flush a
        structurally-parseable block whose only content is a blank hypothesis
        entry (`hypotheses: [{}]`). parse_diagnose_output turns that into a
        non-empty hypotheses tuple of one blank bullet — but it carries no
        investigative content, so the content floor must still reject it and
        the flow must fail without touching the issue body."""
        config = self._setup_config(tmp_path)
        original_body = "Bug report: diagnose lands empty scaffolding.\n"
        mock_fetch.return_value = {
            "number": 1595,
            "title": "broken diagnose",
            "body": original_body,
            "state": "OPEN",
        }
        scaffold_yaml = "```yaml\nhypotheses:\n  - {}\n```"
        mock_agent.return_value = _fake_agent_result(scaffold_yaml, success=False)

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1595,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not mock_edit.called, "issue body must not be edited"
        assert not mock_post.called, "no comment must be posted"
        assert result.state.landed_location is None
        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert "Partial diagnosis landed" not in result.message


class TestStartingEvidenceInjection:
    """Seam test for the FETCH→INVESTIGATE boundary: when the issue body cites a
    run id whose log exists on disk, the flow pre-loads a STARTING EVIDENCE block
    into the prompt handed to the agent and records what it injected in the
    audit. An issue with no recognizable references leaves the prompt unchanged."""

    def _write_run_log(self, tmp_path: Path, run_id: str, marker: str) -> None:
        log = tmp_path / ".forge" / "logs" / "issues-1135" / f"run-{run_id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"[forge] earlier\n[forge] {marker}\n", encoding="utf-8")

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_cited_run_id_injects_evidence_and_records_audit(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = _make_config(tmp_path)
        run_id = "7cf3f238d8d8"
        marker = "issue-1135 reached APPROVE"
        self._write_run_log(tmp_path, run_id, marker)
        mock_fetch.return_value = {
            "number": 1420,
            "title": "landing bug",
            "body": f"Sprint run {run_id}: landing_status wrong",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1420,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        # The prompt actually handed to the agent carried the pre-loaded excerpt.
        sent_prompt = mock_agent.call_args.kwargs["prompt"]
        assert "== STARTING EVIDENCE" in sent_prompt
        assert marker in sent_prompt
        # Audit records what was injected (instrumented cross-phase data).
        audit = tmp_path / ".forge" / "audits" / f"diagnose-issue-1420-{result.state.run_id}.yaml"
        loaded = yaml.safe_load(audit.read_text())
        assert loaded["starting_evidence"]["reference_labels"]
        assert loaded["starting_evidence"]["chars"] > 0

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_no_references_leaves_prompt_and_audit_clean(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1,
            "title": "x",
            "body": "Something is broken but I don't know where.",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://example/comment"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        sent_prompt = mock_agent.call_args.kwargs["prompt"]
        assert "== STARTING EVIDENCE" not in sent_prompt
        audit = tmp_path / ".forge" / "audits" / f"diagnose-issue-1-{result.state.run_id}.yaml"
        loaded = yaml.safe_load(audit.read_text())
        assert loaded["starting_evidence"]["reference_labels"] == []
        assert loaded["starting_evidence"]["chars"] == 0


# ── dry-run stdout tests ──────────────────────────────────────────────


class TestDiagnoseCostFidelity:
    """A killed diagnose run records real/unknown cost in the audit, never $0.00.

    Covers the runner -> diagnose_flow -> audit seam: a run killed at the timeout
    deadline still incurred cost, and the audit must reflect that faithfully.
    """

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_reconstructed_cost_lands_in_audit(self, mock_agent, mock_fetch, tmp_path):
        """A non-zero (reconstructed) cost from the runner is preserved in the audit."""
        config = _make_config(tmp_path)
        mock_fetch.return_value = {"number": 1, "title": "x", "body": "y", "state": "OPEN"}
        mock_agent.return_value = _fake_agent_result(
            "TIMEOUT: Agent exceeded limit", success=False, cost=0.37
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        audit = tmp_path / ".forge" / "audits" / f"diagnose-issue-1-{result.state.run_id}.yaml"
        loaded = yaml.safe_load(audit.read_text())
        assert loaded["agent"]["cost_usd"] == 0.37

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_unmeasured_cost_is_null_not_zero(self, mock_agent, mock_fetch, tmp_path):
        """An unmeasured cost (None) is written as null, distinct from a free 0.0."""
        config = _make_config(tmp_path)
        mock_fetch.return_value = {"number": 1, "title": "x", "body": "y", "state": "OPEN"}
        mock_agent.return_value = _fake_agent_result(
            "TIMEOUT: Agent exceeded limit", success=False, cost=None
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        audit = tmp_path / ".forge" / "audits" / f"diagnose-issue-1-{result.state.run_id}.yaml"
        loaded = yaml.safe_load(audit.read_text())
        assert loaded["agent"]["cost_usd"] is None


class TestDryRunPrintsArtifact:
    def _setup(self, tmp_path: Path):
        from coord_test_helpers import _make_config

        return _make_config(tmp_path)

    def _issue_fetch(self):
        return {
            "number": 42,
            "title": "broken sprint",
            "body": "story 3 never starts",
            "state": "OPEN",
        }

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_dry_run_comment_prints_artifact_to_stdout(
        self, mock_agent, mock_fetch, tmp_path, capsys
    ):
        config = self._setup(tmp_path)
        mock_fetch.return_value = self._issue_fetch()
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=42,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
            dry_run=True,
        )

        assert result.state.landed_location == "<dry-run: comment>"
        captured = capsys.readouterr()
        assert "## Diagnosis" in captured.out
        assert "Worker pool" in captured.out

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_dry_run_body_section_prints_artifact_to_stdout(
        self, mock_agent, mock_fetch, tmp_path, capsys
    ):
        config = self._setup(tmp_path)
        mock_fetch.return_value = self._issue_fetch()
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=42,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
            dry_run=True,
        )

        assert result.state.landed_location == "<dry-run: body_section>"
        captured = capsys.readouterr()
        assert "## Diagnosis" in captured.out

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_dry_run_pr_to_body_prints_artifact_to_stdout_without_writing_file(
        self, mock_agent, mock_fetch, tmp_path, capsys
    ):
        config = self._setup(tmp_path)
        mock_fetch.return_value = self._issue_fetch()
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        run_diagnose_flow(
            issue_number=42,
            config=config,
            project_root=tmp_path,
            output_destination="pr_to_body",
            dry_run=True,
        )

        captured = capsys.readouterr()
        assert "## Diagnosis" in captured.out
        # File must NOT be written in dry-run mode
        assert not (tmp_path / ".forge" / "diagnoses" / "issue-42.md").exists()

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_dry_run_does_not_post_to_github(self, mock_agent, mock_fetch, tmp_path):
        config = self._setup(tmp_path)
        mock_fetch.return_value = self._issue_fetch()
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        with (
            patch("theforge.coordinator.diagnose_flow._gh_post_comment") as mock_post,
            patch("theforge.coordinator.diagnose_flow._gh_edit_body") as mock_edit,
        ):
            run_diagnose_flow(
                issue_number=42,
                config=config,
                project_root=tmp_path,
                output_destination="comment",
                dry_run=True,
            )
            assert not mock_post.called
            assert not mock_edit.called


# ── DiagnoseState / phase transitions ─────────────────────────────────


class TestDiagnoseState:
    def test_transition_records_history(self):
        s = DiagnoseState(issue_number=1)
        s.transition(DiagnosePhase.FETCH, "t1")
        s.transition(DiagnosePhase.INVESTIGATE, "t2")
        assert s.phase == DiagnosePhase.INVESTIGATE
        assert s.phase_transitions == [("FETCH", "t1"), ("INVESTIGATE", "t2")]


# ── Premise verification (already-resolved detection) ─────────────────


def _git(args: list[str], cwd: Path) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ.get("PATH", ""),
    }
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(["init", "-q"], root)


def _commit_file(root: Path, rel: str, content: str, message: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", rel], root)
    _git(["commit", "-q", "-m", message], root)
    return _git(["rev-parse", "HEAD"], root)


def _remove_file(root: Path, rel: str, message: str) -> str:
    _git(["rm", "-q", rel], root)
    _git(["commit", "-q", "-m", message], root)
    return _git(["rev-parse", "HEAD"], root)


def _agent_yaml_with_anchor(
    *,
    affected: str,
    anchor_file: str,
    anchor_pattern: str,
    inspected: tuple[str, ...] = (),
) -> str:
    payload = {
        "observed_symptom": "The buggy path miscomputes the slot count",
        "reproduction_or_evidence": "Call the affected function with N=3",
        "hypotheses": [
            {
                "statement": "Off-by-one in the reservation loop",
                "status": "confirmed",
                "evidence": "The loop reserves N-1 slots",
                "claim_verification": {
                    "verification_type": "source",
                    "detail": "Checked against the target repository source.",
                },
            }
        ],
        "confirmed_cause": "Off-by-one reservation in the affected function",
        "confirmed_cause_verification": {
            "verification_type": "source",
            "detail": "Checked against the target repository source.",
        },
        "affected_code_path": affected,
        "fix_success_criterion": "N=3 reserves 3 slots",
        "notes": "",
        "premise_anchors": [{"file": anchor_file, "pattern": anchor_pattern}],
    }
    if inspected:
        payload["inspected_files"] = list(inspected)
    return f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"


class TestPremiseVerification:
    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_pattern_removed_from_file_reports_already_resolved(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Seam test: an agent that confirms a cause anchored to a pattern that a
        later commit removed must NOT land a confirmed-cause body. The flow must
        divert to ALREADY_RESOLVED naming the removing commit, leaving the issue
        body untouched. Exercises the PARSE→VERIFY_PREMISE→(no LAND) boundary."""
        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/mod.py",
            "def buggy_func():\n    return reserve(n - 1)\n",
            "add buggy func",
        )
        removing = _commit_file(
            tmp_path,
            "src/mod.py",
            "def other_func():\n    return reserve(n)\n",
            "remove buggy func premise",
        )

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1494,
            "title": "buggy func miscounts",
            "body": "The buggy_func reserves the wrong number of slots.\n",
            "state": "OPEN",
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_with_anchor(
                affected="src/mod.py:1",
                anchor_file="src/mod.py",
                anchor_pattern="def buggy_func",
            )
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1494,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert not mock_edit.called, "must not write a confirmed-cause body"
        assert not mock_post.called
        assert result.state.landed_location is None
        # Names the removing commit
        assert result.state.absent_premises
        assert result.state.absent_premises[0].removing_commit.startswith(removing[:8])
        assert removing[:12] in result.message
        # Audit records the already-resolved verdict
        audit_files = list((tmp_path / ".forge" / "audits").glob("diagnose-issue-1494-*.yaml"))
        assert audit_files
        loaded = yaml.safe_load(audit_files[0].read_text())
        assert loaded["final_phase"] == "ALREADY_RESOLVED"
        assert loaded["already_resolved"] is True
        assert loaded["absent_premises"][0]["pattern"] == "def buggy_func"

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_symbol_removed_from_present_file_without_anchors_reports_already_resolved(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Seam test for the review finding: the target file still exists, but the
        function the diagnosis pins the bug to was removed, and the agent supplied
        NO premise_anchors — only affected_code_path names the removed symbol
        (``src/mod.py:buggy_func``). This must still divert to ALREADY_RESOLVED
        rather than landing a live ## Diagnosis section."""
        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/mod.py",
            "def buggy_func():\n    return reserve(n - 1)\n",
            "add buggy func",
        )
        # File survives; only buggy_func is removed (other_func remains).
        removing = _commit_file(
            tmp_path,
            "src/mod.py",
            "def other_func():\n    return reserve(n)\n",
            "remove buggy_func, keep module",
        )

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1494,
            "title": "buggy_func miscounts",
            "body": "buggy_func reserves the wrong number of slots\n",
            "state": "OPEN",
        }
        # Confirmed cause, NO premise_anchors, symbol locator in affected path.
        payload = {
            "observed_symptom": "s",
            "reproduction_or_evidence": "r",
            "hypotheses": [
                {
                    "statement": "h",
                    "status": "confirmed",
                    "evidence": "e",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "off-by-one in buggy_func",
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": "src/mod.py:buggy_func",
            "fix_success_criterion": "c",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1494,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert not mock_edit.called, "must not land a ## Diagnosis section"
        assert not mock_post.called
        assert result.state.absent_premises
        absent = result.state.absent_premises[0]
        assert absent.file == "src/mod.py"
        assert absent.pattern == "buggy_func"
        assert absent.removing_commit.startswith(removing[:8])

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_removed_symbol_in_path_line_symbol_prose_reports_already_resolved(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Regression for #1312's recorded prose shape: ``path:line, symbol``
        must resolve the removed symbol even when the file still exists and the
        diagnosis emitted no premise anchors."""
        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/theforge/config/load.py",
            "def _validate_auto_api_fallback_schema():\n    return None\n",
            "add auto api fallback validator",
        )
        removing = _commit_file(
            tmp_path,
            "src/theforge/config/load.py",
            "def other_func():\n    return None\n",
            "remove auto api fallback validator",
        )

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1312,
            "title": "stale removed premise",
            "body": "The auto_api_fallback validator misbehaves.\n",
            "state": "OPEN",
        }
        payload = {
            "observed_symptom": "s",
            "reproduction_or_evidence": "r",
            "hypotheses": [
                {
                    "statement": "h",
                    "status": "confirmed",
                    "evidence": "e",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "",
            "confirmed_cause_support": (
                "The cited premise was removed by a named commit, so this is already resolved."
            ),
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": (
                "None - the cited code path "
                "(src/theforge/config/load.py:301, _validate_auto_api_fallback_schema) "
                "does not exist at HEAD. It was removed by commit "
                f"{removing[:8]}."
            ),
            "fix_success_criterion": "c",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1312,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert not mock_edit.called
        assert not mock_post.called
        assert result.state.absent_premises
        absent = result.state.absent_premises[0]
        assert absent.file == "src/theforge/config/load.py"
        assert absent.pattern == "_validate_auto_api_fallback_schema"
        assert absent.removing_commit.startswith(removing[:8])

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_removed_public_symbol_in_path_line_symbol_prose_reports_already_resolved(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Public identifiers in the same ``path:line, symbol`` prose shape
        must be treated the same way as underscore-prefixed symbols."""
        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/theforge/config/load.py",
            "def validate_auto_api_fallback_schema():\n    return None\n",
            "add public auto api fallback validator",
        )
        removing = _commit_file(
            tmp_path,
            "src/theforge/config/load.py",
            "def other_func():\n    return None\n",
            "remove public auto api fallback validator",
        )

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1313,
            "title": "stale removed public premise",
            "body": "The public auto_api_fallback validator misbehaves.\n",
            "state": "OPEN",
        }
        payload = {
            "observed_symptom": "s",
            "reproduction_or_evidence": "r",
            "hypotheses": [
                {
                    "statement": "h",
                    "status": "confirmed",
                    "evidence": "e",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "",
            "confirmed_cause_support": (
                "The cited premise was removed by a named commit, so this is already resolved."
            ),
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": (
                "None - the cited code path "
                "(src/theforge/config/load.py:301, validate_auto_api_fallback_schema) "
                "does not exist at HEAD. It was removed by commit "
                f"{removing[:8]}."
            ),
            "fix_success_criterion": "c",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1313,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert not mock_edit.called
        assert not mock_post.called
        assert result.state.absent_premises
        absent = result.state.absent_premises[0]
        assert absent.file == "src/theforge/config/load.py"
        assert absent.pattern == "validate_auto_api_fallback_schema"
        assert absent.removing_commit.startswith(removing[:8])

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_present_symbol_in_affected_path_lands_normally(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        """A symbol locator in affected_code_path that still exists must NOT
        divert: behavior unchanged for a live bug cited as ``path:symbol``."""
        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/mod.py",
            "def buggy_func():\n    return reserve(n - 1)\n",
            "add buggy func",
        )

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 301,
            "title": "buggy_func miscounts",
            "body": (
                "## Observed\n\n"
                "buggy_func reserves the wrong number of slots.\n\n"
                "## Expected\n\n"
                "buggy_func should reserve the requested number of slots.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        payload = {
            "observed_symptom": "s",
            "reproduction_or_evidence": "r",
            "hypotheses": [
                {
                    "statement": "h",
                    "status": "confirmed",
                    "evidence": "e",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "off-by-one in buggy_func",
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": "src/mod.py:buggy_func",
            "fix_success_criterion": "c",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=301,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert mock_edit.called

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_deleted_affected_file_reports_already_resolved(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        """A confirmed cause whose affected_code_path file was deleted entirely
        must report already-resolved, naming the deleting commit — even without
        premise anchors (AC: a cited code path must currently exist)."""
        _init_repo(tmp_path)
        # A second file keeps the repo non-empty after deletion.
        _commit_file(tmp_path, "keep.txt", "keep\n", "seed")
        _commit_file(tmp_path, "src/gone.py", "def gone():\n    pass\n", "add gone")
        deleting = _remove_file(tmp_path, "src/gone.py", "delete gone module")

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 200,
            "title": "gone module bug",
            "body": "src/gone.py misbehaves\n",
            "state": "OPEN",
        }
        # No premise_anchors — rely on affected_code_path file-existence check.
        payload = {
            "observed_symptom": "s",
            "reproduction_or_evidence": "r",
            "hypotheses": [
                {
                    "statement": "h",
                    "status": "confirmed",
                    "evidence": "e",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "cause in gone module",
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": "src/gone.py:1",
            "fix_success_criterion": "c",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=200,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert not mock_edit.called
        assert result.state.absent_premises[0].removing_commit.startswith(deleting[:8])

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_deleted_affected_file_with_line_and_prose_still_reports_already_resolved(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        """A deleted-file citation with a ``path:line`` locator followed by prose
        must still preserve the file premise and divert to already-resolved."""
        _init_repo(tmp_path)
        _commit_file(tmp_path, "keep.txt", "keep\n", "seed")
        _commit_file(tmp_path, "src/deleted_mod.py", "def gone():\n    pass\n", "add deleted mod")
        deleting = _remove_file(tmp_path, "src/deleted_mod.py", "delete deleted module")

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 201,
            "title": "deleted module bug",
            "body": "src/deleted_mod.py misbehaves\n",
            "state": "OPEN",
        }
        payload = {
            "observed_symptom": "s",
            "reproduction_or_evidence": "r",
            "hypotheses": [
                {
                    "statement": "h",
                    "status": "confirmed",
                    "evidence": "e",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "",
            "confirmed_cause_support": "The module was removed before this diagnosis landed.",
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": (
                "src/deleted_mod.py:42, removed entirely in an earlier refactor."
            ),
            "fix_success_criterion": "c",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=201,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert not mock_edit.called
        assert result.state.absent_premises[0].file == "src/deleted_mod.py"
        assert result.state.absent_premises[0].pattern == ""
        assert result.state.absent_premises[0].removing_commit.startswith(deleting[:8])

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_removed_symbol_in_path_line_in_symbol_prose_reports_already_resolved(
        self, mock_agent, mock_fetch, mock_edit, tmp_path
    ):
        """Compact ``path:line in symbol`` phrasing should verify the removed
        symbol rather than falling back to a live diagnosis."""
        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/mod.py",
            "def removed_func():\n    return reserve(n - 1)\n",
            "add removed func",
        )
        removing = _commit_file(
            tmp_path,
            "src/mod.py",
            "def other_func():\n    return reserve(n)\n",
            "remove removed_func, keep module",
        )

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 202,
            "title": "removed symbol bug",
            "body": "removed_func reserves the wrong number of slots\n",
            "state": "OPEN",
        }
        payload = {
            "observed_symptom": "s",
            "reproduction_or_evidence": "r",
            "hypotheses": [
                {
                    "statement": "h",
                    "status": "confirmed",
                    "evidence": "e",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "",
            "confirmed_cause_support": "The cited symbol was removed by a named commit.",
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": "src/mod.py:301 in removed_func.",
            "fix_success_criterion": "c",
        }
        mock_agent.return_value = _fake_agent_result(
            f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=202,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert not mock_edit.called
        assert result.state.absent_premises[0].file == "src/mod.py"
        assert result.state.absent_premises[0].pattern == "removed_func"
        assert result.state.absent_premises[0].removing_commit.startswith(removing[:8])

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_present_premise_lands_normally(self, mock_agent, mock_fetch, mock_edit, tmp_path):
        """Given a still-present bug, behavior is unchanged: the premise check
        passes and the confirmed-cause diagnosis lands normally."""
        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/mod.py",
            "def buggy_func():\n    return reserve(n - 1)\n",
            "add buggy func",
        )

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 300,
            "title": "buggy func miscounts",
            "body": (
                "## Observed\n\n"
                "The buggy_func reserves the wrong number of slots.\n\n"
                "## Expected\n\n"
                "buggy_func should reserve the requested number of slots.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_with_anchor(
                affected="src/mod.py:1",
                anchor_file="src/mod.py",
                anchor_pattern="def buggy_func",
            )
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=300,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
        )

        assert result.success
        assert result.state.phase == DiagnosePhase.DONE
        assert mock_edit.called
        new_body = mock_edit.call_args[0][1]
        assert "## Diagnosis" in new_body

    def test_verify_premise_fails_open_without_baseline(self):
        """No baseline SHA (non-git checkout) → never diverts a live diagnosis."""
        from theforge.coordinator.diagnose_flow import verify_premise
        from theforge.diagnose_types import DiagnosisArtifact, Hypothesis, PremiseAnchor

        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="src/anything.py:10",
            fix_success_criterion="f",
            premise_anchors=(PremiseAnchor(file="src/anything.py", pattern="def x"),),
        )
        verdict = verify_premise(artifact, "", Path("/nonexistent"))
        assert verdict.resolved is False
        assert verdict.unable_to_check == (
            UncheckedPremise(
                file="src/anything.py",
                pattern="def x",
                reason="baseline SHA unavailable; premise not checked",
            ),
            UncheckedPremise(
                file="src/anything.py",
                pattern="",
                reason="baseline SHA unavailable; affected code path not checked",
            ),
        )

    def test_verify_premise_fails_open_when_pattern_never_existed(self, tmp_path):
        """A pattern that has no removal history yields no removing commit, so the
        check fails open rather than fabricating an already-resolved verdict."""
        from theforge.coordinator.diagnose_flow import verify_premise
        from theforge.diagnose_types import DiagnosisArtifact, Hypothesis, PremiseAnchor

        _init_repo(tmp_path)
        _commit_file(tmp_path, "src/mod.py", "def present():\n    pass\n", "seed")
        head = _git(["rev-parse", "HEAD"], tmp_path)

        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="src/mod.py",
            fix_success_criterion="f",
            # Pattern that was never in the file — absent but no removal commit.
            premise_anchors=(PremiseAnchor(file="src/mod.py", pattern="never_here"),),
        )
        verdict = verify_premise(artifact, head, tmp_path)
        assert verdict.resolved is False
        assert verdict.unable_to_check == (
            UncheckedPremise(
                file="src/mod.py",
                pattern="never_here",
                reason="pattern absent at baseline but no removing commit could be identified",
            ),
        )

    def test_verify_premise_drops_prose_path_candidates_never_tracked_by_git(self, tmp_path):
        """Looser affected_code_path parsing must not turn arbitrary prose into
        unchecked premises when the path token never existed in repo history."""
        from theforge.coordinator.diagnose_flow import verify_premise
        from theforge.diagnose_types import DiagnosisArtifact, Hypothesis

        _init_repo(tmp_path)
        _commit_file(tmp_path, "src/mod.py", "def present():\n    pass\n", "seed")
        head = _git(["rev-parse", "HEAD"], tmp_path)

        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path=(
                "See load.py:301, parse for background, but the live bug is elsewhere."
            ),
            fix_success_criterion="f",
        )
        verdict = verify_premise(artifact, head, tmp_path)
        assert verdict.resolved is False
        assert verdict.absent == ()
        assert verdict.unable_to_check == ()

    def test_verify_premise_keeps_never_tracked_cited_path_in_unchecked_premises(self, tmp_path):
        """A compact path-bearing citation that never existed must still leave
        audit-visible trace instead of being dropped entirely."""
        from theforge.coordinator.diagnose_flow import verify_premise
        from theforge.diagnose_types import DiagnosisArtifact, Hypothesis

        _init_repo(tmp_path)
        _commit_file(tmp_path, "src/mod.py", "def present():\n    pass\n", "seed")
        head = _git(["rev-parse", "HEAD"], tmp_path)

        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="src/never_tracked.py:17, deleted_func",
            fix_success_criterion="f",
        )
        verdict = verify_premise(artifact, head, tmp_path)
        assert verdict.resolved is False
        assert verdict.absent == ()
        assert verdict.unable_to_check == (
            UncheckedPremise(
                file="src/never_tracked.py",
                pattern="deleted_func",
                reason="cited path absent at baseline and not present in reachable git history",
            ),
        )

    def test_verify_premise_ignores_sentence_word_after_line_locator(self, tmp_path):
        """A comma-suffix prose word after ``path:line`` is not a checkable symbol
        premise when the cited file history never referenced it."""
        from theforge.coordinator.diagnose_flow import verify_premise
        from theforge.diagnose_types import DiagnosisArtifact, Hypothesis

        _init_repo(tmp_path)
        _commit_file(
            tmp_path,
            "src/mod.py",
            "def present_func():\n    return 1\n",
            "seed",
        )
        head = _git(["rev-parse", "HEAD"], tmp_path)

        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="src/mod.py:120, unchanged.",
            fix_success_criterion="f",
        )
        verdict = verify_premise(artifact, head, tmp_path)
        assert verdict.resolved is False
        assert verdict.absent == ()
        assert verdict.unable_to_check == ()


class TestParsePremiseAnchors:
    def test_parses_premise_anchors(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: c\naffected_code_path: p\nfix_success_criterion: f\n"
            "premise_anchors:\n  - file: src/mod.py\n    pattern: def buggy\n"
            "  - file: src/other.py\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert len(artifact.premise_anchors) == 2
        assert artifact.premise_anchors[0].file == "src/mod.py"
        assert artifact.premise_anchors[0].pattern == "def buggy"
        assert artifact.premise_anchors[1].pattern == ""

    def test_missing_premise_anchors_is_empty_tuple(self):
        artifact = parse_diagnose_output(_agent_yaml_output(), issue_number=1)
        assert artifact is not None
        assert artifact.premise_anchors == ()


class TestParseRelatedFindings:
    def test_parses_related_findings_out_of_confirmed_cause(self):
        # Boundary discipline: an adjacent defect is surfaced as a separate
        # related finding, NOT folded into confirmed_cause.
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: the retry is missing\naffected_code_path: p\n"
            "fix_success_criterion: f\n"
            "related_findings:\n"
            "  - summary: no process-group isolation on subprocess kill\n"
            "    related: '#1649'\n"
            "  - summary: a second adjacent problem\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1672)
        assert artifact is not None
        assert len(artifact.related_findings) == 2
        assert artifact.related_findings[0].related == "#1649"
        assert "process-group" in artifact.related_findings[0].summary
        assert artifact.related_findings[1].related == ""
        # The adjacent problem must not have leaked into the fix scope.
        assert "process-group" not in artifact.confirmed_cause
        assert artifact.confirmed_cause == "the retry is missing"

    def test_related_findings_accepts_bare_string_entries(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: c\naffected_code_path: p\nfix_success_criterion: f\n"
            "related_findings:\n  - just a bare string finding\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert len(artifact.related_findings) == 1
        assert artifact.related_findings[0].summary == "just a bare string finding"
        assert artifact.related_findings[0].related == ""

    def test_missing_related_findings_is_empty_tuple(self):
        artifact = parse_diagnose_output(_agent_yaml_output(), issue_number=1)
        assert artifact is not None
        assert artifact.related_findings == ()

    def test_blank_related_summaries_dropped_and_deduped(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: c\naffected_code_path: p\nfix_success_criterion: f\n"
            "related_findings:\n"
            "  - summary: '   '\n    related: '#9'\n"
            "  - summary: dup\n    related: '#1'\n"
            "  - summary: dup\n    related: '#1'\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert len(artifact.related_findings) == 1
        assert artifact.related_findings[0].summary == "dup"


class TestParseAdvisoryRepairProposal:
    def test_parses_advisory_repair_proposal(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: c\naffected_code_path: p\nfix_success_criterion: f\n"
            "advisory_repair_proposal: |\n"
            "  Likely belongs in runners/api.py instead of knowledge_summary.py.\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=2501)
        assert artifact is not None
        assert artifact.advisory_repair_proposal.startswith("Likely belongs in runners/api.py")

    def test_missing_advisory_repair_proposal_is_empty_string(self):
        artifact = parse_diagnose_output(_agent_yaml_output(), issue_number=1)
        assert artifact is not None
        assert artifact.advisory_repair_proposal == ""

    def test_null_advisory_repair_proposal_is_empty_string(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: c\naffected_code_path: p\nfix_success_criterion: f\n"
            "advisory_repair_proposal:\n"
            "notes:\n"
            "confirmed_cause_support:\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert artifact.advisory_repair_proposal == ""
        assert artifact.notes == ""
        assert artifact.confirmed_cause_support == ""

    def test_prompt_contract_types_advisory_field_and_notes_boundary(self):
        prompt = build_diagnose_prompt(issue_number=1, title="t", body="b", mode="autonomous")
        assert "advisory_repair_proposal" in prompt
        assert "Content class: advisory, unverified repair proposal." in prompt
        assert "Do NOT put repair" in prompt
        assert "proposals here." in prompt


class TestParseSupportProvenance:
    def test_parses_support_provenance_fields(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n"
            "  - statement: a\n    status: confirmed\n    evidence: e\n"
            "    claim_verification:\n"
            "      verification_type: attached_evidence\n"
            "      detail: Missing local artifact.\n"
            "    evidence_provenance:\n"
            "      source_type: prior_assertion\n"
            "      detail: Earlier diagnosis already stated it.\n"
            "confirmed_cause: c\n"
            "confirmed_cause_support: commit message already states the same cause\n"
            "confirmed_cause_verification:\n"
            "  verification_type: source\n"
            "  detail: Checked in source.\n"
            "confirmed_cause_support_provenance:\n"
            "  source_type: mixed\n"
            "  detail: Mix of reproduced failure and operator note.\n"
            "affected_code_path: p\nfix_success_criterion: f\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert artifact.hypotheses[0].claim_verification.verification_type == "attached_evidence"
        assert artifact.hypotheses[0].claim_verification.detail == "Missing local artifact."
        assert artifact.hypotheses[0].evidence_provenance.source_type == "prior_assertion"
        assert artifact.hypotheses[0].evidence_provenance.detail == (
            "Earlier diagnosis already stated it."
        )
        assert artifact.confirmed_cause_support == "commit message already states the same cause"
        assert artifact.confirmed_cause_verification.verification_type == "source"
        assert artifact.confirmed_cause_verification.detail == "Checked in source."
        assert artifact.confirmed_cause_support_provenance.source_type == "mixed"
        assert artifact.confirmed_cause_support_provenance.detail == (
            "Mix of reproduced failure and operator note."
        )

    def test_missing_support_provenance_defaults_to_unknown(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n  - statement: a\n    status: confirmed\n    evidence: e\n"
            "confirmed_cause: c\naffected_code_path: p\nfix_success_criterion: f\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert artifact.hypotheses[0].claim_verification == ClaimVerification()
        assert artifact.hypotheses[0].evidence_provenance == SupportProvenance()
        assert artifact.confirmed_cause_support == ""
        assert artifact.confirmed_cause_verification == ClaimVerification()
        assert artifact.confirmed_cause_support_provenance == SupportProvenance()

    def test_null_or_empty_provenance_detail_does_not_stringify_none(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n"
            "  - statement: a\n    status: confirmed\n    evidence: e\n"
            "    claim_verification:\n"
            "      verification_type:\n"
            "      detail:\n"
            "    evidence_provenance:\n"
            "      source_type:\n"
            "      detail:\n"
            "confirmed_cause: c\n"
            "confirmed_cause_verification:\n"
            "  verification_type:\n"
            "  detail:\n"
            "confirmed_cause_support_provenance:\n"
            "  source_type:\n"
            "  detail:\n"
            "affected_code_path: p\nfix_success_criterion: f\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert artifact.hypotheses[0].claim_verification == ClaimVerification()
        assert artifact.hypotheses[0].evidence_provenance == SupportProvenance()
        assert artifact.confirmed_cause_verification == ClaimVerification()
        assert artifact.confirmed_cause_support_provenance == SupportProvenance()


class TestParseSymptomScopeCoverage:
    def test_parses_categorical_scope_coverage(self):
        payload = (
            "observed_symptom: s\nreproduction_or_evidence: r\n"
            "hypotheses:\n"
            "  - statement: a\n    status: confirmed\n    evidence: e\n"
            "    claim_verification:\n"
            "      verification_type: source\n"
            "      detail: Checked against the target repository source.\n"
            "confirmed_cause: c\n"
            "confirmed_cause_verification:\n"
            "  verification_type: source\n"
            "  detail: Checked against the target repository source.\n"
            "affected_code_path: p\nfix_success_criterion: f\n"
            "symptom_scope_coverage:\n"
            "  symptom_is_categorical: true\n"
            "  stated_scope: every sibling renderer\n"
            "  examined_locations:\n"
            "    - location: src/foo.py:render_cli\n"
            "      status: covered\n"
            "      rationale: same helper omitted the field\n"
            "    - location: src/foo.py:render_web\n"
            "      status: excluded\n"
            "      rationale: sibling checked; different serializer already includes it\n"
        )
        artifact = parse_diagnose_output(payload, issue_number=1)
        assert artifact is not None
        assert artifact.symptom_scope_coverage.symptom_is_categorical is True
        assert artifact.symptom_scope_coverage.stated_scope == "every sibling renderer"
        assert len(artifact.symptom_scope_coverage.examined_locations) == 2
        assert artifact.symptom_scope_coverage.examined_locations[0].status == "covered"
        assert artifact.symptom_scope_coverage.examined_locations[1].status == "excluded"
        assert artifact.is_complete()

    def test_missing_scope_coverage_stays_non_categorical(self):
        artifact = parse_diagnose_output(_agent_yaml_output(), issue_number=1)
        assert artifact is not None
        assert artifact.symptom_scope_coverage == SymptomScopeCoverage()

    def test_categorical_scope_without_examined_locations_is_incomplete(self):
        artifact = parse_diagnose_output(
            _agent_yaml_output(
                symptom_scope_coverage={
                    "symptom_is_categorical": True,
                    "stated_scope": "every sibling renderer",
                    "examined_locations": [],
                }
            ),
            issue_number=1,
        )
        assert artifact is not None
        assert artifact.symptom_scope_coverage.symptom_is_categorical is True
        assert not artifact.is_complete()


class TestBuildDiagnoseProfile:
    def test_default_uses_config_timeout(self, tmp_path):
        from theforge.coordinator import diagnose_flow

        config = _make_config(tmp_path)
        profile = diagnose_flow._build_diagnose_profile(config)
        assert profile.timeout_seconds == config.diagnose.timeout_seconds

    def test_override_replaces_config_timeout(self, tmp_path):
        from theforge.coordinator import diagnose_flow

        config = _make_config(tmp_path)
        profile = diagnose_flow._build_diagnose_profile(config, timeout_seconds=42.0)
        assert profile.timeout_seconds == 42.0
        assert profile.timeout_seconds != config.diagnose.timeout_seconds


class TestDiagnoseHeartbeat:
    """The investigative agent runs for up to the diagnose timeout; the flow must
    emit periodic progress so a live run is distinguishable from a hang."""

    def test_heartbeat_emitted_while_agent_runs(self, tmp_path):
        import time as _time

        from theforge.coordinator import diagnose_flow

        profile = diagnose_flow._build_diagnose_profile(_make_config(tmp_path))

        def _slow_agent(**_kwargs):
            _time.sleep(0.18)
            return _fake_agent_result(_agent_yaml_output())

        with (
            patch.object(diagnose_flow, "run_agent", _slow_agent),
            patch.object(diagnose_flow, "_progress_log") as mock_log,
        ):
            result = diagnose_flow._run_agent_with_heartbeat(
                prompt="p",
                profile=profile,
                working_dir=tmp_path,
                secrets=None,
                heartbeat_interval_s=0.05,
            )

        assert result is not None
        heartbeats = [c.args[0] for c in mock_log.call_args_list]
        assert heartbeats, "expected at least one heartbeat line"
        assert all("still investigating" in line for line in heartbeats)
        assert any("elapsed" in line for line in heartbeats)

    def test_no_heartbeat_when_agent_returns_fast(self, tmp_path):
        from theforge.coordinator import diagnose_flow

        profile = diagnose_flow._build_diagnose_profile(_make_config(tmp_path))

        def _fast_agent(**_kwargs):
            return _fake_agent_result(_agent_yaml_output())

        with (
            patch.object(diagnose_flow, "run_agent", _fast_agent),
            patch.object(diagnose_flow, "_progress_log") as mock_log,
        ):
            diagnose_flow._run_agent_with_heartbeat(
                prompt="p",
                profile=profile,
                working_dir=tmp_path,
                secrets=None,
                heartbeat_interval_s=5.0,
            )

        assert not mock_log.called

    def test_agent_exception_is_reraised(self, tmp_path):
        from theforge.coordinator import diagnose_flow

        profile = diagnose_flow._build_diagnose_profile(_make_config(tmp_path))

        def _boom(**_kwargs):
            raise RuntimeError("agent blew up")

        with patch.object(diagnose_flow, "run_agent", _boom):
            try:
                diagnose_flow._run_agent_with_heartbeat(
                    prompt="p",
                    profile=profile,
                    working_dir=tmp_path,
                    secrets=None,
                    heartbeat_interval_s=0.05,
                )
            except RuntimeError as exc:
                assert "agent blew up" in str(exc)
            else:
                raise AssertionError("expected RuntimeError to propagate")

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_flow_emits_heartbeat_during_investigate(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        """Seam test: a full diagnose run whose INVESTIGATE agent is slow must
        surface heartbeat lines between the runner's start line and the result."""
        import time as _time

        from theforge.coordinator import diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1421,
            "title": "silent diagnose",
            "body": "no output while model works",
            "state": "OPEN",
        }
        mock_post.return_value = "https://example/comment"

        def _slow_agent(**_kwargs):
            _time.sleep(0.18)
            return _fake_agent_result(_agent_yaml_output())

        mock_agent.side_effect = _slow_agent

        with (
            patch.object(diagnose_flow, "_DIAGNOSE_HEARTBEAT_INTERVAL_S", 0.05),
            patch.object(diagnose_flow, "_progress_log") as mock_log,
        ):
            result = diagnose_flow.run_diagnose_flow(
                issue_number=1421,
                config=config,
                project_root=tmp_path,
                output_destination="comment",
            )

        assert result.success
        heartbeats = [
            c.args[0] for c in mock_log.call_args_list if "still investigating" in c.args[0]
        ]
        assert heartbeats, "expected heartbeat lines during INVESTIGATE"


# ── Attached evidence (issue filed by `forge report` elsewhere) ────────


def _attached_report(
    *,
    artifacts: tuple[tuple[str, str, str], ...],
    missing: tuple[tuple[str, str, str], ...] = (),
    attach_all: bool = True,
) -> tuple[str, list[dict]]:
    """Render an issue the way ``forge report`` files one, from another project.

    ``artifacts``/``missing`` are ``(kind, name, content|reason)`` triples.
    Returns ``(body, comments)`` in the shape ``gh issue view`` returns.
    """
    from theforge.reporting.evidence import EvidenceArtifact, MissingEvidence, RunEvidence
    from theforge.reporting.render import (
        Diagnosis,
        Publication,
        build_evidence_chunks,
        render_issue_body,
    )

    evidence = RunEvidence(
        run_id="f5aa21cf2d8d",
        run_kind="sprint",
        forge_version="v0.14.2",
        observed_project="fuzzypete/hdp",
        sprint_name="nightly",
        sprint_id="0f0f0f0f0f0f",
        story_slugs=("issue-9",),
        story_run_ids=("aaaaaaaaaaaa",),
        config_summary="resolved snapshot attached (12 recorded keys)",
        artifacts=tuple(
            EvidenceArtifact(kind=kind, name=name, content=content)
            for kind, name, content in artifacts
        ),
        missing=tuple(
            MissingEvidence(kind=kind, name=name, reason=reason) for kind, name, reason in missing
        ),
    )
    chunks, _dropped = build_evidence_chunks(evidence)
    posted = chunks if attach_all else chunks[:-1]
    publication = Publication(
        expected=tuple(c.label for c in chunks),
        posted=tuple(c.label for c in posted),
        started=True,
    )
    body = render_issue_body(
        evidence,
        description="Layer-3 injection did not fire for this run.",
        diagnosis=Diagnosis(symptom="no injection banner in the observed run log"),
        publication=publication,
    )
    return body, [{"body": c.body} for c in posted]


def _plant_contradictory_local_state(root: Path) -> None:
    """Local state whose answers contradict the attached run's, at every layer."""
    (root / ".forge" / "logs" / "nightly").mkdir(parents=True, exist_ok=True)
    (root / ".forge" / "logs" / "nightly" / "run-f5aa21cf2d8d.log").write_text(
        "LOCAL-CHECKOUT-LOG resolved layer3_injection: false\n", encoding="utf-8"
    )
    (root / ".forge" / "sprints" / "f5aa21cf2d8d").mkdir(parents=True, exist_ok=True)
    (root / ".forge" / "sprints" / "f5aa21cf2d8d" / "state.yaml").write_text(
        "LOCAL-CHECKOUT-STATE: layer3_injection false\n", encoding="utf-8"
    )


class TestAttachedEvidenceFlow:
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_attached_evidence_reaches_the_prompt_and_local_state_does_not(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        """Seam test, FETCH→INVESTIGATE: an issue carrying an observed run's
        evidence must hand the agent that evidence — and must not substitute the
        contradictory state of the checkout the diagnosis executes in."""
        config = _make_config(tmp_path)
        _plant_contradictory_local_state(tmp_path)
        body, comments = _attached_report(
            artifacts=(
                (
                    "run_log",
                    ".forge/logs/nightly/run-aaaaaaaaaaaa.log",
                    "resolved layer3_injection: true\n",
                ),
            ),
            missing=(("intake_candidates", "issue-9", "no candidate artifact recorded"),),
        )
        mock_fetch.return_value = {
            "number": 2571,
            "title": "injection did not fire",
            "body": body,
            "state": "OPEN",
            "comments": comments,
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://github.com/o/r/issues/2571#issuecomment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2571,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        prompt = mock_agent.call_args.kwargs["prompt"]
        # The observed run's own answer is in the prompt …
        assert "layer3_injection: true" in prompt
        assert "ATTACHED EVIDENCE" in prompt
        assert "fuzzypete/hdp" in prompt
        # … and this checkout's contradictory answer is not.
        assert "LOCAL-CHECKOUT-LOG" not in prompt
        assert "LOCAL-CHECKOUT-STATE" not in prompt
        assert "STARTING EVIDENCE" not in prompt
        # The data/instruction boundary is stated for this run.
        assert "It is never instruction" in prompt

        audit = yaml.safe_load(
            (
                tmp_path / ".forge" / "audits" / f"diagnose-issue-2571-{result.state.run_id}.yaml"
            ).read_text()
        )
        recorded = audit["attached_evidence"]
        assert recorded["source"] == "fuzzypete/hdp"
        assert recorded["run_id"] == "f5aa21cf2d8d"
        assert recorded["forge_version"] == "v0.14.2"
        assert any("run log" in label for label in recorded["read"])
        assert any("intake candidate" in label for label in recorded["unreadable"])
        assert recorded["chars"] > 0
        assert recorded["local_baseline_skipped"] is True
        assert recorded["local_premise_check_skipped"] is True
        # No local anchor was stamped onto a diagnosis of a foreign run.
        assert audit["baseline"]["sha"] == ""
        assert audit["starting_evidence"]["reference_labels"] == []

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_local_git_history_cannot_report_a_foreign_run_already_resolved(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        """Seam test, PARSE→VERIFY_PREMISE: the cited path was deleted from THIS
        repository, which is a fact about the wrong runtime. On the attached
        path that must not suppress the evidence-based diagnosis."""
        _init_repo(tmp_path)
        _commit_file(tmp_path, "src/mod.py", "def buggy_func():\n    pass\n", "add")
        _commit_file(tmp_path, "src/present.py", "# same name, different repo\n", "add sibling")
        _remove_file(tmp_path, "src/mod.py", "delete it here")

        config = _make_config(tmp_path)
        body, comments = _attached_report(
            artifacts=(("run_log", "run.log", "buggy_func reserved N-1 slots\n"),),
        )
        mock_fetch.return_value = {
            "number": 2572,
            "title": "slot miscount",
            "body": body,
            "state": "OPEN",
            "comments": comments,
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_with_anchor(
                affected="src/mod.py:buggy_func",
                anchor_file="src/mod.py",
                anchor_pattern="def buggy_func",
                # A path that also exists HERE. Hashing it locally would attach
                # this checkout's content digest to a foreign run's diagnosis.
                inspected=("src/present.py",),
            )
        )
        mock_post.return_value = "https://github.com/o/r/issues/2572#issuecomment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2572,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success, result.message
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.already_resolved is False
        assert mock_post.called
        # Nothing from this checkout's git was stamped onto the artifact.
        assert result.state.artifact is not None
        assert result.state.artifact.baseline_sha == ""
        inspected = result.state.artifact.inspected_files
        assert [f.path for f in inspected] == ["src/present.py"]
        assert all(f.content_sha256 == "" for f in inspected)
        posted_body = mock_post.call_args[0][1]
        assert "### Premise verification" in posted_body
        assert "premise check skipped" in posted_body
        audit = yaml.safe_load(
            (
                tmp_path / ".forge" / "audits" / f"diagnose-issue-2572-{result.state.run_id}.yaml"
            ).read_text()
        )
        assert audit["unchecked_premises"]
        assert audit["artifact"]["unchecked_premises"]

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_local_git_history_still_reports_already_resolved_without_attachment(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Companion to the test above: with no attached evidence the local
        premise check is unchanged — the same deletion still diverts."""
        _init_repo(tmp_path)
        _commit_file(tmp_path, "src/mod.py", "def buggy_func():\n    pass\n", "add")
        removing = _remove_file(tmp_path, "src/mod.py", "delete it here")

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 2573,
            "title": "slot miscount",
            "body": "buggy_func reserves the wrong number of slots.\n",
            "state": "OPEN",
            "comments": [],
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_with_anchor(
                affected="src/mod.py:buggy_func",
                anchor_file="src/mod.py",
                anchor_pattern="def buggy_func",
            )
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2573,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        assert removing[:12] in result.message
        assert not mock_post.called

    @patch("theforge.coordinator.diagnose_flow._gh_edit_body")
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_attached_evidence_reports_skipped_premise_when_affected_path_is_only_prose(
        self, mock_agent, mock_fetch, mock_post, mock_edit, tmp_path
    ):
        """Seam test: attached-evidence diagnoses with prose-only affected paths
        must still surface that premise verification was skipped."""
        _init_repo(tmp_path)
        config = _make_config(tmp_path)
        body, comments = _attached_report(
            artifacts=(("run_log", "run.log", "reservation helper dropped one slot\n"),),
        )
        mock_fetch.return_value = {
            "number": 2574,
            "title": "slot miscount",
            "body": body,
            "state": "OPEN",
            "comments": comments,
        }
        mock_agent.return_value = _fake_agent_result(
            """```yaml
observed_symptom: Slot count is wrong
reproduction_or_evidence: Attached run record shows the mismatch
hypotheses:
  - statement: Reservation logic is off by one
    status: confirmed
    evidence: The attached audit shows one fewer slot than requested
    claim_verification:
      verification_type: attached_evidence
      detail: Verified against the attached run record.
confirmed_cause: Reservation loop drops the last slot
confirmed_cause_verification:
  verification_type: attached_evidence
  detail: Verified against the attached run record.
affected_code_path: >-
  The reservation helper in the slot allocator computes one fewer slot
  than requested.
fix_success_criterion: >-
  Attached evidence and follow-up reproduction both show the requested
  slot count.
```"""
        )
        mock_post.return_value = "https://github.com/o/r/issues/2574#issuecomment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2574,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success, result.message
        assert result.state.phase == DiagnosePhase.DONE
        assert mock_post.called
        posted_body = mock_post.call_args[0][1]
        assert "### Premise verification" in posted_body
        assert "premise check skipped" in posted_body
        assert (
            "did not record any premise anchors or file-like affected-code references"
            in posted_body
        )
        audit = yaml.safe_load(
            (
                tmp_path / ".forge" / "audits" / f"diagnose-issue-2574-{result.state.run_id}.yaml"
            ).read_text()
        )
        assert audit["unchecked_premises"] == [
            {
                "file": (
                    "The reservation helper in the slot allocator computes one fewer "
                    "slot than requested."
                ),
                "pattern": "",
                "reason": (
                    "premise check skipped: diagnosis is anchored to attached "
                    "cross-project evidence, so this checkout cannot verify the "
                    "cited code safely; the diagnosis did not record any premise "
                    "anchors or file-like affected-code references"
                ),
            }
        ]

    @patch("theforge.coordinator.diagnose_flow._emit_dry_run")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_already_resolved_report_keeps_unchecked_premises(
        self, mock_agent, mock_fetch, mock_emit_dry_run, tmp_path
    ):
        """Seam test: an already-resolved report must still surface premises the
        same pass could not verify, rather than dropping them from the operator
        report while retaining them only in audit."""
        _init_repo(tmp_path)
        _commit_file(tmp_path, "src/mod.py", "def buggy_func():\n    pass\n", "add")
        _commit_file(tmp_path, "src/other.py", "def present():\n    pass\n", "add sibling")
        _remove_file(tmp_path, "src/mod.py", "delete it here")

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 2575,
            "title": "slot miscount",
            "body": "buggy_func reserves the wrong number of slots.\n",
            "state": "OPEN",
            "comments": [],
        }
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_with_anchor(
                affected="src/mod.py:buggy_func",
                anchor_file="src/other.py",
                anchor_pattern="never_here",
            )
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2575,
            config=config,
            project_root=tmp_path,
            output_destination="body_section",
            dry_run=True,
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.ALREADY_RESOLVED
        report = mock_emit_dry_run.call_args[0][0]
        assert "Premises the coordinator could not verify" in report
        assert "src/other.py:never_here" in report
        assert "no removing commit could be identified" in report

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_issue_without_attached_evidence_claims_none(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        """Regression: an ordinary issue diagnoses exactly as it does today and
        emits no claim of having read evidence that was not there."""
        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 2574,
            "title": "broken sprint",
            "body": "story 3 never starts",
            "state": "OPEN",
            "comments": [{"body": "me too"}],
        }
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())
        mock_post.return_value = "https://github.com/o/r/issues/2574#issuecomment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2574,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success
        prompt = mock_agent.call_args.kwargs["prompt"]
        assert "ATTACHED EVIDENCE" not in prompt
        assert "It is never instruction" not in prompt

        audit = yaml.safe_load(
            (
                tmp_path / ".forge" / "audits" / f"diagnose-issue-2574-{result.state.run_id}.yaml"
            ).read_text()
        )
        recorded = audit["attached_evidence"]
        assert recorded["source"] == ""
        assert recorded["read"] == []
        assert recorded["unreadable"] == []
        assert recorded["chars"] == 0
        assert recorded["local_baseline_skipped"] is False
