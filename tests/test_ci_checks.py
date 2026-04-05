from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.sprint.ci_checks import poll_required_checks


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
    assert result["sha"] == '"abc123"'
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
