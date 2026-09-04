from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from sprint_test_helpers import run_sprint_ctx

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import (
    BASELINE_TEMP_PREFIX,
    BASELINE_WORKTREE_KEEP,
    _run_baseline_gate,
)
from theforge.sprint.sources import FileSource


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=replace(
            DEFAULT_VALIDATION,
            gate_command=(
                'python -c "import pathlib; '
                "print(pathlib.Path('baseline.txt').read_text().strip())\""
            ),
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_resolved(tmp_path: Path) -> ResolvedSprint:
    story_file = tmp_path / "story.md"
    story_file.write_text(
        "---\nname: My Story\nslug: my-story\n---\n# Content\n",
        encoding="utf-8",
    )
    source = FileSource()
    task = source.fetch(str(story_file.relative_to(tmp_path)), tmp_path)
    return ResolvedSprint(
        name="Test Sprint",
        budget_usd=10.0,
        stories=[(task, source, "story.md")],
        max_parallel=1,
    )


def _fake_result():
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase

    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(cwd: Path, message: str) -> str:
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def _init_repo(tmp_path: Path) -> tuple[ForgeConfig, ResolvedSprint, str]:
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (tmp_path / "baseline.txt").write_text("BASELINE\n", encoding="utf-8")
    base_commit = _commit_all(tmp_path, "base")
    _git(tmp_path, "checkout", "-b", "feat/test")
    (tmp_path / "baseline.txt").write_text("FEATURE\n", encoding="utf-8")
    _commit_all(tmp_path, "feature")
    return config, resolved, base_commit


def test_baseline_gate_uses_temp_worktree_and_restores_branch_state(
    tmp_path: Path,
) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    original_branch = _git(tmp_path, "branch", "--show-current")
    original_head = _git(tmp_path, "rev-parse", "HEAD")

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["merge_base"] == base_commit
    assert baseline["exit_code"] == 0
    assert "BASELINE" in str(baseline.get("output_tail", ""))
    assert _git(tmp_path, "branch", "--show-current") == original_branch
    assert _git(tmp_path, "rev-parse", "HEAD") == original_head
    assert (tmp_path / "baseline.txt").read_text(encoding="utf-8") == "FEATURE\n"
    worktrees = _git(tmp_path, "worktree", "list")
    assert "forge-baseline-" not in worktrees
    forge_entries = [path.name for path in (tmp_path / ".forge").iterdir()]
    assert not any(name.startswith("forge-baseline-") for name in forge_entries)


def test_baseline_gate_preserves_actual_gate_exit_code(tmp_path: Path) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command="python -c 'import sys; print(\"boom\"); sys.exit(2)'",
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    assert baseline["status"] == "fail"
    assert baseline["merge_base"] == base_commit
    assert baseline["exit_code"] == 2
    assert "boom" in str(baseline.get("output_tail", ""))


def test_baseline_gate_records_gate_time_worktree_state(tmp_path: Path) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        workspace=replace(
            config.workspace,
            setup_command=(
                "mkdir -p build generated "
                "&& printf residue > build/cache.txt "
                "&& printf ignored > generated/cache.txt"
            ),
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["worktree_state"] == {
        "untracked": ["build/cache.txt"],
        "ignored": ["generated/"],
    }


def _evidence_files(project_root: Path, sprint_name: str = "Test Sprint") -> list[Path]:
    log_dir = project_root / ".forge" / "logs" / sprint_name
    return sorted(log_dir.glob("*baseline-gate*.txt")) if log_dir.exists() else []


def _preserved_temp_roots(project_root: Path) -> list[Path]:
    return sorted(
        path
        for path in (project_root / ".forge").iterdir()
        if path.is_dir() and path.name.startswith(BASELINE_TEMP_PREFIX)
    )


#: Emitted before ~5000 characters of filler, so it cannot survive in the
#: 2000-character output tail — only a full-output record can carry it.
_EARLY_MARKER = "EARLY-MARKER-9d2f"

_LONG_FAILING_GATE = (
    f"python -c \"import sys; print('{_EARLY_MARKER}'); print('x' * 5000); sys.exit(1)\""
)


def test_baseline_gate_failure_persists_full_output_beyond_the_tail(tmp_path: Path) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(config.validation, gate_command=_LONG_FAILING_GATE),
    )

    baseline = _run_baseline_gate(config, resolved, run_id="abc123")

    assert baseline["passed"] is False
    # The tail alone cannot answer why: the failure's own first line is outside it.
    assert _EARLY_MARKER not in str(baseline.get("output_tail", ""))

    evidence_path = baseline["evidence_path"]
    assert evidence_path is not None
    assert baseline["evidence_unavailable"] is None
    assert Path(evidence_path).name == "run-abc123-baseline-gate.txt"
    evidence = Path(evidence_path).read_text(encoding="utf-8")
    assert _EARLY_MARKER in evidence
    assert str(baseline["merge_base"]) in evidence
    # The verdict itself names where the evidence lives.
    assert evidence_path in str(baseline["message"])


def test_baseline_gate_failure_preserves_the_worktree_that_produced_it(
    tmp_path: Path,
) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(config.validation, gate_command=_LONG_FAILING_GATE),
    )

    baseline = _run_baseline_gate(config, resolved)

    worktree = baseline["worktree"]
    assert worktree is not None
    worktree_path = Path(str(worktree))
    assert worktree_path.is_dir()
    # Still a usable checkout of the merge base, not a husk: the failure can be
    # re-run in the environment that produced it.
    assert _git(worktree_path, "rev-parse", "HEAD") == base_commit
    assert str(worktree) in _git(tmp_path, "worktree", "list")
    assert str(worktree) in str(baseline["message"])


def test_baseline_gate_reproduced_failure_records_failing_target_extraction(
    tmp_path: Path,
) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=(
                'python -c "import sys; '
                "print('FAILED tests/test_storage.py::test_dates'); "
                "print('AssertionError: dates drifted'); sys.exit(1)\""
            ),
            failed_test_pattern=r"^FAILED (?P<test>\S+)",
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    assert baseline["failing_targets"] == ["tests/test_storage.py::test_dates"]
    assert baseline["failing_target_extraction"] == {
        "source": "custom_pattern",
        "format_recognized": True,
    }


def test_baseline_gate_evidence_and_worktree_survive_the_sprint_abort(
    tmp_path: Path,
) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(config.validation, gate_command=_LONG_FAILING_GATE),
    )

    with (
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
        patch("theforge.sprint.runner.run_batch_preflight") as mock_preflight,
        patch("theforge.sprint.runner.run_task") as mock_run_task,
    ):
        try:
            run_sprint_ctx(config, resolved, no_pull=True, run_id="abc123")
            raise AssertionError("expected baseline failure")
        except RuntimeError as exc:
            message = str(exc)

    assert not mock_preflight.called
    assert not mock_run_task.called
    assert "Broken baseline" in message

    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    baseline_check = audit["baseline_check"]
    evidence_path = baseline_check["evidence_path"]
    assert evidence_path is not None
    # Both artifacts outlive the run that produced them.
    assert _EARLY_MARKER in Path(evidence_path).read_text(encoding="utf-8")
    assert Path(str(baseline_check["worktree"])).is_dir()
    assert evidence_path in message


def test_baseline_gate_records_that_output_was_unavailable(tmp_path: Path) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(config.validation, gate_command="exit 4"),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    evidence = Path(str(baseline["evidence_path"])).read_text(encoding="utf-8")
    # An absent account, stated as absent — distinguishable from "nothing went wrong".
    assert "not captured" in evidence
    assert "exit 4" in evidence


def test_preserved_baseline_worktrees_stay_bounded_across_failing_runs(
    tmp_path: Path,
) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(config.validation, gate_command=_LONG_FAILING_GATE),
    )

    evidence_paths = []
    for _ in range(BASELINE_WORKTREE_KEEP + 2):
        baseline = _run_baseline_gate(config, resolved)
        evidence_paths.append(Path(str(baseline["evidence_path"])))

    preserved = _preserved_temp_roots(tmp_path)
    assert len(preserved) <= BASELINE_WORKTREE_KEEP
    # Reclaimed worktrees are unregistered too, not left as missing entries.
    registered = [
        line
        for line in _git(tmp_path, "worktree", "list").splitlines()
        if BASELINE_TEMP_PREFIX in line
    ]
    assert len(registered) == len(preserved)
    # Reclaiming worktrees never reclaims the durable record.
    assert all(path.exists() for path in evidence_paths)
    assert len(_evidence_files(tmp_path)) == len(evidence_paths)


def test_baseline_gate_pass_leaves_no_preserved_worktree_or_evidence(
    tmp_path: Path,
) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline.get("worktree") is None
    assert _preserved_temp_roots(tmp_path) == []
    assert _evidence_files(tmp_path) == []


def test_baseline_gate_setup_failure_preserves_evidence_and_worktree(
    tmp_path: Path,
) -> None:
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        workspace=replace(config.workspace, setup_command="echo SETUP-BOOM; exit 3"),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["status"] == "error"
    assert Path(str(baseline["worktree"])).is_dir()
    evidence = Path(str(baseline["evidence_path"])).read_text(encoding="utf-8")
    assert "SETUP-BOOM" in evidence


def test_baseline_gate_substitutes_test_target_placeholder(tmp_path: Path) -> None:
    # A gate_command that uses {test_target}/{slug} (as HDP's did) must not
    # leak the literal placeholder text into the baseline gate's shell
    # command, since the baseline gate has no TaskStory to source them from.
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command="echo {test_target} {slug}",
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["merge_base"] == base_commit
    resolved_cmd = baseline["command"]
    assert "{test_target}" not in resolved_cmd
    assert "{slug}" not in resolved_cmd


def test_baseline_gate_runs_setup_command_before_gate(tmp_path: Path) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    # setup_command bootstraps a marker the gate depends on; the merge base has
    # no such marker, so the gate can only pass if setup runs first inside the
    # temporary baseline worktree.
    config = replace(
        config,
        workspace=replace(config.workspace, setup_command="echo READY > toolchain.txt"),
        validation=replace(
            config.validation,
            gate_command=(
                'python -c "import pathlib; '
                "print(pathlib.Path('toolchain.txt').read_text().strip())\""
            ),
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["status"] == "pass"
    assert baseline["merge_base"] == base_commit
    assert "READY" in str(baseline.get("output_tail", ""))
    # The setup marker must not leak into the project root checkout.
    assert not (tmp_path / "toolchain.txt").exists()


def test_baseline_gate_reports_setup_command_failure(tmp_path: Path) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        workspace=replace(config.workspace, setup_command="exit 3"),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    assert baseline["status"] == "error"
    assert baseline["merge_base"] == base_commit
    assert "workspace setup command failed" in str(baseline["message"]).lower()


def test_baseline_gate_passes_configured_setup_timeout_to_workspace_setup(
    tmp_path: Path, capsys
) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        workspace=replace(
            config.workspace,
            setup_command="echo READY > baseline.txt",
            setup_timeout=480,
        ),
    )

    with patch(
        "theforge.sprint.runner.coordinator_workspace._run_setup_split",
        return_value=(True, "ok"),
    ) as mock_setup:
        baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["merge_base"] == base_commit
    mock_setup.assert_called_once()
    assert mock_setup.call_args.kwargs["timeout"] == 480
    assert "Running baseline workspace setup (timeout 480s): echo READY > baseline.txt" in (
        capsys.readouterr().err
    )


def test_baseline_gate_accepts_symlinked_project_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    config, resolved, _base_commit = _init_repo(real_root)
    symlink_root = tmp_path / "linked-root"
    os.symlink(real_root, symlink_root)
    config = replace(config, project_root=symlink_root)

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["status"] == "pass"


def test_baseline_gate_fail_aborts_before_any_agent_runner(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path)
    baseline = {
        "passed": False,
        "status": "fail",
        "exit_code": 2,
        "duration_seconds": 1.25,
        "message": (
            "Broken baseline: configured gate failed on sprint merge base abc123 "
            "before any dev work started (Gate returned FAIL)"
        ),
    }

    with (
        patch("theforge.sprint.runner._run_baseline_gate", return_value=baseline),
        patch("theforge.sprint.runner.run_batch_preflight") as mock_preflight,
        patch("theforge.sprint.runner.run_task") as mock_run_task,
    ):
        try:
            run_sprint_ctx(config, resolved)
            raise AssertionError("expected baseline failure")
        except RuntimeError as exc:
            assert "Broken baseline" in str(exc)

    assert not mock_preflight.called
    assert not mock_run_task.called

    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert audit["baseline_check"]["passed"] is False
    assert audit["baseline_check"]["exit_code"] == 2
    assert audit["sprint"]["stopped_reason"] == "broken_baseline"


def test_baseline_pass_proceeds_to_normal_sprint_flow(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path)

    with (
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch(
            "theforge.sprint.runner.run_task",
            return_value=_fake_result(),
        ) as mock_run_task,
        patch("theforge.sprint.audit_publish._write_sprint_audit"),
        patch("theforge.sprint.audit_publish._write_sprint_summary"),
    ):
        result = run_sprint_ctx(config, resolved)

    assert result.specs_succeeded == 1
    assert mock_run_task.called


def test_baseline_gate_records_processes_it_left_running(tmp_path: Path) -> None:
    """A baseline gate that leaks workers says so in the record it returns (#2309).

    The baseline gate runs the project's own gate command — the shape that leaves
    test workers behind — before any story exists to own the record. The kill
    happens at release either way; what this pins is that the sprint's baseline
    record carries the fact, instead of it living only in a log line nobody
    correlates with the sprint afterwards.
    """
    import sys
    import time

    config, resolved, _base_commit = _init_repo(tmp_path)
    pidfile = tmp_path / "leaked.pid"
    leaky_gate = (
        f'{sys.executable} -c "import subprocess,sys,pathlib;'
        "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))\""
    )
    config = replace(config, validation=replace(config.validation, gate_command=leaky_gate))

    baseline = _run_baseline_gate(config, resolved)

    leaked_pid = int(pidfile.read_text().strip())
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(leaked_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the baseline gate's descendant outlived the gate run")
    finally:
        try:
            os.kill(leaked_pid, 9)
        except OSError:
            pass

    assert baseline["passed"] is True, "the gate itself passed; only its leftovers died"
    teardowns = baseline["process_teardowns"]
    assert isinstance(teardowns, list) and len(teardowns) == 1
    assert teardowns[0]["action"] == "killed_survivors"
    assert teardowns[0]["completed"] is True


def test_a_clean_baseline_gate_records_no_teardown(tmp_path: Path) -> None:
    """Absence has to mean something: a gate that left nothing records nothing."""
    config, resolved, _base_commit = _init_repo(tmp_path)

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["process_teardowns"] == []


def test_a_failing_baseline_setup_still_records_what_it_left_running(tmp_path: Path) -> None:
    """A setup command that leaks *and then fails* still reports the teardown.

    The failure path returns early, before the gate ever runs, so it was
    returning a record with no ``process_teardowns`` at all even though the
    collector had been filled moments earlier. A leak is not less real because
    the command that caused it went on to fail — if anything it is more likely,
    since a command that died partway is the one most apt to leave work behind
    (#2309).
    """
    import sys
    import time

    config, resolved, _base_commit = _init_repo(tmp_path)
    pidfile = tmp_path / "setup.pid"
    leaky_then_fail = (
        f'{sys.executable} -c "import subprocess,sys,pathlib;'
        "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid));"
        'sys.exit(3)"'
    )
    config = replace(config, workspace=replace(config.workspace, setup_command=leaky_then_fail))

    baseline = _run_baseline_gate(config, resolved)

    leaked = int(pidfile.read_text().strip())
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(leaked, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the setup command's descendant outlived the baseline")
    finally:
        try:
            os.kill(leaked, 9)
        except OSError:
            pass

    assert baseline["passed"] is False
    assert baseline["status"] == "error"
    teardowns = baseline["process_teardowns"]
    assert isinstance(teardowns, list) and len(teardowns) == 1, (
        f"the setup command's teardown never reached the baseline record: {baseline}"
    )
    assert teardowns[0]["action"] == "killed_survivors"


def test_every_baseline_record_answers_whether_it_left_processes(tmp_path: Path) -> None:
    """One shape for every outcome, so a reader never has to ask which kind it got.

    The early exits happen before any project command runs, so their answer is an
    empty list rather than a missing key.
    """
    config, resolved, _base_commit = _init_repo(tmp_path)
    passing = _run_baseline_gate(config, resolved)
    assert passing["process_teardowns"] == []

    # An outcome that returns before the worktree is even created, let alone
    # before any project command runs.
    not_a_checkout = tmp_path / "not-a-repo"
    not_a_checkout.mkdir()
    early = _run_baseline_gate(replace(config, project_root=not_a_checkout), resolved)
    assert early["passed"] is False
    assert early["process_teardowns"] == []


# --- confirmation of a failing baseline gate (#2434) -------------------------


def _flaky_gate_command(marker: Path) -> str:
    """A gate that fails its first invocation and passes every one after.

    Stands in for the process-teardown/sandbox flakes that fail one run of an
    8,000-test suite and pass the next against the identical commit.
    """
    return (
        'python -c "import pathlib, sys; '
        f"m = pathlib.Path(r'{marker}'); first = not m.exists(); m.write_text('ran'); "
        "print('FLAKE-FAILURE' if first else 'CLEAN-RERUN'); "
        'sys.exit(1 if first else 0)"'
    )


def _counting_gate_command(counter: Path, *, exit_code: int) -> str:
    return (
        'python -c "import pathlib, sys; '
        f"c = pathlib.Path(r'{counter}'); "
        "c.write_text(str(int(c.read_text() or 0) + 1) if c.exists() else '1'); "
        "print('GATE-RAN'); "
        f'sys.exit({exit_code})"'
    )


def test_baseline_gate_failure_that_does_not_reproduce_does_not_break_the_baseline(
    tmp_path: Path,
) -> None:
    """One failing invocation is not evidence about the merge base (#2434).

    The gate fails, an immediate re-run of the identical command against the
    identical worktree passes, and the sprint is allowed to start — the verdict
    that decides whether work may begin is the one that reproduced.
    """
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=_flaky_gate_command(tmp_path / "flake.marker"),
        ),
    )

    baseline = _run_baseline_gate(config, resolved, run_id="abc123")

    assert baseline["passed"] is True
    assert baseline["status"] == "pass_unreproduced_failure"
    assert baseline["merge_base"] == base_commit
    # The ambiguity is on the record, not smoothed away into a plain pass.
    assert baseline["confirmation_attempted"] is True
    assert baseline["failure_reproduced"] is False
    assert baseline["initial_result"]["exit_code"] == 1
    assert baseline["confirmation_result"]["decision"] == "PASS"
    assert "did not reproduce" in str(baseline["message"])


def test_an_unreproduced_baseline_failure_leaves_no_preserved_worktree(
    tmp_path: Path,
) -> None:
    """Nothing halted, so nothing is preserved — but the failure is still recorded.

    Preservation exists so a halting failure can be reproduced in the workspace
    that produced it. A first run that vanished on re-run halts nothing; leaking
    a worktree per flake would spend disk on every one of them. Its output is
    still written, under a filename that cannot be read as a halting verdict's
    evidence.
    """
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=_flaky_gate_command(tmp_path / "flake.marker"),
        ),
    )

    baseline = _run_baseline_gate(config, resolved, run_id="abc123")

    assert baseline.get("worktree") is None
    assert _preserved_temp_roots(tmp_path) == []
    assert "forge-baseline-" not in _git(tmp_path, "worktree", "list")

    evidence_path = Path(str(baseline["evidence_path"]))
    assert evidence_path.name == "run-abc123-baseline-gate-unreproduced-failure.txt"
    evidence = evidence_path.read_text(encoding="utf-8")
    assert "FLAKE-FAILURE" in evidence
    assert "NOT REPRODUCED" in evidence


def test_a_baseline_failure_is_re_run_once_before_it_is_believed(tmp_path: Path) -> None:
    """Exactly two runs on a failure, exactly one on a pass.

    The confirmation doubles a many-minute gate's wall time, so it is spent only
    where the answer could change the sprint's fate.
    """
    config, resolved, _base_commit = _init_repo(tmp_path)
    failing_counter = tmp_path / "failing.count"
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=_counting_gate_command(failing_counter, exit_code=1),
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    assert failing_counter.read_text(encoding="utf-8") == "2"

    passing_counter = tmp_path / "passing.count"
    passing_config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=_counting_gate_command(passing_counter, exit_code=0),
        ),
    )

    passing = _run_baseline_gate(passing_config, resolved)

    assert passing["passed"] is True
    assert passing["confirmation_attempted"] is False
    assert passing["failure_reproduced"] is None
    assert passing_counter.read_text(encoding="utf-8") == "1"


def test_a_reproduced_baseline_failure_still_breaks_the_baseline(tmp_path: Path) -> None:
    """A failure that repeats is the merge base's verdict and halts the sprint."""
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(config.validation, gate_command=_LONG_FAILING_GATE),
    )

    baseline = _run_baseline_gate(config, resolved, run_id="abc123")

    assert baseline["passed"] is False
    assert baseline["status"] == "fail"
    assert baseline["confirmation_attempted"] is True
    assert baseline["failure_reproduced"] is True
    assert baseline["initial_result"]["exit_code"] == 1
    assert baseline["confirmation_result"]["decision"] == "FAIL"
    assert "reproduced" in str(baseline["message"])
    # The halting outcome keeps everything it kept before the re-run existed.
    assert Path(str(baseline["worktree"])).is_dir()
    assert _EARLY_MARKER in Path(str(baseline["evidence_path"])).read_text(encoding="utf-8")


def test_an_unreproduced_baseline_failure_lets_the_sprint_run_its_stories(
    tmp_path: Path,
) -> None:
    """Seam test: the sprint that would have aborted now starts its work.

    Exercises the whole path — gate, confirmation, run_sprint's abort branch —
    rather than _run_baseline_gate's return value alone, because the abort is
    what cost the overnight window in the reported occurrence.
    """
    config, resolved, _base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command=_flaky_gate_command(tmp_path / "flake.marker"),
        ),
    )

    with (
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
        patch("theforge.sprint.runner.run_task", return_value=_fake_result()) as mock_run_task,
        patch("theforge.sprint.audit_publish._write_sprint_summary"),
        # The fixture repo sits on the feature branch, which the audit publish
        # refuses; unrelated to what this test pins.
        patch("theforge.sprint.audit_publish._commit_story_run_audits"),
    ):
        result = run_sprint_ctx(config, resolved, no_pull=True, run_id="abc123")

    assert mock_run_task.called
    assert result.specs_succeeded == 1

    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    baseline_check = audit["baseline_check"]
    assert baseline_check["passed"] is True
    assert baseline_check["failure_reproduced"] is False
    assert baseline_check["initial_result"]["exit_code"] == 1
