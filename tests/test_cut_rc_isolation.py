"""Regression checks for ``scripts/cut-rc.sh``.

Two structural invariants the script must preserve:

1. **Substrate isolation.** Every install of the cut RC must go through the
   isolated venv's ``pip`` at ``.forge/rc-envs/<RC_TAG>/bin/pip`` — never
   through whatever Python environment happens to be active when the
   operator runs the script. The historical failure mode this guards
   against: an earlier version of the script ran
   ``pip install --force-reinstall git+...@<RC_TAG>`` with no environment
   qualifier, which silently overwrote the operator's editable install of
   source.

2. **Default-command rebinding.** After a successful cut, plain ``forge``
   in the operator's normal shell must resolve to the just-cut RC. The
   script accomplishes this by repointing a managed launcher symlink
   (``~/.local/bin/forge`` by default, overridable via
   ``FORGE_MANAGED_LAUNCHER``) at the new venv's binary — without
   mutating any pre-existing Python environment. The script must refuse
   to overwrite a launcher that lives inside a venv's ``bin/``, since
   ``pip install -e .`` would later regenerate that file and silently
   break the binding.
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
    """All executed installs must go through the isolated venv's pip; the
    historical bare ``pip install ...`` line that mutated the active env is
    forbidden in code (echoed help text is fine).
    """
    body = SCRIPT.read_text(encoding="utf-8")

    code_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("echo "):
            continue
        code_lines.append(line)

    code = "\n".join(code_lines)
    assert not re.search(
        r"^\s*(run\s+)?pip\s+install\s+.*git\+https://github\.com/fuzzypete/theforge",
        code,
        flags=re.MULTILINE,
    ), "cut-rc.sh installs the RC into whatever Python env is active; must use the isolated venv"


def test_script_creates_isolated_rc_env() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert ".forge/rc-envs/" in body, "cut-rc.sh must reference .forge/rc-envs/<tag>/"
    assert "python3 -m venv" in body, "cut-rc.sh must create an isolated venv"


def test_script_repoints_managed_launcher_at_isolated_venv() -> None:
    """The script must symlink a forge-managed launcher path at the
    isolated venv's binary, not pip-install into any pre-existing env.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    assert "MANAGED_LAUNCHER" in body, (
        "cut-rc.sh must define a managed launcher path that gets repointed at the RC"
    )
    # The repoint must be a symlink (atomic, no env mutation), pointing at $RC_ENV_FORGE.
    assert re.search(r'ln\s+-snf\s+"\$RC_ENV_FORGE"\s+"\$MANAGED_LAUNCHER"', body), (
        "cut-rc.sh must `ln -snf $RC_ENV_FORGE $MANAGED_LAUNCHER` to repoint the launcher"
    )


def test_script_refuses_to_overwrite_venv_resident_launcher() -> None:
    """If plain ``forge`` resolves into a Python env's ``bin/``, the script
    must refuse rather than clobber a pip-managed console script.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # Detection of in-venv launcher: must check for activate / pyvenv.cfg next to the binary.
    assert "pyvenv.cfg" in body, (
        "cut-rc.sh must detect a venv-resident launcher via pyvenv.cfg before overwriting"
    )
    assert "activate" in body
    # And it must exit non-zero in that branch.
    refuse_block = re.search(r"pyvenv\.cfg.*?exit\s+1", body, flags=re.DOTALL)
    assert refuse_block is not None, (
        "cut-rc.sh must `exit 1` when plain forge resolves inside a venv bin/"
    )


def test_script_verifies_plain_forge_reports_rc() -> None:
    """After repointing, the script must invoke plain ``forge version``
    (PATH-resolved, no path qualification) and fail loud if the resolved
    binary doesn't report the RC version.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # The post-repoint verify uses bare `forge version` (PATH-resolved).
    assert re.search(r"PLAIN_OUTPUT=\$\(forge version", body), (
        "cut-rc.sh must verify plain `forge version` after repointing the launcher"
    )
    # And RC_ENV_FORGE is still used for the in-venv install verification.
    assert "$RC_ENV_FORGE" in body


def test_test_ladder_uses_plain_forge() -> None:
    """The operator-facing test ladder must invoke plain ``forge`` (the
    repointed default command), not the path-qualified RC venv binary.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    ladder = re.search(r"Smoke pass.*?Moneyshot pass[^\n]*", body, flags=re.DOTALL)
    assert ladder is not None
    ladder_text = ladder.group(0)
    assert "$RC_ENV_FORGE" not in ladder_text, (
        "Test ladder must use plain `forge`, not the path-qualified RC venv binary"
    )
    assert ".forge/rc-envs/" not in ladder_text
    assert re.search(r"forge sprint --verbose --issues", ladder_text), (
        "Test ladder must show plain `forge sprint ...` invocation"
    )


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_full_execution_repoints_managed_launcher_and_does_not_mutate_operator_env(
    tmp_path: Path,
) -> None:
    """End-to-end install seam test:

    Run ``scripts/cut-rc.sh`` against a controlled fixture with all external
    boundaries shimmed at PATH. Verify that:

    - the operator's installed ``theforge`` package is bit-for-bit unchanged
      (no pip install ever ran outside the isolated venv);
    - the managed launcher (``$HOME/.local/bin/forge`` in this fixture)
      exists, is a symlink, and points at the isolated venv's binary;
    - plain ``forge version`` (PATH-resolved) reports the cut RC version
      via the symlink chain.
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

    pip_log = tmp_path / "pip_calls.log"
    py_shim = bin_dir / "python3"
    py_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
                VENV="$3"
                mkdir -p "$VENV/bin"
                # Mark the dir as a real venv so the script's refuse-to-clobber
                # heuristic would correctly identify it (defence-in-depth).
                : > "$VENV/pyvenv.cfg"
                cat > "$VENV/bin/pip" <<'PIP'
            #!/bin/sh
            echo "$@" >> "{pip_log}"
            exit 0
            PIP
                chmod +x "$VENV/bin/pip"
                cat > "$VENV/bin/forge" <<'FORGE'
            #!/bin/sh
            if [ "$1" = "version" ]; then
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

    # Isolated HOME so the script's ~/.local/bin/forge symlink lands in tmp,
    # not in the developer's real home directory.
    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    # Build a hermetic PATH so the test process's own `.venv/bin/forge`
    # (an editable install) does not leak in and trip the refuse-to-clobber
    # branch. The managed launcher dir must come BEFORE bin_dir so the
    # script's post-repoint `command -v forge` lookup resolves to the symlink.
    env["PATH"] = f"{fake_home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    # VIRTUAL_ENV would also poison `command -v` heuristics; clear it.
    env.pop("VIRTUAL_ENV", None)

    script_copy = tmp_path / "cut-rc.sh"
    script_text = SCRIPT.read_text(encoding="utf-8")
    # Strip the hardcoded /opt/homebrew PATH override so our shims win.
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

    assert proc.returncode == 0, (
        f"cut-rc.sh exited non-zero ({proc.returncode}). Output:\n{combined}"
    )

    # The isolated venv must exist and be populated.
    rc_env = repo / ".forge" / "rc-envs" / "v0.99.0rc0"
    assert rc_env.is_dir(), (
        "cut-rc.sh did not create the isolated venv; install seam not exercised. "
        f"Output:\n{combined}"
    )
    rc_forge = rc_env / "bin" / "forge"
    assert rc_forge.is_file()

    # The managed launcher must exist, be a symlink, and point at the RC venv.
    managed_launcher = fake_home / ".local" / "bin" / "forge"
    assert managed_launcher.is_symlink(), (
        f"managed launcher {managed_launcher} is not a symlink. Output:\n{combined}"
    )
    assert os.readlink(managed_launcher) == str(rc_forge), (
        f"managed launcher points at {os.readlink(managed_launcher)}, expected {rc_forge}"
    )

    # Plain `forge version` (PATH-resolved through the managed launcher)
    # must report the cut RC. Substrate provenance: the binary actually
    # executing is the RC venv's forge, not whatever was on PATH before.
    plain_check = subprocess.run(
        ["bash", "-c", "command -v forge && forge version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert plain_check.returncode == 0, plain_check.stderr
    assert str(managed_launcher) in plain_check.stdout
    assert "0.99.0rc0" in plain_check.stdout, (
        f"plain forge version did not report cut RC: {plain_check.stdout}"
    )

    # Operator env theforge package must be bit-for-bit unchanged.
    post_mtime = operator_pkg.stat().st_mtime
    post_size = operator_pkg.stat().st_size
    post_bytes = operator_pkg.read_bytes()
    assert post_mtime == pre_mtime
    assert post_size == pre_size
    assert post_bytes == pre_bytes, "Operator env theforge contents changed"

    # No bare `pip install` of the RC tag escaped the venv-pip seam.
    bad_pattern = re.compile(
        r"^\+ pip install .*git\+https://github\.com/fuzzypete/theforge",
        flags=re.MULTILINE,
    )
    assert not bad_pattern.search(combined)

    if pip_log.exists():
        log = pip_log.read_text()
        assert "git+https://github.com/fuzzypete/theforge.git@v0.99.0rc0" in log, (
            f"Isolated venv pip was not invoked with the RC tag. pip log:\n{log}"
        )


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_full_execution_refuses_to_clobber_venv_resident_forge(tmp_path: Path) -> None:
    """If plain ``forge`` resolves into a Python env's ``bin/`` (e.g. an
    editable install's console script), the script must refuse and exit
    non-zero rather than overwrite a pip-managed file.
    """
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

    # Fake operator venv with a `forge` console script in its bin/.
    fake_venv = tmp_path / "operator_venv"
    (fake_venv / "bin").mkdir(parents=True)
    (fake_venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    venv_forge = fake_venv / "bin" / "forge"
    venv_forge.write_text("#!/bin/sh\necho 'editable-install forge'\n")
    venv_forge.chmod(0o755)
    venv_forge_pre_bytes = venv_forge.read_bytes()

    pip_log = tmp_path / "pip_calls.log"
    py_shim = bin_dir / "python3"
    py_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
                VENV="$3"
                mkdir -p "$VENV/bin"
                : > "$VENV/pyvenv.cfg"
                cat > "$VENV/bin/pip" <<'PIP'
            #!/bin/sh
            echo "$@" >> "{pip_log}"
            exit 0
            PIP
                chmod +x "$VENV/bin/pip"
                cat > "$VENV/bin/forge" <<'FORGE'
            #!/bin/sh
            if [ "$1" = "version" ]; then
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

    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    # Hermetic PATH (don't inherit the test runner's own venv); fake_venv
    # is AHEAD of the managed launcher dir to simulate an active venv whose
    # forge precedes ~/.local/bin on PATH.
    env["PATH"] = f"{fake_venv}/bin:{fake_home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env.pop("VIRTUAL_ENV", None)

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

    assert proc.returncode != 0, (
        f"cut-rc.sh should refuse to overwrite a venv-resident forge launcher. Output:\n{combined}"
    )
    # Operator's venv forge must not have been touched.
    assert venv_forge.read_bytes() == venv_forge_pre_bytes
    # The error message must guide the operator and explain why (no
    # importable theforge in that env).
    assert "inside a Python environment" in combined
    assert "no importable" in combined or "cannot reason" in combined
    # The managed launcher must NOT have been created.
    managed_launcher = fake_home / ".local" / "bin" / "forge"
    assert not managed_launcher.exists()


def _build_cut_rc_fixture(
    tmp_path: Path,
    installed_theforge_version: str | None,
) -> tuple[Path, dict[str, str], Path, Path]:
    """Shared fixture builder for the venv-resident-forge discrimination tests.

    Constructs a fake repo, a hermetic PATH with shimmed gh/make/git/python3,
    and a fake operator venv whose ``bin/forge`` is a fake console script. If
    ``installed_theforge_version`` is not None, the fake venv's ``bin/python``
    answers ``importlib.metadata.version('theforge')`` with that string.

    Returns (script_copy_path, env_dict, fake_home, fake_venv).
    """
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

    pip_log = tmp_path / "pip_calls.log"
    py_shim = bin_dir / "python3"
    py_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
                VENV="$3"
                mkdir -p "$VENV/bin"
                : > "$VENV/pyvenv.cfg"
                cat > "$VENV/bin/pip" <<'PIP'
            #!/bin/sh
            echo "$@" >> "{pip_log}"
            exit 0
            PIP
                chmod +x "$VENV/bin/pip"
                cat > "$VENV/bin/forge" <<'FORGE'
            #!/bin/sh
            if [ "$1" = "version" ]; then
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

    # Fake operator venv. Its bin/python answers importlib.metadata if
    # installed_theforge_version is provided; otherwise the probe fails.
    fake_venv = tmp_path / "operator_venv"
    (fake_venv / "bin").mkdir(parents=True)
    (fake_venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    venv_forge = fake_venv / "bin" / "forge"
    if installed_theforge_version is not None:
        # Make `forge version` report the env's installed theforge version so
        # the post-repoint plain-version check has something realistic to
        # match against, even when this binary still wins PATH after the
        # symlink is created. The script's vetting check requires a literal
        # non-comment `from theforge…` / `import theforge…` line at column 0
        # (the pip-generated console script has `from theforge.cli import main`).
        # Place that line after `exit 0` so it lives in the file (and the grep
        # finds it) without breaking shell parsing.
        venv_forge.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                if [ "$1" = "version" ]; then
                    echo "TheForge v{installed_theforge_version}"
                fi
                exit 0
                from theforge.cli import main
                """
            )
        )
    else:
        venv_forge.write_text("#!/bin/sh\necho 'in-venv forge'\n")
    venv_forge.chmod(0o755)
    venv_python = fake_venv / "bin" / "python"
    if installed_theforge_version is not None:
        venv_python.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                # Minimal stand-in: respond to the cut-rc probe
                #   python -c 'import importlib.metadata as m; print(m.version("theforge"))'
                # by emitting the configured version. Any other invocation is a no-op.
                case "$*" in
                    *importlib.metadata*theforge*)
                        echo "{installed_theforge_version}"
                        exit 0
                        ;;
                esac
                exit 0
                """
            )
        )
    else:
        venv_python.write_text("#!/bin/sh\nexit 1\n")
    venv_python.chmod(0o755)

    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    # fake_venv/bin is AHEAD of $MANAGED_LAUNCHER_DIR. This is the documented
    # dogfood operator state (pyenv on PATH ahead of ~/.local/bin) — the
    # post-repoint check must NOT require the symlink to win PATH for the
    # in-venv-with-theforge case.
    env["PATH"] = f"{fake_venv}/bin:{fake_home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env.pop("VIRTUAL_ENV", None)

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

    return script_copy, env, fake_home, fake_venv


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_venv_resident_forge_with_same_rc_proceeds(tmp_path: Path) -> None:
    """When plain ``forge`` lives in a Python env that has ``theforge``
    pip-installed at the SAME version we're cutting, the repoint is
    identity — the script must proceed silently and the symlink must land.
    """
    script_copy, env, fake_home, _fake_venv = _build_cut_rc_fixture(
        tmp_path, installed_theforge_version="0.99.0rc0"
    )

    repo = tmp_path / "fake_repo"
    proc = subprocess.run(
        ["bash", str(script_copy), "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "cut-rc.sh must proceed when the in-venv forge is the same RC we're cutting. "
        f"Output:\n{combined}"
    )
    managed_launcher = fake_home / ".local" / "bin" / "forge"
    assert managed_launcher.is_symlink(), f"managed launcher must be created. Output:\n{combined}"
    # No "uninstall" / "deactivate" / "re-run" remediation must appear.
    assert "uninstall" not in combined.lower()
    assert "deactivate" not in combined.lower()
    # Spec: the same-RC path completes silently — no identity note about
    # what the env already has, no takeover note, no shadowing note.
    assert "repoint is identity" not in combined
    assert "taking over plain" not in combined
    assert "shadow" not in combined.lower()


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_venv_resident_forge_with_theforge_installed_but_foreign_launcher_refuses(
    tmp_path: Path,
) -> None:
    """An environment can have ``theforge`` importable while its ``bin/forge``
    is a hand-written wrapper or a console script for a different package.
    In that case the cut script must still refuse — having theforge in the
    env is not sufficient to vet what plain ``forge`` actually does.
    """
    script_copy, env, fake_home, fake_venv = _build_cut_rc_fixture(
        tmp_path, installed_theforge_version="0.10.0rc12"
    )
    # Overwrite the venv's forge with a wrapper that does NOT mention
    # theforge.cli (i.e., it's not the pip-generated console script). The
    # env still has theforge importable per the python shim, but bin/forge
    # is foreign.
    venv_forge = fake_venv / "bin" / "forge"
    venv_forge.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            # hand-written wrapper for some other tool; no theforge reference
            echo "not-theforge-launcher"
            exit 0
            """
        )
    )
    venv_forge.chmod(0o755)
    venv_forge_pre_bytes = venv_forge.read_bytes()

    repo = tmp_path / "fake_repo"
    proc = subprocess.run(
        ["bash", str(script_copy), "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "cut-rc.sh must refuse when bin/forge is not identifiable as the "
        "theforge console script, even if the env has theforge importable. "
        f"Output:\n{combined}"
    )
    # Operator's bin/forge must not be touched.
    assert venv_forge.read_bytes() == venv_forge_pre_bytes
    # Managed launcher must not be created.
    managed_launcher = fake_home / ".local" / "bin" / "forge"
    assert not managed_launcher.exists()
    # The diagnostic must explain the bin/forge mismatch and reference the
    # installed-but-not-the-launcher condition.
    assert "from theforge" in combined or "import theforge" in combined
    assert "0.10.0rc12" in combined


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_venv_resident_forge_with_comment_only_theforge_mention_refuses(
    tmp_path: Path,
) -> None:
    """Stronger refusal: a hand-written wrapper whose only mention of
    theforge is in COMMENT text (no real Python import line at column 0)
    must still be refused, even when the env has theforge importable.
    This guards against the loose text-grep regression: ``# copied from
    theforge`` or ``# uses theforge.cli`` inside a shell wrapper must not
    be accepted as evidence that bin/forge is the pip-generated console
    script.
    """
    script_copy, env, fake_home, fake_venv = _build_cut_rc_fixture(
        tmp_path, installed_theforge_version="0.10.0rc12"
    )
    # Overwrite bin/forge with a wrapper whose only theforge mention is in
    # comment lines. No non-comment `from theforge` / `import theforge`
    # line — must NOT match the strict probe.
    venv_forge = fake_venv / "bin" / "forge"
    venv_forge.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            # hand-written wrapper, copied from theforge originally,
            # references theforge.cli only in this comment text — not a
            # real `from theforge` import line.
            echo "wrapper output"
            exit 0
            """
        )
    )
    venv_forge.chmod(0o755)
    venv_forge_pre_bytes = venv_forge.read_bytes()

    repo = tmp_path / "fake_repo"
    proc = subprocess.run(
        ["bash", str(script_copy), "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "cut-rc.sh must refuse when bin/forge mentions theforge only in "
        f"comments — not a real import line. Output:\n{combined}"
    )
    assert venv_forge.read_bytes() == venv_forge_pre_bytes
    managed_launcher = fake_home / ".local" / "bin" / "forge"
    assert not managed_launcher.exists()
    # Diagnostic must mention the installed-but-not-the-launcher condition,
    # naming the strict non-comment requirement.
    assert "non-comment" in combined or "column 0" in combined


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_venv_resident_forge_with_different_theforge_fails_before_ladder(
    tmp_path: Path,
) -> None:
    """When plain ``forge`` lives in a Python env that has ``theforge``
    pip-installed at a DIFFERENT version (older RC, dev install, etc.), the
    script may install the managed symlink but must not complete the cut or
    print the bare-``forge`` dogfood ladder. Plain ``forge`` is the candidate
    selector for dogfood/dev, so a shadowed older install is a hard failure.
    """
    script_copy, env, fake_home, fake_venv = _build_cut_rc_fixture(
        tmp_path, installed_theforge_version="0.10.0rc12"
    )

    repo = tmp_path / "fake_repo"
    rc_env_forge = repo / ".forge" / "rc-envs" / "v0.99.0rc0" / "bin" / "forge"
    proc = subprocess.run(
        ["bash", str(script_copy), "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "cut-rc.sh must fail when plain forge still resolves to a different "
        f"theforge version after repointing. Output:\n{combined}"
    )
    managed_launcher = fake_home / ".local" / "bin" / "forge"
    assert managed_launcher.is_symlink()
    assert managed_launcher.resolve() == rc_env_forge
    # Diagnostic must mention the shadowing version and remediation, but the
    # dogfood ladder must not print because bare `forge sprint` would target
    # the wrong binary.
    assert "0.10.0rc12" in combined
    assert "shadow" in combined.lower()
    assert "--resume" in combined
    assert "Test ladder" not in combined
    assert "forge sprint" not in combined
    assert "uninstall" not in combined.lower()
    assert "deactivate" not in combined.lower()


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_pyenv_shim_resolves_to_real_launcher_before_vetting(tmp_path: Path) -> None:
    """A pyenv shim directory may contain generic ``forge``, ``python``, and
    ``activate`` shims. That must not make the shim itself the launcher being
    vetted; resolve through ``pyenv which forge`` and inspect the real console
    script instead.
    """
    script_copy, env, fake_home, fake_venv = _build_cut_rc_fixture(
        tmp_path, installed_theforge_version="0.99.0rc0"
    )

    pyenv_root = fake_home / ".pyenv"
    pyenv_shims = pyenv_root / "shims"
    pyenv_shims.mkdir(parents=True)
    pyenv_bin = pyenv_root / "versions" / "3.12.12" / "bin"
    pyenv_bin.mkdir(parents=True)
    real_forge = pyenv_bin / "forge"
    real_python = pyenv_bin / "python"
    real_forge.write_bytes((fake_venv / "bin" / "forge").read_bytes())
    real_forge.chmod(0o755)
    real_python.write_bytes((fake_venv / "bin" / "python").read_bytes())
    real_python.chmod(0o755)

    pyenv_shim_body = textwrap.dedent(
        f"""\
        #!/bin/sh
        exec "{real_forge}" "$@"
        """
    )
    (pyenv_shims / "forge").write_text(pyenv_shim_body)
    (pyenv_shims / "forge").chmod(0o755)
    (pyenv_shims / "python").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            exec "{real_python}" "$@"
            """
        )
    )
    (pyenv_shims / "python").chmod(0o755)
    # This generic pyenv shim was the false-positive trigger: the old
    # heuristic treated ~/.pyenv/shims as a venv bin/ because activate existed.
    (pyenv_shims / "activate").write_text("#!/bin/sh\nexit 0\n")
    (pyenv_shims / "activate").chmod(0o755)

    shim_bin = tmp_path / "shim_bin"
    pyenv_cmd = shim_bin / "pyenv"
    pyenv_cmd.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "which" ] && [ "$2" = "forge" ]; then
                echo "{real_forge}"
                exit 0
            fi
            exit 1
            """
        )
    )
    pyenv_cmd.chmod(0o755)

    env["PYENV_ROOT"] = str(pyenv_root)
    env["PATH"] = f"{pyenv_shims}:{fake_home}/.local/bin:{shim_bin}:/usr/bin:/bin"

    repo = tmp_path / "fake_repo"
    proc = subprocess.run(
        ["bash", str(script_copy), "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "cut-rc.sh must resolve pyenv shims to the real forge launcher before "
        f"applying the theforge-console-script vetting check. Output:\n{combined}"
    )
    assert "does not look" not in combined
    assert "0.99.0rc0" in combined
    assert (fake_home / ".local" / "bin" / "forge").is_symlink()


@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_script_isolates_running_source_from_git_checkouts() -> None:
    """The script performs ``git checkout`` mid-run; bash continues reading
    its source by byte offset from the on-disk file. If the checked-out
    branch carries a divergent copy of ``scripts/cut-rc.sh``, bash executes
    whatever bytes land at the current offset in the swapped file (silent
    corruption per byte, per branch). The fix: copy the launching source to
    a temp path before any git operation runs and re-exec from there, so
    the running interpreter reads from a source no git operation can reach.
    """
    body = SCRIPT.read_text(encoding="utf-8")

    self_copy_guard = re.search(r'if\s+\[\[\s+"\$\{CUT_RC_SELF_COPY:-\}"\s*!=\s*"1"\s+\]\]', body)
    assert self_copy_guard is not None, (
        "cut-rc.sh must guard against self-mutation by re-execing from a temp copy "
        "(expected CUT_RC_SELF_COPY sentinel guarding initial entry)"
    )
    assert re.search(r"_self_copy=.*mktemp", body), (
        "cut-rc.sh must mktemp a temp copy of itself before re-exec"
    )
    assert re.search(r'cat\s+"\$0"\s*>\s*"\$_self_copy"', body), (
        "cut-rc.sh must copy its launching source ($0) into the temp file"
    )
    assert re.search(r'exec\s+bash\s+"\$_self_copy"\s+"\$@"', body), (
        "cut-rc.sh must re-exec bash against the temp copy with original args"
    )

    # The slurp/re-exec must precede the first git operation. Find both
    # offsets and assert ordering.
    self_copy_pos = body.find("CUT_RC_SELF_COPY")
    first_git_op = re.search(r"^\s*(run\s+)?git\s+(checkout|pull|fetch|push)", body, re.MULTILINE)
    assert first_git_op is not None
    assert self_copy_pos < first_git_op.start(), (
        "Self-copy/re-exec guard must precede the first git operation in the script"
    )


def test_running_interpreter_survives_swapping_on_disk_script(tmp_path: Path) -> None:
    """End-to-end proof: launch the script, then while it is running have a
    git checkout swap the on-disk source for a divergent copy. The running
    interpreter must still execute the launching version's bytes — not the
    swapped-in bytes — because it is reading from a temp copy.
    """
    # Build a tiny test harness script that mirrors the self-copy idiom and
    # demonstrably survives an on-disk overwrite mid-run. The harness uses
    # the same guard pattern as cut-rc.sh so a regression in the pattern
    # surfaces here too.
    launcher = tmp_path / "harness.sh"
    launcher.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${HARNESS_SELF_COPY:-}" != "1" ]]; then
                _self_copy="$(mktemp -t harness.XXXXXX)"
                cat "$0" > "$_self_copy"
                chmod +x "$_self_copy"
                export HARNESS_SELF_COPY=1
                export HARNESS_SELF_COPY_PATH="$_self_copy"
                exec bash "$_self_copy" "$@"
            fi
            trap 'rm -f "$HARNESS_SELF_COPY_PATH"' EXIT
            echo "PRE_SWAP"
            # Overwrite our own on-disk source with a divergent script.
            cat > "$1" <<'SWAPPED'
            #!/usr/bin/env bash
            echo "SWAPPED_BYTES_EXECUTED"
            exit 99
            SWAPPED
            # Pad the swap so its byte length differs from the original
            # (the original failure mode depended on offset alignment).
            for _ in $(seq 1 50); do echo "# pad" >> "$1"; done
            echo "POST_SWAP"
            exit 0
            """
        )
    )
    launcher.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(launcher), str(launcher)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"harness should complete normally (rc=0); got rc={proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "PRE_SWAP" in proc.stdout
    assert "POST_SWAP" in proc.stdout, (
        "Running interpreter did not continue executing launching version's bytes "
        "after on-disk source was swapped — self-copy idiom is broken.\n"
        f"stdout:\n{proc.stdout}"
    )
    assert "SWAPPED_BYTES_EXECUTED" not in proc.stdout, (
        "Running interpreter executed bytes from the swapped-in file — "
        "self-copy guard is not preventing self-mutation."
    )


def test_script_parses_resume_flag() -> None:
    """The script must accept a ``--resume`` flag and a corresponding
    ``RESUME`` state variable. This is the surface contract for
    recovering from a cut that aborted after tag-push.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    assert "--resume" in body, "cut-rc.sh must accept a --resume flag"
    assert re.search(r"--resume\)\s*RESUME=true", body), (
        "cut-rc.sh must parse --resume into a RESUME=true state"
    )


def test_resume_skips_bump_commit_tag_push() -> None:
    """In resume mode, the bump/commit/tag/push phase must be guarded
    off — the previous cut already pushed the tag, so re-running those
    steps would either no-op noisily or fail outright.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # The mutating commands must live inside a RESUME==false branch.
    bump_block = re.search(
        r'if\s+\[\[\s+"\$RESUME"\s*==\s*false\s+\]\];\s*then(.*?)(?:^else|^fi)',
        body,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert bump_block is not None, (
        "cut-rc.sh must gate bump/commit/tag/push behind `if [[ $RESUME == false ]]`"
    )
    guarded = bump_block.group(1)
    for needle in (
        "git commit -m",
        'git tag "$RC_TAG"',
        'git push origin "$RC_TAG"',
        "sed -i ''",
    ):
        assert needle in guarded, (
            f"cut-rc.sh must keep {needle!r} inside the RESUME==false branch; "
            "resume must not re-run that step"
        )


def test_resume_requires_existing_tag_on_origin() -> None:
    """Resume only makes sense if the previous cut reached tag-push.
    The script must verify the tag is already on origin before skipping
    the bump/commit/tag/push phase.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # Find the specific else attached to the RESUME guard by string search:
    idx = body.find('if [[ "$RESUME" == false ]]')
    assert idx >= 0
    tail = body[idx:]
    assert re.search(
        r"ls-remote\s+--tags\s+origin\s+\"refs/tags/\$RC_TAG\"",
        tail,
    ), "resume branch must verify the tag exists on origin via git ls-remote"
    assert "--resume requires" in tail, (
        "resume branch must emit a clear error when the tag isn't on origin"
    )


def test_launcher_refusal_messages_reference_resume() -> None:
    """The two error branches that previously printed 'scripts/cut-rc.sh
    $VERSION $RC_NUM' as remediation must now print the --resume form.
    The plain re-run is broken once the tag has been pushed.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    # Both refusal/repoint-failure error messages must mention --resume.
    refuse_msgs = re.findall(
        r'echo\s+"[^"]*scripts/cut-rc\.sh[^"]*"',
        body,
    )
    # Filter to error-message branches (those redirected to stderr appear in
    # the same echo line preceded by content). We just check that no
    # remediation message tells the operator to re-run the plain form.
    for msg in refuse_msgs:
        # The ladder-print suggestion at end of script is for the NEXT RC
        # (bump RC_NUM), not a recovery — allow that one through.
        if "RC_NUM + 1" in msg:
            continue
        # All remaining script-invocation suggestions in operator-facing text
        # must be the --resume form.
        assert "--resume" in msg, (
            "remediation message still suggests plain re-run: "
            f"{msg}. The tag is already on origin in this branch — "
            "--resume is the only invocation that completes the cut."
        )


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_resume_skips_mutating_steps_and_reaches_install(tmp_path: Path) -> None:
    """End-to-end: with ``--resume`` the script must not run
    bump/commit/tag/push, but must still drive the isolated-venv install
    and managed-launcher repoint. The git shim refuses any push/tag in
    this fixture so any regression that leaves those calls outside the
    RESUME guard would fail the run.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    # pyproject is ALREADY at the RC version — simulating the prior cut's
    # bump commit having landed on the release branch.
    (repo / "pyproject.toml").write_text('version = "0.99.0rc0"\n')
    (repo / "Makefile").write_text("gate:\n\t@echo 'gate-should-not-run-in-resume'; exit 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)

    real_git = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True
    ).stdout.strip()

    bin_dir = tmp_path / "shim_bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "gh").chmod(0o755)
    # `make` must NOT run in resume mode (gate is skipped). Make it loud if it does.
    (bin_dir / "make").write_text("#!/bin/sh\necho 'MAKE_INVOKED_IN_RESUME' >&2\nexit 1\n")
    (bin_dir / "make").chmod(0o755)
    # git shim: pretend the RC tag exists on origin via ls-remote, but refuse
    # push/tag operations so a regression that left them outside the RESUME
    # guard would surface here.
    git_shim = bin_dir / "git"
    git_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            case "$1" in
                push)
                    echo "GIT_PUSH_INVOKED_IN_RESUME: $*" >&2
                    exit 1
                    ;;
                tag)
                    echo "GIT_TAG_INVOKED_IN_RESUME: $*" >&2
                    exit 1
                    ;;
                commit)
                    echo "GIT_COMMIT_INVOKED_IN_RESUME: $*" >&2
                    exit 1
                    ;;
                ls-remote)
                    # Match the script's ls-remote --tags origin refs/tags/<tag> call
                    case "$*" in
                        *refs/tags/v0.99.0rc0*)
                            printf 'deadbeef\\trefs/tags/v0.99.0rc0\\n'
                            exit 0
                            ;;
                    esac
                    exit 0
                    ;;
                pull) exit 0 ;;
            esac
            exec {real_git} "$@"
            """
        )
    )
    git_shim.chmod(0o755)

    pip_log = tmp_path / "pip_calls.log"
    py_shim = bin_dir / "python3"
    py_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
                VENV="$3"
                mkdir -p "$VENV/bin"
                : > "$VENV/pyvenv.cfg"
                cat > "$VENV/bin/pip" <<'PIP'
            #!/bin/sh
            echo "$@" >> "{pip_log}"
            exit 0
            PIP
                chmod +x "$VENV/bin/pip"
                cat > "$VENV/bin/forge" <<'FORGE'
            #!/bin/sh
            if [ "$1" = "version" ]; then
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

    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["PATH"] = f"{fake_home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env.pop("VIRTUAL_ENV", None)

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
        ["bash", str(script_copy), "--resume", "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, (
        f"--resume should complete the cut without re-running mutating steps. Output:\n{combined}"
    )
    # No mutating git op may have run.
    for forbidden in (
        "GIT_PUSH_INVOKED_IN_RESUME",
        "GIT_TAG_INVOKED_IN_RESUME",
        "GIT_COMMIT_INVOKED_IN_RESUME",
        "MAKE_INVOKED_IN_RESUME",
    ):
        assert forbidden not in combined, (
            f"--resume re-ran a mutating step ({forbidden}). Output:\n{combined}"
        )
    # The isolated venv must still be created and pip-installed against.
    rc_env = repo / ".forge" / "rc-envs" / "v0.99.0rc0"
    assert rc_env.is_dir(), f"--resume must still build the isolated venv. Output:\n{combined}"
    assert pip_log.exists(), "--resume must still drive the isolated venv pip install"
    # Managed launcher must be repointed.
    managed_launcher = fake_home / ".local" / "bin" / "forge"
    assert managed_launcher.is_symlink(), (
        f"--resume must still repoint the managed launcher. Output:\n{combined}"
    )


@pytest.mark.network_integration
@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_resume_fails_loud_when_tag_not_on_origin(tmp_path: Path) -> None:
    """--resume is only valid when the previous cut reached tag-push. If
    the tag isn't on origin, the script must abort with a clear message
    rather than silently fall through to the install phase against a
    non-existent tag.
    """
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

    bin_dir = tmp_path / "shim_bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "make").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "make").chmod(0o755)
    # ls-remote returns nothing — tag is not on origin.
    git_shim = bin_dir / "git"
    git_shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            case "$1" in
                ls-remote) exit 0 ;;
                push) exit 0 ;;
                pull) exit 0 ;;
            esac
            exec {real_git} "$@"
            """
        )
    )
    git_shim.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"

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
        ["bash", str(script_copy), "--resume", "0.99.0", "0"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        f"--resume must fail when the RC tag is not on origin. Output:\n{combined}"
    )
    assert "--resume requires" in combined


@pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="cut-rc.sh not present",
)
def test_dry_run_does_not_emit_default_env_pip_install(tmp_path: Path) -> None:
    """In ``--dry-run`` mode, no echoed install command may target bare
    ``pip``; every install must go through the isolated venv's pip.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "pyproject.toml").write_text('version = "0.99.0"\n')
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)

    env = os.environ.copy()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_shim = bin_dir / "gh"
    gh_shim.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "issue" ] && [ "$2" = "list" ]; then
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

    combined = proc.stdout + proc.stderr
    bad_pattern = re.compile(
        r"^\+ pip install .*git\+https://github\.com/fuzzypete/theforge",
        flags=re.MULTILINE,
    )
    assert not bad_pattern.search(combined), (
        "cut-rc.sh dry-run echoed a bare `pip install` of the RC tag; "
        "this would mutate the operator's default env. Output:\n" + combined
    )
