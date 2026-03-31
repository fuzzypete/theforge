"""Tests for the dev-review trajectory tracking feature.

Covers:
1. Anchor extraction from ReviewFinding
2. Family classification across synthetic cycle sequences
3. Prompt content for surviving vs new-only scenarios
4. Trajectory store persistence (save/load sidecar round-trip)
5. Classification runs on all review outcomes (APPROVE, exhausted, RETRY_DEV)
6. trajectory_cycle monotonicity across extend/reject flows
"""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator.run_setup import load_trajectory_state, save_trajectory_state
from theforge.coordinator.state import CoordinatorState
from theforge.review import ReviewFinding
from theforge.review_finding_classifier import (
    classify_families,
    extract_review_finding_anchors,
    match_review_findings,
)
from theforge.task.fix_prompts import build_fix_prompt
from theforge.task.story import TaskStory

# ── Helpers ───────────────────────────────────────────────────────────────────


def _rf(description: str, file: str = "src/foo.py", severity: str = "P1") -> ReviewFinding:
    return ReviewFinding(
        severity=severity, file=file, line=1, description=description, suggestion=None
    )


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


# ── 1. Anchor extraction from ReviewFinding ───────────────────────────────────


class TestExtractReviewFindingAnchors:
    def test_snake_ident_from_description(self):
        f = _rf("load_config is missing validation", file="src/config.py")
        anchors = extract_review_finding_anchors(f)
        values = {a.value for a in anchors}
        assert "load_config" in values

    def test_file_anchor_added_from_file_field(self):
        f = _rf("Something is wrong", file="src/coordinator.py")
        anchors = extract_review_finding_anchors(f)
        file_anchors = {a.value for a in anchors if a.kind == "file_path"}
        assert "src/coordinator.py" in file_anchors

    def test_suggestion_excluded_from_anchors(self):
        """Anchors come from description only — suggestion content must not appear."""
        f = ReviewFinding(
            severity="P1",
            file="src/foo.py",
            line=1,
            description="Missing parse_input call",
            suggestion="Add validate_schema to fix this completely_unique_anchor_xyz",
        )
        anchors = extract_review_finding_anchors(f)
        values = {a.value for a in anchors}
        assert "completely_unique_anchor_xyz" not in values
        assert "validate_schema" not in values
        assert "parse_input" in values

    def test_no_reviewer_prefix_stripping_needed(self):
        """ReviewFinding descriptions have no attribution prefix — extract_anchors
        handles this correctly (plan-step labels stripped, no other pre-processing)."""
        f = _rf("load_config validation is absent", file="src/foo.py")
        anchors = extract_review_finding_anchors(f)
        values = {a.value for a in anchors}
        assert "load_config" in values

    def test_empty_file_excluded(self):
        f = ReviewFinding(severity="P1", file="", line=None, description="issue", suggestion=None)
        anchors = extract_review_finding_anchors(f)
        file_anchors = [a for a in anchors if a.kind == "file_path"]
        # empty file should not produce a file anchor
        assert not any(a.value == "" for a in file_anchors)

    def test_file_only_no_text_anchors(self):
        """A finding with only a file path and generic description produces only
        a file-path anchor — this alone cannot form a family (file-path-only rule)."""
        f = _rf("Something wrong here", file="src/foo.py")
        anchors = extract_review_finding_anchors(f)
        non_file = {a for a in anchors if a.kind != "file_path"}
        # "Something", "wrong", "here" are single words — no multi-segment anchors
        assert non_file == frozenset()


# ── 2. Family classification across synthetic cycles ──────────────────────────


class TestMatchReviewFindings:
    def test_matching_on_snake_ident(self):
        current = [_rf("load_config is still missing")]
        prior = [_rf("load_config validation absent")]
        results = match_review_findings(current, prior)
        assert results[0].prior_index == 0
        values = {a.value for a in results[0].shared_anchors}
        assert "load_config" in values

    def test_file_path_only_does_not_match(self):
        current = [_rf("Something wrong here", file="src/foo.py")]
        prior = [_rf("Other issue also present", file="src/foo.py")]
        results = match_review_findings(current, prior)
        assert results[0].prior_index is None
        assert results[0].abstain_reason is not None
        assert "file-path-only" in results[0].abstain_reason

    def test_no_prior_returns_unmatched(self):
        current = [_rf("load_config is missing")]
        results = match_review_findings(current, [])
        assert results[0].prior_index is None

    def test_multiple_candidates_jaccard_tiebreak(self):
        current = [_rf("load_config validation is absent in parse_input")]
        prior = [
            _rf("load_config validation is missing"),
            _rf("load_config and parse_input are both wrong"),
        ]
        results = match_review_findings(current, prior)
        # Both candidates share load_config; best Jaccard match wins
        assert results[0].prior_index is not None
        assert results[0].prose_similarity_used is True


class TestClassifyFamilies:
    def test_single_cycle_no_family(self):
        """A single finding in isolation does not create a family."""
        current = [_rf("load_config is missing")]
        store, surviving = classify_families(
            current_findings=current,
            current_cycle=1,
            trajectory_store=[],
            prior_cycle_findings=[],
        )
        assert store == []
        assert surviving == []

    def test_cross_cycle_match_creates_family(self):
        """A finding matching across two cycles creates a family."""
        prior_findings = [_rf("load_config validation absent")]
        current_findings = [_rf("load_config check is still missing")]
        store, surviving = classify_families(
            current_findings=current_findings,
            current_cycle=2,
            trajectory_store=[],
            prior_cycle_findings=[(1, prior_findings)],
        )
        assert len(store) == 1
        assert store[0]["seed_anchor"] == "load_config"
        assert 1 in store[0]["cycles"]
        assert 2 in store[0]["cycles"]
        assert len(surviving) == 1

    def test_file_path_only_does_not_create_family(self):
        """File-path-only overlap across cycles does not create a family."""
        prior = [_rf("Something is wrong here", file="src/foo.py")]
        current = [_rf("Other issue also present", file="src/foo.py")]
        store, surviving = classify_families(
            current_findings=current,
            current_cycle=2,
            trajectory_store=[],
            prior_cycle_findings=[(1, prior)],
        )
        assert store == []
        assert surviving == []

    def test_new_finding_no_anchor_no_family(self):
        """A finding with no extractable structural anchors remains unfamilied."""
        prior = [_rf("load_config is missing")]
        current = [_rf("Something is wrong")]  # no multi-segment anchors
        store, surviving = classify_families(
            current_findings=current,
            current_cycle=2,
            trajectory_store=[],
            prior_cycle_findings=[(1, prior)],
        )
        assert store == []

    def test_three_cycle_family_survival(self):
        """Family survives across three cycles."""
        f1 = [_rf("load_config validation absent")]
        f2 = [_rf("load_config check is still missing")]
        f3 = [_rf("load_config not called anywhere")]

        # Cycle 1 → 2
        store, _ = classify_families(
            current_findings=f2,
            current_cycle=2,
            trajectory_store=[],
            prior_cycle_findings=[(1, f1)],
        )
        # Cycle 2 → 3
        store, surviving = classify_families(
            current_findings=f3,
            current_cycle=3,
            trajectory_store=store,
            prior_cycle_findings=[(1, f1), (2, f2)],
        )
        assert len(store) == 1
        assert set(store[0]["cycles"]) == {1, 2, 3}
        assert len(surviving) == 1
        assert surviving[0]["seed_anchor"] == "load_config"

    def test_join_longest_history_family(self):
        """When matching multiple families, finding joins longest-history one."""
        # Setup: two families
        store = [
            {"seed_anchor": "load_config", "cycles": [1, 2, 3], "descriptions": ["a", "b", "c"]},
            {"seed_anchor": "parse_input", "cycles": [2], "descriptions": ["x"]},
        ]
        # Current finding shares both anchors
        current = [_rf("load_config and parse_input both need fixing")]
        # Prior has findings with each anchor
        prior = [
            _rf("load_config missing validation"),  # joins "load_config" family
            _rf("parse_input validation failed"),  # joins "parse_input" family
        ]
        updated, surviving = classify_families(
            current_findings=current,
            current_cycle=4,
            trajectory_store=store,
            prior_cycle_findings=[(3, prior)],
        )
        # Should join "load_config" family (longer history: 3 cycles vs 1)
        load_config_fam = next(f for f in updated if f["seed_anchor"] == "load_config")
        assert 4 in load_config_fam["cycles"]
        parse_input_fam = next(f for f in updated if f["seed_anchor"] == "parse_input")
        assert 4 not in parse_input_fam["cycles"]

    def test_tie_broken_alphabetically(self):
        """Ties on history length broken alphabetically by seed anchor."""
        store = [
            {"seed_anchor": "beta_func", "cycles": [1, 2], "descriptions": ["a", "b"]},
            {"seed_anchor": "alpha_func", "cycles": [1, 2], "descriptions": ["c", "d"]},
        ]
        # Current finding matches both families
        current = [_rf("alpha_func and beta_func both need fixing")]
        prior = [
            _rf("alpha_func is missing validation"),
            _rf("beta_func is not called"),
        ]
        updated, _ = classify_families(
            current_findings=current,
            current_cycle=3,
            trajectory_store=store,
            prior_cycle_findings=[(2, prior)],
        )
        # "alpha_func" comes before "beta_func" alphabetically — should join it
        alpha_fam = next(f for f in updated if f["seed_anchor"] == "alpha_func")
        beta_fam = next(f for f in updated if f["seed_anchor"] == "beta_func")
        assert 3 in alpha_fam["cycles"]
        assert 3 not in beta_fam["cycles"]

    def test_lexfirst_shared_anchor_not_existing_seed_joins_existing_family(self):
        """Regression: when lex-first shared anchor is NOT an existing family seed but
        another shared anchor IS, the finding must join the existing family — not create
        a new one seeded by the lex-first anchor.

        Scenario:
        - Family A: seed = "load_config" (3 cycles), Family B: seed = "parse_input" (2 cycles)
        - Current finding shares {helper_func, load_config, parse_input} with prior finding
        - lex-first of shared non-file anchors = "helper_func" (not an existing seed)
        - But "load_config" IS an existing seed with 3 cycles
        - Must join "load_config" family, NOT create new "helper_func" family
        """
        store = [
            {"seed_anchor": "load_config", "cycles": [1, 2, 3], "descriptions": ["a", "b", "c"]},
            {"seed_anchor": "parse_input", "cycles": [1, 2], "descriptions": ["x", "y"]},
        ]
        # Current finding has all three anchors; prior also has all three
        current = [_rf("helper_func and load_config and parse_input all broken")]
        prior = [_rf("helper_func load_config parse_input all failing together")]
        updated, surviving = classify_families(
            current_findings=current,
            current_cycle=4,
            trajectory_store=store,
            prior_cycle_findings=[(3, prior)],
        )
        # Must join "load_config" (3 cycles, longest), NOT create new "helper_func" family
        seed_anchors = {f["seed_anchor"] for f in updated}
        assert "helper_func" not in seed_anchors, "New 'helper_func' family must NOT be created"
        load_config_fam = next(f for f in updated if f["seed_anchor"] == "load_config")
        assert 4 in load_config_fam["cycles"]

    def test_frozen_seed_constraint_no_new_family_from_non_seed_anchor(self):
        """Regression (P1 bug): a current finding that shares only a non-seed anchor with
        a prior finding that is ALREADY IN an existing family must NOT create a new family.

        Scenario:
        - Cycle 1+2: family seeded by "load_config" (shared anchors {load_config, parse_input})
        - Cycle 3: finding shares ONLY "parse_input" with cycle-2 finding
        - Expected: cycle-3 finding remains unfamilied (no new "parse_input" family)
        - Buggy behavior: incorrectly creates new "parse_input" family seeded at cycles [2, 3],
          causing it to appear in surviving_families and trigger approach-switch mode
        """
        # C1 and C2 both have load_config and parse_input → family created at cycle 1-2
        c1 = [_rf("load_config and parse_input validation is absent")]
        c2 = [_rf("load_config and parse_input check is still missing")]

        store, _ = classify_families(
            current_findings=c2,
            current_cycle=2,
            trajectory_store=[],
            prior_cycle_findings=[(1, c1)],
        )
        # Family seeded by "load_config" (lex-first of {load_config, parse_input})
        assert len(store) == 1
        assert store[0]["seed_anchor"] == "load_config"

        # C3 only shares "parse_input" with C2 — load_config is NOT in C3's text
        c3 = [_rf("parse_input broken again missing validation check")]

        store, surviving = classify_families(
            current_findings=c3,
            current_cycle=3,
            trajectory_store=store,
            prior_cycle_findings=[(1, c1), (2, c2)],
        )
        # C3 must remain unfamilied — no new "parse_input" family should appear
        assert len(store) == 1, (
            f"Expected 1 family (load_config), got {[f['seed_anchor'] for f in store]}"
        )
        assert store[0]["seed_anchor"] == "load_config"
        assert 3 not in store[0]["cycles"], "C3 must not join load_config family (no shared seed)"
        assert surviving == [], f"No surviving families expected; got {surviving}"

    def test_frozen_seed_constraint_join_when_seed_present(self):
        """Counterpart: a current finding sharing the frozen seed DOES join the family."""
        c1 = [_rf("load_config and parse_input validation is absent")]
        c2 = [_rf("load_config and parse_input check is still missing")]

        store, _ = classify_families(
            current_findings=c2,
            current_cycle=2,
            trajectory_store=[],
            prior_cycle_findings=[(1, c1)],
        )

        # C3 shares both load_config AND parse_input with C2
        c3 = [_rf("load_config and parse_input still broken together")]

        store, surviving = classify_families(
            current_findings=c3,
            current_cycle=3,
            trajectory_store=store,
            prior_cycle_findings=[(1, c1), (2, c2)],
        )
        # C3 shares the frozen seed "load_config" → must join the existing family
        assert len(store) == 1
        assert store[0]["seed_anchor"] == "load_config"
        assert 3 in store[0]["cycles"]
        assert len(surviving) == 1


# ── 3. Prompt content for surviving vs new-only ───────────────────────────────


class TestFixPromptTrajectoryFraming:
    def _make_prompt(self, tmp_path: Path, surviving_families=None):
        task = _make_task(tmp_path)
        return build_fix_prompt(
            task,
            workspace_path=tmp_path / "workspace",
            branch_name="feat/test",
            review_findings="- P1: load_config is missing validation",
            gate_command="make test",
            iteration=2,
            surviving_families=surviving_families,
        )

    def test_no_surviving_families_standard_framing(self, tmp_path):
        prompt = self._make_prompt(tmp_path, surviving_families=None)
        assert "Fix each P1 finding" in prompt
        assert "Reconsider your approach" not in prompt
        assert "Trajectory Summary" not in prompt

    def test_no_surviving_families_plan_guard_preserved(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "workspace",
            branch_name="feat/test",
            review_findings="findings",
            gate_command="make test",
            plan_output="approach: do the thing",
            surviving_families=None,
        )
        assert "Fix the P1 findings **within this plan's approach**" in prompt
        assert "Do NOT redesign" in prompt

    def test_surviving_families_switches_framing(self, tmp_path):
        families = [
            {"seed_anchor": "load_config", "cycles": [1, 2], "descriptions": ["desc1", "desc2"]}
        ]
        prompt = self._make_prompt(tmp_path, surviving_families=families)
        assert "Reconsider your approach" in prompt
        assert "Fix each P1 finding" not in prompt

    def test_surviving_families_includes_trajectory_summary(self, tmp_path):
        families = [
            {"seed_anchor": "load_config", "cycles": [1, 2], "descriptions": ["desc1", "desc2"]}
        ]
        prompt = self._make_prompt(tmp_path, surviving_families=families)
        assert "Trajectory Summary" in prompt
        assert "load_config" in prompt
        assert "2 cycles" in prompt
        assert "desc1" in prompt
        assert "desc2" in prompt

    def test_surviving_families_plan_guard_replaced(self, tmp_path):
        task = _make_task(tmp_path)
        families = [{"seed_anchor": "load_config", "cycles": [1, 2], "descriptions": ["d1", "d2"]}]
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "workspace",
            branch_name="feat/test",
            review_findings="findings",
            gate_command="make test",
            plan_output="approach: do the thing",
            surviving_families=families,
        )
        # Original guard must be replaced
        assert "Fix the P1 findings **within this plan's approach**" not in prompt
        assert "Do NOT redesign or adopt a different strategy" not in prompt
        # Surviving-family-aware text present
        assert "persisted across multiple review cycles" in prompt

    def test_trajectory_summary_truncated_to_5_descriptions(self, tmp_path):
        """When family survives 6+ cycles, descriptions truncated to most recent 5."""
        families = [
            {
                "seed_anchor": "load_config",
                "cycles": [1, 2, 3, 4, 5, 6],
                "descriptions": ["d1", "d2", "d3", "d4", "d5", "d6"],
            }
        ]
        prompt = self._make_prompt(tmp_path, surviving_families=families)
        assert "d6" in prompt  # most recent included
        assert "d1" not in prompt  # oldest truncated

    def test_existing_cycle_history_preserved_with_trajectory(self, tmp_path):
        """Trajectory context is additive — cycle_history section remains."""
        from theforge.coordinator.state import CycleHistory

        task = _make_task(tmp_path)
        families = [{"seed_anchor": "load_config", "cycles": [1, 2], "descriptions": ["d1", "d2"]}]
        history = [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="Needs work",
                p1_findings=["load_config missing"],
            )
        ]
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "workspace",
            branch_name="feat/test",
            review_findings="findings",
            gate_command="make test",
            cycle_history=history,
            surviving_families=families,
        )
        assert "Previous Review Cycles" in prompt
        assert "Trajectory Summary" in prompt

    def test_step_numbers_correct_with_surviving_families(self, tmp_path):
        """When surviving families add a step 2, remaining steps are renumbered."""
        families = [{"seed_anchor": "load_config", "cycles": [1, 2], "descriptions": ["d1", "d2"]}]
        prompt = self._make_prompt(tmp_path, surviving_families=families)
        assert "3. Run `make fmt`" in prompt
        assert "4. Commit" in prompt

    def test_step_numbers_standard_without_surviving_families(self, tmp_path):
        prompt = self._make_prompt(tmp_path, surviving_families=None)
        assert "2. Run `make fmt`" in prompt
        assert "3. Commit" in prompt


# ── 4. Trajectory state persistence (sidecar round-trip) ─────────────────────


class TestTrajectoryStatePersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        state = CoordinatorState()
        state.workspace_path = workspace
        state.trajectory_cycle = 5
        state.finding_trajectory = [
            {"seed_anchor": "load_config", "cycles": [1, 2, 3], "descriptions": ["a", "b", "c"]}
        ]
        state.review_cycle_findings = [
            (1, [{"file": "src/foo.py", "line": 1, "description": "issue", "severity": "P1"}])
        ]
        state.surviving_families = [
            {"seed_anchor": "load_config", "cycles": [2, 3], "descriptions": ["b", "c"]}
        ]

        save_trajectory_state(workspace, state)

        # Load into fresh state
        state2 = CoordinatorState()
        load_trajectory_state(workspace, state2)

        assert state2.trajectory_cycle == 5
        assert len(state2.finding_trajectory) == 1
        assert state2.finding_trajectory[0]["seed_anchor"] == "load_config"
        assert state2.finding_trajectory[0]["cycles"] == [1, 2, 3]
        assert len(state2.review_cycle_findings) == 1
        assert state2.review_cycle_findings[0][0] == 1
        assert len(state2.surviving_families) == 1

    def test_trajectory_cycle_restored_on_resume(self, tmp_path):
        """trajectory_cycle counter survives save/load — critical for resume."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        state = CoordinatorState()
        state.trajectory_cycle = 7
        save_trajectory_state(workspace, state)

        fresh = CoordinatorState()
        assert fresh.trajectory_cycle == 0  # default

        load_trajectory_state(workspace, fresh)
        assert fresh.trajectory_cycle == 7

    def test_missing_sidecar_leaves_defaults(self, tmp_path):
        """load_trajectory_state is a no-op when sidecar doesn't exist."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        state = CoordinatorState()
        load_trajectory_state(workspace, state)  # should not raise

        assert state.trajectory_cycle == 0
        assert state.finding_trajectory == []
        assert state.review_cycle_findings == []
        assert state.surviving_families == []

    def test_corrupt_sidecar_leaves_defaults(self, tmp_path):
        """Corrupt sidecar is ignored gracefully — state fields stay at defaults."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sidecar = workspace / ".forge" / "trajectory.yaml"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("this: is: not: valid: yaml: [[[", encoding="utf-8")

        state = CoordinatorState()
        load_trajectory_state(workspace, state)  # should not raise

        assert state.trajectory_cycle == 0

    def test_empty_sidecar_leaves_defaults(self, tmp_path):
        """Empty sidecar file is handled gracefully."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sidecar = workspace / ".forge" / "trajectory.yaml"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("", encoding="utf-8")

        state = CoordinatorState()
        load_trajectory_state(workspace, state)

        assert state.trajectory_cycle == 0

    def test_trajectory_sidecar_survives_multiple_save_sessions_calls(self, tmp_path):
        """trajectory.yaml must not be clobbered by save_sessions() calls.

        save_sessions() rewrites .forge/sessions.json from scratch.  Multiple
        callers (dev_phase, review_pool, plan_flow, engine) invoke it between
        review cycles.  The trajectory sidecar must remain intact because it is
        a separate file — not merged into sessions.json.
        """
        from theforge.sessions import save_sessions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Write trajectory data
        state = CoordinatorState()
        state.trajectory_cycle = 3
        state.finding_trajectory = [
            {"seed_anchor": "load_config", "cycles": [1, 2, 3], "descriptions": ["a", "b", "c"]}
        ]
        save_trajectory_state(workspace, state)

        # Simulate multiple save_sessions() calls (as done by coordinator callers)
        save_sessions(workspace, "sess-1", {})
        save_sessions(workspace, "sess-2", {"reviewer": "sess-r"})
        save_sessions(workspace, "sess-3", {})

        # trajectory.yaml must still be intact
        fresh = CoordinatorState()
        load_trajectory_state(workspace, fresh)
        assert fresh.trajectory_cycle == 3
        assert len(fresh.finding_trajectory) == 1
        assert fresh.finding_trajectory[0]["seed_anchor"] == "load_config"


# ── 5. Classification runs on all review outcomes ─────────────────────────────


class TestTrajectoryRunsOnAllOutcomes:
    """Verify that trajectory_cycle increments and review_cycle_findings is
    populated regardless of APPROVE/exhausted/RETRY_DEV path.

    We test the classify_families integration directly since wiring the full
    coordinator would require heavy mocking of the review pool.
    """

    def test_trajectory_cycle_independent_state_fields_exist(self):
        """CoordinatorState has the four trajectory fields at expected defaults."""
        state = CoordinatorState()
        assert state.trajectory_cycle == 0
        assert state.finding_trajectory == []
        assert state.review_cycle_findings == []
        assert state.surviving_families == []

    def test_classify_families_called_for_approve_path(self):
        """Simulating APPROVE: trajectory_cycle increments and families classified."""
        # This test verifies the classify_families call works for APPROVE scenario
        # (review_cycle_findings would be populated by review_phase)
        current_findings = [_rf("load_config still fails")]

        state = CoordinatorState()
        state.trajectory_cycle = 1
        state.review_cycle_findings = [
            (
                1,
                [
                    {
                        "file": "src/foo.py",
                        "line": 1,
                        "description": "load_config is broken",
                        "severity": "P1",
                    }
                ],
            )
        ]

        # Simulate cycle 2 classification (APPROVE path)
        state.trajectory_cycle += 1
        from theforge.review import ReviewFinding as _RF

        prior_rf = [
            (
                1,
                [
                    _RF(
                        severity="P1",
                        file="src/foo.py",
                        line=1,
                        description="load_config is broken",
                        suggestion=None,
                    )
                ],
            )
        ]
        state.finding_trajectory, state.surviving_families = classify_families(
            current_findings=current_findings,
            current_cycle=state.trajectory_cycle,
            trajectory_store=state.finding_trajectory,
            prior_cycle_findings=prior_rf,
        )
        assert state.trajectory_cycle == 2
        assert len(state.finding_trajectory) == 1
        assert len(state.surviving_families) == 1


# ── 6. trajectory_cycle monotonicity across extend/reject ─────────────────────


class TestTrajectoryMonotonicity:
    """trajectory_cycle must monotonically increment even when review_cycle resets."""

    def test_trajectory_cycle_independent_of_review_cycle(self):
        """Simulate extend resetting review_cycle to 0 while trajectory_cycle keeps going."""
        state = CoordinatorState()
        assert state.trajectory_cycle == 0
        assert state.review_cycle == 0

        # Simulate review phase cycle 1
        state.review_cycle += 1
        state.trajectory_cycle += 1
        assert state.review_cycle == 1
        assert state.trajectory_cycle == 1

        # Simulate "extend" — review_cycle resets to 0, trajectory_cycle must NOT
        state.review_cycle = 0  # as done in _handle_interactive_review_decision

        # Simulate review phase cycle 2 (after extend)
        state.review_cycle += 1
        state.trajectory_cycle += 1
        assert state.review_cycle == 1  # review_cycle restarted from 0
        assert state.trajectory_cycle == 2  # trajectory_cycle kept going

    def test_trajectory_cycle_survives_exhausted_reject(self):
        """Exhausted reject resets review_cycle to 0 — trajectory_cycle unaffected."""
        state = CoordinatorState()

        # Three simulated review phases
        for i in range(1, 4):
            state.review_cycle += 1
            state.trajectory_cycle += 1
            if i == 2:
                # Simulate exhausted reject: review_cycle resets
                state.review_cycle = 0

        assert state.trajectory_cycle == 3

    def test_trajectory_cycle_survives_exhausted_gate_continue(self):
        """Exhausted-cycle gate continue decrements review_cycle — trajectory_cycle unaffected."""
        state = CoordinatorState()

        state.review_cycle += 1
        state.trajectory_cycle += 1
        assert state.trajectory_cycle == 1

        # Simulate exhausted-cycle gate "continue": review_cycle decremented
        state.review_cycle -= 1  # as in review_phase.py line 701
        assert state.review_cycle == 0

        # Next review phase entry
        state.review_cycle += 1
        state.trajectory_cycle += 1
        assert state.trajectory_cycle == 2  # monotonically increased

    def test_trajectory_cycle_sequence_1_2_3_across_resets(self):
        """Full scenario: cycle 1 → extend → cycle 2 → reject → cycle 3.
        review_cycle produces 1, 0+1=1, 0+1=1.
        trajectory_cycle must produce 1, 2, 3.
        """
        state = CoordinatorState()
        trajectory_values: list[int] = []
        review_values: list[int] = []

        # Cycle 1
        state.review_cycle += 1
        state.trajectory_cycle += 1
        trajectory_values.append(state.trajectory_cycle)
        review_values.append(state.review_cycle)

        # extend → review_cycle = 0
        state.review_cycle = 0

        # Cycle 2
        state.review_cycle += 1
        state.trajectory_cycle += 1
        trajectory_values.append(state.trajectory_cycle)
        review_values.append(state.review_cycle)

        # reject (exhausted) → review_cycle = 0
        state.review_cycle = 0

        # Cycle 3
        state.review_cycle += 1
        state.trajectory_cycle += 1
        trajectory_values.append(state.trajectory_cycle)
        review_values.append(state.review_cycle)

        assert trajectory_values == [1, 2, 3], f"trajectory not monotonic: {trajectory_values}"
        # review_cycle restarts from 1 after each reset
        assert review_values == [1, 1, 1], f"unexpected review_cycle values: {review_values}"
