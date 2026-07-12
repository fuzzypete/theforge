"""Tests for scripts/apply-branch-protection.sh.

The helper is the branch-protection step extracted out of cut-rc.sh so the
critical RC merge path can be exercised under a PATH-mocked `gh`. Covers:
  - --dry-run reports the planned PUT and does NOT call `gh`
  - existing protection is preserved (probe GET returns 0; no PUT)
  - new protection is applied via PUT when probe GET returns non-zero
  - PUT failures warn-and-continue (script exits 0 so cut-rc.sh proceeds)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "apply-branch-protection.sh"


def _write_fake_gh(bin_dir: Path, *, get_exit: int, put_exit: int) -> Path:
    """Install a fake `gh` on PATH that records every invocation.

    The fake distinguishes the protection probe (a GET, no --method flag)
    from the protection apply (PUT via --method PUT) and exits with the
    caller-specified codes for each.
    """
    log = bin_dir / "gh.log"
    fake = bin_dir / "gh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "is_put=false\n"
        'for a in "$@"; do\n'
        '    if [[ "$a" == "PUT" ]]; then is_put=true; fi\n'
        "done\n"
        'if [[ "$is_put" == true ]]; then\n'
        "    cat >/dev/null\n"
        f"    exit {put_exit}\n"
        "else\n"
        f"    exit {get_exit}\n"
        "fi\n"
    )
    fake.chmod(0o755)
    return log


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_does_not_call_gh(sandbox):
    log = _write_fake_gh(sandbox, get_exit=0, put_exit=0)

    result = _run("--dry-run", "fuzzypete/theforge", "release/v0.10")

    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert "would apply branch protection to release/v0.10" in result.stdout
    assert "PUT repos/fuzzypete/theforge/branches/release/v0.10/protection" in result.stdout
    # The log file must NOT exist — the fake `gh` was never invoked.
    assert not log.exists(), f"gh was called in dry-run: {log.read_text()}"


def test_existing_protection_is_preserved(sandbox):
    # Probe GET succeeds (exit 0) → branch already protected → no PUT.
    log = _write_fake_gh(sandbox, get_exit=0, put_exit=99)

    result = _run("fuzzypete/theforge", "release/v0.10")

    assert result.returncode == 0, result.stderr
    assert "already exists on release/v0.10; preserving it" in result.stdout
    calls = log.read_text().strip().splitlines()
    assert len(calls) == 1, f"expected exactly one gh call (the probe), got: {calls}"
    assert "PUT" not in calls[0], "must not call PUT when protection already exists"


def test_protection_applied_when_missing(sandbox):
    # Probe GET fails (exit 1) → not protected → PUT to apply.
    log = _write_fake_gh(sandbox, get_exit=1, put_exit=0)

    result = _run("fuzzypete/theforge", "release/v0.10")

    assert result.returncode == 0, result.stderr
    assert "applying branch protection to release/v0.10" in result.stdout
    assert "branch protected; auto-merge enabled" in result.stdout
    calls = log.read_text().strip().splitlines()
    assert len(calls) == 2, f"expected probe + PUT, got: {calls}"
    assert "PUT" in calls[1], "second call must be the PUT apply"


def test_put_failure_warns_and_continues(sandbox):
    # Probe says unprotected, PUT fails (no admin perms / fork).
    log = _write_fake_gh(sandbox, get_exit=1, put_exit=1)

    result = _run("fuzzypete/theforge", "release/v0.10")

    # Critical: exit code MUST be 0 so cut-rc.sh's `|| true` is unnecessary
    # and the cut proceeds even on permission-limited accounts.
    assert result.returncode == 0, result.stderr
    assert "failed to apply branch protection" in result.stderr
    assert "apply manually" in result.stderr
    calls = log.read_text().strip().splitlines()
    assert len(calls) == 2, f"expected probe + PUT attempt, got: {calls}"


def test_missing_args_exits_nonzero(sandbox):
    _write_fake_gh(sandbox, get_exit=0, put_exit=0)

    result = _run("only-one-arg")

    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_cut_rc_invokes_helper_with_release_branch():
    """Smoke check: cut-rc.sh references the helper with the right args.

    Cheaper than spinning up the full cut-rc.sh flow under mocks — the
    helper itself is already covered by the tests above; here we just pin
    that cut-rc.sh actually calls it with the release branch name and
    forwards --dry-run.
    """
    cut_rc = (REPO_ROOT / "scripts" / "cut-rc.sh").read_text()
    assert "apply-branch-protection.sh" in cut_rc
    assert '"$RELEASE_BRANCH"' in cut_rc
    assert '[[ "$DRY_RUN" == true ]] && PROTECT_ARGS+=("--dry-run")' in cut_rc
