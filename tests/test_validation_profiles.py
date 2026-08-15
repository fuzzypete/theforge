"""Validation profiles: declaration, selection, authority, and provenance (#2358).

Validation used to be one authoritative command plus one advisory string with
no stated relationship to it. These tests pin the contract that replaced it: a
project declares named profiles, exactly one carries merge authority, unknown
inputs widen rather than narrow, and every recorded run says which profile
produced it and what that result was worth.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from coord_test_helpers import _make_config

from theforge.config.load import _validated_validation_profiles
from theforge.config.types import ValidationConfig, ValidationProfile
from theforge.coordinator.gate import run_gate_full
from theforge.coordinator.review_context import gate_profile_prompt_kwargs
from theforge.coordinator.state import CoordinatorState
from theforge.task import TaskStory
from theforge.task.dev_prompts import build_dev_prompt
from theforge.task.fix_prompts import build_fix_prompt
from theforge.task.review_prompts import build_review_prompt
from theforge.validation_profiles import (
    PHASE_ADVISORY,
    PHASE_MERGE,
    has_merge_authority_result,
    last_merge_authority_record,
    override_selection,
    select_validation,
    validation_run_record,
)


def _profiles(**spec: object) -> tuple[ValidationProfile, ...]:
    return _validated_validation_profiles(spec)


def _validation(**kwargs: object) -> ValidationConfig:
    return ValidationConfig(gate_command="make gate", **kwargs)  # type: ignore[arg-type]


def _task(**kwargs: object) -> TaskStory:
    base = {"name": "Story", "slug": "story", "story_path": "stories/story.md"}
    base.update(kwargs)
    return TaskStory(**base)  # type: ignore[arg-type]


# ── Declaration / parsing ────────────────────────────────────────────────


def test_profiles_parse_from_mapping_with_short_and_full_forms() -> None:
    profiles = _profiles(
        complete={"command": "make gate", "authority": "merge"},
        fast="make test-fast",
        targeted={"command": "make test T={test_target}"},
    )

    by_name = {p.name: p for p in profiles}
    assert by_name["complete"].authority == "merge"
    assert by_name["complete"].is_merge_authority
    assert by_name["fast"].authority == "advisory"
    assert not by_name["fast"].is_merge_authority
    assert by_name["targeted"].command == "make test T={test_target}"


def test_absent_or_empty_profiles_declare_nothing() -> None:
    assert _validated_validation_profiles(None) == ()
    assert _validated_validation_profiles({}) == ()


@pytest.mark.parametrize(
    "spec, expected",
    [
        ({"smoke": "make smoke"}, "unknown profile name"),
        ({"complete": "make gate"}, "exactly one profile"),
        (
            {
                "complete": {"command": "a", "authority": "merge"},
                "fast": {"command": "b", "authority": "merge"},
            },
            "exactly one profile",
        ),
        ({"complete": {"command": "", "authority": "merge"}}, "non-empty string"),
        ({"complete": {"command": 7, "authority": "merge"}}, "non-empty string"),
        ({"complete": {"command": "a", "authority": "advisory"}}, "exactly one profile"),
        ({"complete": {"command": "a", "authority": "sometimes"}}, "authority must be one of"),
        ({"complete": {"command": "a", "authority": "merge", "cost": 3}}, "unknown key"),
        ({"complete": ["make gate"]}, "must be a command string or a mapping"),
        ("make gate", "must be a mapping"),
    ],
)
def test_malformed_profile_declarations_are_rejected(spec: object, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        _validated_validation_profiles(spec)


def test_every_accepted_advisory_declaration_is_selectable() -> None:
    """No declaration may load successfully and then never run.

    The closed name vocabulary exists for this: a profile forge cannot select
    for is a profile that would silently never execute.
    """
    for name in ("fast", "targeted"):
        config = _validation(
            profiles=_profiles(
                complete={"command": "make gate", "authority": "merge"},
                **{name: f"make {name}"},
            )
        )
        selected = select_validation(config, phase=PHASE_ADVISORY)
        assert selected.profile == name
        assert not selected.widened


def test_profiles_load_from_forge_yaml(tmp_path: Path) -> None:
    from theforge.config.load import load_config

    (tmp_path / "forge.yaml").write_text(
        yaml.dump(
            {
                "project": "p",
                "validation": {
                    "gate_command": "make gate",
                    "profiles": {
                        "complete": {"command": "make gate", "authority": "merge"},
                        "targeted": "make test T={test_target}",
                    },
                },
                "models": ["anthropic/sonnet/cli"],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path / "forge.yaml")

    assert config.validation.declared_merge_profile() is not None
    assert config.validation.declared_merge_profile().name == "complete"  # type: ignore[union-attr]
    assert config.validation.profile("targeted") is not None


# ── Selection ────────────────────────────────────────────────────────────


def test_validate_selects_the_merge_authority_profile() -> None:
    config = _validation(
        profiles=_profiles(
            complete={"command": "make gate", "authority": "merge"},
            fast="make test-fast",
        )
    )

    selected = select_validation(config, phase=PHASE_MERGE)

    assert (selected.profile, selected.authority) == ("complete", "merge")
    assert selected.command == "make gate"
    assert selected.is_merge_authority


def test_advisory_prefers_targeted_then_fast() -> None:
    both = _validation(
        profiles=_profiles(
            complete={"command": "make gate", "authority": "merge"},
            fast="make test-fast",
            targeted="make test T={test_target}",
        )
    )
    assert select_validation(both, phase=PHASE_ADVISORY).profile == "targeted"

    fast_only = _validation(
        profiles=_profiles(
            complete={"command": "make gate", "authority": "merge"},
            fast="make test-fast",
        )
    )
    assert select_validation(fast_only, phase=PHASE_ADVISORY).profile == "fast"


def test_scoping_context_is_substituted_into_the_selected_command() -> None:
    config = _validation(
        default_test_target="tests/",
        profiles=_profiles(
            complete={"command": "make gate", "authority": "merge"},
            targeted="make test T={test_target} S={slug}",
        ),
    )

    selected = select_validation(
        config, phase=PHASE_ADVISORY, task=_task(test_target="tests/unit")
    )

    assert selected.command == "make test T=tests/unit S=story"


def test_default_test_target_fills_in_when_the_story_has_none() -> None:
    config = _validation(
        default_test_target="tests/",
        profiles=_profiles(
            complete={"command": "make gate", "authority": "merge"},
            targeted="make test T={test_target}",
        ),
    )

    assert select_validation(config, phase=PHASE_ADVISORY, task=_task()).command == (
        "make test T=tests/"
    )


def test_no_advisory_profile_widens_to_the_complete_profile() -> None:
    config = _validation(
        profiles=_profiles(complete={"command": "make gate", "authority": "merge"})
    )

    selected = select_validation(config, phase=PHASE_ADVISORY)

    assert selected.profile == "complete"
    assert selected.is_merge_authority
    assert selected.widened


def test_selection_resolving_to_nothing_widens_to_the_complete_profile() -> None:
    """An empty resolved command must run more validation, never none."""
    config = _validation(
        profiles=_profiles(
            complete={"command": "make gate", "authority": "merge"},
            targeted="{test_target}",
        ),
    )

    selected = select_validation(config, phase=PHASE_ADVISORY, task=_task(test_target=" "))

    assert selected.command == "make gate"
    assert selected.profile == "complete"
    assert selected.widened


def test_a_merge_authority_targeted_profile_is_never_selected_as_advisory() -> None:
    config = _validation(
        profiles=_profiles(targeted={"command": "make test T={test_target}", "authority": "merge"})
    )

    assert select_validation(config, phase=PHASE_ADVISORY).widened
    assert select_validation(config, phase=PHASE_ADVISORY).profile == "targeted"


# ── Legacy (no profiles declared) ────────────────────────────────────────


def test_legacy_config_keeps_gate_command_as_the_complete_profile() -> None:
    config = _validation(test_command="make test-fast")

    merge = select_validation(config, phase=PHASE_MERGE)
    advisory = select_validation(config, phase=PHASE_ADVISORY)

    assert (merge.profile, merge.authority, merge.command) == ("complete", "merge", "make gate")
    assert (advisory.profile, advisory.authority) == ("fast", "advisory")
    assert advisory.command == "make test-fast"
    assert not merge.declared and not advisory.declared


def test_legacy_config_without_test_command_widens_to_the_gate_command() -> None:
    advisory = select_validation(_validation(), phase=PHASE_ADVISORY)

    assert advisory.command == "make gate"
    assert advisory.widened


# ── Records and trust ────────────────────────────────────────────────────


def test_run_record_carries_profile_authority_and_command() -> None:
    config = _validation(
        profiles=_profiles(complete={"command": "make gate", "authority": "merge"})
    )
    record = validation_run_record(
        select_validation(config, phase=PHASE_MERGE), result="PASS", commit="abc123"
    )

    assert record["profile"] == "complete"
    assert record["authority"] == "merge"
    assert record["command"] == "make gate"
    assert record["result"] == "PASS"
    assert record["commit"] == "abc123"
    assert record["skipped"] is False


def test_skipped_gate_is_recorded_as_an_advisory_non_run() -> None:
    record = validation_run_record(None, result="SKIPPED", skipped=True)

    assert record["skipped"] is True
    assert record["authority"] == "advisory"
    assert not has_merge_authority_result([record])


def test_record_without_a_selection_reads_as_legacy_complete_merge() -> None:
    """An older record carries no profile; absence means legacy, not untrusted."""
    record = validation_run_record(None, result="PASS")

    assert (record["profile"], record["authority"]) == ("complete", "merge")
    assert has_merge_authority_result([record])


def test_advisory_results_never_establish_merge_trust() -> None:
    advisory = validation_run_record(
        override_selection("make quick", declared=True), result="PASS"
    )

    assert advisory["authority"] == "advisory"
    assert not has_merge_authority_result([advisory])
    assert last_merge_authority_record([advisory]) is None


def test_override_on_a_legacy_config_keeps_its_historical_standing() -> None:
    legacy = validation_run_record(override_selection("make quick", declared=False), result="PASS")

    assert has_merge_authority_result([legacy])


# ── Gate execution ───────────────────────────────────────────────────────


def test_run_gate_full_runs_the_declared_merge_authority_profile(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command="exit 1",
            profiles=_profiles(
                complete={"command": "echo declared-complete", "authority": "merge"},
                fast="echo never-run",
            ),
        ),
    )
    selection: list = []

    decision, _error, _tail, resolved, _exit = run_gate_full(
        config, tmp_path, task=None, selection_out=selection
    )

    assert resolved == "echo declared-complete"
    assert decision == "PASS"
    assert selection[0].profile == "complete"
    assert selection[0].is_merge_authority


def test_run_gate_full_reports_the_legacy_gate_command_as_the_complete_profile(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config = replace(config, validation=replace(config.validation, gate_command="echo legacy"))
    selection: list = []

    _decision, _error, _tail, resolved, _exit = run_gate_full(
        config, tmp_path, task=None, selection_out=selection
    )

    assert resolved == "echo legacy"
    assert selection[0].profile == "complete"
    assert selection[0].authority == "merge"
    assert not selection[0].declared


def test_gate_override_is_advisory_once_profiles_are_declared(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            profiles=_profiles(complete={"command": "echo complete", "authority": "merge"}),
        ),
    )
    selection: list = []

    _decision, _error, _tail, resolved, _exit = run_gate_full(
        config,
        tmp_path,
        task=_task(gate_override="echo custom"),
        selection_out=selection,
    )

    assert resolved == "echo custom"
    assert selection[0].authority == "advisory"
    assert not selection[0].is_merge_authority


def test_gate_override_keeps_merge_authority_on_the_legacy_path(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    selection: list = []

    run_gate_full(
        config, tmp_path, task=_task(gate_override="echo custom"), selection_out=selection
    )

    assert selection[0].is_merge_authority


# ── Prompts ──────────────────────────────────────────────────────────────


def test_dev_prompt_marks_the_advisory_profile_as_advisory(tmp_path: Path) -> None:
    prompt = build_dev_prompt(
        _task(),
        workspace_path=tmp_path,
        branch_name="forge/story",
        story_content="Spec",
        gate_command="make gate",
        test_command="make test T=tests/unit",
        test_profile="targeted",
        test_authority="advisory",
        gate_profile="complete",
    )

    assert "make test T=tests/unit" in prompt
    assert "`targeted` validation profile" in prompt
    assert "does not establish merge authority" in prompt
    assert "`complete` validation profile" in prompt


def test_dev_prompt_is_unchanged_when_no_profiles_are_declared(tmp_path: Path) -> None:
    kwargs = dict(
        workspace_path=tmp_path,
        branch_name="forge/story",
        story_content="Spec",
        gate_command="make gate",
    )
    legacy = build_dev_prompt(_task(), **kwargs)  # type: ignore[arg-type]

    assert "validation profile" not in legacy
    assert "Do NOT run the full gate command (`make gate`)" in legacy


def test_fix_prompt_marks_the_advisory_profile_as_advisory(tmp_path: Path) -> None:
    prompt = build_fix_prompt(
        _task(),
        workspace_path=tmp_path,
        branch_name="forge/story",
        review_findings="P1: fix it",
        gate_command="make gate",
        test_command="make test-fast",
        test_profile="fast",
        test_authority="advisory",
        gate_profile="complete",
    )

    assert "`fast` profile" in prompt
    assert "does not establish merge authority" in prompt
    assert "merge-authority `complete` profile" in prompt


def test_review_prompt_states_the_authority_behind_the_verdict(tmp_path: Path) -> None:
    kwargs = dict(
        story_content="Spec",
        commit_log="abc123 work",
        diff_stat="1 file changed",
        diff_content="diff",
        commit_diffs="diff",
        handoff_content="handoff",
        workspace_path=str(tmp_path),
        branch="forge/story",
        authoritative_gate_decision="PASS",
        authoritative_gate_commit="abc123",
    )
    with_profile = build_review_prompt(
        _task(),
        authoritative_gate_profile="complete",
        authoritative_gate_authority="merge",
        **kwargs,  # type: ignore[arg-type]
    )
    without_profile = build_review_prompt(_task(), **kwargs)  # type: ignore[arg-type]

    assert "Validation profile: complete (merge authority)" in with_profile
    assert "Validation profile" not in without_profile


def test_review_prompt_kwargs_come_from_the_recorded_merge_authority_run() -> None:
    state = CoordinatorState()
    state.validation_runs = [
        validation_run_record(override_selection("make quick", declared=True), result="PASS"),
        validation_run_record(
            select_validation(
                _validation(
                    profiles=_profiles(complete={"command": "make gate", "authority": "merge"})
                ),
                phase=PHASE_MERGE,
            ),
            result="PASS",
        ),
    ]

    assert gate_profile_prompt_kwargs(state) == {
        "authoritative_gate_profile": "complete",
        "authoritative_gate_authority": "merge",
    }


def test_review_prompt_kwargs_are_empty_for_an_older_state_with_no_records() -> None:
    assert gate_profile_prompt_kwargs(CoordinatorState()) == {}
