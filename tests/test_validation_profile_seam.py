"""Seam test: validation profiles across the DEV → VALIDATE → REVIEW boundary (#2358).

Unit tests cover declaration and selection in isolation
(``test_validation_profiles.py``). What is proven here is the handoff: the dev
agent is told the advisory profile, the coordinator runs the merge-authority
one, the verdict it records carries that profile's provenance through resume
and the audit trail, and an advisory run never becomes the story's verdict.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _gate_side_effect,
    _make_agent_result,
    _make_config,
    _make_task,
    patch_gate_shell,
)

from theforge.config.load import _validated_validation_profiles
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.run_setup import load_trajectory_state, save_trajectory_state
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.validation_profiles import has_merge_authority_result

PROFILES = _validated_validation_profiles(
    {
        # "gate" appears in the command so the sanctioned shell seam recognises
        # it as the gate invocation.
        "complete": {"command": "make gate ALL=1", "authority": "merge"},
        "targeted": "make test-targeted T={test_target}",
    }
)


def _config_with_profiles(tmp_path: Path, **validation_kwargs):
    base = _make_config(tmp_path)
    return dataclasses.replace(
        base,
        validation=dataclasses.replace(base.validation, profiles=PROFILES, **validation_kwargs),
    )


def _run(config, task, tmp_path: Path, *, decisions="PASS"):
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    prompts: list[str] = []
    commands: list[str] = []
    gate = _gate_side_effect(workspace, decisions)

    def shell(cmd, cwd, **kwargs):
        commands.append(cmd)
        return gate(cmd, cwd, **kwargs)

    def run_agent(*, prompt, **kwargs):
        prompts.append(prompt)
        return _make_agent_result()

    with (
        patch_gate_shell(side_effect=shell),
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=run_agent),
        patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
        patch(
            "theforge.coordinator.review_pool.run_agent_pool",
            return_value=[
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ],
        ),
    ):
        result = run_task(config, task)
    return result, prompts, commands


class TestProfilesAcrossThePhaseBoundary:
    def test_dev_is_told_the_advisory_profile_and_validate_runs_the_authoritative_one(
        self, tmp_path: Path
    ) -> None:
        config = _config_with_profiles(tmp_path, default_test_target="tests/")
        task = _make_task(tmp_path)

        result, prompts, commands = _run(config, task, tmp_path)

        assert result.success is True
        # What the dev agent was told to run: the scoped profile, resolved with
        # the context forge supplies, and explicitly marked non-authoritative.
        assert "make test-targeted T=tests/" in prompts[0]
        assert "`targeted` validation profile" in prompts[0]
        assert "does not establish merge authority" in prompts[0]
        # What the coordinator actually ran for the verdict: the declared
        # merge-authority profile, never the raw gate_command field.
        assert "make gate ALL=1" in commands
        assert config.validation.gate_command not in commands

    def test_the_recorded_verdict_carries_its_profile_and_authority(self, tmp_path: Path) -> None:
        config = _config_with_profiles(tmp_path)
        task = _make_task(tmp_path)

        result, _prompts, _commands = _run(config, task, tmp_path)

        (record,) = [r for r in result.state.validation_runs if not r["skipped"]]
        assert record["profile"] == "complete"
        assert record["authority"] == "merge"
        assert record["command"] == "make gate ALL=1"
        assert record["result"] == "PASS"
        assert record["commit"]
        assert result.state.gate_decisions == ["PASS"]
        assert has_merge_authority_result(result.state.validation_runs)

    def test_a_passing_override_widens_to_the_merge_authority_profile(
        self, tmp_path: Path
    ) -> None:
        """An undeclared command cannot inherit a declared profile's standing.

        It runs and is recorded advisory, and then the declared merge-authority
        profile runs too — otherwise the story would reach DONE with no result
        that carries merge authority behind it.
        """
        config = _config_with_profiles(tmp_path)
        task = dataclasses.replace(_make_task(tmp_path), gate_override="make gate-quick")

        result, _prompts, commands = _run(config, task, tmp_path)

        assert "make gate-quick" in commands
        advisory, authoritative = result.state.validation_runs
        assert (advisory["profile"], advisory["authority"]) == ("override", "advisory")
        assert advisory["result"] == "PASS"
        # The advisory PASS is not the verdict; the widened run is.
        assert (authoritative["profile"], authoritative["authority"]) == ("complete", "merge")
        assert authoritative["command"] == "make gate ALL=1"
        assert "make gate ALL=1" in commands
        assert result.state.gate_decisions == ["PASS"]
        assert result.state.last_gate_decision == "PASS"
        assert has_merge_authority_result(result.state.validation_runs)

    def test_a_failing_override_blocks_without_paying_for_the_complete_profile(
        self, tmp_path: Path
    ) -> None:
        """A failing advisory run already blocks; widening would buy no decision."""
        config = _config_with_profiles(tmp_path)
        task = dataclasses.replace(_make_task(tmp_path), gate_override="make gate-quick")

        result, _prompts, commands = _run(config, task, tmp_path, decisions="FAIL")

        assert result.success is False
        assert "make gate ALL=1" not in commands
        assert not has_merge_authority_result(result.state.validation_runs)

    def test_a_story_cannot_reach_done_on_an_advisory_result_alone(self, tmp_path: Path) -> None:
        """The widened run is what decides: if it fails, the story does not land."""
        config = _config_with_profiles(tmp_path)
        task = dataclasses.replace(_make_task(tmp_path), gate_override="make gate-quick")

        # The override passes; every subsequent (widened, merge-authority) run fails.
        result, _prompts, _commands = _run(
            config, task, tmp_path, decisions=["PASS"] + ["FAIL"] * 10
        )

        assert result.success is False
        assert result.state.phase is not Phase.DONE
        assert not has_merge_authority_result(result.state.validation_runs)
        assert [r["authority"] for r in result.state.validation_runs][:2] == [
            "advisory",
            "merge",
        ]

    def test_a_suppressed_gate_is_recorded_as_skipped_not_as_a_pass(self, tmp_path: Path) -> None:
        config = _config_with_profiles(tmp_path)
        task = dataclasses.replace(_make_task(tmp_path), gate_override="none")

        result, _prompts, _commands = _run(config, task, tmp_path)

        (record,) = result.state.validation_runs
        assert record["skipped"] is True
        assert record["authority"] == "advisory"
        assert result.state.last_gate_decision == "SKIPPED"
        assert not has_merge_authority_result(result.state.validation_runs)

    def test_a_legacy_config_runs_and_records_exactly_as_before(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        result, prompts, commands = _run(config, task, tmp_path)

        assert result.success is True
        assert config.validation.gate_command in commands
        assert "validation profile" not in prompts[0]
        (record,) = result.state.validation_runs
        assert record["profile"] == "complete"
        assert record["authority"] == "merge"
        assert record["declared"] is False
        assert result.state.gate_decisions == ["PASS"]


class TestProvenanceSurvivesResumeAndAudit:
    def test_validation_runs_round_trip_through_the_resume_sidecar(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.validation_runs = [
            {
                "profile": "complete",
                "authority": "merge",
                "command": "make gate",
                "result": "PASS",
                "commit": "abc123",
                "skipped": False,
                "declared": True,
                "widened": False,
            }
        ]
        (tmp_path / ".forge").mkdir()
        save_trajectory_state(tmp_path, state)

        restored = CoordinatorState()
        load_trajectory_state(tmp_path, restored)

        assert restored.validation_runs == state.validation_runs

    def test_an_older_sidecar_without_profiles_still_loads(self, tmp_path: Path) -> None:
        """A resume record written before profiles existed keeps legacy meaning."""
        import yaml

        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "trajectory.yaml").write_text(
            yaml.dump({"gate_runs": 2, "last_gate_decision": "PASS", "last_gate_commit": "abc"}),
            encoding="utf-8",
        )

        state = CoordinatorState()
        load_trajectory_state(tmp_path, state)

        assert state.gate_runs == 2
        assert state.last_gate_decision == "PASS"
        assert state.validation_runs == []

    def test_the_audit_record_names_the_profile_behind_the_verdict(self, tmp_path: Path) -> None:
        config = _config_with_profiles(tmp_path)
        task = _make_task(tmp_path)

        result, _prompts, _commands = _run(config, task, tmp_path)
        record = generate_audit_log(config, task, result)

        runs = record["iterations"]["validation_runs"]
        assert [r["profile"] for r in runs if not r["skipped"]] == ["complete"]
        assert all(r["authority"] == "merge" for r in runs if not r["skipped"])
