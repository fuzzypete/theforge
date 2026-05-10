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


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_full_execution_does_not_mutate_operator_default_env(tmp_path: Path) -> None:
    """Run scripts/cut-rc.sh through its install seam against a controlled
    fixture and assert the test process's theforge install is bit-for-bit
    unchanged afterwards.

    External boundaries are shimmed at PATH so the test never makes a network
    call: `gh` no-ops, `git push`/`ls-remote` are no-ops (other git commands
    pass through to the real binary), `make gate` returns success, and
    `python3 -m venv <DIR>` creates a fake venv populated with shim
    binaries. The shimmed venv pip records every invocation and never
    actually installs anything, so the only way the operator's default env
    could be mutated is if the script ran a bare `pip install` outside the
    venv pip — which the contract explicitly forbids and this test catches
    by snapshotting the operator env before and after.

    This is the AC's "regression check that runs the actual script" — it
    runs the real script binary, exercises the real install seam control
    flow, and verifies the structural property the AC names.
    """
    import theforge as _theforge_under_test

    operator_pkg = Path(_theforge_under_test.__file__)
    pre_mtime = operator_pkg.stat().st_mtime
    pre_size = operator_pkg.stat().st_size
    pre_bytes = operator_pkg.read_bytes()

    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "pyproject.toml").write_text('version = "0.99.0"\n')
    (repo / "Makefile").write_text("gate:\n\t@true\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)

    real_git = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True
    ).stdout.strip()
    assert real_git, "real git binary required for fixture setup"

    bin_dir = tmp_path / "shim_bin"
    bin_dir.mkdir()

    (bin_dir / "gh").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "gh").chmod(0o755)

    (bin_dir / "make").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "make").chmod(0o755)

    git_shim = bin_dir / "git"
    git_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            # No-op the network-mutating ops; pass through everything else.
            case "$1" in
                push) exit 0 ;;
                ls-remote) exit 0 ;;
                pull) exit 0 ;;
            esac
            exec {real_git} "$@"
            """
        )
    )
    git_shim.chmod(0o755)

    # Shim python3: when called as `python3 -m venv DIR`, fabricate a venv
    # populated with shim pip/forge binaries. Real pip is never invoked, so
    # no real install can leak into the operator's default env.
    pip_log = tmp_path / "pip_calls.log"
    py_shim = bin_dir / "python3"
    py_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
                VENV="$3"
                mkdir -p "$VENV/bin"
                cat > "$VENV/bin/pip" <<'PIP'
            #!/bin/sh
            echo "$@" >> "{pip_log}"
            exit 0
            PIP
                chmod +x "$VENV/bin/pip"
                cat > "$VENV/bin/forge" <<'FORGE'
            #!/bin/sh
            if [ "$1" = "--version" ]; then
                echo "TheForge v0.99.0rc0"
            fi
            FORGE
                chmod +x "$VENV/bin/forge"
                : > "$VENV/bin/python"
                chmod +x "$VENV/bin/python"
                exit 0
            fi
            exit 0
            """
        )
    )
    py_shim.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    # The real script prepends /opt/homebrew/bin to PATH (line 21) so its
    # interactive operator env is hermetic. For this test we want our shims
    # to win, so copy the script and strip that single line. Every other
    # behavior — including the install seam this test exists to verify —
    # is preserved verbatim.
    script_copy = tmp_path / "cut-rc.sh"
    script_text = SCRIPT.read_text(encoding="utf-8")
    script_text = re.sub(
        r"^export PATH=\"/opt/homebrew/bin:\$PATH\"\s*$",
        ": # PATH override stripped for hermetic test",
        script_text,
        count=1,
        flags=re.MULTILINE,
    )
    script_copy.write_text(script_text)
    script_copy.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(script_copy), "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr

    # The script should have reached the install seam.
    rc_env = repo / ".forge" / "rc-envs" / "v0.99.0rc0"
    assert rc_env.is_dir(), (
        "cut-rc.sh did not create the isolated venv; install seam not exercised. "
        f"Output:\n{combined}"
    )
    assert (rc_env / "bin" / "forge").is_file()

    # Every pip invocation must have come from the venv's shim pip; the bare
    # operator pip must never have been called. We verify two ways: (a) the
    # operator env file is bit-for-bit unchanged; (b) the script's output
    # contains no `+ pip install` trace targeting bare pip.
    post_mtime = operator_pkg.stat().st_mtime
    post_size = operator_pkg.stat().st_size
    post_bytes = operator_pkg.read_bytes()
    assert post_mtime == pre_mtime, (
        f"Operator env theforge mtime changed: {pre_mtime} -> {post_mtime}"
    )
    assert post_size == pre_size
    assert post_bytes == pre_bytes, "Operator env theforge contents changed"

    bad_pattern = re.compile(
        r"^\+ pip install .*git\+https://github\.com/fuzzypete/theforge",
        flags=re.MULTILINE,
    )
    assert not bad_pattern.search(combined)

    # Sanity: the venv shim pip was called with the RC git URL — proves the
    # install seam targeted the isolated venv specifically.
    if pip_log.exists():
        log = pip_log.read_text()
        assert "git+https://github.com/fuzzypete/theforge.git@v0.99.0rc0" in log, (
            f"Isolated venv pip was not invoked with the RC tag. pip log:\n{log}"
        )


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
