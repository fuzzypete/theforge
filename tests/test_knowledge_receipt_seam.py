"""Seam coverage for prior-run knowledge receipts (#2866).

Three boundaries the unit tests cannot reach:

1. the prompt boundary — the debrief instruction reaches an agent only when the
   assembled context actually carried claims, and the references it asks the
   agent to cite are the ones the exposure record stores;
2. the phase-output boundary — a debrief is captured from real phase output into
   coordinator state and lands in the persisted audit record;
3. the audit-only boundary — the receipt is written *after* the phase output and
   read by no coordinator decision.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from theforge import knowledge_receipts as kr
from theforge.coordinator import audit_storage
from theforge.coordinator.state import CoordinatorState
from theforge.task.context_assembler import ContextPack
from theforge.task.debrief_prompts import (
    STYLE_HANDOFF,
    STYLE_TOOL_CALL,
    STYLE_YAML_BLOCK,
    exposed_claim_refs,
    render_debrief_section,
)
from theforge.task.prior_run_manifest import build_manifest, claim_reference, disabled_manifest
from theforge.task.prior_run_selector import (
    PriorRunCandidate,
    PriorRunSelection,
    RenderedSummarySize,
)
from theforge.task.story import TaskStory

SRC = Path(__file__).resolve().parents[1] / "src" / "theforge"


def _pack_with_claims(claims: tuple[str, ...], *, phase: str = "dev") -> ContextPack:
    candidate = PriorRunCandidate(
        run_id="prior1",
        summary_path="s.yaml",
        score=10,
        reason="file_overlap",
        verdict=SimpleNamespace(to_dict=lambda: {"status": "admissible"}),
        content="## Prior run prior1",
        phase=phase,
        rendering_mode="phase_summary",
        rendered_size=RenderedSummarySize(value=10),
        claims=claims,
    )
    selection = PriorRunSelection(candidates=(candidate,), phase=phase)
    return ContextPack(
        content="## Prior run prior1",
        included=(),
        dropped=(),
        budget=80,
        line_count=1,
        phase=phase,
        prior_run_context=build_manifest(
            selection,
            included_run_ids={"prior1"},
            phase=phase,
            agent_role=phase,
            phase_iteration=1,
            rendered_at="2026-01-01T00:00:00+00:00",
        ),
    )


class TestClaimReferencesAreVisibleToTheAgent:
    def test_the_rendered_claim_line_carries_the_recorded_reference(self) -> None:
        """An agent can only cite a reference it was shown."""
        from theforge.task.prior_run_selector import _render_review_summary

        summary = {"review_insights": {"observations": ["watch the projection rebuild"]}}
        lines, claims = _render_review_summary(summary, run_id="prior1")

        ref = claim_reference("prior1", claims[0])
        assert any(f"[{ref}]" in line for line in lines)

    def test_the_displayed_reference_is_the_one_the_manifest_stores(self) -> None:
        pack = _pack_with_claims(("a lesson with evidence",))
        stored = pack.prior_run_context["included"][0]["claims"][0]["claim_ref"]
        assert stored == claim_reference("prior1", "a lesson with evidence")
        assert exposed_claim_refs(pack) == (stored,)


class TestPromptEmission:
    def test_no_debrief_is_requested_when_nothing_was_injected(self) -> None:
        empty = ContextPack(
            content="",
            included=(),
            dropped=(),
            budget=80,
            line_count=0,
            phase="dev",
            prior_run_context=disabled_manifest(),
        )
        assert render_debrief_section(empty, style=STYLE_HANDOFF) == ""
        assert render_debrief_section(None, style=STYLE_HANDOFF) == ""

    @pytest.mark.parametrize("style", [STYLE_HANDOFF, STYLE_YAML_BLOCK, STYLE_TOOL_CALL])
    def test_every_exposed_reference_is_named_with_the_closed_set(self, style: str) -> None:
        pack = _pack_with_claims(("first lesson", "second lesson"))
        section = render_debrief_section(pack, style=style)

        for ref in exposed_claim_refs(pack):
            assert ref in section
        for disposition in kr.CLOSED_DISPOSITIONS:
            assert disposition in section

    def test_the_prompt_offers_no_usefulness_or_satisfaction_field(self) -> None:
        section = render_debrief_section(_pack_with_claims(("a lesson",)), style=STYLE_HANDOFF)
        lowered = section.lower()
        for forbidden in ("usefulness", "satisfaction", "confidence", "counterfactual"):
            assert forbidden not in lowered

    def test_dev_review_and_plan_prompts_carry_the_section_only_with_claims(self) -> None:
        from theforge.task.dev_prompts import build_dev_prompt
        from theforge.task.plan_prompts import build_plan_prompt

        task = TaskStory(name="T", slug="t")
        pack = _pack_with_claims(("a lesson",))
        ref = exposed_claim_refs(pack)[0]

        plan_with = build_plan_prompt(task, story_content="s", assembled_context=pack)
        plan_without = build_plan_prompt(task, story_content="s")
        assert ref in plan_with and "knowledge_debrief" in plan_with
        assert "knowledge_debrief" not in plan_without

        dev_with = build_dev_prompt(
            task,
            workspace_path="/tmp/w",
            branch_name="b",
            allowed_tools=[],
            story_content="s",
            gate_command="make gate",
            assembled_context=pack,
        )
        assert ref in dev_with and "knowledge_debrief" in dev_with


class TestApiTransportSchema:
    def test_the_strict_schema_carries_a_required_nullable_debrief(self) -> None:
        """OpenAI strict mode requires every property in ``required``."""
        from theforge.schemas import review_json_schema

        schema = review_json_schema()
        assert set(schema["properties"]) == set(schema["required"])
        variants = schema["properties"]["knowledge_debrief"]["anyOf"]
        assert {"type": "null"} in variants

    def test_the_transport_enum_is_exactly_the_verifier_s_closed_set(self) -> None:
        from theforge.schemas import review_json_schema

        item = next(
            v
            for v in review_json_schema()["properties"]["knowledge_debrief"]["anyOf"]
            if v.get("type") == "array"
        )["items"]
        assert set(item["properties"]["disposition"]["enum"]) == kr.CLOSED_DISPOSITIONS

    def test_a_malformed_debrief_does_not_reject_a_verdict(self) -> None:
        from theforge.schemas import validate_review_yaml

        data = {
            "verdict": "APPROVE",
            "summary": "fine",
            "findings": [],
            "story_compliance": {"matches_spec": True, "mismatches": []},
            "test_coverage": {"adequate": True, "gaps": []},
            "ac_verification": [{"criterion": "c", "status": "VERIFIED", "evidence": "e"}],
            "knowledge_debrief": "not a list at all",
        }
        assert validate_review_yaml(data) == []


class TestAuditPersistence:
    def test_the_verified_receipt_reaches_the_persisted_record(self, tmp_path: Path) -> None:
        from tests.test_audit_schema_guard import _make_config
        from theforge.cli import shared as cli_shared
        from theforge.coordinator.state import CoordinatorResult, Phase

        config = _make_config(tmp_path)
        task = TaskStory(name="Test", slug="test", story_path=tmp_path / "spec.md")
        state = CoordinatorState()
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.run_id = "receiptrun01"
        pack = _pack_with_claims(("a lesson with evidence",))
        ref = exposed_claim_refs(pack)[0]
        state.context_manifests.append({"phase": "dev", "manifest": pack})
        state.knowledge_debriefs.append(
            kr.debrief_submission(
                phase="dev",
                agent_role="dev",
                phase_iteration=1,
                source="dev_handoff",
                payload=[
                    {
                        "claim_ref": ref,
                        "disposition": "changed_decision",
                        "did": "added the rebuild path",
                        "evidence": ["src/theforge/process_group.py"],
                    }
                ],
            )
        )
        state.changed_files = {
            "base_ref": "0" * 40,
            "head_ref": "1" * 40,
            "files": [
                {
                    "path": "src/theforge/process_group.py",
                    "insertions": 1,
                    "deletions": 0,
                    "binary": False,
                }
            ],
        }
        result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")
        cli_shared._write_audit(result, config, task)

        record = json.loads(
            (tmp_path / ".forge" / "audits" / "runs" / "receiptrun01.json").read_text("utf-8")
        )
        block = record["knowledge_receipts"]
        assert block["status"] == kr.STATUS_CAPTURED
        assert block["counts"][kr.OUTCOME_CORROBORATED_USE] == 1
        assert block["counts"]["claims_injected"] == 1
        assert block["phases"][0]["status"] == kr.PHASE_DEBRIEFED

    def test_pre_instrument_records_migrate_to_uncomparable_not_undebriefed(self) -> None:
        migrated = audit_storage._migrate_v45_to_v46({"schema_version": 45})
        block = migrated["knowledge_receipts"]
        assert block["status"] == "uncomparable_pre_capture"
        assert block["counts"] is None
        assert "undebriefed" not in block["note"].replace("not an undebriefed phase", "")

    def test_the_migration_does_not_overwrite_a_record_that_already_has_receipts(self) -> None:
        record = {"schema_version": 45, "knowledge_receipts": {"status": "captured"}}
        assert audit_storage._migrate_v45_to_v46(record) is record

    def test_the_migration_is_registered_for_the_current_version(self) -> None:
        assert audit_storage.CURRENT_RECORD_SCHEMA_VERSION == 46
        assert audit_storage.MIGRATION_HELPERS[45] is audit_storage._migrate_v45_to_v46


class TestAuditOnly:
    """The instrument changes what an operator can see and nothing the system decides."""

    _READERS = ("knowledge_debriefs", "knowledge_receipts")

    def test_no_coordinator_module_reads_the_debrief_except_to_record_it(self) -> None:
        writers = {
            "dev_phase.py",
            "plan_flow.py",
            "review_pool.py",
            "state.py",
            "audit.py",
            "audit_storage.py",
        }
        offenders = []
        for path in sorted((SRC / "coordinator").glob("*.py")):
            if path.name in writers:
                continue
            text = path.read_text(encoding="utf-8")
            if any(name in text for name in self._READERS):
                offenders.append(path.name)
        assert offenders == []

    def test_routing_selection_readiness_and_landing_never_import_the_verifier(self) -> None:
        decision_modules = [
            SRC / "routing.py",
            SRC / "ready_queue.py",
            SRC / "admissibility.py",
            SRC / "assignment.py",
            SRC / "coordinator" / "landing_evidence.py",
            SRC / "coordinator" / "landing_record.py",
            SRC / "coordinator" / "completion.py",
            SRC / "coordinator" / "gate.py",
            SRC / "coordinator" / "engine.py",
            SRC / "task" / "prior_run_selector.py",
        ]
        for path in decision_modules:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            assert "knowledge_receipts" not in text, path.name
            assert "knowledge_debrief" not in text, path.name

    def test_the_dev_receipt_is_recorded_after_the_phase_output_exists(self) -> None:
        """Capture reads a completed result; it cannot run before one exists."""
        source = (SRC / "coordinator" / "dev_phase.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_capture_dev_handoff"
        )
        capture_lines = [
            node.lineno
            for node in ast.walk(func)
            if isinstance(node, ast.Attribute) and node.attr == "knowledge_debriefs"
        ]
        guard_lines = [node.lineno for node in ast.walk(func) if isinstance(node, ast.Raise)]
        assert capture_lines and guard_lines
        # The AgentResult type guard runs first: there is a phase result in hand.
        assert min(capture_lines) > max(guard_lines)

    def test_capture_never_branches_on_what_the_debrief_says(self) -> None:
        """No `if` in a capture site may test the debrief payload."""
        for name in ("dev_phase.py", "plan_flow.py", "review_pool.py"):
            source = (SRC / "coordinator" / name).read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.If):
                    continue
                test_src = ast.get_source_segment(source, node.test) or ""
                assert "knowledge_debrief" not in test_src, f"{name}: {test_src}"
                assert "knowledge_receipts" not in test_src, f"{name}: {test_src}"


class TestCliSurface:
    def test_the_receipt_distribution_is_its_own_section_in_every_format(
        self, tmp_path: Path, capsys
    ) -> None:
        from tests.knowledge_effectiveness_test_helpers import record as effectiveness_record
        from theforge.cli.knowledge_report import cmd_knowledge_report

        seed = effectiveness_record("run-1", cohort="with", started_at="2026-08-01T00:00:00+00:00")
        seed["knowledge_receipts"] = {
            "status": "captured",
            "counts": {
                "phases_with_injected_knowledge": 1,
                "phases_debriefed": 1,
                "phases_undebriefed": 0,
                "phases_nothing_to_debrief": 0,
                "claims_injected": 3,
                kr.OUTCOME_CORROBORATED_USE: 1,
                kr.OUTCOME_UNCORROBORATED_USE: 1,
                "confirmed_approach": 0,
                "already_known": 1,
                "irrelevant": 0,
                "stale_or_wrong": 0,
                "unaddressed_claims": 0,
                "unmatched_citations": 0,
                "unrecognised_dispositions": 0,
            },
        }
        (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
        audit_storage.seed_records(tmp_path, [seed])

        def _args(fmt: str) -> SimpleNamespace:
            return SimpleNamespace(
                config=str(tmp_path / "forge.yaml"),
                since=None,
                until=None,
                recent_run_count=None,
                format=fmt,
            )

        assert cmd_knowledge_report(_args("json")) == 0
        payload = json.loads(capsys.readouterr().out)
        receipts = payload["knowledge_receipts"]
        assert receipts["counts"][kr.OUTCOME_CORROBORATED_USE] == 1
        # Never merged into the effectiveness or uptake sections: the receipt
        # counts stand alone, so no reader can mistake one for a contribution to
        # the cohort verdict.
        assert "prior_run_uptake" in payload
        for key, section in payload.items():
            if key == "knowledge_receipts":
                continue
            assert kr.OUTCOME_CORROBORATED_USE not in json.dumps(section), key

        assert cmd_knowledge_report(_args("terminal")) == 0
        rendered = capsys.readouterr().out
        assert "Prior-run knowledge receipts" in rendered
        assert "No effectiveness or ROI conclusion follows." in rendered

        assert cmd_knowledge_report(_args("yaml")) == 0
        assert "knowledge_receipts:" in capsys.readouterr().out
