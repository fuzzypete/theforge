"""Tests for the sibling-worktree write detector in dev_phase.

Covers:
  - _iter_sibling_worktrees: discovers siblings, excludes active worktree
  - _git_status_porcelain_ignored: returns status lines, handles errors gracefully
  - _run_dev_phase: escalates when a sibling worktree has new writes after the dev agent runs
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.dev_phase import (
    _git_status_porcelain_ignored,
    _is_forge_artifact_status_line,
    _iter_sibling_worktrees,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# ── _iter_sibling_worktrees ──────────────────────────────────────────


class TestIterSiblingWorktrees:
    def test_no_worktrees_dir(self, tmp_path: Path) -> None:
        """Returns empty list when .forge/worktrees/ does not exist."""
        active = tmp_path / ".forge" / "worktrees" / "issue-1"
        result = _iter_sibling_worktrees(active, tmp_path)
        assert result == []

    def test_only_active_worktree(self, tmp_path: Path) -> None:
        """Returns empty list when the only worktree is the active one."""
        active = tmp_path / ".forge" / "worktrees" / "issue-1"
        active.mkdir(parents=True)
        result = _iter_sibling_worktrees(active, tmp_path)
        assert result == []

    def test_returns_siblings_excludes_active(self, tmp_path: Path) -> None:
        """Returns sibling worktrees but not the active one."""
        wt_dir = tmp_path / ".forge" / "worktrees"
        active = wt_dir / "issue-1"
        sibling_a = wt_dir / "issue-2"
        sibling_b = wt_dir / "issue-3"
        for d in (active, sibling_a, sibling_b):
            d.mkdir(parents=True)
        result = _iter_sibling_worktrees(active, tmp_path)
        assert set(result) == {sibling_a, sibling_b}
        assert active not in result

    def test_non_directory_entries_excluded(self, tmp_path: Path) -> None:
        """Non-directory entries under .forge/worktrees/ are ignored."""
        wt_dir = tmp_path / ".forge" / "worktrees"
        active = wt_dir / "issue-1"
        sibling = wt_dir / "issue-2"
        for d in (active, sibling):
            d.mkdir(parents=True)
        # Create a file (not a directory) in worktrees/
        (wt_dir / "README.txt").write_text("notes", encoding="utf-8")
        result = _iter_sibling_worktrees(active, tmp_path)
        assert result == [sibling]

    def test_resolves_symlinks(self, tmp_path: Path) -> None:
        """Active worktree matched by resolved path (handles symlinks)."""
        wt_dir = tmp_path / ".forge" / "worktrees"
        real = wt_dir / "issue-1"
        real.mkdir(parents=True)
        link = tmp_path / "active-link"
        link.symlink_to(real)
        sibling = wt_dir / "issue-2"
        sibling.mkdir()
        # Pass the symlink as active_worktree — it resolves to real
        result = _iter_sibling_worktrees(link, tmp_path)
        assert result == [sibling]
        assert real not in result


# ── _git_status_porcelain_ignored ───────────────────────────────────


class TestGitStatusPorcelainIgnored:
    def test_returns_status_lines(self, tmp_path: Path) -> None:
        """Returns frozenset of non-empty status lines on success."""
        output = " M src/foo.py\n?? new_file.txt\n!! build/\n"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = output
        with patch("theforge.coordinator.dev_phase.subprocess.run", return_value=mock_proc):
            result = _git_status_porcelain_ignored(tmp_path)
        assert result == frozenset([" M src/foo.py", "?? new_file.txt", "!! build/"])

    def test_empty_on_clean_worktree(self, tmp_path: Path) -> None:
        """Returns empty frozenset for a clean worktree."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        with patch("theforge.coordinator.dev_phase.subprocess.run", return_value=mock_proc):
            result = _git_status_porcelain_ignored(tmp_path)
        assert result == frozenset()

    def test_empty_on_git_error(self, tmp_path: Path) -> None:
        """Returns empty frozenset when git returns non-zero exit."""
        mock_proc = MagicMock()
        mock_proc.returncode = 128
        mock_proc.stdout = ""
        with patch("theforge.coordinator.dev_phase.subprocess.run", return_value=mock_proc):
            result = _git_status_porcelain_ignored(tmp_path)
        assert result == frozenset()

    def test_empty_on_exception(self, tmp_path: Path) -> None:
        """Returns empty frozenset when subprocess raises (e.g. not a git repo)."""
        with patch(
            "theforge.coordinator.dev_phase.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            result = _git_status_porcelain_ignored(tmp_path)
        assert result == frozenset()

    def test_uses_ignored_flag(self, tmp_path: Path) -> None:
        """Passes --ignored to git so that writes to ignored dirs are detected."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        with patch(
            "theforge.coordinator.dev_phase.subprocess.run", return_value=mock_proc
        ) as mock_run:
            _git_status_porcelain_ignored(tmp_path)
        args = mock_run.call_args[0][0]
        assert "--ignored" in args
        assert "--porcelain" in args

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        """Blank lines in git output are excluded from the result set."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = " M src/foo.py\n\n   \n?? bar.txt\n"
        with patch("theforge.coordinator.dev_phase.subprocess.run", return_value=mock_proc):
            result = _git_status_porcelain_ignored(tmp_path)
        assert result == frozenset([" M src/foo.py", "?? bar.txt"])

    def test_returns_forge_artifact_lines_unfiltered(self, tmp_path: Path) -> None:
        """_git_status_porcelain_ignored returns raw lines; caller filters forge artifacts."""
        output = " M src/foo.py\n!! .forge/plan.md\n!! build/leaked.o\n"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = output
        with patch("theforge.coordinator.dev_phase.subprocess.run", return_value=mock_proc):
            result = _git_status_porcelain_ignored(tmp_path)
        # All non-blank lines returned; forge artifact filtering is the caller's job
        assert result == frozenset([" M src/foo.py", "!! .forge/plan.md", "!! build/leaked.o"])


# ── _is_forge_artifact_status_line ──────────────────────────────────


class TestIsForgeArtifactStatusLine:
    def test_forge_plan_md(self) -> None:
        assert _is_forge_artifact_status_line("!! .forge/plan.md") is True

    def test_forge_handoff_yaml(self) -> None:
        assert _is_forge_artifact_status_line("!! .forge/handoff.yaml") is True

    def test_forge_sessions_json(self) -> None:
        assert _is_forge_artifact_status_line("!! .forge/sessions.json") is True

    def test_forge_traces_directory(self) -> None:
        """Directories are reported with trailing slash."""
        assert _is_forge_artifact_status_line("!! .forge/traces/") is True

    def test_forge_audit_yaml(self) -> None:
        assert _is_forge_artifact_status_line("!! .forge/audit.yaml") is True

    def test_forge_root_directory(self) -> None:
        """Bare .forge entry (no trailing slash) is also excluded."""
        assert _is_forge_artifact_status_line("!! .forge") is True

    def test_src_file_not_artifact(self) -> None:
        assert _is_forge_artifact_status_line("?? src/leaked.py") is False

    def test_build_dir_not_artifact(self) -> None:
        assert _is_forge_artifact_status_line("!! build/") is False

    def test_modified_non_forge(self) -> None:
        assert _is_forge_artifact_status_line(" M README.md") is False

    def test_short_line_no_crash(self) -> None:
        """Lines shorter than 3 chars do not raise."""
        assert _is_forge_artifact_status_line("!!") is False
        assert _is_forge_artifact_status_line("") is False


# ── Escalation integration via run_task ─────────────────────────────


class TestSiblingWriteDetectorEscalation:
    """Integration tests: sibling-worktree write detection escalates the run."""

    def _make_subprocess_run_spy(
        self, sibling_before: frozenset[str], sibling_after: frozenset[str]
    ):
        """Return a side_effect for subprocess.run that returns sibling status.

        First call to git status --porcelain --ignored for the sibling → baseline.
        Second call → changed state (simulating a write by the dev agent).
        Other subprocess.run calls (e.g. git rev-parse HEAD) are handled normally
        by a MagicMock with returncode=0.
        """
        call_counts: dict[str, int] = {"sibling_status": 0}

        def side_effect(args, **kwargs):
            cwd = kwargs.get("cwd", "")
            if (
                isinstance(args, list)
                and "--porcelain" in args
                and "--ignored" in args
                and "sibling" in str(cwd)
            ):
                n = call_counts["sibling_status"]
                call_counts["sibling_status"] += 1
                proc = MagicMock()
                proc.returncode = 0
                if n == 0:
                    proc.stdout = "\n".join(sorted(sibling_before)) + (
                        "\n" if sibling_before else ""
                    )
                else:
                    proc.stdout = "\n".join(sorted(sibling_after)) + (
                        "\n" if sibling_after else ""
                    )
                return proc
            # Default: success
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = b"" if isinstance(b"", bytes) else ""
            try:
                proc.stdout = "abc1234\n"
            except Exception:
                pass
            return proc

        return side_effect

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_no_siblings_no_escalation(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """When there are no sibling worktrees, the run proceeds normally."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        # No .forge/worktrees/ dir — zero siblings
        result = run_task(config, task, notify=False)
        assert result.state.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_unchanged_sibling_no_escalation(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """Sibling worktree unchanged after dev run → no escalation."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Create a sibling worktree dir
        sibling = tmp_path / ".forge" / "worktrees" / "issue-999"
        sibling.mkdir(parents=True)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        # Both baseline and post-run status are identical (clean)
        clean_status = frozenset()
        with patch(
            "theforge.coordinator.dev_phase._git_status_porcelain_ignored",
            return_value=clean_status,
        ):
            result = run_task(config, task, notify=False)

        assert result.state.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_sibling_write_escalates(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """Sibling worktree has new files after dev run → ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Create a sibling worktree dir
        sibling = tmp_path / ".forge" / "worktrees" / "issue-999"
        sibling.mkdir(parents=True)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()

        # First call (snapshot) → clean; second call (post-dev check) → new file
        call_n = {"n": 0}

        def status_side_effect(path: Path) -> frozenset[str]:
            if path.resolve() == sibling.resolve():
                n = call_n["n"]
                call_n["n"] += 1
                if n == 0:
                    return frozenset()  # baseline: clean
                return frozenset(["?? src/leaked_file.py"])  # post-dev: contaminated
            return frozenset()

        with patch(
            "theforge.coordinator.dev_phase._git_status_porcelain_ignored",
            side_effect=status_side_effect,
        ):
            result = run_task(config, task, notify=False)

        assert result.state.phase == Phase.ESCALATE
        assert "sibling worktree write detected" in result.message.lower()
        assert str(sibling) in result.message

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_sibling_forge_artifact_write_does_not_escalate(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """Forge-owned .forge/ artifact changes in sibling do NOT escalate.

        Normal coordinator operation writes plan.md, sessions.json, handoff.yaml,
        traces/, and audit.yaml under .forge/ in worktrees. These must not trigger
        the sibling-write detector.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        sibling = tmp_path / ".forge" / "worktrees" / "issue-888"
        sibling.mkdir(parents=True)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        # All .forge/ artifact forms that the coordinator writes
        forge_artifact_lines = frozenset(
            [
                "!! .forge/plan.md",
                "!! .forge/handoff.yaml",
                "!! .forge/sessions.json",
                "!! .forge/traces/",
                "!! .forge/audit.yaml",
            ]
        )

        call_n = {"n": 0}

        def status_side_effect(path: Path) -> frozenset[str]:
            if path.resolve() == sibling.resolve():
                n = call_n["n"]
                call_n["n"] += 1
                # After dev runs, sibling has forge artifact writes — not escalatable
                return frozenset() if n == 0 else forge_artifact_lines
            return frozenset()

        with patch(
            "theforge.coordinator.dev_phase._git_status_porcelain_ignored",
            side_effect=status_side_effect,
        ):
            result = run_task(config, task, notify=False)

        assert result.state.phase != Phase.ESCALATE, (
            f"Should not escalate on forge artifact writes, got: {result.message}"
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_sibling_non_forge_ignored_write_escalates(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """Ignored-file write outside .forge/ in sibling (e.g. build/) → ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        sibling = tmp_path / ".forge" / "worktrees" / "issue-889"
        sibling.mkdir(parents=True)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()

        call_n = {"n": 0}

        def status_side_effect(path: Path) -> frozenset[str]:
            if path.resolve() == sibling.resolve():
                n = call_n["n"]
                call_n["n"] += 1
                if n == 0:
                    return frozenset()
                # Agent leaked a build artifact into sibling — should escalate
                return frozenset(["!! build/leaked_output.o"])
            return frozenset()

        with patch(
            "theforge.coordinator.dev_phase._git_status_porcelain_ignored",
            side_effect=status_side_effect,
        ):
            result = run_task(config, task, notify=False)

        assert result.state.phase == Phase.ESCALATE
        assert "sibling worktree write detected" in result.message.lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalation_message_includes_changes(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """Escalation message includes the changed paths for diagnosis."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        sibling = tmp_path / ".forge" / "worktrees" / "issue-777"
        sibling.mkdir(parents=True)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()

        call_n = {"n": 0}
        changed_line = "?? src/cross_contamination.py"

        def status_side_effect(path: Path) -> frozenset[str]:
            if path.resolve() == sibling.resolve():
                n = call_n["n"]
                call_n["n"] += 1
                return frozenset() if n == 0 else frozenset([changed_line])
            return frozenset()

        with patch(
            "theforge.coordinator.dev_phase._git_status_porcelain_ignored",
            side_effect=status_side_effect,
        ):
            result = run_task(config, task, notify=False)

        assert result.state.phase == Phase.ESCALATE
        assert changed_line in result.message
