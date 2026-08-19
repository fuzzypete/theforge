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
- Batch group: a member is judged against its own commits, not the group's
- Batch group: an absent or hostile handoff grounds nothing, never the branch
- Grounding is per-cycle: a P1 blocks once the change grows to include its file
- Path normalization unit cases
"""

from __future__ import annotations

import subprocess
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
from theforge.coordinator.batch_diff import BatchReviewContext
from theforge.coordinator.diff_grounding import (
    RESTORED_DISPOSITION,
    StoryDiff,
    ground_p1_records,
    is_diff_grounded,
    normalize_finding_path,
    ungrounded_reason,
)
from theforge.coordinator.engine import run_from_review, run_review_only
from theforge.coordinator.state import FindingRecord, Phase

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


class TestGroundingIsPerCycleNotSticky:
    """Grounding describes a cycle's diff, not the finding.

    The same P1 can be out of the diff in cycle 1 and inside it in cycle 2
    because the dev touched the file it cites in between. The verdict must be
    re-decided every cycle in both directions, or a finding that becomes
    genuinely about this change stays permanently unblockable (#2525).
    """

    def test_a_p1_blocks_once_the_change_grows_to_include_its_file(self, tmp_path):
        """Cycle 1 suppresses it; cycle 2 must re-decide rather than inherit.

        Cycle 1 raises two P1s: one in the diff (which blocks and buys the dev
        retry) and one citing ``src/late.py``, which the story has not touched
        yet and is therefore suppressed. The dev iteration touches that file.
        Cycle 2 re-reports only the ``src/late.py`` finding — now squarely about
        this change, so it must block. Inheriting cycle 1's verdict would let the
        run complete on a suppression that is no longer true.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        cycle1 = f"""\
```yaml
verdict: REQUEST_CHANGES
summary: "Changes requested."
findings:
  - severity: P1
    file: src/changed.py
    line: 30
    observed: "{_SIBLING_DESC}"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix it"
  - severity: P1
    file: src/late.py
    line: 12
    observed: "{_OWN_DESC}"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix it"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""
        cycle2 = _review_yaml(file="src/late.py", observed=_OWN_DESC, matches_spec=True)

        # The story has not touched src/late.py when cycle 1 runs; the dev
        # iteration between the cycles adds it.
        cycle_diff = {"files": ["src/changed.py"]}
        pool_calls = {"n": 0}

        with (
            patch_gate_shell() as mock_shell,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
        ):

            def shell_side_effect(cmd, cwd, **kwargs):
                return _shell_with_gate(workspace, "PASS", changed_files=cycle_diff["files"])(
                    cmd, cwd, **kwargs
                )

            def dev_side_effect(*_args, **_kwargs):
                cycle_diff["files"] = ["src/changed.py", "src/late.py"]
                return _make_agent_result(success=True, output="Fixed.")

            def pool_side_effect(**_kwargs):
                pool_calls["n"] += 1
                output = cycle1 if pool_calls["n"] == 1 else cycle2
                return [_make_agent_result(success=True, output=output, profile_name="review")]

            mock_shell.side_effect = shell_side_effect
            mock_preflight.return_value = _PREFLIGHT_RESULT
            mock_dev.side_effect = dev_side_effect
            mock_pool.side_effect = pool_side_effect
            result = run_from_review(config, task, workspace)

        assert pool_calls["n"] == 2, "cycle 1's grounded P1 should have bought a dev retry"
        # Cycle 2's finding grounds now, so it blocks; the run exhausts its
        # cycles and escalates instead of completing on a stale suppression.
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert _dispositions(config, task, result)[_OWN_DESC] != "diff_ungrounded"

        # Cycle 2's grounding record shows nothing suppressed — the verdict was
        # re-decided against the grown diff, not inherited from cycle 1.
        grounding = generate_audit_log(config, task, result)["review_diff_grounding"]
        assert grounding["review_cycle"] == 2
        assert grounding["ungrounded_p1_ids"] == []
        assert "src/late.py" in grounding["files"]

    def test_grounding_clears_a_stale_verdict_and_records_the_reversal(self, tmp_path):
        """Unit-level: the backstop that makes the suppression reversible.

        Whatever disposition a record arrives with, grounding owns the answer for
        this cycle. A record that grounds must never keep a ``diff_ungrounded``
        written in an earlier one.
        """
        record = FindingRecord(
            finding_id="abc123",
            cycle_first_seen=1,
            cycle_last_seen=2,
            file="src/late.py",
            line=10,
            severity="P1",
            description="now about this change",
            reporter="reviewer-a",
            disposition="diff_ungrounded",
        )

        grounding = ground_p1_records(
            [record],
            tmp_path,
            "main",
            story_diff=StoryDiff(files=frozenset(["src/late.py"]), source="branch_diff"),
        )

        assert record.disposition == RESTORED_DISPOSITION == "unresolved"
        assert grounding.restored == (record,)
        assert grounding.ungrounded == ()
        assert grounding.only_ungrounded is False

    def test_a_record_still_out_of_the_diff_stays_suppressed(self, tmp_path):
        """The other direction: re-grounding does not indiscriminately unsuppress."""
        record = FindingRecord(
            finding_id="abc123",
            cycle_first_seen=1,
            cycle_last_seen=2,
            file="src/sibling.py",
            line=10,
            severity="P1",
            description="still someone else's change",
            reporter="reviewer-a",
            disposition="unresolved",
        )

        grounding = ground_p1_records(
            [record],
            tmp_path,
            "main",
            story_diff=StoryDiff(files=frozenset(["src/changed.py"]), source="branch_diff"),
        )

        assert record.disposition == "diff_ungrounded"
        assert grounding.ungrounded == (record,)
        assert grounding.restored == ()


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


# ── Batch groups: a member is judged against its own commits ─────────────────


def _init_batch_repo(path: Path) -> dict[str, str]:
    """One branch carrying two independent stories, as a batch group produces."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "forge/leader"], cwd=path, check=True)

    shas: dict[str, str] = {}
    for slug, relpath in (("test-task", "src/mine.py"), ("sibling-story", "src/sibling.py")):
        target = path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"feat({slug}): work"], cwd=path, check=True)
        shas[slug] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
        ).stdout.strip()
    return shas


_SIBLING_FILE_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Changes requested."
findings:
  - severity: P1
    file: src/sibling.py
    line: 12
    observed: "The sibling story's acceptance criterion is unmet"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Restore the field"
story_compliance:
  matches_spec: false
  mismatches:
    - "Acceptance criterion is unmet"
test_coverage:
  adequate: true
  gaps: []
```
"""

_OWN_FILE_REVIEW = _SIBLING_FILE_REVIEW.replace("src/sibling.py", "src/mine.py")


def _run_batch_member(
    tmp_path,
    review_yaml: str,
    attribution: list[dict] | None,
    *,
    raw_handoff: dict | None = None,
):
    """Review one batch member on the shared worktree.

    ``attribution`` names which slugs the shared handoff attributes commits to;
    the real SHAs are substituted from the repo. ``attribution=None`` with no
    ``raw_handoff`` is the case where the dev pass produced no structured
    handoff at all — still a batch member, just one with nothing to attribute.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)  # slug "test-task"
    workspace = tmp_path / "shared-worktree"
    workspace.mkdir()
    shas = _init_batch_repo(workspace)

    handoff = raw_handoff
    if attribution is not None:
        handoff = {
            "commits": [
                {"sha": shas[entry["slug"]], "slug": entry["slug"]} for entry in attribution
            ]
        }

    with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
        mock_pool.return_value = [
            _make_agent_result(success=True, output=review_yaml, profile_name="review")
        ]
        result = run_review_only(
            config,
            task,
            workspace,
            branch_name="forge/leader",
            batch_context=BatchReviewContext(dev_handoff=handoff),
        )
    return config, task, result


class TestBatchMemberGrounding:
    def test_sibling_member_s_file_does_not_escalate_this_member(self, tmp_path):
        """The reviewer cites a file changed by another member of the same batch.

        It is in the shared branch's diff, so grounding against the branch would
        treat it as this member's own change and fail the wrong story. Per-story
        commit attribution is what keeps them apart.
        """
        config, task, result = _run_batch_member(
            tmp_path,
            _SIBLING_FILE_REVIEW,
            [{"slug": "test-task"}, {"slug": "sibling-story"}],
        )

        assert result.success is True
        assert result.phase == Phase.DONE
        assert [r.disposition for r in result.state.finding_registry] == ["diff_ungrounded"]

        audit = generate_audit_log(config, task, result)
        grounding = audit["review_diff_grounding"]
        # The record names what the finding was checked against, so the
        # suppression can be re-derived rather than taken on trust.
        assert grounding["source"] == "batch_commit_attribution"
        assert grounding["files"] == ["src/mine.py"]
        assert grounding["available"] is True

    def test_this_member_s_own_file_still_blocks(self, tmp_path):
        """Non-regression: narrowing the set must not disarm review inside it."""
        _config, _task, result = _run_batch_member(
            tmp_path,
            _OWN_FILE_REVIEW,
            [{"slug": "test-task"}, {"slug": "sibling-story"}],
        )

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert [r.disposition for r in result.state.finding_registry] != ["diff_ungrounded"]

    def test_absent_handoff_does_not_fall_back_to_the_shared_branch_diff(self, tmp_path):
        """The shared dev pass produced no structured handoff at all.

        Membership is what decides how a story is grounded, not whether the
        handoff arrived. The member is still on a branch carrying its siblings'
        work, so its file set is unknown — never the branch diff, which contains
        src/sibling.py and would ground a sibling's finding against it.
        """
        config, task, result = _run_batch_member(tmp_path, _SIBLING_FILE_REVIEW, None)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert [r.disposition for r in result.state.finding_registry] == ["diff_ungrounded"]

        audit = generate_audit_log(config, task, result)
        grounding = audit["review_diff_grounding"]
        assert grounding["source"] == "batch_commit_attribution"
        assert grounding["available"] is False
        assert grounding["files"] is None

    def test_a_non_batch_review_still_uses_the_branch_diff(self, tmp_path):
        """No batch context: the story owns its worktree, so the branch diff is its own."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "shared-worktree"
        workspace.mkdir()
        _init_batch_repo(workspace)

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=_OWN_FILE_REVIEW, profile_name="review")
            ]
            result = run_review_only(config, task, workspace, branch_name="forge/leader")

        assert result.success is False
        audit = generate_audit_log(config, task, result)
        assert audit["review_diff_grounding"]["source"] == "branch_diff"

    def test_a_hostile_commit_id_in_the_handoff_is_never_executed(self, tmp_path):
        """The handoff is agent output; a "sha" that is a command must stay data."""
        marker = tmp_path / "pwned"
        config, task, result = _run_batch_member(
            tmp_path,
            _OWN_FILE_REVIEW,
            None,
            raw_handoff={"commits": [{"sha": f"abc1234; touch {marker}", "slug": "test-task"}]},
        )

        assert not marker.exists()
        # Refused as attribution, so the member is grounded against nothing
        # rather than against the shared branch.
        audit = generate_audit_log(config, task, result)
        assert audit["review_diff_grounding"]["available"] is False
        assert [r.disposition for r in result.state.finding_registry] == ["diff_ungrounded"]

    def test_unattributed_commits_make_every_finding_ungrounded(self, tmp_path):
        """A handoff that names no slugs cannot split the branch by story."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "shared-worktree"
        workspace.mkdir()
        _init_batch_repo(workspace)

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(success=True, output=_OWN_FILE_REVIEW, profile_name="review")
            ]
            result = run_review_only(
                config,
                task,
                workspace,
                branch_name="forge/leader",
                batch_context=BatchReviewContext(
                    dev_handoff={"commits": [{"sha": "abc1234", "message": "feat: work"}]}
                ),
            )

        assert result.success is True
        assert [r.disposition for r in result.state.finding_registry] == ["diff_ungrounded"]
        audit = generate_audit_log(config, task, result)
        assert audit["review_diff_grounding"]["available"] is False

    def test_member_with_no_commits_still_escalates(self, tmp_path):
        """A story that demonstrably produced nothing has no work to approve.

        Its file set is known and empty, so every finding is ungrounded — but
        "no finding is about this change" is only a reason to stop blocking when
        there IS a change. Review must stay free to fail an unimplemented member.
        """
        config, task, result = _run_batch_member(
            tmp_path,
            _SIBLING_FILE_REVIEW,
            [{"slug": "sibling-story"}],
        )

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        audit = generate_audit_log(config, task, result)
        assert audit["review_diff_grounding"]["files"] == []
        assert audit["review_diff_grounding"]["available"] is True
