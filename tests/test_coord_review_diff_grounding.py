"""Tests for diff-grounding as a precondition on review's blocking paths (#2525).

A P1 may only decide a story's outcome when it is about that story's change.
Grounding is checked against the story's whole merge-base-to-HEAD diff, before
any promotion path (AC-violation override, allow_net_new_bypass, cycle-1
any-P1, the merged matches_spec=false signal) can reach a blocking disposition.

Covers:
- Out-of-diff P1 + matches_spec=false → diff_ungrounded, no rejection
- Out-of-diff net-new P1 with allow_net_new_bypass=false → not promoted
- Story diff that cannot be computed → diff_ungrounded, no rejection
- Unresolvable / absent cited file → diff_ungrounded
- Non-regression: in-diff P1 + matches_spec=false still blocks as ac_blocking
- Ungrounded findings stay visible in non_blocking_p1s and the registry
- Mixed cycle: only the grounded finding reaches the dev fix prompt
- Path normalization unit cases
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.diff_grounding import (
    is_diff_grounded,
    normalize_finding_path,
    ungrounded_reason,
)
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.state import Phase

#: What the story under test actually touched.
_STORY_DIFF = ["src/changed.py"]

_SIBLING_DESC = "VO2 payloads no longer report gate trigger thresholds"
_OWN_DESC = "Respiratory rows do not carry the overlapping workout identifier"


def _review_yaml(*, file: str, observed: str, matches_spec: bool) -> str:
    mismatches = (
        "mismatches: []" if matches_spec else 'mismatches:\n    - "Acceptance criterion is unmet"'
    )
    return f"""\
```yaml
verdict: REQUEST_CHANGES
summary: "Changes requested."
findings:
  - severity: P1
    file: {file}
    line: 12
    observed: "{observed}"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Restore the field"
story_compliance:
  matches_spec: {str(matches_spec).lower()}
  {mismatches}
test_coverage:
  adequate: true
  gaps: []
```
"""


_MIXED_REVIEW = f"""\
```yaml
verdict: REQUEST_CHANGES
summary: "Changes requested."
findings:
  - severity: P1
    file: src/vo2_service.py
    line: 12
    observed: "{_SIBLING_DESC}"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Restore the field"
  - severity: P1
    file: src/changed.py
    line: 30
    observed: "{_OWN_DESC}"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Key the rows to the workout"
story_compliance:
  matches_spec: false
  mismatches:
    - "Acceptance criterion is unmet"
test_coverage:
  adequate: true
  gaps: []
```
"""


def _run(tmp_path, review_yaml: str, *, changed_files: list[str] | None = _STORY_DIFF):
    """Run REVIEW→DONE/ESCALATE with one reviewer returning ``review_yaml`` every cycle."""
    config = _make_config(tmp_path)  # max_review_cycles=2, allow_net_new_bypass=False
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    with (
        patch_gate_shell() as mock_shell,
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS", changed_files=changed_files)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=review_yaml, profile_name="review")
        ]
        result = run_from_review(config, task, workspace)

    return config, task, result


def _dispositions(config, task, result) -> dict[str, str]:
    audit = generate_audit_log(config, task, result)
    return {r["description"]: r["disposition"] for r in audit["finding_registry"]}


class TestOutOfDiffFindingsDoNotBlock:
    def test_out_of_diff_p1_with_matches_spec_false_does_not_reject(self, tmp_path):
        """The #2525 shape: a sibling story's criterion must not fail this story."""
        config, task, result = _run(
            tmp_path,
            _review_yaml(file="src/vo2_service.py", observed=_SIBLING_DESC, matches_spec=False),
        )

        assert result.success is True
        assert result.phase == Phase.DONE
        # One cycle only: nothing blocked, so no dev retry was bought.
        assert result.state.review_cycle == 1
        assert _dispositions(config, task, result)[_SIBLING_DESC] == "diff_ungrounded"

    def test_out_of_diff_net_new_p1_not_promoted_when_bypass_disabled(self, tmp_path):
        """allow_net_new_bypass=false must not promote a P1 it cannot ground."""
        config, task, result = _run(
            tmp_path,
            _review_yaml(file="src/vo2_service.py", observed=_SIBLING_DESC, matches_spec=True),
        )

        assert result.success is True
        assert result.phase == Phase.DONE
        assert _dispositions(config, task, result)[_SIBLING_DESC] == "diff_ungrounded"

    def test_unavailable_story_diff_yields_diff_ungrounded(self, tmp_path):
        """A comparison that could not be made is not evidence about the change."""
        config, task, result = _run(
            tmp_path,
            _review_yaml(file="src/changed.py", observed=_OWN_DESC, matches_spec=False),
            changed_files=None,
        )

        assert result.success is True
        assert result.phase == Phase.DONE
        assert _dispositions(config, task, result)[_OWN_DESC] == "diff_ungrounded"

    def test_unresolvable_cited_file_yields_diff_ungrounded(self, tmp_path):
        """A finding whose file does not resolve cannot be checked, so cannot block."""
        config, task, result = _run(
            tmp_path,
            _review_yaml(
                file='"/elsewhere/vo2_service.py"', observed=_SIBLING_DESC, matches_spec=False
            ),
        )

        assert result.success is True
        assert result.phase == Phase.DONE
        assert _dispositions(config, task, result)[_SIBLING_DESC] == "diff_ungrounded"

    def test_ungrounded_p1_is_visible_in_the_audit_not_dropped(self, tmp_path):
        """A suppression that leaves no trace is indistinguishable from a dismissal."""
        config, task, result = _run(
            tmp_path,
            _review_yaml(file="src/vo2_service.py", observed=_SIBLING_DESC, matches_spec=False),
        )

        audit = generate_audit_log(config, task, result)
        non_blocking = [r for r in audit["non_blocking_p1s"] if r["description"] == _SIBLING_DESC]
        assert len(non_blocking) == 1
        # The real reason is preserved, not flattened to net_new.
        assert non_blocking[0]["disposition"] == "diff_ungrounded"
        assert non_blocking[0]["file"] == "src/vo2_service.py"


class TestGroundedFindingsStillBlock:
    def test_in_diff_p1_with_matches_spec_false_still_blocks(self, tmp_path):
        """Non-regression: this change narrows eligibility, it does not weaken review."""
        config, task, result = _run(
            tmp_path,
            _review_yaml(file="src/changed.py", observed=_OWN_DESC, matches_spec=False),
        )

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.review_cycle == 2
        assert _dispositions(config, task, result)[_OWN_DESC] in ("ac_blocking", "unresolved")


class TestMixedCycleDevHandoff:
    def test_only_the_grounded_finding_reaches_the_dev_fix_prompt(self, tmp_path):
        """A retry driven by a grounded blocker must not carry back the ungrounded one."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch_gate_shell() as mock_shell,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.dev_phase.build_fix_prompt") as mock_fix_prompt,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS", changed_files=_STORY_DIFF)
            mock_preflight.return_value = _PREFLIGHT_RESULT
            mock_dev.return_value = _make_agent_result(success=True, output="Fixed.")
            mock_fix_prompt.return_value = "fix prompt"
            mock_pool.return_value = [
                _make_agent_result(success=True, output=_MIXED_REVIEW, profile_name="review")
            ]
            result = run_from_review(config, task, workspace)

        # The grounded P1 blocked, so a dev retry ran.
        assert mock_fix_prompt.called
        kwargs = mock_fix_prompt.call_args.kwargs

        # Channel 1: the rendered review handoff.
        assert _OWN_DESC in kwargs["review_findings"]
        assert _SIBLING_DESC not in kwargs["review_findings"]

        # Channel 2: the classified current-cycle P1 records.
        classified_descs = [r.description for r in kwargs["classified_p1s"] or []]
        assert _OWN_DESC in classified_descs
        assert _SIBLING_DESC not in classified_descs

        # Both are still recorded — the ungrounded one is suppressed, not erased.
        registry = _dispositions(config, task, result)
        assert registry[_SIBLING_DESC] == "diff_ungrounded"
        assert _OWN_DESC in registry


class TestPathNormalization:
    def test_relative_path_passes_through(self, tmp_path):
        assert normalize_finding_path("src/a.py", tmp_path) == "src/a.py"

    def test_dot_slash_prefix_is_stripped(self, tmp_path):
        assert normalize_finding_path("./src/a.py", tmp_path) == "src/a.py"

    def test_absolute_path_inside_the_workspace_is_made_relative(self, tmp_path):
        absolute = tmp_path / "src" / "a.py"
        assert normalize_finding_path(str(absolute), tmp_path) == "src/a.py"

    def test_absolute_path_outside_the_workspace_does_not_resolve(self, tmp_path):
        other = tmp_path.parent / "elsewhere" / "a.py"
        assert normalize_finding_path(str(other), tmp_path) is None

    def test_empty_and_missing_paths_do_not_resolve(self, tmp_path):
        assert normalize_finding_path(None, tmp_path) is None
        assert normalize_finding_path("   ", tmp_path) is None

    def test_grounding_fails_closed_on_an_unavailable_diff(self, tmp_path):
        assert is_diff_grounded("src/a.py", None, tmp_path) is False
        assert ungrounded_reason("src/a.py", None, tmp_path) == "story diff unavailable"

    def test_grounding_matches_a_normalized_path(self, tmp_path):
        changed = frozenset(["src/a.py"])
        assert is_diff_grounded("./src/a.py", changed, tmp_path) is True
        assert is_diff_grounded(str(tmp_path / "src" / "a.py"), changed, tmp_path) is True
        assert is_diff_grounded("src/b.py", changed, tmp_path) is False

    def test_ungrounded_reason_names_the_normalized_path(self, tmp_path):
        reason = ungrounded_reason("./src/b.py", frozenset(["src/a.py"]), tmp_path)
        assert "src/b.py" in reason
        assert "not in story diff" in reason

    def test_ungrounded_reason_reports_an_unresolvable_citation(self, tmp_path):
        assert (
            ungrounded_reason(None, frozenset(["src/a.py"]), tmp_path)
            == "no resolvable file cited"
        )


def test_changed_file_set_reads_the_collect_changed_files_shape(tmp_path):
    """Renames are decomposed upstream, so both sides appear as ordinary paths."""
    from theforge.coordinator.diff_grounding import story_changed_files

    snapshot = {
        "base_ref": "b" * 40,
        "head_ref": "a" * 40,
        "files": [
            {"path": "src/old.py", "insertions": 0, "deletions": 9, "binary": False},
            {"path": "src/new.py", "insertions": 9, "deletions": 0, "binary": False},
        ],
    }
    with patch("theforge.coordinator.diff_grounding.collect_changed_files", return_value=snapshot):
        assert story_changed_files(Path(tmp_path), "main") == frozenset(
            ["src/old.py", "src/new.py"]
        )

    with patch("theforge.coordinator.diff_grounding.collect_changed_files", return_value=None):
        assert story_changed_files(Path(tmp_path), "main") is None
