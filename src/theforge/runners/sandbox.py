from __future__ import annotations

import logging
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


def _macos_profile(allowed_root: Path) -> str:
    root = allowed_root.resolve()
    escaped_root = str(root).replace("\\", "\\\\").replace('"', '\\"')
    return f'''(version 1)
(deny default)
(import "system.sb")
(allow process-exec)
(allow process-fork)
(allow signal (target self))
(allow file-write*
    (subpath "{escaped_root}")
    (subpath "/private/tmp")
    (subpath "/tmp")
    (subpath "/dev")
)
(allow file-read*
    (subpath "{escaped_root}")
    (subpath "/usr")
    (subpath "/bin")
    (subpath "/sbin")
    (subpath "/lib")
    (subpath "/private")
    (subpath "/dev")
    (subpath "/System")
    (subpath "/Applications")
)
'''


def _linux_command(cmd: list[str], allowed_root: Path) -> list[str]:
    root = allowed_root.resolve()
    return [
        "bwrap",
        "--bind",
        str(root),
        str(root),
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
        "--tmpfs",
        "/tmp",
        "--chdir",
        str(root),
        *cmd,
    ]


def sandbox_command(cmd: list[str], allowed_root: Path) -> list[str]:
    root = allowed_root.resolve()
    if _SYSTEM == "Darwin":
        profile = _macos_profile(root)
        if _sandbox_available(
            "sandbox-exec", ("sandbox-exec", "-p", "(version 1) (allow default)", "true")
        ):
            return ["sandbox-exec", "-p", profile, *cmd]
        return cmd
    if _SYSTEM == "Linux":
        if _sandbox_available("bwrap", ("bwrap", "--ro-bind", "/", "/", "true")):
            return _linux_command(cmd, root)
        return cmd
    return cmd
