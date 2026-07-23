from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.runners.sandbox import sandbox_command
from theforge.runners.tool_runtime import _handle_bash


def test_sandbox_command_macos_wraps(tmp_path: Path) -> None:
    cmd = ["claude", "-p"]
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = sandbox_command(cmd, tmp_path)
    assert wrapped[:2] == ["sandbox-exec", "-p"]
    assert "claude" in wrapped


def test_sandbox_command_linux_wraps(tmp_path: Path) -> None:
    cmd = ["bash", "-c", "pwd"]
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = sandbox_command(cmd, tmp_path)
    assert wrapped[0] == "bwrap"
    assert "bash" in wrapped


def test_sandbox_command_fallback_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cmd = ["claude", "-p"]
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=False),
        caplog.at_level(logging.WARNING),
    ):
        wrapped = sandbox_command(cmd, tmp_path)
    assert wrapped == cmd


def test_handle_bash_runs_raw_bash_command_without_sandbox_wrapping(tmp_path: Path) -> None:
    with patch("theforge.runners.tool_runtime.subprocess.run") as mock_run:
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0
        _handle_bash(command="pwd", working_dir=tmp_path)

    argv = mock_run.call_args[0][0]
    assert argv == ["bash", "-c", "pwd"]
    assert argv[0] != "sandbox-exec"
    assert "sandbox-exec" not in argv
    assert not any("(version 1)" in arg for arg in argv)


def test_bash_permission_error_returns_workspace_sandbox_violation() -> None:
    with patch(
        "theforge.runners.tool_runtime.subprocess.run",
        side_effect=PermissionError("Operation not permitted"),
    ):
        output = _handle_bash(command="cat ../other/file", working_dir=Path("/tmp/worktree"))
    assert "workspace sandbox violation" in output


def test_macos_profile_does_not_allow_global_reads(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _macos_profile

    profile = _macos_profile(tmp_path)

    assert "(allow file-read*)\n" not in profile
    assert f'(subpath "{tmp_path.resolve()}")' in profile


def test_sandbox_command_linux_probe_uses_hashable_cache_key(tmp_path: Path) -> None:
    cmd = ["bash", "-c", "pwd"]

    def fake_available(binary: str, probe_key: tuple[str, ...]) -> bool:
        assert isinstance(probe_key, tuple)
        return True

    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", side_effect=fake_available),
    ):
        wrapped = sandbox_command(cmd, tmp_path)

    assert wrapped[0] == "bwrap"


def test_workspace_sandbox_allows_project_root_but_blocks_sibling_worktrees(
    tmp_path: Path,
) -> None:
    from theforge.runners.sandbox import workspace_effect_sandbox_command

    worktree = tmp_path / ".forge" / "worktrees" / "issue-592"
    sibling = tmp_path / ".forge" / "worktrees" / "issue-777"
    worktree.mkdir(parents=True)
    sibling.mkdir(parents=True)

    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = workspace_effect_sandbox_command(["bash", "-c", "pwd"], worktree)

    assert wrapped[0] == "bwrap"
    assert str(tmp_path) in wrapped
    assert str(sibling) in wrapped
    assert wrapped.count(str(worktree)) >= 2


def test_macos_profile_blocks_sibling_worktrees_but_keeps_project_root_readable(
    tmp_path: Path,
) -> None:
    from theforge.runners.sandbox import _blocked_worktree_roots, _macos_profile

    worktree = tmp_path / ".forge" / "worktrees" / "issue-592"
    sibling = tmp_path / ".forge" / "worktrees" / "issue-777"
    worktree.mkdir(parents=True)
    sibling.mkdir(parents=True)

    profile = _macos_profile(worktree, denied_read_roots=_blocked_worktree_roots(worktree))

    assert f'(subpath "{tmp_path.resolve()}")' in profile
    assert profile.index("(allow file-read*") < profile.index("(deny file-read*")
    assert f'(subpath "{sibling.resolve()}")' in profile


def test_user_config_roots_excludes_sensitive_credentials(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _user_config_roots

    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".ssh" / "known_hosts").write_text("", encoding="utf-8")
    (fake_home / ".gitconfig").write_text("", encoding="utf-8")
    (fake_home / ".git-credentials").write_text("", encoding="utf-8")

    _user_config_roots.cache_clear()
    with patch("theforge.runners.sandbox.Path.home", return_value=fake_home):
        roots = _user_config_roots()

    assert fake_home / ".gitconfig" in roots
    assert fake_home / ".ssh" / "known_hosts" in roots
    assert fake_home / ".git-credentials" not in roots
    assert fake_home / ".ssh" not in roots


def test_macos_profile_grants_network_and_dns(tmp_path: Path) -> None:
    """A wrapped network-bound CLI needs outbound egress + mDNS to reach its API (#1907)."""
    from theforge.runners.sandbox import _macos_profile

    profile = _macos_profile(tmp_path)
    assert "(allow network-outbound)" in profile
    assert '(allow mach-lookup (global-name "com.apple.mDNSResponder"))' in profile


def test_macos_profile_credential_services_adds_keychain_access(tmp_path: Path) -> None:
    """allow_credential_services grants securityd + keychain reads so auth survives (#1907)."""
    from theforge.runners.sandbox import _macos_profile

    without = _macos_profile(tmp_path)
    assert "com.apple.SecurityServer" not in without

    with_creds = _macos_profile(tmp_path, allow_credential_services=True)
    assert '(allow mach-lookup (global-name "com.apple.SecurityServer"))' in with_creds
    keychains = str((Path.home() / "Library" / "Keychains").resolve())
    if (Path.home() / "Library" / "Keychains").exists():
        assert keychains in with_creds


def test_macos_profile_extra_write_roots_are_writable(tmp_path: Path) -> None:
    """Claude's ~/.claude-style state dir appears in the file-write* block, not read-only."""
    from theforge.runners.sandbox import _macos_profile

    state_dir = tmp_path / "state"
    profile = _macos_profile(tmp_path, extra_write_roots=(state_dir,))
    write_block = profile.split("(allow file-read*")[0]
    assert f'(subpath "{state_dir.resolve()}")' in write_block


def test_linux_command_binds_extra_write_roots(tmp_path: Path) -> None:
    from theforge.runners.sandbox import workspace_effect_sandbox_command

    worktree = tmp_path / ".forge" / "worktrees" / "issue-1"
    worktree.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Linux"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = workspace_effect_sandbox_command(
            ["bash", "-c", "pwd"], worktree, extra_write_roots=(state_dir,)
        )
    assert "--bind-try" in wrapped
    idx = wrapped.index("--bind-try")
    assert wrapped[idx + 1] == str(state_dir.resolve())


def test_workspace_effect_command_threads_credential_services(tmp_path: Path) -> None:
    """allow_credential_services reaches the macOS profile through the public wrapper."""
    from theforge.runners.sandbox import workspace_effect_sandbox_command

    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = workspace_effect_sandbox_command(
            ["claude", "-p"], tmp_path, allow_credential_services=True
        )
    assert wrapped[0] == "sandbox-exec"
    profile = wrapped[2]
    assert "com.apple.SecurityServer" in profile


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only sandbox-exec test")
def test_macos_sandbox_profile_blocks_sibling_worktree_reads_in_practice(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _blocked_worktree_roots, _macos_profile

    if shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec unavailable")

    # Probe: some macOS environments deny sandbox-exec itself (SIP, CI entitlements).
    probe = subprocess.run(
        ["sandbox-exec", "-p", "(version 1)(allow default)", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 and "Operation not permitted" in (probe.stderr + probe.stdout):
        pytest.skip("sandbox-exec present but denied by host environment")

    worktree = tmp_path / ".forge" / "worktrees" / "issue-592"
    sibling = tmp_path / ".forge" / "worktrees" / "issue-777"
    worktree.mkdir(parents=True)
    sibling.mkdir(parents=True)

    allowed_file = tmp_path / "shared-context.txt"
    blocked_file = sibling / "secret.txt"
    allowed_file.write_text("shared context\n", encoding="utf-8")
    blocked_file.write_text("sibling secret\n", encoding="utf-8")

    profile = _macos_profile(worktree, denied_read_roots=_blocked_worktree_roots(worktree))

    allowed = subprocess.run(
        ["sandbox-exec", "-p", profile, "cat", str(allowed_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    blocked = subprocess.run(
        ["sandbox-exec", "-p", profile, "cat", str(blocked_file)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert allowed.returncode == 0
    assert allowed.stdout == "shared context\n"
    assert blocked.returncode != 0
    assert "Operation not permitted" in blocked.stderr
