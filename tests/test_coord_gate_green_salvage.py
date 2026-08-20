"""Gate-green salvage: land the last validated commit instead of failing (#2028).

A story whose gate passed at commit ``C``, whose review approved ``C``, and whose
final P2-cleanup iteration then turned the gate red used to be reported FAILED
with every commit discarded. These tests pin the four acceptance criteria:

* a terminal gate failure after a reviewed gate-green commit lands that commit;
* the outcome is distinguishable in the landing record and names what was dropped;
* the rollback really resets the branch, and only the checkpoint merges;
* a story with no *reviewed* gate-green commit still fails exactly as before.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _gate_side_effect,
    _make_agent_result,
    _make_config,
    _make_task,
    patch_gate_shell,
)

from theforge.coordinator.gate_green_salvage import (
    GATE_GREEN_LANDING_PATH,
    SALVAGE_PENDING,
    annotate_gate_green_landing,
    apply_gate_green_rollback,
    record_gate_green_checkpoint,
    salvage_gate_green_landing,
)
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    GateGreenCheckpoint,
    Phase,
    RetryReason,
    ReviewCycleMetadata,
    ReviewedCommitVerification,
)
from theforge.review import ReviewFinding, ReviewResult

APPROVE_WITH_P2 = """\
```yaml
verdict: APPROVE
summary: "Approved; one advisory left."
findings:
  - severity: P2
    file: src/foo.py
    line: 12
    observed: "Restored-session reconcile is not covered"
    expected: "Reconcile semantics documented in the module docstring"
    evidence: "src/foo.py:12 has no note"
    suggestion: "Document it"
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
ac_verification:
  - criterion: "Implementation satisfies the spec"
    status: VERIFIED
    evidence: "diff hunks present"
```
"""


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _approve_review(p2: int = 1) -> ReviewResult:
    return ReviewResult(
        verdict="APPROVE",
        summary="ok",
        findings=[
            ReviewFinding(
                severity="P2",
                file=f"src/file{i}.py",
                line=i,
                observed=f"P2 issue {i}",
                suggestion=None,
            )
            for i in range(p2)
        ],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _cycle_meta(*, reviewed: str, gate_commit: str, gate_decision: str) -> ReviewCycleMetadata:
    meta = ReviewCycleMetadata(pool_models=["r"], successful=["r"], failed=[], synthesized=False)
    meta.reviewed_commit = reviewed
    meta.verification = ReviewedCommitVerification.derive(
        reviewed_commit=reviewed,
        gate_commit=gate_commit,
        gate_decision=gate_decision,
        gate_runs=1,
    )
    return meta


def _state_at_terminal_gate_fail(
    *, checkpoint: GateGreenCheckpoint | None, reason: str = "p2_cleanup"
) -> CoordinatorState:
    state = CoordinatorState()
    state.branch_name = "forge/issue-246"
    state.retry_reason = RetryReason.GATE_FAIL
    state.gate_decisions = ["PASS", "FAIL"]
    # The PASS ran on "a"*40; the FAIL that ended the story ran on "b"*40. The
    # two are deliberately different so nothing can pass by reading the latest
    # gate commit and calling it the green one.
    state.validation_runs = [
        {"profile": "complete", "result": "PASS", "commit": "a" * 40, "skipped": False},
        {"profile": "complete", "result": "FAIL", "commit": "b" * 40, "skipped": False},
    ]
    state.last_gate_commit = "b" * 40
    state.last_gate_decision = "FAIL"
    state.gate_green_checkpoint = checkpoint
    state.validate_blocks = [{"kind": "gate", "outcome": "terminal", "reason": reason}]
    state.error = "Gate returned FAIL after 3 gate run(s); P2 cleanup ..."
    state.phase = Phase.ESCALATE
    return state


def _state_at_cleanup_refusal(*, checkpoint: GateGreenCheckpoint | None) -> CoordinatorState:
    state = CoordinatorState()
    state.branch_name = "forge/issue-246"
    state.retry_reason = RetryReason.P2_CLEANUP
    state.gate_decisions = ["PASS"]
    state.validation_runs = [
        {"profile": "complete", "result": "PASS", "commit": "a" * 40, "skipped": False}
    ]
    state.last_gate_commit = "a" * 40
    state.last_gate_decision = "PASS"
    state.gate_green_checkpoint = checkpoint
    state.error_type = "allocation_exhausted"
    state.error = "Story allocation exhausted: cleanup dev attempt was not funded."
    state.phase = Phase.ESCALATE
    return state


def _checkpoint(commit: str = "a" * 40, p2: int = 1) -> GateGreenCheckpoint:
    return GateGreenCheckpoint(
        commit=commit,
        review_cycle=1,
        dev_iterations_spent=2,
        review_verdict="APPROVE",
        carried_p2_count=p2,
        branch_name="forge/issue-246",
        review_result=_approve_review(p2),
    )


def _escalated_result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(
        success=False, phase=Phase.ESCALATE, state=state, message=state.error or ""
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo_with_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """A worktree whose HEAD is a gate-red commit on top of a gate-green one."""
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-b", "work")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("green\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "feat: the gate-green change")
    green = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_text("p2 fix\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "fix: P2 cleanup that broke the gate")
    red = _git(repo, "rev-parse", "HEAD")
    return repo, green, red


# ── 1. Checkpoint capture ─────────────────────────────────────────────────────


class TestCheckpointCapture:
    def test_records_commit_the_gate_passed_and_the_review_approved(self):
        state = CoordinatorState()
        state.branch_name = "forge/issue-246"
        state.review_cycle = 1
        state.review_cycle_metadata = [
            _cycle_meta(reviewed="a" * 40, gate_commit="a" * 40, gate_decision="PASS")
        ]

        cp = record_gate_green_checkpoint(state, _approve_review(2), carried_p2_count=2)

        assert cp is not None
        assert cp.commit == "a" * 40
        assert cp.carried_p2_count == 2
        assert cp.review_verdict == "APPROVE"
        assert state.gate_green_checkpoint is cp

    def test_declines_when_gate_passed_on_a_different_commit(self):
        """The #2052 gate_stale shape: VALIDATE's post-PASS auto-commit moved HEAD.

        The gate ran on the parent, so the approving reviewers judged a tree no
        gate has ever seen. Checkpointing the gate's commit would reset the
        branch onto a tree nobody approved.
        """
        state = CoordinatorState()
        state.review_cycle_metadata = [
            _cycle_meta(reviewed="b" * 40, gate_commit="a" * 40, gate_decision="PASS")
        ]

        assert record_gate_green_checkpoint(state, _approve_review(), carried_p2_count=1) is None
        assert state.gate_green_checkpoint is None
        assert state.review_cycle_metadata[0].verification.state == "gate_stale"

    def test_declines_when_gate_did_not_pass(self):
        state = CoordinatorState()
        state.review_cycle_metadata = [
            _cycle_meta(reviewed="a" * 40, gate_commit="a" * 40, gate_decision="FAIL")
        ]

        assert record_gate_green_checkpoint(state, _approve_review(), carried_p2_count=1) is None

    def test_declines_when_gate_was_skipped_by_override(self):
        state = CoordinatorState()
        state.review_cycle_metadata = [
            _cycle_meta(reviewed="a" * 40, gate_commit="a" * 40, gate_decision="SKIPPED")
        ]

        assert record_gate_green_checkpoint(state, _approve_review(), carried_p2_count=1) is None

    def test_declines_when_no_review_cycle_has_run(self):
        assert (
            record_gate_green_checkpoint(CoordinatorState(), _approve_review(), carried_p2_count=1)
            is None
        )


# ── 2. Terminal-failure conversion ────────────────────────────────────────────


class TestSalvageDecision:
    def _salvage(self, state, config, task, *, workspace, auto_merge=True):
        return salvage_gate_green_landing(
            state,
            config,
            task,
            _escalated_result(state),
            workspace_path=workspace,
            branch_name="forge/issue-246",
            auto_merge=auto_merge,
        )

    def test_terminal_gate_fail_with_checkpoint_becomes_pending_landing(self, tmp_path):
        """AC1: the gate-green commit lands instead of the story failing."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.landing_status == "pending_integration"
        assert result.merge == {"action": "merge", "pending": True}
        # The audit and state serializers read the *state's* phase; a story
        # reported LANDED must not still carry ESCALATE.
        assert state.phase == Phase.DONE
        assert state.error is None
        # merge-pr fails closed without a review; the checkpoint's approval is
        # the one this lands on.
        assert state.landing_review_source == "gate_green_checkpoint"
        assert state.landing_review_result is state.gate_green_checkpoint.review_result

        salvage = state.gate_green_salvage
        assert salvage["status"] == SALVAGE_PENDING
        assert salvage["checkpoint_commit"] == "a" * 40
        assert salvage["dropped_head"] == "c" * 40
        assert salvage["outstanding_p2_count"] == 1
        assert "no dev iterations remained" in salvage["dropped_reason"]
        # The escalation this supersedes is named, not silently dropped.
        assert salvage["superseded_escalation"].startswith("Gate returned FAIL")

    def test_no_checkpoint_still_fails_unchanged(self, tmp_path):
        """AC4: a story with no reviewed gate-green commit is untouched."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=None)
        state.gate_decisions = ["FAIL", "FAIL"]
        state.validation_runs = [
            {"profile": "complete", "result": "FAIL", "commit": "b" * 40, "skipped": False}
        ]
        state.last_gate_commit = None

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert state.phase == Phase.ESCALATE
        assert state.gate_green_salvage is None
        assert state.gate_green_salvage_declined is None

    def test_cleanup_refusal_with_checkpoint_becomes_pending_landing(self, tmp_path):
        """A funded APPROVE floor survives when optional cleanup is refused pre-DEV."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_cleanup_refusal(checkpoint=_checkpoint())

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.landing_status == "pending_integration"
        assert state.phase == Phase.DONE
        salvage = state.gate_green_salvage
        assert salvage["checkpoint_commit"] == "a" * 40
        assert salvage["dropped_head"] == "c" * 40
        assert salvage["block_reason"] == "allocation_exhausted"
        assert "refused before it ran" in salvage["dropped_reason"]

    def test_cleanup_refusal_without_checkpoint_still_fails_unchanged(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_cleanup_refusal(checkpoint=None)

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert state.gate_green_salvage is None

    def test_gate_green_but_never_approved_records_the_near_miss(self, tmp_path):
        """A gate that went green on a commit no review approved is not landable.

        It still fails — but the operator can tell that from "salvageable but
        unapproved" rather than reading it as "nothing gate-green existed".
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=None)

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        declined = state.gate_green_salvage_declined
        assert declined["reason"] == "gate_green_never_approved"
        # The message must name the commit that actually went green, not the one
        # the *latest* gate judged — on this story that is the failing commit.
        assert declined["detail"].startswith("the gate passed at aaaaaaaa")
        assert "b" * 8 not in declined["detail"]

    def test_suppressed_gate_is_not_reported_as_gate_green(self, tmp_path):
        """A story ``gate:`` override is a skipped gate, not a passing one.

        ``gate_decisions`` gains a synthetic ``PASS`` on the skip path, so
        reading it would tell the operator a gate passed at a commit no gate
        ever ran on.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=None)
        state.validation_runs = [
            {"profile": "skipped", "result": "SKIPPED", "commit": "a" * 40, "skipped": True},
            {"profile": "complete", "result": "FAIL", "commit": "b" * 40, "skipped": False},
        ]

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage_declined is None

    def test_legacy_state_without_validation_runs_claims_no_gate_green(self, tmp_path):
        """A pre-#2358 record carries no validation runs; absence is not a pass."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=None)
        state.validation_runs = []

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage_declined is None

    def test_non_gate_failure_is_not_salvaged(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())
        state.retry_reason = RetryReason.CONVENTION_VIOLATIONS

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage is None

    def test_non_terminal_block_is_not_salvaged(self, tmp_path):
        """A gate failure the dev can still fix must go back to the dev."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())
        state.validate_blocks = [
            {"kind": "gate", "outcome": "opened_review_cycle", "reason": "review_cycle_bought"}
        ]

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage is None

    def test_batch_group_leader_declines(self, tmp_path):
        """A shared branch carries other stories' commits; a reset would drop them
        after they were reviewed against a tree that never lands."""
        config = _make_config(tmp_path)
        task = dataclasses.replace(_make_task(tmp_path), batch_group="batch-1")
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage is None
        assert state.gate_green_salvage_declined["reason"] == "batch_group_leader"

    def test_head_already_at_checkpoint_declines(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())

        with patch_gate_shell(side_effect=_shell_at_head("a" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage_declined["reason"] == "head_is_checkpoint"

    def test_dirty_worktree_declines(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40, dirty=" M src/foo.py")):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage_declined["reason"] == "dirty_worktree"

    def test_checkpoint_not_ancestor_declines(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40, ancestor=False)):
            result = self._salvage(state, config, task, workspace=tmp_path)

        assert result.success is False
        assert state.gate_green_salvage_declined["reason"] == "checkpoint_not_ancestor"

    def test_on_approve_none_declines(self, tmp_path):
        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config, workspace=dataclasses.replace(config.workspace, on_approve="none")
        )
        task = _make_task(tmp_path)
        state = _state_at_terminal_gate_fail(checkpoint=_checkpoint())

        with patch_gate_shell(side_effect=_shell_at_head("c" * 40)):
            result = self._salvage(state, config, task, workspace=tmp_path, auto_merge=False)

        assert result.success is False
        assert state.gate_green_salvage_declined["reason"] == "landing_disabled"


def _shell_at_head(head: str, *, dirty: str = "", ancestor: bool = True):
    """4-tuple ``_run_shell_detailed`` side_effect for the salvage git queries."""

    def side_effect(cmd, cwd, **kwargs):
        if "rev-parse HEAD" in cmd:
            return (True, head + "\n", 0, False)
        if "git status --porcelain" in cmd:
            return (True, dirty, 0, False)
        if "merge-base --is-ancestor" in cmd:
            return (ancestor, "", 0 if ancestor else 1, False)
        return (True, "OK", 0, False)

    return side_effect


# ── 3. Landing-time rollback (real git) ───────────────────────────────────────


class TestRollback:
    def _pending_state(self, green: str, red: str, *, p2: int = 1) -> CoordinatorState:
        state = CoordinatorState()
        state.gate_green_checkpoint = _checkpoint(green, p2)
        state.gate_green_salvage = {
            "status": SALVAGE_PENDING,
            "checkpoint": state.gate_green_checkpoint.to_audit_dict(),
            "checkpoint_commit": green,
            "dropped_head": red,
            "dropped_reason": "the final dev iteration left the gate red",
            "outstanding_p2_count": p2,
        }
        return state

    def test_resets_branch_to_checkpoint_and_records_dropped_commits(self, tmp_path):
        repo, green, red = _repo_with_two_commits(tmp_path)
        state = self._pending_state(green, red)

        out = apply_gate_green_rollback(
            state, repo, effective_on_approve="merge", base_branch="main"
        )

        assert out["ok"] is True
        assert out["checkpoint_commit"] == green
        assert out["landed_commit"] == green
        assert out["dropped_head"] == red
        assert out["dropped_commit_count"] == 1
        assert out["dropped_commits"][0]["sha"] == red
        assert "P2 cleanup that broke the gate" in out["dropped_commits"][0]["subject"]
        # The branch really moved: only the checkpoint is reachable now, so only
        # its commit can merge.
        assert _git(repo, "rev-parse", "HEAD") == green
        assert not (repo / "b.txt").exists()

    def test_refuses_when_checkpoint_is_not_an_ancestor(self, tmp_path):
        repo, _green, red = _repo_with_two_commits(tmp_path)
        state = self._pending_state("0" * 40, red)

        out = apply_gate_green_rollback(
            state, repo, effective_on_approve="merge", base_branch="main"
        )

        assert out["ok"] is False
        assert "not an ancestor" in out["error"]
        assert _git(repo, "rev-parse", "HEAD") == red

    def test_refuses_over_uncommitted_tracked_changes(self, tmp_path):
        repo, green, red = _repo_with_two_commits(tmp_path)
        (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")
        state = self._pending_state(green, red)

        out = apply_gate_green_rollback(
            state, repo, effective_on_approve="merge", base_branch="main"
        )

        assert out["ok"] is False
        assert "uncommitted tracked changes" in out["error"]
        assert _git(repo, "rev-parse", "HEAD") == red

    def test_merge_pr_on_advanced_base_does_not_claim_the_checkpoint_sha_landed(self, tmp_path):
        """merge-pr rebases before force-pushing; an advanced base rewrites the SHA.

        Reporting the checkpoint as ``landed_commit`` would name a commit GitHub
        never merged, so the identity is dropped and the rebase recorded instead.
        """
        repo, green, red = _repo_with_two_commits(tmp_path)
        # Simulate an origin/main that HEAD is not a descendant of.
        _git(repo, "branch", "main", green)
        _git(repo, "update-ref", "refs/remotes/origin/main", "main")
        (repo / "c.txt").write_text("base moved\n", encoding="utf-8")
        _git(repo, "add", "c.txt")
        _git(repo, "commit", "-m", "chore: base advanced", "--", "c.txt")
        advanced = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/main", advanced)
        _git(repo, "reset", "--hard", red)

        state = self._pending_state(green, red)
        out = apply_gate_green_rollback(
            state, repo, effective_on_approve="merge-pr", base_branch="main"
        )

        assert out["ok"] is True
        assert out["checkpoint_commit"] == green
        assert out["rebase_expected"] is True
        assert out["landed_commit"] is None

    def test_merge_pr_on_current_base_keeps_the_checkpoint_identity(self, tmp_path):
        repo, green, red = _repo_with_two_commits(tmp_path)
        _git(repo, "branch", "main", green)
        _git(repo, "update-ref", "refs/remotes/origin/main", "main")

        state = self._pending_state(green, red)
        out = apply_gate_green_rollback(
            state, repo, effective_on_approve="merge-pr", base_branch="main"
        )

        assert out["ok"] is True
        assert out["rebase_expected"] is False
        assert out["landed_commit"] == green


class TestAnnotation:
    def test_fresh_merge_takes_the_rollback_label_and_keeps_the_underlying_path(self):
        merged = annotate_gate_green_landing(
            {"merged": True, "landing_path": "fresh-merge", "pr_url": "u"},
            {"ok": True, "checkpoint_commit": "a" * 40, "dropped_commit_count": 1},
        )

        assert merged["landing_path"] == GATE_GREEN_LANDING_PATH
        assert merged["underlying_landing_path"] == "fresh-merge"
        assert merged["gate_green_rollback"]["checkpoint_commit"] == "a" * 40

    def test_guard_short_circuit_keeps_its_own_path(self):
        """A zero-delta guard did not ship the checkpoint; labelling it as a
        successful salvage would report a short-circuit as landed work."""
        merged = annotate_gate_green_landing(
            {"merged": False, "landing_path": "zero-delta"},
            {"ok": True, "checkpoint_commit": "a" * 40},
        )

        assert merged["landing_path"] == "zero-delta"
        assert "underlying_landing_path" not in merged
        assert merged["gate_green_rollback"]["checkpoint_commit"] == "a" * 40


# ── 4. land_story seam ────────────────────────────────────────────────────────


class TestLandStorySeam:
    def test_land_story_resets_then_merges_only_the_checkpoint(self, tmp_path):
        from theforge.coordinator.completion import land_story

        repo, green, red = _repo_with_two_commits(tmp_path)
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.gate_green_checkpoint = _checkpoint(green)
        state.gate_green_salvage = {
            "status": SALVAGE_PENDING,
            "checkpoint": state.gate_green_checkpoint.to_audit_dict(),
            "checkpoint_commit": green,
            "dropped_head": red,
            "dropped_reason": "the final gate was red with no dev iterations remaining",
            "outstanding_p2_count": 1,
        }

        seen: dict = {}

        def fake_merge_branch(project_root, base, branch, slug, wt, **kwargs):
            seen["head_at_merge"] = _git(repo, "rev-parse", "HEAD")
            return {"merged": True, "landing_path": "fresh-merge", "error": None}

        with (
            patch("theforge.coordinator.completion._merge_branch", fake_merge_branch),
            patch("theforge.coordinator.completion.capture_changed_files"),
        ):
            merge_info, landing_status = land_story(
                config, task, "forge/issue-246", repo, _approve_review(), state, "merge"
            )

        assert landing_status == "landed"
        # The tree that merged is the checkpoint, not the gate-red HEAD.
        assert seen["head_at_merge"] == green
        assert merge_info["landing_path"] == GATE_GREEN_LANDING_PATH
        rollback = merge_info["gate_green_rollback"]
        assert rollback["landed_commit"] == green
        assert rollback["dropped_head"] == red
        assert [c["sha"] for c in rollback["dropped_commits"]] == [red]
        assert state.gate_green_salvage["status"] == "applied"

    def test_land_story_fails_closed_when_the_rollback_cannot_run(self, tmp_path):
        from theforge.coordinator.completion import land_story

        repo, _green, red = _repo_with_two_commits(tmp_path)
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.gate_green_salvage = {
            "status": SALVAGE_PENDING,
            "checkpoint_commit": "0" * 40,
            "dropped_head": red,
        }

        with patch("theforge.coordinator.completion._merge_branch") as merge_branch:
            merge_info, landing_status = land_story(
                config, task, "forge/issue-246", repo, _approve_review(), state, "merge"
            )

        # Landing the gate-red HEAD is exactly what this path prevents.
        assert merge_branch.call_count == 0
        assert landing_status == "failed"
        assert merge_info["landing_path"] == "gate-green-rollback-failed"
        assert _git(repo, "rev-parse", "HEAD") == red


# ── 5. End-to-end coordinator seam ────────────────────────────────────────────


def _shell_with_moving_head(workspace: Path, decisions: list[str], head: dict):
    """Gate side_effect whose ``git rev-parse HEAD`` reads a caller-owned SHA.

    The real loop commits between iterations; the mocked one cannot, so the dev
    mock advances ``head`` instead. That keeps the property the checkpoint
    depends on honest: the reviewed commit equals the gated commit only while
    nothing has moved the tree since the gate ran.
    """
    base = _gate_side_effect(workspace, decisions)

    def side_effect(cmd, cwd, **kwargs):
        if "rev-parse HEAD" in cmd:
            return (True, head["sha"] + "\n", 0, False)
        if "merge-base --is-ancestor" in cmd:
            return (True, "", 0, False)
        return base(cmd, cwd, **kwargs)

    return side_effect


def _dev_advancing_head(head: dict, shas: list[str]):
    """Dev mock that moves HEAD the way a real dev iteration's commit would."""
    calls = {"n": 0}

    def run_dev(*args, **kwargs):
        head["sha"] = shas[min(calls["n"], len(shas) - 1)]
        calls["n"] += 1
        return _make_agent_result(success=True, output="Fixed the P2.")

    return run_dev


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.dev_phase.run_agent")
def test_p2_cleanup_that_breaks_the_gate_lands_the_checkpoint(mock_dev, mock_pool, tmp_path):
    """AC1 end to end: PASS → APPROVE+P2 → cleanup dev → FAIL → landed, not failed.

    This is the #246 shape: the gate is green at the reviewed commit, the review
    approves with a P2, the story spends its remaining dev iterations on that P2
    and turns the gate red, and nothing is left to recover it.
    """
    from theforge.coordinator.engine import run_from_review

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config, retry=dataclasses.replace(config.retry, max_dev_iterations=3)
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    green, red, redder = "b" * 40, "c" * 40, "d" * 40
    head = {"sha": "a" * 40}
    side_effect = _shell_with_moving_head(workspace, ["PASS", "FAIL", "FAIL"], head)

    with patch_gate_shell(side_effect=side_effect):
        mock_dev.side_effect = _dev_advancing_head(head, [green, red, redder])
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_WITH_P2, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, defer_landing=True, auto_merge=True)

    checkpoint = result.state.gate_green_checkpoint
    assert checkpoint is not None
    assert checkpoint.commit == green
    assert result.success is True
    assert result.phase == Phase.DONE
    assert result.state.phase == Phase.DONE
    assert result.landing_status == "pending_integration"
    salvage = result.state.gate_green_salvage
    assert salvage["checkpoint_commit"] == green
    assert salvage["dropped_head"] == redder
    assert salvage["outstanding_p2_count"] >= 1
    assert result.state.landing_review_source == "gate_green_checkpoint"
    # The decline path reads validation_runs to name the gate-green commit;
    # assert against what VALIDATE really wrote, so the helper cannot drift onto
    # a key production never produces.
    passing = [
        r
        for r in result.state.validation_runs
        if r.get("result") == "PASS" and not r.get("skipped")
    ]
    assert [r["commit"] for r in passing] == [green]


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.dev_phase.run_agent")
def test_gate_never_green_still_fails(mock_dev, mock_pool, tmp_path):
    """AC4: no gate-green commit in history → the story fails exactly as before."""
    from theforge.coordinator.engine import run_from_review

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config, retry=dataclasses.replace(config.retry, max_dev_iterations=2)
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    head = {"sha": "a" * 40}
    side_effect = _shell_with_moving_head(workspace, ["FAIL", "FAIL", "FAIL"], head)

    with patch_gate_shell(side_effect=side_effect):
        mock_dev.side_effect = _dev_advancing_head(head, ["b" * 40, "c" * 40])
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_WITH_P2, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, defer_landing=True, auto_merge=True)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert result.state.gate_green_checkpoint is None
    assert result.state.gate_green_salvage is None
    assert result.landing_status is None


@patch("theforge.coordinator.story_budget.nonreview_funding_exhausted")
@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.dev_phase.run_agent")
def test_cleanup_refusal_before_dev_lands_checkpoint(
    mock_dev, mock_pool, mock_nonreview_funds, tmp_path
):
    """AC seam: APPROVE+checkpoint then pre-DEV cleanup refusal lands the floor."""
    from theforge.coordinator.engine import run_from_review

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config, retry=dataclasses.replace(config.retry, max_dev_iterations=3)
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    green = "b" * 40
    head = {"sha": "a" * 40}
    side_effect = _shell_with_moving_head(workspace, ["PASS"], head)

    shortfall = {
        "allocation_usd": 1.69,
        "nonreview_allocation_usd": 0.0,
        "reserved_review_usd": 0.46,
        "reserved_review_cycles": 1,
        "reserved_review_remaining_usd": 0.46,
        "reserved_review_released": False,
        "observed_usd": 1.48,
        "participants": [config.dev_profile.name],
        "phase": "dev",
        "nonreview_exhausted": True,
    }

    with patch_gate_shell(side_effect=side_effect):
        mock_dev.side_effect = _dev_advancing_head(head, [green])
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_WITH_P2, profile_name="review")
        ]
        mock_nonreview_funds.return_value = shortfall

        result = run_from_review(config, task, workspace, defer_landing=True, auto_merge=True)

    checkpoint = result.state.gate_green_checkpoint
    assert checkpoint is not None
    assert checkpoint.commit == green
    assert result.success is True
    assert result.phase == Phase.DONE
    assert result.state.phase == Phase.DONE
    assert result.landing_status == "pending_integration"
    assert mock_dev.call_count == 1
    salvage = result.state.gate_green_salvage
    assert salvage["checkpoint_commit"] == green
    assert salvage["dropped_head"] == green
    assert salvage["block_reason"] == "allocation_exhausted"
    assert salvage["outstanding_p2_count"] >= 1
    assert "refused before it ran" in salvage["dropped_reason"]


# ── 6. Resume persistence ─────────────────────────────────────────────────────


def test_checkpoint_and_pending_salvage_survive_a_resume(tmp_path):
    """A resumed run must not lose the commit it was going to fall back to."""
    from theforge.coordinator.run_setup import load_trajectory_state, save_trajectory_state

    workspace = tmp_path / "wt"
    workspace.mkdir()
    saved = CoordinatorState()
    saved.gate_green_checkpoint = _checkpoint("a" * 40, p2=2)
    saved.gate_green_salvage = {
        "status": SALVAGE_PENDING,
        "checkpoint_commit": "a" * 40,
        "dropped_head": "c" * 40,
        "dropped_reason": "the final gate was red",
        "outstanding_p2_count": 2,
    }
    saved.gate_green_salvage_declined = None

    save_trajectory_state(workspace, saved)
    restored = CoordinatorState()
    load_trajectory_state(workspace, restored)

    assert restored.gate_green_checkpoint is not None
    assert restored.gate_green_checkpoint.commit == "a" * 40
    assert restored.gate_green_checkpoint.carried_p2_count == 2
    # The review the landing must post is restored too; merge-pr fails closed
    # without one.
    assert restored.gate_green_checkpoint.review_result is not None
    assert restored.gate_green_checkpoint.review_result.verdict == "APPROVE"
    assert restored.gate_green_salvage["status"] == SALVAGE_PENDING
    assert restored.gate_green_salvage["dropped_head"] == "c" * 40


def test_legacy_sidecar_without_salvage_keys_restores_nothing(tmp_path):
    from theforge.coordinator.run_setup import load_trajectory_state

    workspace = tmp_path / "wt"
    (workspace / ".forge").mkdir(parents=True)
    (workspace / ".forge" / "trajectory.yaml").write_text("gate_runs: 2\n", encoding="utf-8")

    state = CoordinatorState()
    load_trajectory_state(workspace, state)

    assert state.gate_green_checkpoint is None
    assert state.gate_green_salvage is None
