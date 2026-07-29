"""Pre-story gate visibility: labeled gate logs and live per-story gate phase (#2014).

A sprint can spend many minutes in the baseline gate and in the per-story reuse
gates that resume triage runs, while every gate logs the same resolved command
and `forge status` reports `starting` with all stories `waiting`. These tests
pin both halves of the fix: the gate log line names its purpose and target, and
the live state file shows a concrete phase for the story being gated.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from theforge.cli.sprint_status import _format_sprint_phase, _read_sprint_meta_from_state
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import util as coordinator_util
from theforge.coordinator.gate import run_gate_full
from theforge.coordinator.state import GateLabel
from theforge.sprint.dag import REUSE_GATE_PHASE, _triage_spec
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import (
    SPRINT_PHASE_BASELINE_GATE,
    _publish_reuse_gate_end,
    _publish_reuse_gate_start,
    _run_baseline_gate,
)
from theforge.sprint.sources import FileSource
from theforge.sprint.state_writer import (
    update_state_phase,
    update_state_story,
    write_bootstrap_state,
)
from theforge.sprint.status_reader import read_live_status

# ── Helpers ──────────────────────────────────────────────────────────


def _make_config(tmp_path: Path, gate_command: str = "echo gate") -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            base_branch="main",
        ),
        validation=replace(DEFAULT_VALIDATION, gate_command=gate_command),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_spec_file(tmp_path: Path, slug: str) -> Path:
    spec = tmp_path / f"{slug}.md"
    spec.write_text(
        f"---\nname: Story {slug}\nslug: {slug}\n---\n# Story {slug}\nDo the thing.",
        encoding="utf-8",
    )
    return spec


def _triage_git_mock(commit_line: bytes = b"abc1234 some commit\n"):
    """git stub for a story whose branch has commits ahead of an unmerged base."""

    def _mock_run(cmd, **kwargs):
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1  # not merged
        elif "log" in cmd:
            m.returncode = 0
            m.stdout = commit_line
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    return _mock_run


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[ForgeConfig, ResolvedSprint, str]:
    config = _make_config(tmp_path)
    story_file = tmp_path / "story.md"
    story_file.write_text(
        "---\nname: My Story\nslug: my-story\n---\n# Content\n", encoding="utf-8"
    )
    source = FileSource()
    task = source.fetch("story.md", tmp_path)
    resolved = ResolvedSprint(
        name="Test Sprint",
        budget_usd=10.0,
        stories=[(task, source, "story.md")],
        max_parallel=1,
    )
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base_commit = _git(tmp_path, "rev-parse", "HEAD")
    return config, resolved, base_commit


# ── Gate labeling ────────────────────────────────────────────────────


class TestGateLabel:
    def test_baseline_label_names_merge_base_and_short_sha(self) -> None:
        label = GateLabel(
            purpose="baseline gate",
            target="merge base",
            commit="10fdbd8ae9d06ab628dc15da924fa691c0bcaefd",
        )
        assert label.describe() == "baseline gate on merge base 10fdbd8a"

    def test_reuse_label_names_story_branch_and_commit(self) -> None:
        label = GateLabel(
            purpose="reuse gate",
            slug="issue-50",
            target="feat/issue-50",
            commit="abc1234",
            worktree_path="/repo/.forge/worktrees/issue-50",
        )
        assert label.describe() == "reuse gate for issue-50 on feat/issue-50 abc1234"

    def test_label_falls_back_to_worktree_when_no_branch(self) -> None:
        label = GateLabel(purpose="reuse gate", slug="issue-50", worktree_path="/repo/wt/issue-50")
        assert label.describe() == "reuse gate for issue-50 on /repo/wt/issue-50"


class TestRunGateFullLogging:
    def _capture_gate_log(self, tmp_path: Path, label: GateLabel | None) -> list[str]:
        lines: list[str] = []
        config = _make_config(tmp_path)
        with patch.object(coordinator_util, "_log_verbose", side_effect=lines.append):
            with patch.object(
                coordinator_util,
                "_run_shell_detailed",
                return_value=(True, "ok", 0, False),
            ):
                run_gate_full(config, tmp_path, label=label)
        return lines

    def test_labeled_gate_log_names_purpose_and_target(self, tmp_path: Path) -> None:
        lines = self._capture_gate_log(
            tmp_path,
            GateLabel(
                purpose="reuse gate",
                slug="issue-50",
                target="feat/issue-50",
                commit="abc1234",
            ),
        )
        assert lines == ["Running reuse gate for issue-50 on feat/issue-50 abc1234: echo gate"]

    def test_unlabeled_gate_log_is_unchanged(self, tmp_path: Path) -> None:
        assert self._capture_gate_log(tmp_path, None) == ["Running gate: echo gate"]

    def test_label_does_not_reach_the_shell_command(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        with patch.object(
            coordinator_util, "_run_shell_detailed", return_value=(True, "ok", 0, False)
        ) as shell:
            _decision, _err, _tail, resolved_cmd, _code = run_gate_full(
                config,
                tmp_path,
                label=GateLabel(purpose="reuse gate", slug="issue-50"),
            )
        assert resolved_cmd == "echo gate"
        assert shell.call_args.args[0] == "echo gate"


def test_baseline_gate_log_line_names_the_merge_base(tmp_path: Path) -> None:
    """Seam: the real baseline gate call site labels its gate with the merge base."""
    config, resolved, base_commit = _init_repo(tmp_path)
    lines: list[str] = []

    with patch.object(coordinator_util, "_log_verbose", side_effect=lines.append):
        baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    gate_lines = [line for line in lines if line.startswith("Running ")]
    assert gate_lines == [f"Running baseline gate on merge base {base_commit[:8]}: echo gate"]


def test_triage_labels_the_reuse_gate_with_story_branch_and_commit(tmp_path: Path) -> None:
    """Seam: resume triage's gate call carries the story's identity."""
    _make_spec_file(tmp_path, "issue-50")
    config = _make_config(tmp_path)
    (tmp_path / "issue-50").mkdir()

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_triage_git_mock()):
        with patch(
            "theforge.sprint.dag._run_gate", return_value=("PASS", None, "")
        ) as run_gate_mock:
            triage = _triage_spec("issue-50.md", config, tmp_path)

    assert triage.action == "review"
    label = run_gate_mock.call_args.kwargs["label"]
    assert label.describe() == "reuse gate for issue-50 on feat/issue-50 abc1234"
    assert label.worktree_path == str(tmp_path / "issue-50")


def test_triage_brackets_only_the_gate_with_progress_callbacks(tmp_path: Path) -> None:
    """A story that never reaches the gate publishes no gate progress."""
    _make_spec_file(tmp_path, "issue-50")
    config = _make_config(tmp_path)
    events: list[str] = []

    # No worktree on disk → triage returns "full" before any gate runs.
    with patch("theforge.sprint.dag.subprocess.run", side_effect=_triage_git_mock()):
        triage = _triage_spec(
            "issue-50.md",
            config,
            tmp_path,
            on_gate_start=lambda label: events.append("start"),
            on_gate_end=lambda label: events.append("end"),
        )

    assert triage.action == "full"
    assert events == []


def test_gate_end_callback_fires_even_when_the_gate_raises(tmp_path: Path) -> None:
    _make_spec_file(tmp_path, "issue-50")
    config = _make_config(tmp_path)
    (tmp_path / "issue-50").mkdir()
    events: list[str] = []

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_triage_git_mock()):
        with patch("theforge.sprint.dag._run_gate", side_effect=RuntimeError("boom")):
            try:
                _triage_spec(
                    "issue-50.md",
                    config,
                    tmp_path,
                    on_gate_start=lambda label: events.append("start"),
                    on_gate_end=lambda label: events.append("end"),
                )
            except RuntimeError:
                pass

    assert events == ["start", "end"]


# ── Live status visibility ───────────────────────────────────────────


def _bootstrap(tmp_path: Path, run_id: str = "run-2014") -> str:
    write_bootstrap_state(
        run_id,
        tmp_path,
        sprint_name="Test Sprint",
        sprint_phase="starting",
        issues=[{"number": 50}],
    )
    return run_id


class TestReuseGateStateVisibility:
    def test_story_is_running_in_reuse_gate_phase_while_the_gate_runs(
        self, tmp_path: Path
    ) -> None:
        """Seam: with the real triage callbacks wired, status shows the gated story.

        The stubbed gate reads live status from inside the gate window — exactly
        where the operator's `forge status` lands during a multi-minute gate.
        """
        run_id = _bootstrap(tmp_path)
        _make_spec_file(tmp_path, "issue-50")
        config = _make_config(tmp_path)
        (tmp_path / "issue-50").mkdir()
        observed: list = []

        def _gate_reads_status(*args, **kwargs):
            observed.extend(read_live_status(run_id, tmp_path) or [])
            return ("PASS", None, "")

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_triage_git_mock()):
            with patch("theforge.sprint.dag._run_gate", side_effect=_gate_reads_status):
                _triage_spec(
                    "issue-50.md",
                    config,
                    tmp_path,
                    on_gate_start=lambda label: _publish_reuse_gate_start(run_id, tmp_path, label),
                    on_gate_end=lambda label: _publish_reuse_gate_end(run_id, tmp_path, label),
                )

        during = [entry for entry in observed if entry.slug == "issue-50"]
        assert len(during) == 1
        entry = during[0]
        assert entry.status == "running"
        assert entry.phase == REUSE_GATE_PHASE
        assert entry.stage == "reuse gate"
        assert entry.detail == "validating feat/issue-50 @ abc1234"
        assert entry.elapsed_seconds is not None

    def test_story_returns_to_waiting_once_the_gate_finishes(self, tmp_path: Path) -> None:
        run_id = _bootstrap(tmp_path)
        _make_spec_file(tmp_path, "issue-50")
        config = _make_config(tmp_path)
        (tmp_path / "issue-50").mkdir()

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_triage_git_mock()):
            with patch("theforge.sprint.dag._run_gate", return_value=("PASS", None, "")):
                _triage_spec(
                    "issue-50.md",
                    config,
                    tmp_path,
                    on_gate_start=lambda label: _publish_reuse_gate_start(run_id, tmp_path, label),
                    on_gate_end=lambda label: _publish_reuse_gate_end(run_id, tmp_path, label),
                )

        entries = read_live_status(run_id, tmp_path) or []
        entry = next(e for e in entries if e.slug == "issue-50")
        assert entry.status == "waiting"
        assert entry.phase is None
        assert entry.elapsed_seconds is None

        state = yaml.safe_load((tmp_path / ".forge" / "runs" / f"{run_id}.state").read_text())
        story = next(s for s in state["stories"] if s["slug"] == "issue-50")
        assert story["detail"] == {}
        assert story["outcome"] == "waiting"

    def test_publish_is_a_noop_without_a_run_id(self, tmp_path: Path) -> None:
        label = GateLabel(purpose="reuse gate", slug="issue-50", target="feat/issue-50")
        _publish_reuse_gate_start(None, tmp_path, label)
        _publish_reuse_gate_end(None, tmp_path, label)
        assert not (tmp_path / ".forge" / "runs").exists()

    def test_update_state_story_ignores_unknown_slugs(self, tmp_path: Path) -> None:
        run_id = _bootstrap(tmp_path)
        update_state_story(run_id, tmp_path, "issue-999", status="running")
        entries = read_live_status(run_id, tmp_path) or []
        assert [e.slug for e in entries] == ["issue-50"]
        assert entries[0].status == "waiting"


class TestBaselineGatePhaseVisibility:
    def test_phase_carries_target_and_start_time(self, tmp_path: Path) -> None:
        run_id = _bootstrap(tmp_path)
        update_state_phase(
            run_id,
            tmp_path,
            SPRINT_PHASE_BASELINE_GATE,
            detail="merge base of main",
            started_at="2026-07-28T21:56:41.137562+00:00",
        )

        meta = _read_sprint_meta_from_state(tmp_path / ".forge" / "runs" / f"{run_id}.state")
        assert meta["sprint_phase"] == "baseline-gate"
        assert meta["sprint_phase_detail"] == "merge base of main"
        assert meta["sprint_phase_started_at"] == "2026-07-28T21:56:41.137562+00:00"

    def test_finishing_the_gate_clears_the_transient_phase_context(self, tmp_path: Path) -> None:
        run_id = _bootstrap(tmp_path)
        update_state_phase(
            run_id,
            tmp_path,
            SPRINT_PHASE_BASELINE_GATE,
            detail="merge base of main",
            started_at="2026-07-28T21:56:41.137562+00:00",
        )
        update_state_phase(run_id, tmp_path, "starting")

        meta = _read_sprint_meta_from_state(tmp_path / ".forge" / "runs" / f"{run_id}.state")
        assert meta["sprint_phase"] == "starting"
        assert "sprint_phase_detail" not in meta
        assert "sprint_phase_started_at" not in meta

    def test_header_reports_target_and_elapsed(self) -> None:
        import datetime

        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=13)
        rendered = _format_sprint_phase("baseline-gate", "merge base of main", started.isoformat())
        assert rendered == "phase: baseline-gate (merge base of main, 13m)"

    def test_header_without_phase_context_is_unchanged(self) -> None:
        assert _format_sprint_phase("starting", None, None) == "phase: starting"
