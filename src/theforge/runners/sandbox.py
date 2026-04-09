from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)
_SYSTEM = platform.system()

# DEV-PHASE WORKSPACE BOUNDARY:
# coordinator.dev_phase threads workspace_path into runners.cli.run_agent(...)
# as working_dir, and cli.run_agent passes that same Path into the CLI runners
# and API tool handlers. The enforced sandbox boundary here is therefore the
# per-story worktree root, not the repository root.


@lru_cache(maxsize=None)
def _sandbox_available(binary: str, probe_key: tuple[str, ...]) -> bool:
    probe = list(probe_key)
    if shutil.which(binary) is None:
        logger.warning("Sandbox binary '%s' not found; filesystem isolation disabled", binary)
        return False
    try:
        result = subprocess.run(probe, capture_output=True, text=True, timeout=5, check=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sandbox binary '%s' probe failed (%s); filesystem isolation disabled",
            binary,
            exc,
        )
        return False
    if result.returncode != 0:
        logger.warning(
            "Sandbox binary '%s' probe exited %s; filesystem isolation disabled",
            binary,
            result.returncode,
        )
        return False
    return True


def _escape_subpath(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def _unique_existing_paths(paths: list[Path] | tuple[Path, ...]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return tuple(unique)


def _path_parent_roots(path: Path) -> tuple[Path, ...]:
    roots = [path.parent]
    if path.parent != path.parent.parent:
        roots.append(path.parent.parent)
    return _unique_existing_paths(roots)


def _layout_roots(allowed_root: Path) -> tuple[Path | None, Path | None]:
    root = allowed_root.resolve()
    for candidate in (root.parent, *root.parents):
        if candidate.name == "worktrees" and candidate.parent.name == ".forge":
            return (candidate.parent.parent.resolve(), candidate.resolve())
    return (None, None)


def _blocked_worktree_roots(allowed_root: Path) -> tuple[Path, ...]:
    root = allowed_root.resolve()
    _, worktrees_dir = _layout_roots(root)
    if worktrees_dir is None or not worktrees_dir.exists():
        return ()
    blocked: list[Path] = []
    for child in sorted(worktrees_dir.iterdir()):
        if not child.is_dir():
            continue
        resolved = child.resolve()
        if resolved == root:
            continue
        blocked.append(resolved)
    return tuple(blocked)


def _user_config_roots() -> tuple[Path, ...]:
    home = Path.home()
    candidates = [
        home / ".config",
        home / ".gitconfig",
        home / ".gitignore",
        home / ".git-credentials",
        home / ".local",
        home / ".npm",
        home / ".pyenv",
        home / ".cache",
        home / ".claude",
        home / ".codex",
        home / ".gemini",
        home / ".ssh",
        home / "Library" / "Application Support",
        home / "Library" / "Preferences",
    ]
    return _unique_existing_paths(candidates)


def _path_environment_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        roots.extend(_path_parent_roots(Path(entry)))
    return _unique_existing_paths(roots)


def _toolchain_read_roots() -> tuple[Path, ...]:
    commands = ("bash", "sh", "python", "python3", "pytest", "git", "make", "ruff")
    roots: list[Path] = []
    for command in commands:
        executable = shutil.which(command)
        if executable is None:
            continue
        roots.extend(_path_parent_roots(Path(executable)))
    roots.extend(_path_environment_roots())
    roots.extend(_user_config_roots())
    return _unique_existing_paths(roots)


def _workspace_read_roots(allowed_root: Path) -> tuple[Path, ...]:
    root = allowed_root.resolve()
    project_root, _ = _layout_roots(root)
    roots = [root]
    if project_root is not None and project_root != root:
        roots.append(project_root)
    roots.extend(_toolchain_read_roots())
    return _unique_existing_paths(roots)


def _launcher_extra_read_roots(cmd: list[str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    if cmd:
        executable = shutil.which(cmd[0])
        if executable is not None:
            roots.extend(_path_parent_roots(Path(executable)))
    roots.extend(_user_config_roots())
    return _unique_existing_paths(roots)


def _macos_profile(
    allowed_root: Path,
    *,
    extra_read_roots: tuple[Path, ...] = (),
    denied_read_roots: tuple[Path, ...] = (),
) -> str:
    root = allowed_root.resolve()
    read_roots = _unique_existing_paths(
        [
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/lib"),
            Path("/private"),
            Path("/dev"),
            Path("/System"),
            Path("/Applications"),
            *_workspace_read_roots(root),
            *extra_read_roots,
        ]
    )
    read_rules = "\n".join(f'    (subpath "{_escape_subpath(path)}")' for path in read_roots)
    deny_rules = "\n".join(
        f'    (subpath "{_escape_subpath(path)}")'
        for path in _unique_existing_paths(list(denied_read_roots))
    )
    escaped_root = _escape_subpath(root)
    deny_block = ""
    if deny_rules:
        deny_block = f"""(deny file-read*
{deny_rules}
)
"""
    return f'''(version 1)
(deny default)
(import "system.sb")
(allow process-exec)
(allow process-fork)
(allow signal (target self))
{deny_block}(allow file-write*
    (subpath "{escaped_root}")
    (subpath "/private/tmp")
    (subpath "/tmp")
    (subpath "/dev")
)
(allow file-read*
{read_rules}
)
'''


def _linux_command(
    cmd: list[str],
    allowed_root: Path,
    *,
    extra_read_roots: tuple[Path, ...] = (),
    masked_read_roots: tuple[Path, ...] = (),
) -> list[str]:
    root = allowed_root.resolve()
    read_roots = _unique_existing_paths([*_workspace_read_roots(root), *extra_read_roots])
    wrapped = [
        "bwrap",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/bin",
        "/bin",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--ro-bind-try",
        "/sbin",
        "/sbin",
        "--ro-bind-try",
        "/etc",
        "/etc",
    ]
    for read_root in read_roots:
        if read_root == root:
            continue
        wrapped.extend(["--ro-bind-try", str(read_root), str(read_root)])
    for masked_root in _unique_existing_paths(list(masked_read_roots)):
        wrapped.extend(["--tmpfs", str(masked_root)])
    wrapped.extend(
        [
            "--bind",
            str(root),
            str(root),
            "--tmpfs",
            "/tmp",
            "--chdir",
            str(root),
            *cmd,
        ]
    )
    return wrapped


def workspace_effect_sandbox_command(cmd: list[str], allowed_root: Path) -> list[str]:
    root = allowed_root.resolve()
    blocked_worktrees = _blocked_worktree_roots(root)
    if _SYSTEM == "Darwin":
        profile = _macos_profile(root, denied_read_roots=blocked_worktrees)
        if _sandbox_available(
            "sandbox-exec", ("sandbox-exec", "-p", "(version 1) (allow default)", "true")
        ):
            return ["sandbox-exec", "-p", profile, *cmd]
        return cmd
    if _SYSTEM == "Linux":
        if _sandbox_available("bwrap", ("bwrap", "--ro-bind", "/", "/", "true")):
            return _linux_command(cmd, root, masked_read_roots=blocked_worktrees)
        return cmd
    return cmd


def launcher_command(cmd: list[str], allowed_root: Path) -> list[str]:
    root = allowed_root.resolve()
    blocked_worktrees = _blocked_worktree_roots(root)
    extra_read_roots = _launcher_extra_read_roots(cmd)
    if _SYSTEM == "Darwin":
        profile = _macos_profile(
            root,
            extra_read_roots=extra_read_roots,
            denied_read_roots=blocked_worktrees,
        )
        if _sandbox_available(
            "sandbox-exec", ("sandbox-exec", "-p", "(version 1) (allow default)", "true")
        ):
            return ["sandbox-exec", "-p", profile, *cmd]
        return cmd
    if _SYSTEM == "Linux":
        if _sandbox_available("bwrap", ("bwrap", "--ro-bind", "/", "/", "true")):
            return _linux_command(
                cmd,
                root,
                extra_read_roots=extra_read_roots,
                masked_read_roots=blocked_worktrees,
            )
        return cmd
    return cmd


def sandbox_command(cmd: list[str], allowed_root: Path) -> list[str]:
    return workspace_effect_sandbox_command(cmd, allowed_root)
