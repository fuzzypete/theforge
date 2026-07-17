"""Tests for scripts/apply-branch-protection.sh.

The helper is the branch-protection step extracted out of cut-rc.sh so the
critical RC merge path can be exercised under a PATH-mocked `gh`. Covers:
  - --dry-run reports the planned PUT and does NOT call `gh`
  - protection is always (re-)applied via PUT to bring the branch up to the
    required state, whether or not it was already protected
  - the PUT body requires the CI gate checks as status contexts
  - PUT failures warn-and-continue (script exits 0 so cut-rc.sh proceeds)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "apply-branch-protection.sh"


def _write_fake_gh(bin_dir: Path, *, put_exit: int) -> Path:
    """Install a fake `gh` on PATH that records every invocation.

    The PUT apply (via --method PUT) exits with the caller-specified code and
    its stdin (the protection body) is captured to gh.put_body.json for content
    assertions. The script no longer probes for existing protection — it always
    PUTs to bring the branch to the required state — so any non-PUT call is a
    regression and is failed loudly here.
    """
    log = bin_dir / "gh.log"
    fake = bin_dir / "gh"
    # Drain stdin on EVERY exit path. The script under test writes the protection
    # body via `echo "$BODY" | gh ...` under `set -o pipefail`; if this fake exits
    # without reading stdin the writer can receive SIGPIPE and pipefail fails the
    # pipeline nondeterministically. Reading stdin to a file (PUT) or /dev/null
    # (non-PUT) before exiting removes that race — see docs/flake-register.md #2.
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "is_put=false\n"
        'for a in "$@"; do\n'
        '    if [[ "$a" == "PUT" ]]; then is_put=true; fi\n'
        "done\n"
        'if [[ "$is_put" == true ]]; then\n'
        f'    cat > "{bin_dir}/gh.put_body.json"\n'
        f"    exit {put_exit}\n"
        "else\n"
        "    cat > /dev/null\n"
        "    exit 3\n"
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
    log = _write_fake_gh(sandbox, put_exit=0)

    result = _run("--dry-run", "fuzzypete/theforge", "release/v0.10")

    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert "would apply branch protection to release/v0.10" in result.stdout
    assert "PUT repos/fuzzypete/theforge/branches/release/v0.10/protection" in result.stdout
    # The dry-run body must carry the gate contexts so operators eyeballing the
    # planned PUT see the required checks.
    assert '"gate (3.11)"' in result.stdout
    assert '"gate (3.12)"' in result.stdout
    assert '"gate (3.13)"' in result.stdout
    # The log file must NOT exist — the fake `gh` was never invoked.
    assert not log.exists(), f"gh was called in dry-run: {log.read_text()}"


def test_protection_is_applied_unconditionally(sandbox):
    # The script always PUTs — no probe — so an already-protected branch is
    # brought up to the required state rather than left as-is.
    log = _write_fake_gh(sandbox, put_exit=0)

    result = _run("fuzzypete/theforge", "release/v0.10")

    assert result.returncode == 0, result.stderr
    assert "applying branch protection to release/v0.10" in result.stdout
    assert "branch protected; auto-merge enabled" in result.stdout
    calls = log.read_text().strip().splitlines()
    assert len(calls) == 1, f"expected a single PUT (no probe), got: {calls}"
    assert "PUT" in calls[0], "the only call must be the PUT apply"


def test_put_body_requires_gate_status_checks(sandbox):
    log = _write_fake_gh(sandbox, put_exit=0)

    result = _run("fuzzypete/theforge", "release/v0.10")

    assert result.returncode == 0, result.stderr
    assert len(log.read_text().strip().splitlines()) == 1

    # required_status_checks.contexts must name the CI gate checks. Empty
    # contexts (or a null block) leaves nothing for enablePullRequestAutoMerge
    # to wait on, so GitHub refuses to arm auto-merge (MERGE_ARMING_FAILED),
    # and lets a PR merge into the release branch with a red gate. Confirmed
    # live on release/v0.11 by the #1441 sprint on 2026-07-13.
    put_body = json.loads((sandbox / "gh.put_body.json").read_text())
    checks = put_body["required_status_checks"]
    assert checks is not None, (
        "required_status_checks must not be null or GitHub refuses to arm auto-merge"
    )
    assert checks["contexts"] == [
        "gate (3.11)",
        "gate (3.12)",
        "gate (3.13)",
    ], "contexts must match the ci.yml gate matrix so CI is required before merge"


def test_gate_contexts_match_ci_matrix():
    """Pin the required contexts to the ci.yml gate matrix.

    The context names GitHub reports are "<job> (<matrix value>)". This test
    guards the coupling the script comment documents: if ci.yml's python-version
    matrix drifts from the contexts hard-coded in the script, auto-merge arming
    silently breaks, so fail loudly here instead.
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    script = SCRIPT.read_text()
    for version in ("3.11", "3.12", "3.13"):
        assert f'"{version}"' in ci, f"ci.yml no longer runs python {version}"
        assert f"gate ({version})" in script, (
            f"script must require the 'gate ({version})' status check"
        )


def test_put_failure_warns_and_continues(sandbox):
    # PUT fails (no admin perms / fork).
    log = _write_fake_gh(sandbox, put_exit=1)

    result = _run("fuzzypete/theforge", "release/v0.10")

    # Critical: exit code MUST be 0 so cut-rc.sh's `|| true` is unnecessary
    # and the cut proceeds even on permission-limited accounts.
    assert result.returncode == 0, result.stderr
    assert "failed to apply branch protection" in result.stderr
    assert "apply manually" in result.stderr
    calls = log.read_text().strip().splitlines()
    assert len(calls) == 1, f"expected a single PUT attempt, got: {calls}"


def test_missing_args_exits_nonzero(sandbox):
    _write_fake_gh(sandbox, put_exit=0)

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
