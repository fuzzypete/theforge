from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.runners.sandbox import read_only_sandbox_command, sandbox_command
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


def test_handle_bash_uses_sandbox_command(tmp_path: Path) -> None:
    with (
        patch(
            "theforge.runners.tool_runtime.sandbox_command", return_value=["bash", "-c", "pwd"]
        ) as mock_sandbox,
        patch("theforge.runners.tool_runtime.subprocess.run") as mock_run,
    ):
        mock_run.return_value.stdout = "ok\n"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0
        _handle_bash(command="pwd", working_dir=tmp_path)
    mock_sandbox.assert_called_once()
    assert mock_run.call_args[0][0] == ["bash", "-c", "pwd"]


def test_bash_outside_allowed_root_rejected_when_sandbox_denies() -> None:
    with (
        patch(
            "theforge.runners.tool_runtime.sandbox_command",
            return_value=["bash", "-c", "cat ../other/file"],
        ),
        patch(
            "theforge.runners.tool_runtime.subprocess.run",
            side_effect=PermissionError("Operation not permitted"),
        ),
    ):
        output = _handle_bash(command="cat ../other/file", working_dir=Path("/tmp/worktree"))
    assert "workspace sandbox violation" in output


def test_macos_profile_does_not_allow_global_reads(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _macos_profile

    profile = _macos_profile(tmp_path)

    assert "(allow file-read*)\n" not in profile
    assert f'(subpath "{tmp_path.resolve()}")' in profile


def test_macos_profile_allows_extra_write_roots(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _macos_profile

    extra_write_root = tmp_path / "home" / ".claude"
    extra_write_root.mkdir(parents=True)

    profile = _macos_profile(tmp_path, extra_write_roots=(extra_write_root,))

    assert f'(subpath "{extra_write_root.resolve()}")' in profile
    write_block = profile.split("(allow file-write*", 1)[1].split("(allow file-read*", 1)[0]
    assert f'(subpath "{extra_write_root.resolve()}")' in write_block


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


def test_linux_command_uses_bind_for_extra_write_roots(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _linux_command

    extra_write_root = tmp_path / "home" / ".claude"
    extra_write_root.mkdir(parents=True)

    wrapped = _linux_command(
        ["bash", "-c", "pwd"], tmp_path, extra_write_roots=(extra_write_root,)
    )

    bind_triplets = [wrapped[i : i + 3] for i in range(len(wrapped) - 2)]
    assert ["--bind", str(extra_write_root), str(extra_write_root)] in bind_triplets
    assert ["--ro-bind-try", str(extra_write_root), str(extra_write_root)] not in bind_triplets


def test_linux_command_read_only_binds_workspace_read_only(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _linux_command

    wrapped = _linux_command(["bash", "-c", "pwd"], tmp_path, sandbox_mode="read-only")

    triplets = [wrapped[i : i + 3] for i in range(len(wrapped) - 2)]
    assert ["--ro-bind", str(tmp_path.resolve()), str(tmp_path.resolve())] in triplets
    assert ["--bind", str(tmp_path.resolve()), str(tmp_path.resolve())] not in triplets


def test_read_only_sandbox_command_sets_read_only_mode(tmp_path: Path) -> None:
    with patch(
        "theforge.runners.sandbox.workspace_effect_sandbox_command",
        return_value=["sandbox-exec", "-p", "profile", "bash"],
    ) as mock_workspace:
        wrapped = read_only_sandbox_command(
            ["bash"], tmp_path, extra_write_roots=(tmp_path / ".claude",)
        )

    assert wrapped == ["sandbox-exec", "-p", "profile", "bash"]
    assert mock_workspace.call_args.kwargs["sandbox_mode"] == "read-only"
    assert mock_workspace.call_args.kwargs["extra_write_roots"] == (tmp_path / ".claude",)


def test_macos_profile_read_only_omits_workspace_write_access(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _macos_profile

    profile = _macos_profile(tmp_path, sandbox_mode="read-only")

    write_block = profile.split("(allow file-write*", 1)[1].split("(allow file-read*", 1)[0]
    assert f'(subpath "{tmp_path.resolve()}")' not in write_block


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
