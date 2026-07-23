from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
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


def _make_fake_linked_worktree(root: Path, name: str, branch: str) -> Path:
    """Build a linked-worktree layout (``.git`` pointer file + gitdir) without git."""
    common = root / ".git"
    gitdir = common / "worktrees" / name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (gitdir / "HEAD").write_text(f"ref: {branch}\n", encoding="utf-8")
    (common / "objects").mkdir(parents=True)
    (common / "refs" / "heads").mkdir(parents=True)
    worktree = root / ".forge" / "worktrees" / name
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return worktree


def test_git_worktree_write_roots_scopes_to_branch_namespace(tmp_path: Path) -> None:
    """The worktree's own gitdir/objects/branch-namespace are writable; the common
    root and other branches' refs are not (preserves the #1443 containment)."""
    from theforge.runners.sandbox import _git_worktree_write_roots

    worktree = _make_fake_linked_worktree(tmp_path, "issue-1907", "refs/heads/feat/issue-1907")
    common = tmp_path / ".git"

    roots = {str(p) for p in _git_worktree_write_roots(worktree)}

    assert str((common / "worktrees" / "issue-1907").resolve()) in roots
    assert str((common / "objects").resolve()) in roots
    assert str((common / "refs" / "heads" / "feat").resolve()) in roots
    # Must NOT grant the common root (main index/HEAD) or the whole refs tree.
    assert str(common.resolve()) not in roots
    assert str((common / "refs").resolve()) not in roots
    assert str((common / "refs" / "heads").resolve()) not in roots


def test_git_worktree_write_roots_plain_repo_grants_whole_gitdir(tmp_path: Path) -> None:
    """A non-worktree checkout (``.git`` is a real dir) grants its whole .git."""
    from theforge.runners.sandbox import _git_worktree_write_roots

    (tmp_path / ".git" / "objects").mkdir(parents=True)
    roots = {str(p) for p in _git_worktree_write_roots(tmp_path)}
    assert str((tmp_path / ".git").resolve()) in roots


def test_git_worktree_write_roots_no_git_returns_empty(tmp_path: Path) -> None:
    from theforge.runners.sandbox import _git_worktree_write_roots

    assert _git_worktree_write_roots(tmp_path) == ()


def test_workspace_effect_command_grants_worktree_git_writes_macos(tmp_path: Path) -> None:
    """The generated macOS profile's write block includes the worktree git roots."""
    from theforge.runners.sandbox import workspace_effect_sandbox_command

    worktree = _make_fake_linked_worktree(tmp_path, "issue-1907", "refs/heads/feat/issue-1907")
    common = tmp_path / ".git"
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = workspace_effect_sandbox_command(["claude", "-p"], worktree)
    profile = wrapped[2]
    write_block = profile.split("(allow file-read*")[0]
    assert f'(subpath "{(common / "objects").resolve()}")' in write_block
    assert f'(subpath "{(common / "refs" / "heads" / "feat").resolve()}")' in write_block


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only sandbox-exec test")
def test_macos_sandbox_allows_worktree_commit_but_blocks_escape_in_practice() -> None:
    """Executable regression for #1443/#1907: under the real sandbox profile a
    legitimate commit inside the story worktree succeeds, while writing/committing
    to the project root or a sibling worktree fails mechanically.

    The project tree is anchored under /private/var/tmp rather than pytest's
    tmp_path: the sandbox profile intentionally grants write to /tmp (agent
    scratch), and under the scrubbed gate (env -i strips TMPDIR) tmp_path lands
    in /tmp, which would make every "write is denied" assertion vacuously fail.
    /private/var/tmp is a real, writable, non-granted location on macOS — the
    same relationship the real worktree has to the project checkout.
    """
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    if shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec unavailable")
    probe = subprocess.run(
        ["sandbox-exec", "-p", "(version 1)(allow default)", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 and "Operation not permitted" in (probe.stderr + probe.stdout):
        pytest.skip("sandbox-exec present but denied by host environment")

    base = Path("/private/var/tmp")
    if not base.is_dir():
        pytest.skip("/private/var/tmp unavailable")

    from theforge.runners.sandbox import workspace_effect_sandbox_command

    proot = Path(tempfile.mkdtemp(dir=str(base))).resolve()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.co",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.co",
    }

    def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [git, *args], cwd=str(cwd), capture_output=True, text=True, env=env, check=True
        )

    try:
        _git("init", "-q", "-b", "main", cwd=proot)
        (proot / "base.txt").write_text("hello\n", encoding="utf-8")
        _git("add", "base.txt", cwd=proot)
        _git("commit", "-qm", "init", cwd=proot)
        main_sha_before = _git("rev-parse", "main", cwd=proot).stdout.strip()
        (proot / ".forge" / "worktrees").mkdir(parents=True)
        worktree = proot / ".forge" / "worktrees" / "issue-1443"
        sibling = proot / ".forge" / "worktrees" / "issue-999"
        _git("worktree", "add", "-q", str(worktree), "-b", "feat/issue-1443", cwd=proot)
        _git("worktree", "add", "-q", str(sibling), "-b", "feat/issue-999", cwd=proot)

        # Clear the lru-cached probe: an earlier test that mocked subprocess.run
        # can have poisoned it to False for this worker.
        from theforge.runners import sandbox as _sbmod

        _sbmod._sandbox_available.cache_clear()
        wrapped = workspace_effect_sandbox_command(["true"], worktree)
        if wrapped[0] != "sandbox-exec":
            pytest.skip("host sandbox wrapper unavailable in this environment")
        profile = wrapped[2]

        def _sandboxed(script: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["sandbox-exec", "-p", profile, "bash", "-c", script],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

        # 1. Legitimate commit inside the story worktree SUCCEEDS.
        ok = _sandboxed(
            f"cd {worktree} && echo work > feature.txt && {git} add feature.txt && "
            f"{git} commit -qm 'legit worktree commit'"
        )
        assert ok.returncode == 0, f"worktree commit failed: {ok.stderr}"

        # 2. Writing a source file in the project root FAILS.
        rogue_write = _sandboxed(f"echo x > {proot}/rogue.txt")
        assert rogue_write.returncode != 0
        assert not (proot / "rogue.txt").exists()

        # 3. The #1443 vector — commit from the project root — FAILS.
        rogue_commit = _sandboxed(
            f"cd {proot} && echo x >> base.txt && {git} add base.txt && {git} commit -qm rogue"
        )
        assert rogue_commit.returncode != 0

        # 4. Rewriting the main branch ref FAILS (main is unchanged).
        rogue_ref = _sandboxed(f"cd {worktree} && {git} update-ref refs/heads/main HEAD")
        assert rogue_ref.returncode != 0
        assert _git("rev-parse", "main", cwd=proot).stdout.strip() == main_sha_before

        # 5. Writing into a sibling worktree FAILS.
        sibling_write = _sandboxed(f"echo x > {sibling}/rogue.txt")
        assert sibling_write.returncode != 0
        assert not (sibling / "rogue.txt").exists()
    finally:
        shutil.rmtree(proot, ignore_errors=True)


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
