from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.sprint.ci_checks import poll_required_checks, required_pr_check_state


def failing_required_pr_checks(project_root: Path, pr_url: str, base_branch: str) -> list[str]:
    """Judged-failure names only — keeps the pre-#2270 assertions readable."""
    return required_pr_check_state(project_root, pr_url, base_branch).failing


class _Proc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_poll_required_checks_pass(tmp_path: Path) -> None:
    responses = [
        _Proc('{"nameWithOwner":"acme/repo"}'),
        _Proc('"abc123"\n'),
        _Proc('["build","tests"]'),
        _Proc(
            '[{"name":"build","status":"completed","conclusion":"success"},{"name":"tests","status":"completed","conclusion":"neutral"}]'
        ),
        _Proc('{"context":"ignored"}'),
    ]

    with patch("theforge.sprint.ci_checks.subprocess.run", side_effect=responses):
        result = poll_required_checks(tmp_path, "main", 30)

    assert result["status"] == "pass"
    assert result["sha"] == "abc123"
    assert result["failing_checks"] == []


def test_poll_required_checks_fail_from_legacy_status(tmp_path: Path) -> None:
    responses = [
        _Proc('{"nameWithOwner":"acme/repo"}'),
        _Proc("deadbeef"),
        _Proc('["tests"]'),
        _Proc("[]"),
        _Proc('[{"context":"tests","state":"failure"}]'),
    ]

    with patch("theforge.sprint.ci_checks.subprocess.run", side_effect=responses):
        result = poll_required_checks(tmp_path, "main", 30)

    assert result["status"] == "fail"
    assert result["failing_checks"] == ["tests"]
    assert "deadbeef" in result["message"]


def test_poll_required_checks_timeout(tmp_path: Path) -> None:
    responses = [
        _Proc('{"nameWithOwner":"acme/repo"}'),
        _Proc("deadbeef"),
        _Proc('["tests"]'),
        _Proc("[]"),
        _Proc("[]"),
    ]
    monotonic_values = [0.0, 301.0]

    with (
        patch("theforge.sprint.ci_checks.subprocess.run", side_effect=responses),
        patch("theforge.sprint.ci_checks.time.monotonic", side_effect=monotonic_values),
        patch("theforge.sprint.ci_checks.time.sleep") as sleep,
    ):
        result = poll_required_checks(tmp_path, "main", 300)

    assert result["status"] == "timeout"
    assert result["failing_checks"] == []
    sleep.assert_not_called()


def test_poll_required_checks_skips_when_branch_protection_missing(tmp_path: Path) -> None:
    def _run(*args, **kwargs):
        cmd = args[0]
        if cmd[:3] == ["gh", "repo", "view"]:
            return _Proc('{"nameWithOwner":"acme/repo"}')
        if "branches/main/protection/required_status_checks" in cmd[2]:
            return _Proc("", "gh: Not Found (HTTP 404)", 1)
        if "branches/main" in cmd[2]:
            return _Proc("deadbeef")
        raise AssertionError(cmd)

    with patch("theforge.sprint.ci_checks.subprocess.run", side_effect=_run):
        result = poll_required_checks(tmp_path, "main", 300)

    assert result["status"] == "skipped"
    assert result["sha"] == "deadbeef"


def test_poll_required_checks_polls_until_success(tmp_path: Path) -> None:
    responses = [
        _Proc('{"nameWithOwner":"acme/repo"}'),
        _Proc("deadbeef"),
        _Proc('["tests"]'),
        _Proc('[{"name":"tests","status":"queued","conclusion":null}]'),
        _Proc("[]"),
        _Proc('[{"name":"tests","status":"completed","conclusion":"success"}]'),
        _Proc("[]"),
    ]
    monotonic_values = [0.0, 1.0, 2.0]

    with (
        patch("theforge.sprint.ci_checks.subprocess.run", side_effect=responses),
        patch("theforge.sprint.ci_checks.time.monotonic", side_effect=monotonic_values),
        patch("theforge.sprint.ci_checks.time.sleep") as sleep,
    ):
        result = poll_required_checks(tmp_path, "main", 300)

    assert result["status"] == "pass"
    sleep.assert_called_once_with(15)


def _pr_check_responses(rollup: str) -> list[_Proc]:
    """gh calls made by failing_required_pr_checks, in order."""
    return [
        _Proc('{"nameWithOwner":"acme/repo"}'),
        _Proc('["gate","lint"]'),
        _Proc(rollup),
    ]


def test_failing_required_pr_checks_reports_terminal_failures(tmp_path: Path) -> None:
    rollup = (
        '[{"__typename":"CheckRun","name":"gate","status":"COMPLETED","conclusion":"FAILURE"},'
        '{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS"}]'
    )

    with patch(
        "theforge.sprint.ci_checks.subprocess.run", side_effect=_pr_check_responses(rollup)
    ):
        failing = failing_required_pr_checks(tmp_path, "https://github.com/x/y/pull/1", "main")

    assert failing == ["gate"]


def test_failing_required_pr_checks_ignores_pending_and_non_required(tmp_path: Path) -> None:
    """A required check still running is not a failure, and a failing check that
    is not required cannot block the merge — neither may abandon the wait."""
    rollup = (
        '[{"__typename":"CheckRun","name":"gate","status":"IN_PROGRESS","conclusion":null},'
        '{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS"},'
        '{"__typename":"CheckRun","name":"optional-bench","status":"COMPLETED",'
        '"conclusion":"FAILURE"}]'
    )

    with patch(
        "theforge.sprint.ci_checks.subprocess.run", side_effect=_pr_check_responses(rollup)
    ):
        assert failing_required_pr_checks(tmp_path, "https://github.com/x/y/pull/1", "main") == []


def test_failing_required_pr_checks_reads_legacy_status_contexts(tmp_path: Path) -> None:
    rollup = (
        '[{"__typename":"StatusContext","context":"gate","state":"ERROR"},'
        '{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS"}]'
    )

    with patch(
        "theforge.sprint.ci_checks.subprocess.run", side_effect=_pr_check_responses(rollup)
    ):
        failing = failing_required_pr_checks(tmp_path, "https://github.com/x/y/pull/1", "main")

    assert failing == ["gate"]


def test_failing_required_pr_checks_returns_empty_when_unresolvable(tmp_path: Path) -> None:
    """An un-answerable probe must leave the caller waiting rather than abandon a
    PR whose check state could not be read."""

    with patch("theforge.sprint.ci_checks.subprocess.run", side_effect=OSError("gh missing")):
        assert failing_required_pr_checks(tmp_path, "https://github.com/x/y/pull/1", "main") == []


def test_failing_required_pr_checks_empty_when_branch_unprotected(tmp_path: Path) -> None:
    responses = [
        _Proc('{"nameWithOwner":"acme/repo"}'),
        _Proc("", "gh: Not Found (HTTP 404)", 1),
    ]

    with patch("theforge.sprint.ci_checks.subprocess.run", side_effect=responses):
        assert failing_required_pr_checks(tmp_path, "https://github.com/x/y/pull/1", "main") == []


@pytest.mark.parametrize(
    "conclusion", ["CANCELLED", "TIMED_OUT", "STALE", "STARTUP_FAILURE", "SKIPPED"]
)
def test_interrupted_required_check_is_unjudged_not_failing(
    tmp_path: Path, conclusion: str
) -> None:
    """Issue #2270: a check that stopped without judging the change is an absence
    of evidence about it, never evidence against it."""
    rollup = (
        f'[{{"__typename":"CheckRun","name":"gate","status":"COMPLETED",'
        f'"conclusion":"{conclusion}"}},'
        '{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS"}]'
    )

    with patch(
        "theforge.sprint.ci_checks.subprocess.run", side_effect=_pr_check_responses(rollup)
    ):
        state = required_pr_check_state(tmp_path, "https://github.com/x/y/pull/1", "main")

    assert state.failing == []
    assert state.unjudged == ["gate"]


def test_unrecognised_conclusion_is_unjudged_not_failing(tmp_path: Path) -> None:
    """An outcome the table does not know is not a verdict either: the safer
    default in both directions is 'no judgement yet'."""
    rollup = (
        '[{"__typename":"CheckRun","name":"gate","status":"COMPLETED",'
        '"conclusion":"SOME_NEW_GITHUB_OUTCOME"},'
        '{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS"}]'
    )

    with patch(
        "theforge.sprint.ci_checks.subprocess.run", side_effect=_pr_check_responses(rollup)
    ):
        state = required_pr_check_state(tmp_path, "https://github.com/x/y/pull/1", "main")

    assert state.failing == []
    assert state.unjudged == ["gate"]


def test_judged_failures_still_reported_as_failing(tmp_path: Path) -> None:
    """The reclassification must not disarm the decided-red path: a check that
    ran and rejected the change is still a failure."""
    rollup = (
        '[{"__typename":"CheckRun","name":"gate","status":"COMPLETED","conclusion":"FAILURE"},'
        '{"__typename":"CheckRun","name":"lint","status":"COMPLETED",'
        '"conclusion":"ACTION_REQUIRED"}]'
    )

    with patch(
        "theforge.sprint.ci_checks.subprocess.run", side_effect=_pr_check_responses(rollup)
    ):
        state = required_pr_check_state(tmp_path, "https://github.com/x/y/pull/1", "main")

    assert state.failing == ["gate", "lint"]
    assert state.unjudged == []


def test_running_check_is_pending_not_unjudged(tmp_path: Path) -> None:
    """A check that has not stopped has not failed to judge — the wait budget,
    not the unjudged bucket, covers it."""
    rollup = (
        '[{"__typename":"CheckRun","name":"gate","status":"IN_PROGRESS","conclusion":null},'
        '{"__typename":"CheckRun","name":"lint","status":"COMPLETED","conclusion":"SUCCESS"}]'
    )

    with patch(
        "theforge.sprint.ci_checks.subprocess.run", side_effect=_pr_check_responses(rollup)
    ):
        state = required_pr_check_state(tmp_path, "https://github.com/x/y/pull/1", "main")

    assert state == ([], [])


def test_poll_required_checks_cancelled_check_times_out_not_fails(tmp_path: Path) -> None:
    """A cancelled required check on the base branch converges on the existing
    wait-budget expiry, and the recorded message says no verdict was produced."""
    responses = [
        _Proc('{"nameWithOwner":"acme/repo"}'),
        _Proc("deadbeef"),
        _Proc('["tests"]'),
        _Proc('[{"name":"tests","status":"completed","conclusion":"cancelled"}]'),
        _Proc("[]"),
    ]
    monotonic_values = [0.0, 301.0]

    with (
        patch("theforge.sprint.ci_checks.subprocess.run", side_effect=responses),
        patch("theforge.sprint.ci_checks.time.monotonic", side_effect=monotonic_values),
        patch("theforge.sprint.ci_checks.time.sleep"),
    ):
        result = poll_required_checks(tmp_path, "main", 300)

    assert result["status"] == "timeout"
    assert result["failing_checks"] == []
    assert result["unjudged_checks"] == ["tests"]
    assert "No verdict was produced by: tests." in result["message"]
