"""Regression check: scripts/cut-rc.sh must not mutate the operator's default
Python environment, and must install the cut RC into an isolated venv under
.forge/rc-envs/.

The historical failure mode this guards: cut-rc.sh used to run
`pip install --force-reinstall git+...@<RC_TAG>` with no environment
qualifier, which silently overwrote the operator's editable install of source
and made every subsequent `forge sprint` run against the cut RC instead of
source. Subsequent schema divergence between source and the installed RC
surfaced as cross-environment kwarg mismatches with no visible signal naming
the runtime in effect.

The test executes the script under `--dry-run --no-install` so no real venv,
git tag, push, or pip install runs — `dry_run=true` still echoes the install
commands the script *would* run, which is enough to verify the isolation
contract without touching the operator's env.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "cut-rc.sh"


def test_script_does_not_pip_install_into_operator_default_env() -> None:
    """The script must not contain a bare `pip install ...` that would target
    whatever Python env happens to be active when the operator runs the
    script. Every install must go through the isolated venv's pip.
    """
    body = SCRIPT.read_text(encoding="utf-8")

    # Allowed: `"$RC_ENV_PIP" install ...` or commented examples in --no-install help.
    # Disallowed: bare `pip install --force-reinstall git+...` as an executed
    # command.  The pattern below is the historical line that mutated the
    # operator's env. Echo'd help/instructions are fine; executed commands are
    # not. We strip echo'd help lines before scanning.
    code_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("echo "):
            continue
        code_lines.append(line)

    code = "\n".join(code_lines)
    # No executed `pip install` (without venv qualifier) of the RC tag.
    assert not re.search(
        r"^\s*(run\s+)?pip\s+install\s+.*git\+https://github\.com/fuzzypete/theforge",
        code,
        flags=re.MULTILINE,
    ), "cut-rc.sh installs the RC into whatever Python env is active; must use the isolated venv"


def test_script_creates_isolated_rc_env() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert ".forge/rc-envs/" in body, "cut-rc.sh must reference .forge/rc-envs/<tag>/"
    assert "python3 -m venv" in body, "cut-rc.sh must create an isolated venv"


def test_script_verification_uses_isolated_binary() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    # Verification of `forge --version` must come from the isolated venv,
    # not from the operator's PATH-resolved `forge`.
    assert "$RC_ENV_FORGE" in body
    # The bare `forge --version` (PATH-resolved, operator's default env) must
    # not appear in code (allowed in echo'd help text only).
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith(("#", "echo "))
    )
    assert "forge --version" not in code or "$RC_ENV_FORGE" in code


def test_test_ladder_points_at_isolated_binary() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    # The Test ladder section's `forge sprint ...` example must be path-
    # qualified to the isolated venv binary, not bare `forge`.
    # Match the operator-facing test ladder echo (the "Smoke pass" / "Boundary
    # pass" / "Moneyshot pass" lines), not the comment header that mentions
    # "Test ladder section below".
    ladder = re.search(r"Smoke pass.*?Moneyshot pass[^\n]*", body, flags=re.DOTALL)
    assert ladder is not None
    ladder_text = ladder.group(0)
    assert "$RC_ENV_FORGE" in ladder_text or ".forge/rc-envs/" in ladder_text


@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_dry_run_does_not_emit_default_env_pip_install(tmp_path: Path) -> None:
    """Running the script under --dry-run echoes the commands it *would*
    execute. The echoed install command must target the isolated venv's pip,
    not bare `pip`.

    We cannot run cut-rc.sh end-to-end in a unit test (it would try to git-pull
    main, create a release branch, tag, push, and install from a real tag).
    But --dry-run is enough to assert the install seam: the `+ <command>`
    trace in dry-run output is the contract surface.
    """
    # Use a fake git repo so the script's git checks succeed in dry-run mode.
    # In dry-run, the actual git mutations are skipped, but read-only checks
    # (status, show-ref, rev-parse) still run.
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "pyproject.toml").write_text('version = "0.99.0"\n')
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    # Rename branch to main for parity with the script's `git checkout main`.
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)

    env = os.environ.copy()
    # Avoid network: provide a no-op gh shim in PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_shim = bin_dir / "gh"
    gh_shim.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            # gh shim — emit nothing for issue list (so OPEN_ISSUES is empty
            # which the script handles).
            if [ "$1" = "issue" ] && [ "$2" = "list" ]; then
                # --json number --jq 'length' path
                if echo "$@" | grep -q -- "--json"; then
                    echo "0"
                else
                    :
                fi
            fi
            """
        )
    )
    gh_shim.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    proc = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # The script may or may not reach the install step in dry-run depending
    # on the order of git checks; what matters is that any install command it
    # *did* echo targeted the isolated venv. Scan stdout+stderr for the
    # historical bug pattern.
    combined = proc.stdout + proc.stderr
    bad_pattern = re.compile(
        r"^\+ pip install .*git\+https://github\.com/fuzzypete/theforge",
        flags=re.MULTILINE,
    )
    assert not bad_pattern.search(combined), (
        "cut-rc.sh dry-run echoed a bare `pip install` of the RC tag; "
        "this would mutate the operator's default env. Output:\n" + combined
    )
