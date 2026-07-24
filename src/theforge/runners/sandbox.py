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
    for candidate in root.parents:
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


def _dedupe_resolved(paths: list[Path] | tuple[Path, ...]) -> tuple[Path, ...]:
    """Resolve + dedupe paths without dropping ones that do not exist yet.

    Used for *write* roots the wrapped CLI may need to create on first use
    (e.g. Claude's ``~/.claude`` state dir on a fresh host). Existence-filtering
    those would silently strip the allowance and break the CLI under sandbox.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return tuple(out)


def _parse_worktree_gitdir(git_file: Path, root: Path) -> Path | None:
    """Resolve the real gitdir from a linked worktree's ``.git`` pointer file."""
    content = git_file.read_text(encoding="utf-8").strip()
    prefix = "gitdir:"
    if not content.startswith(prefix):
        return None
    target = Path(content[len(prefix) :].strip())
    if not target.is_absolute():
        target = root / target
    return target.resolve()


def _resolve_common_dir(gitdir: Path) -> Path | None:
    """Resolve the shared common dir (object store + refs) for a worktree gitdir."""
    commondir_file = gitdir / "commondir"
    if commondir_file.exists():
        rel = commondir_file.read_text(encoding="utf-8").strip()
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = gitdir / candidate
        return candidate.resolve()
    # Standard layout fallback: <common>/worktrees/<name>.
    if gitdir.parent.name == "worktrees":
        return gitdir.parent.parent.resolve()
    return None


def _worktree_branch_namespace(gitdir: Path, common: Path) -> Path | None:
    """Return the shared refs dir holding the worktree's *own* branch ref.

    E.g. HEAD ``ref: refs/heads/feat/issue-1907`` → ``<common>/refs/heads/feat``.
    Scoping to the branch's namespace lets the worktree update (and lock) its own
    ref while leaving sibling namespaces like ``refs/heads/main`` / ``release``
    read-only. Returns None for a detached HEAD (no shared branch ref).
    """
    head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    prefix = "ref:"
    if not head.startswith(prefix):
        return None
    ref = head[len(prefix) :].strip()
    if not ref:
        return None
    return (common / ref).resolve().parent


def _git_worktree_write_roots(allowed_root: Path) -> tuple[Path, ...]:
    """Writable git paths a wrapped CLI needs to commit *within* this worktree.

    A linked git worktree keeps its private state under
    ``<repo>/.git/worktrees/<name>`` and shares the object store and refs under
    ``<repo>/.git`` — all outside the worktree root. Without write access there,
    a legitimate ``git commit`` from the correctly-scoped worktree fails with
    "Operation not permitted" (#1907 review P1).

    Scope stays tight so containment is preserved: only the worktree's own
    private gitdir, the shared object store and reflogs (a reflog cannot move a
    ref), packed-refs, and the *namespace directory of the worktree's own branch
    ref*. The main checkout's index/HEAD and other branches' refs stay
    read-only, so ``cd <project-root> && git commit`` and
    ``git update-ref refs/heads/main`` still fail mechanically (the #1443 vector).
    """
    root = allowed_root.resolve()
    git_path = root / ".git"
    try:
        if not git_path.exists():
            return ()
        if git_path.is_dir():
            # Non-worktree checkout: allowed_root is the repo itself; its whole
            # .git is the private gitdir and there is no separate main checkout.
            return (git_path.resolve(),)
        gitdir = _parse_worktree_gitdir(git_path, root)
        if gitdir is None:
            return ()
        roots: list[Path] = [gitdir]
        common = _resolve_common_dir(gitdir)
        if common is not None:
            packed_refs = common / "packed-refs"
            roots += [
                common / "objects",
                common / "logs",
                packed_refs,
                packed_refs.with_name("packed-refs.lock"),
            ]
            branch_ns = _worktree_branch_namespace(gitdir, common)
            # Fall back to the shared refs dir only when the branch cannot be
            # determined (e.g. detached HEAD) — a rare, functional degradation.
            roots.append(branch_ns if branch_ns is not None else common / "refs")
        return _dedupe_resolved(roots)
    except OSError:
        return ()


@lru_cache(maxsize=None)
def _credential_read_roots() -> tuple[Path, ...]:
    """Keychain directories a macOS CLI must open to authenticate under sandbox.

    Claude Code stores its OAuth token in the login Keychain, reached via
    ``securityd``. Granting the ``com.apple.SecurityServer`` mach-lookup is not
    enough on a host that does not otherwise expose ``$HOME`` for reads — the
    client still opens the keychain database directory to locate the item. This
    was the root cause of the #925 "Not logged in" regression that reverted the
    prior Claude sandbox-wrapping attempt (#920).
    """
    return _unique_existing_paths(
        [Path.home() / "Library" / "Keychains", Path("/Library") / "Keychains"]
    )


@lru_cache(maxsize=None)
def _user_config_roots() -> tuple[Path, ...]:
    home = Path.home()
    candidates = [
        home / ".config",
        home / ".gitconfig",
        home / ".gitignore",
        home / ".local",
        home / ".npm",
        home / ".pyenv",
        home / ".cache",
        home / ".claude",
        home / ".codex",
        home / ".gemini",
        home / ".ssh" / "known_hosts",
        home / "Library" / "Application Support",
        home / "Library" / "Preferences",
    ]
    return _unique_existing_paths(candidates)


@lru_cache(maxsize=None)
def _path_environment_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        roots.extend(_path_parent_roots(Path(entry)))
    return _unique_existing_paths(roots)


@lru_cache(maxsize=None)
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


def _macos_profile(
    allowed_root: Path,
    *,
    extra_read_roots: tuple[Path, ...] = (),
    extra_write_roots: tuple[Path, ...] = (),
    denied_read_roots: tuple[Path, ...] = (),
    allow_credential_services: bool = False,
) -> str:
    root = allowed_root.resolve()
    cred_read_roots = _credential_read_roots() if allow_credential_services else ()
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
            *cred_read_roots,
            *extra_read_roots,
        ]
    )
    read_rules = "\n".join(f'    (subpath "{_escape_subpath(path)}")' for path in read_roots)
    deny_rules = "\n".join(
        f'    (subpath "{_escape_subpath(path)}")'
        for path in _unique_existing_paths(list(denied_read_roots))
    )
    # Write roots: worktree + ephemeral tmp/dev, plus any CLI-scoped state dirs
    # (e.g. Claude's ~/.claude). Keeping this list tight is the containment
    # boundary — never widen it to the project root or sibling worktrees.
    write_roots = _dedupe_resolved(
        [root, Path("/private/tmp"), Path("/tmp"), Path("/dev"), *extra_write_roots]
    )
    write_rules = "\n".join(f'    (subpath "{_escape_subpath(path)}")' for path in write_roots)
    # A wrapped network-bound CLI (Gemini/Claude) needs outbound egress and DNS
    # resolution via mDNSResponder; without these the launcher cannot reach its
    # provider API. These are capability (not filesystem) grants, so they do not
    # widen the write boundary. SecurityServer is added only when the CLI
    # authenticates through the OS keychain (see _credential_read_roots).
    service_lines = [
        "(allow network-outbound)",
        "(allow network-inbound)",
        "(allow system-socket)",
        '(allow mach-lookup (global-name "com.apple.mDNSResponder"))',
    ]
    if allow_credential_services:
        service_lines.append('(allow mach-lookup (global-name "com.apple.SecurityServer"))')
    service_block = "\n".join(service_lines)
    deny_block = ""
    if deny_rules:
        deny_block = f"""(deny file-read*
{deny_rules}
)
"""
    return f"""(version 1)
(deny default)
(import "system.sb")
(allow process-exec)
(allow process-fork)
(allow signal (target self))
{service_block}
(allow file-write*
{write_rules}
)
(allow file-read*
{read_rules}
)
{deny_block}"""


def _linux_command(
    cmd: list[str],
    allowed_root: Path,
    *,
    extra_read_roots: tuple[Path, ...] = (),
    extra_write_roots: tuple[Path, ...] = (),
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
    wrapped.extend(["--bind", str(root), str(root)])
    # CLI-scoped writable state (e.g. Claude's ~/.claude). --bind-try tolerates
    # a path that does not exist yet so a fresh host can create it. Skip the
    # worktree itself (already bound above) so we never double-bind it.
    for write_root in _dedupe_resolved(extra_write_roots):
        if write_root == root:
            continue
        wrapped.extend(["--bind-try", str(write_root), str(write_root)])
    wrapped.extend(
        [
            "--tmpfs",
            "/tmp",
            "--chdir",
            str(root),
            *cmd,
        ]
    )
    return wrapped


def workspace_effect_sandbox_command(
    cmd: list[str],
    allowed_root: Path,
    *,
    extra_write_roots: tuple[Path, ...] | list[Path] = (),
    allow_credential_services: bool = False,
) -> list[str]:
    """Wrap *cmd* so filesystem writes are confined to *allowed_root*.

    ``extra_write_roots`` grants additional writable paths a CLI needs for its
    own state (e.g. Claude's ``~/.claude``) without widening the worktree
    boundary. ``allow_credential_services`` additionally grants the OS keychain
    access a CLI needs to authenticate under the sandbox (macOS securityd);
    without it, wrapping Claude reproduces the #925 "Not logged in" regression.

    The worktree's own git internals (private gitdir, shared object store, and
    its own branch-ref namespace) are granted automatically so ``git commit``
    from the correctly-scoped worktree works, while the main checkout and other
    branches' refs stay read-only.

    Returns *cmd* unchanged when no host sandbox (sandbox-exec/bwrap) is
    available — callers that require mechanical containment must detect this
    (``result[0] == cmd[0]``) and fail closed.
    """
    root = allowed_root.resolve()
    extra_write_roots = tuple(extra_write_roots) + _git_worktree_write_roots(root)
    blocked_worktrees = _blocked_worktree_roots(root)
    if _SYSTEM == "Darwin":
        profile = _macos_profile(
            root,
            extra_write_roots=extra_write_roots,
            denied_read_roots=blocked_worktrees,
            allow_credential_services=allow_credential_services,
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
                extra_write_roots=extra_write_roots,
                masked_read_roots=blocked_worktrees,
            )
        return cmd
    return cmd


def sandbox_command(cmd: list[str], allowed_root: Path) -> list[str]:
    return workspace_effect_sandbox_command(cmd, allowed_root)
